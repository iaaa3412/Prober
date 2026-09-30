from __future__ import annotations

import csv
import os
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from wafer_map_view import _pz_bind

_DIE_PX = 20


def _parse_q(raw: str):
    raw = (raw or "").strip()
    m = re.search(r'Y\s*([+-]?\d+)\s*X\s*([+-]?\d+)', raw)
    if m:
        return int(m.group(2)), int(m.group(1))
    parts = re.findall(r'[+-]?\d+', raw)
    if len(parts) >= 2:
        return int(parts[1]), int(parts[0])
    raise ValueError(f"Cannot parse Q response: {raw!r}")


class AccrWaferPanel(ttk.Frame):
    def __init__(self, parent, controller, get_folder=None):
        super().__init__(parent)
        self.controller = controller
        self._get_folder = get_folder or (lambda: None)
        self._running = False
        self._abort   = False
        self._dies    = []
        self._loaded_ata_folder = None
        self._die_items: dict = {}
        self._selected_xy = None
        self._press_xy = None

        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_topbar()
        self._build_main()


    def _build_topbar(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.grid(row=0, column=0, sticky="ew")

        self._start_btn = ttk.Button(bar, text="▶  Extract Wafer Map",
                                     command=self._start_extraction)
        self._start_btn.pack(side="left", padx=(0, 4))
        self._abort_btn = ttk.Button(bar, text="⏹  Abort", state="disabled",
                                     command=self._request_abort)
        self._abort_btn.pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        self._separate_var = tk.BooleanVar(value=True)

        ttk.Label(bar, text="Max dies:").pack(side="left", padx=(12, 2))
        self._max_var = tk.StringVar(value="10000")
        ttk.Entry(bar, textvariable=self._max_var, width=7).pack(side="left")

        self._status_var = tk.StringVar(value="Idle")
        self._status_lbl = ttk.Label(bar, textvariable=self._status_var,
                                     font=("Consolas", 10, "bold"),
                                     foreground="#6b7280")
        self._status_lbl.pack(side="right", padx=8)

    def _build_main(self):
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)

        lf = ttk.LabelFrame(pane, text="Collected Dies", padding=6)
        pane.add(lf, weight=1)
        lf.rowconfigure(1, weight=1)
        lf.columnconfigure(0, weight=1)

        self._count_var = tk.StringVar(value="0 dies")
        ttk.Label(lf, textvariable=self._count_var,
                  font=("Consolas", 10, "bold"),
                  foreground="#0077cc").grid(row=0, column=0, sticky="w", pady=(0, 4))

        self._list = tk.Text(lf, width=24, font=("Consolas", 9),
                             state="disabled", bg="#f8fafc")
        self._list.grid(row=1, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self._list.yview)
        lsb.grid(row=1, column=1, sticky="ns")
        self._list.configure(yscrollcommand=lsb.set)

        btns = ttk.Frame(lf)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="Save CSV…", command=self._save_csv).pack(
            side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(btns, text="Copy", command=self._copy_clipboard).pack(
            side="left", expand=True, fill="x", padx=2)
        ttk.Button(btns, text="Clear", command=self._clear).pack(
            side="left", expand=True, fill="x", padx=(2, 0))

        ttk.Button(lf, text="💾 Save to ATA Folder",
                   command=self._save_to_ata).grid(
                   row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        rf = ttk.LabelFrame(pane, text="Wafer Map", padding=4)
        pane.add(rf, weight=3)
        rf.rowconfigure(0, weight=1)
        rf.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(rf, bg="#0f172a", highlightthickness=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        _pz_bind(self._canvas, self._reset_view)
        self._canvas.bind("<ButtonPress-1>", self._on_map_press, add="+")
        self._canvas.bind("<ButtonRelease-1>", self._on_map_release, add="+")
        self._canvas.bind("<Configure>", lambda _e: self._redraw() if not self._dies else None)

        self._die_info_var = tk.StringVar(value="Click a die to see its X/Y/order.")
        ttk.Label(rf, textvariable=self._die_info_var, font=("Consolas", 9),
                 foreground="#334155").grid(row=1, column=0, sticky="w", padx=4, pady=(4, 0))


    def _start_extraction(self):
        if self._running:
            return
        try:
            max_dies = int(self._max_var.get())
            if max_dies <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Input", "Max dies must be a positive whole number.")
            return

        drv = self.controller.drivers.get("prober")
        if not (drv and drv.inst):
            messagebox.showerror("Prober Not Connected",
                                 "Connect the prober before extracting a wafer map.")
            return

        if not messagebox.askyesno(
            "Extract Wafer Map",
            "Start dry-run map extraction?\n\n"
            "The prober will drive to the start die (G) and then step through\n"
            "EVERY testing die (J), reading coordinates (Q) at each one.\n\n"
            "⚠ Physical chuck motion across the whole wafer.\n"
            "Requires active probing (wafer loaded & aligned, lot started).\n"
            + ("D (Separate) is sent first so the needles never contact."
               if self._separate_var.get() else
               "⚠ 'Send D first' is OFF — if the chuck is UP the wafer\n"
               "RE-CONTACTS the probe card at every die!")
        ):
            return

        self._clear()
        self._running = True
        self._abort   = False
        self._start_btn.config(state="disabled")
        self._abort_btn.config(state="normal")
        self._set_status("Running…", "#2563eb")
        threading.Thread(
            target=self._worker,
            args=(self._separate_var.get(), max_dies),
            daemon=True,
        ).start()

    def _request_abort(self):
        if self._running:
            self._abort = True
            self._set_status("Aborting after current step…", "#f97316")
            self._log("[MAP] Abort requested — stopping after current step")
            drv = self.controller.drivers.get("prober")
            if drv and drv.inst:
                def _clear():
                    try:
                        drv.send_es()
                        self._log("[MAP] es sent (buzzer clear)")
                    except Exception as e:
                        self._log(f"[MAP] es error: {e}")
                threading.Thread(target=_clear, daemon=True).start()


    def _worker(self, send_separate: bool, max_dies: int):
        try:
            self._worker_hw(send_separate, max_dies)
        finally:
            self._running = False
            self.after(0, lambda: (self._start_btn.config(state="normal"),
                                   self._abort_btn.config(state="disabled")))

    def _ensure_separated(self, drv, stb: int, cmd: str) -> None:
        if stb == 67:
            self._log(f"[MAP] {cmd} finished chuck UP (STB=67 — contact!) "
                      ">> D  (Separate)")
            drv.z_down()
            self._log("[MAP] << STB=68  (chuck down — separated)")

    def _worker_hw(self, send_separate: bool, max_dies: int):
        drv = self.controller.drivers.get("prober")
        if not (drv and drv.inst):
            self._finish("Prober not connected", error=True)
            return
        try:
            if send_separate:
                self._log("[MAP] >> D  (Z Down — chuck drops, wafer separates)")
                drv.z_down()
                self._log("[MAP] << STB=68  (Z Down done)")

            self._log("[MAP] >> G  (Position start die)")
            stb = drv.move_to_start_die()
            self._log(f"[MAP] << STB={stb}  (start die positioned, chuck "
                      f"{'UP — CONTACT' if stb == 67 else 'DOWN'})")
            if send_separate:
                self._ensure_separated(drv, stb, "G")

            while True:
                if self._abort:
                    self._finish(f"Aborted — {len(self._dies)} dies collected")
                    return
                if len(self._dies) >= max_dies:
                    self._finish(f"Stopped at max-dies cap ({max_dies})", error=True)
                    return

                self._log("[MAP] >> Q  (die coordinates)")
                raw = drv.get_xy_position()
                x, y = _parse_q(raw)
                self._log(f"[MAP] << {raw!r}  → die X={x} Y={y}")
                self._add_die(x, y, raw)

                self._log("[MAP] >> J  (position next testing die)")
                stb = drv.next_die()
                if stb == 81:
                    self._log("[MAP] << STB=81  (wafer end — no more testing dies)")
                    self._finish(f"Complete — {len(self._dies)} dies")
                    return
                if stb == 90:
                    self._log("[MAP] << STB=90  (probing stop — <STOP> pushed)")
                    self._finish(f"<STOP> pushed on prober (STB=90) — "
                                 f"{len(self._dies)} dies collected", error=True)
                    return
                self._log(f"[MAP] << STB={stb}  (moved, chuck "
                          f"{'UP — CONTACT' if stb == 67 else 'DOWN'})")
                if send_separate:
                    self._ensure_separated(drv, stb, "J")

        except Exception as e:
            self._log(f"[MAP] ERROR: {e}")
            self._finish(f"Error after {len(self._dies)} dies — see log", error=True)

    def _add_die(self, x: int, y: int, raw: str):
        self._dies.append((x, y, raw))
        n = len(self._dies)
        def _ui():
            self._count_var.set(f"{n} dies")
            self._list.config(state="normal")
            self._list.insert("end", f"{n:4d}  X{x:+04d} Y{y:+04d}\n")
            self._list.see("end")
            self._list.config(state="disabled")
            self._redraw()
        self.after(0, _ui)

    def _finish(self, msg: str, error: bool = False):
        self._log(f"[MAP] {msg}")
        self.after(0, lambda: self._set_status(msg, "#dc2626" if error else "#16a34a"))

    def _log(self, msg: str):
        self.controller.log(msg)

    def _set_status(self, text: str, color: str = "#6b7280"):
        self._status_var.set(text)
        self._status_lbl.config(foreground=color)


    def _reset_view(self):
        self._redraw()

    def _redraw(self):
        cv = self._canvas
        cv.delete("all")
        self._die_items = {}
        if not self._dies:
            cv.create_text(max(cv.winfo_width() // 2, 50), max(cv.winfo_height() // 2, 50),
                           text="No map data — run an extraction",
                           fill="#475569", font=("Segoe UI", 11))
            return

        xs = [d[0] for d in self._dies]
        ys = [d[1] for d in self._dies]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)

        last = len(self._dies) - 1
        for i, (x, y, _raw) in enumerate(self._dies):
            cx = (x - xmin) * _DIE_PX
            cy = (y - ymin) * _DIE_PX
            color = ("#2563eb" if i == 0
                     else "#f97316" if i == last
                     else "#22c55e")
            outline = "#f8fafc" if (x, y) == self._selected_xy else ""
            width = 2 if (x, y) == self._selected_xy else 0
            rect = cv.create_rectangle(cx + 1, cy + 1, cx + _DIE_PX - 1, cy + _DIE_PX - 1,
                                       fill=color, outline=outline, width=width)
            self._die_items[(x, y)] = rect

        cv.create_text(0, (ymax - ymin + 1) * _DIE_PX + 12, anchor="w",
                       fill="#94a3b8", font=("Consolas", 8),
                       text=f"X {xmin}…{xmax}   Y {ymin}…{ymax}   {len(self._dies)} dies"
                            f"   ■ start  ■ latest")
        cv.configure(scrollregion=(-20000, -20000, 20000, 20000))

    def _on_map_press(self, e):
        self._press_xy = (e.x, e.y)

    def _on_map_release(self, e):
        press = self._press_xy
        self._press_xy = None
        if press is None:
            return
        dx, dy = e.x - press[0], e.y - press[1]
        if dx * dx + dy * dy > 16:
            return
        cx, cy = self._canvas.canvasx(e.x), self._canvas.canvasy(e.y)
        hit = self._canvas.find_closest(cx, cy)
        if not hit:
            return
        xy = next((k for k, v in self._die_items.items() if v == hit[0]), None)
        if xy is None:
            return
        self._show_die_info(xy)

    def _show_die_info(self, xy: tuple):
        prev = self._selected_xy
        self._selected_xy = xy
        if prev is not None and prev in self._die_items:
            try:
                self._canvas.itemconfig(self._die_items[prev], outline="", width=0)
            except tk.TclError:
                pass
        if xy in self._die_items:
            try:
                self._canvas.itemconfig(self._die_items[xy], outline="#f8fafc", width=2)
            except tk.TclError:
                pass
        x, y = xy
        idx = next((i for i, d in enumerate(self._dies, 1) if (d[0], d[1]) == xy), None)
        raw = next((d[2] for d in self._dies if (d[0], d[1]) == xy), "")
        self._die_info_var.set(f"Die #{idx}  X={x:+d}  Y={y:+d}  —  {raw}")


    def _save_csv(self):
        if not self._dies:
            messagebox.showinfo("No Data", "No dies collected yet.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="accretech_wafer_map.csv",
            title="Save wafer map",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["order", "x_die", "y_die", "raw_q"])
            for i, (x, y, raw) in enumerate(self._dies, 1):
                wr.writerow([i, x, y, raw])
        self._log(f"[MAP] Saved {len(self._dies)} dies")

    def _save_to_ata(self):
        if not self._dies:
            messagebox.showinfo("No Data", "No dies collected yet.")
            return
        folder = self._get_folder()
        if not folder:
            messagebox.showerror(
                "No ATA Folder",
                "No ATA folder is loaded — use 📁 Load ATA Folder on the\n"
                "top toolbar first, then Save to ATA Folder here.")
            return
        path = os.path.join(folder, "ata_wafer_map_accretech.csv")
        if os.path.exists(path) and not messagebox.askyesno(
            "Overwrite Wafer Map",
            f"{path}\nalready exists — overwrite it with the "
            f"{len(self._dies)} die(s) extracted here?"
        ):
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["row", "col", "x_die", "y_die", "raw_q"])
            for x, y, raw in self._dies:
                wr.writerow([y, x, x, y, raw])
        self._loaded_ata_folder = folder
        self._log(f"[MAP] Saved {len(self._dies)} dies")
        self._log("[MAP] Proceed with Overlay to finish map.")

    def load_from_ata(self, folder: str) -> int:
        if self._running or not folder:
            return 0
        path = os.path.join(folder, "ata_wafer_map_accretech.csv")
        if not os.path.exists(path):
            if self._loaded_ata_folder is not None:
                self._clear()
            return 0
        dies = []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    try:
                        x = int(float(row.get("x_die", "")))
                        y = int(float(row.get("y_die", "")))
                    except (TypeError, ValueError):
                        continue
                    dies.append((x, y, row.get("raw_q", "")))
        except OSError as exc:
            self._log(f"[MAP] Could not read wafer map: {exc}")
            return 0

        self._dies = dies
        self._loaded_ata_folder = folder
        self._count_var.set(f"{len(dies)} dies")
        self._list.config(state="normal")
        self._list.delete("1.0", "end")
        for i, (x, y, _raw) in enumerate(dies, 1):
            self._list.insert("end", f"{i:4d}  X{x:+04d} Y{y:+04d}\n")
        self._list.config(state="disabled")
        self._redraw()
        if dies:
            self._set_status(f"Loaded {len(dies)} dies from ATA folder", "#16a34a")
            self._log(f"[MAP] Auto-loaded {len(dies)} dies")
        return len(dies)

    def _copy_clipboard(self):
        if not self._dies:
            return
        text = "\n".join(f"{x},{y}" for x, y, _ in self._dies)
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log(f"[MAP] Copied {len(self._dies)} dies to clipboard (x,y per line)")

    def _clear(self):
        self._dies = []
        self._loaded_ata_folder = None
        self._count_var.set("0 dies")
        self._list.config(state="normal")
        self._list.delete("1.0", "end")
        self._list.config(state="disabled")
        self._redraw()
        if not self._running:
            self._set_status("Idle")
