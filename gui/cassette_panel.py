from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

YIELD_THRESHOLD_FILENAME = "ata_cassette_yield.json"


def save_yield_threshold(folder: str, pct: float) -> None:
    path = os.path.join(folder, YIELD_THRESHOLD_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"yield_threshold": float(pct)}, f)
    except OSError:
        pass


def load_yield_threshold(folder: str, default: float = 0.0) -> float:
    path = os.path.join(folder, YIELD_THRESHOLD_FILENAME)
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("yield_threshold", default))
    except (OSError, ValueError, TypeError):
        return default


AUTOEXPORT_SETTINGS_FILENAME = "ata_autoexport.json"


def save_autoexport_settings(folder: str, auto_export: bool, save_csv: bool) -> None:
    path = os.path.join(folder, AUTOEXPORT_SETTINGS_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"auto_export": bool(auto_export), "save_csv": bool(save_csv)}, f)
    except OSError:
        pass


def load_autoexport_settings(folder: str, default: tuple = (False, False)) -> tuple:
    path = os.path.join(folder, AUTOEXPORT_SETTINGS_FILENAME)
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return (bool(data.get("auto_export", default[0])),
               bool(data.get("save_csv", default[1])))
    except (OSError, ValueError, TypeError):
        return default


class CassettePanel(ttk.Frame):

    def __init__(self, parent, controller, ui):
        super().__init__(parent)
        self.controller = controller
        self.ui = ui
        self._wafers: list[str] = []
        self._slot_idx = 0
        self._armed = False
        self._paused_for_yield = False
        self._paused_for_error = False
        self._error_retry_kind = "advance"
        self._move_slot_armed = False
        self._run_mode = "full"
        self._lot_summary: list = []
        self._yield_folder: str | None = None

        self.rowconfigure(3, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_topbar()
        self._build_wafer_list()
        self._build_export()
        self._build_progress()


    def _build_topbar(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.grid(row=0, column=0, sticky="ew")

        self._go_btn = ttk.Button(bar, text="▶  Cassette Automation",
                                  command=self._arm)
        self._go_btn.pack(side="left", padx=4)
        self._stop_btn = ttk.Button(bar, text="⏹  Stop Automation", state="disabled",
                                    command=lambda: self._disarm("Stopped by user."))
        self._stop_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Reset to Slot #1",
                  command=self._reset_slot).pack(side="left", padx=4)
        self._move_slot_btn = ttk.Button(bar, text="Move to Selected Slot",
                                         command=self._move_selected_slot_button)
        self._move_slot_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Load Next Wafer",
                  command=self._manual_load_next).pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(bar, text="Pass yield ≥").pack(side="left")
        self._yield_var = tk.StringVar(value="0")
        yield_ent = ttk.Entry(bar, textvariable=self._yield_var, width=5)
        yield_ent.pack(side="left", padx=(2, 0))
        yield_ent.bind("<Return>", lambda _e: self._on_yield_edited())
        yield_ent.bind("<FocusOut>", lambda _e: self._on_yield_edited())
        ttk.Label(bar, text="% to auto-continue, else pause").pack(side="left", padx=(2, 0))
        self._continue_btn = ttk.Button(bar, text="▶ Continue", state="disabled",
                                        command=self._continue_after_pause)
        self._continue_btn.pack(side="left", padx=(8, 0))

        self._state_var = tk.StringVar(value="IDLE")
        self._state_lbl = ttk.Label(bar, textvariable=self._state_var,
                                    font=("Consolas", 11, "bold"), foreground="#6b7280")
        self._state_lbl.pack(side="right", padx=8)

    def _build_wafer_list(self):
        lf = ttk.LabelFrame(self, text="Cassette Slots", padding=6)
        lf.grid(row=1, column=0, sticky="ew", padx=6, pady=(4, 2))
        lf.columnconfigure(0, weight=1)
        self._slots_lf = lf

        btns = ttk.Frame(lf)
        btns.grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(btns, text="Lot ID (all wafers):").pack(side="left")
        self._lot_id_var = tk.StringVar()
        ttk.Entry(btns, textvariable=self._lot_id_var, width=20).pack(
            side="left", padx=(4, 0))
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(btns, text="＋ Add Slot", command=self._add_slot).pack(side="left", padx=2)
        ttk.Button(btns, text="✎ Edit", command=self._edit_slot).pack(side="left", padx=2)
        ttk.Button(btns, text="Remove", command=self._remove_slot).pack(side="left", padx=2)
        ttk.Button(btns, text="▲", width=3, command=lambda: self._move_slot(-1)).pack(
            side="left", padx=(10, 2))
        ttk.Button(btns, text="▼", width=3, command=lambda: self._move_slot(1)).pack(
            side="left", padx=2)
        ttk.Button(btns, text="Clear All", command=self._clear_slots).pack(side="left", padx=(10, 2))

        cols = ("slot", "lot", "wafer")
        self._slot_tree = ttk.Treeview(lf, columns=cols, show="headings", height=5,
                                       selectmode="browse")
        heads = [("slot", "Slot #", 60), ("lot", "Lot ID", 160), ("wafer", "Wafer ID", 160)]
        for cid, text, width in heads:
            self._slot_tree.heading(cid, text=text)
            self._slot_tree.column(cid, width=width, anchor="center" if cid == "slot" else "w")
        self._slot_tree.grid(row=1, column=0, sticky="ew")
        self._slot_tree.bind("<Double-1>", lambda _e: self._edit_slot())
        self._slot_tree.bind("<<TreeviewSelect>>", self._on_move_slot_row_selected)

    def _build_export(self):
        ef = ttk.Frame(self._slots_lf)
        ef.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        self._auto_export_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ef, text="Auto Export",
                       variable=self._auto_export_var).pack(side="left", padx=(0, 16))
        self._auto_export_csv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ef, text="Also Save CSV",
                       variable=self._auto_export_csv_var).pack(side="left", padx=(0, 16))

        ttk.Label(ef, text="Export Directory:").pack(side="left")
        ttk.Entry(ef, textvariable=self.ui.export_path_var, width=32).pack(
            side="left", padx=6)
        ttk.Button(ef, text="Browse...", command=self._browse_export_dir).pack(side="left")
        export_dir_choices = getattr(self.ui, "_export_dir_choices", None)
        if export_dir_choices:
            export_dir_var = tk.StringVar(value=next(iter(export_dir_choices)))
            export_dir_cb = ttk.Combobox(
                ef, textvariable=export_dir_var, state="readonly",
                width=16, values=list(export_dir_choices.keys()))
            export_dir_cb.pack(side="left", padx=(4, 0))
            export_dir_cb.bind(
                "<<ComboboxSelected>>",
                lambda _e: self.ui.export_path_var.set(
                    export_dir_choices[export_dir_var.get()]))

        ttk.Label(ef, text="Format:").pack(side="left", padx=(16, 2))
        self._export_format_cb = ttk.Combobox(
            ef, textvariable=self.ui.export_format_var, state="readonly", width=32)
        self._export_format_cb.pack(side="left", padx=(0, 4))
        self._export_format_cb.bind("<<ComboboxSelected>>", lambda _e: None)
        ttk.Button(ef, text="↻", width=3, command=self._refresh_export_formats).pack(side="left")

    def _build_progress(self):
        pf = ttk.LabelFrame(self, text="Cassette Automation Log", padding=6)
        pf.grid(row=3, column=0, sticky="nsew", padx=6, pady=(2, 6))
        pf.rowconfigure(0, weight=1)
        pf.columnconfigure(0, weight=1)

        cols = ("timestamp", "slot", "lot", "event")
        self._tree = ttk.Treeview(pf, columns=cols, show="headings",
                                  height=10, selectmode="browse")
        heads = [("timestamp", "Time", 150), ("slot", "Slot", 50),
                 ("lot", "Lot ID", 120), ("event", "Event", 400)]
        for cid, text, width in heads:
            self._tree.heading(cid, text=text)
            self._tree.column(cid, width=width,
                              anchor="center" if cid == "slot" else "w")
        self._tree.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(pf, orient="vertical", command=self._tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=tsb.set)


    def _log(self, msg: str):
        self.controller.log(msg)

    def _set_state(self, text: str, color: str = "#6b7280"):
        self._state_var.set(text)
        self._state_lbl.config(foreground=color)

    def _set_locked(self, locked: bool):
        self._go_btn.config(state="disabled" if locked else "normal")
        self._stop_btn.config(state="normal" if locked else "disabled")
        try:
            self.controller.set_run_lock(locked)
        except Exception:
            pass

    def _drv(self):
        drv = self.controller.drivers.get("prober")
        return drv if (drv and drv.inst) else None

    def _browse_export_dir(self):
        selected = filedialog.askdirectory(
            initialdir=self.ui.export_path_var.get(), title="Select Export Directory")
        if selected:
            self.ui.export_path_var.set(selected)

    def _refresh_export_formats(self):
        names = [f["name"] for f in getattr(self.ui, "_export_formats", [])]
        self._export_format_cb.config(values=names)
        if names and self.ui.export_format_var.get() not in names:
            self.ui.export_format_var.set(names[0])

    def _record(self, slot_num, lot_id: str, event: str):
        row = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
              "slot": slot_num, "lot": lot_id, "event": event}
        def _ui():
            self._tree.insert("", "end", values=(row["timestamp"], row["slot"],
                                                  row["lot"], row["event"]))
            children = self._tree.get_children()
            if children:
                self._tree.see(children[-1])
        self.after(0, _ui)

    def _log_event(self, slot_num, lot_id: str, event: str):
        self._log(f"[CASSETTE] Slot {slot_num}: {event}" if slot_num else f"[CASSETTE] {event}")
        self._record(slot_num, lot_id, event)

    def _yield_threshold(self) -> float:
        try:
            return float(self._yield_var.get())
        except ValueError:
            return 0.0

    def on_ata_folder_loaded(self, folder_path: str):
        self._yield_folder = folder_path
        self._yield_var.set(f"{load_yield_threshold(folder_path):g}")

    def _on_yield_edited(self):
        if not self._yield_folder:
            return
        save_yield_threshold(self._yield_folder, self._yield_threshold())


    def _lot_id(self) -> str:
        return self._lot_id_var.get().strip()

    def _redraw_slots(self):
        self._slot_tree.delete(*self._slot_tree.get_children())
        lot_id = self._lot_id()
        for i, wafer_id in enumerate(self._wafers):
            marker = " (current)" if self._armed and i == self._slot_idx else ""
            self._slot_tree.insert("", "end", iid=str(i), values=(
                f"{i + 1}{marker}", lot_id, wafer_id))

    def _add_slot(self):
        wafer_id = simpledialog.askstring("Add Slot", "Wafer ID:", parent=self)
        if wafer_id is None:
            return
        wafer_id = wafer_id.strip()
        if not wafer_id:
            messagebox.showerror("Wafer ID Required", "Wafer ID can't be blank.")
            return
        self._wafers.append(wafer_id)
        self._redraw_slots()

    def _selected_slot_index(self):
        sel = self._slot_tree.selection()
        return int(sel[0]) if sel else None

    def _edit_slot(self):
        idx = self._selected_slot_index()
        if idx is None:
            return
        wafer_id = simpledialog.askstring(
            "Edit Slot", "Wafer ID:", initialvalue=self._wafers[idx], parent=self)
        if wafer_id is None:
            return
        wafer_id = wafer_id.strip()
        if not wafer_id:
            messagebox.showerror("Wafer ID Required", "Wafer ID can't be blank.")
            return
        self._wafers[idx] = wafer_id
        self._redraw_slots()

    def _remove_slot(self):
        idx = self._selected_slot_index()
        if idx is None:
            return
        del self._wafers[idx]
        if self._slot_idx > idx:
            self._slot_idx -= 1
        self._redraw_slots()

    def _move_slot(self, direction: int):
        idx = self._selected_slot_index()
        if idx is None:
            return
        new_idx = idx + direction
        if not (0 <= new_idx < len(self._wafers)):
            return
        self._wafers[idx], self._wafers[new_idx] = self._wafers[new_idx], self._wafers[idx]
        self._redraw_slots()
        self._slot_tree.selection_set(str(new_idx))

    def _clear_slots(self):
        if self._armed:
            messagebox.showerror("Automation Armed", "Stop automation before clearing the list.")
            return
        if self._wafers and not messagebox.askyesno(
            "Clear All", f"Remove all {len(self._wafers)} slot(s)?"):
            return
        self._wafers = []
        self._slot_idx = 0
        self._redraw_slots()

    def _reset_slot(self):
        if self._armed:
            messagebox.showerror("Automation Armed", "Stop automation before resetting.")
            return
        self._slot_idx = 0
        self._lot_summary = []
        self._redraw_slots()
        if self._wafers:
            self.ui.lot_id.set(self._lot_id())
            self.ui.wafer_id_var.set(self._wafers[0])
        self._log_event(1, "", "Reset — next Arm will start tracking from slot #1.")


    def _move_selected_slot_button(self):
        if self._armed:
            messagebox.showerror("Automation Armed",
                                 "Stop automation before moving to a different slot.")
            return
        if not self._move_slot_armed:
            self._move_slot_armed = True
            self._move_slot_btn.config(text="Cancel Move")
            return
        idx = self._selected_slot_index()
        self._disarm_move_slot()
        if idx is None:
            self._log("[CASSETTE] Move to Selected Slot: cancelled.")
            return
        self._do_move_to_slot(idx)

    def _on_move_slot_row_selected(self, _e=None):
        if not self._move_slot_armed:
            return
        if self._selected_slot_index() is not None:
            self._move_slot_btn.config(text="Move")

    def _disarm_move_slot(self):
        self._move_slot_armed = False
        self._move_slot_btn.config(text="Move to Selected Slot")

    def _do_move_to_slot(self, idx: int):
        if not (0 <= idx < len(self._wafers)):
            return
        self._slot_idx = idx
        wafer_id = self._wafers[idx]
        lot_id = self._lot_id()
        self.ui.lot_id.set(lot_id)
        self.ui.wafer_id_var.set(wafer_id)
        self._redraw_slots()
        self._log_event(
            idx + 1, lot_id,
            f"Moved to slot {idx + 1} ({wafer_id}) — next run/▶ Arm will use this "
            "slot. Make sure the physically loaded wafer actually matches before "
            "starting.")


    def _manual_unload(self):
        drv = self._drv()
        if not drv:
            self._log("[CASSETTE] Unload/Abort Lot: prober not connected.")
            return
        armed_note = ("\n\nAutomation is currently armed/paused - this will "
                      "stop it (same as ⏹ Stop Automation) before unloading, "
                      "so the two never disagree about whether the lot is "
                      "still running." if (self._armed or self._paused_for_yield) else "")
        if not messagebox.askyesno(
            "Unload/Abort Lot",
            "This unloads the wafer on the chuck and ends the current lot - "
            "it does not load the next one." + armed_note + "\n\n"
            "Continue?"):
            return
        if self._armed or self._paused_for_yield:
            self._disarm("Lot stopped (Unload/Abort Lot pressed).")
            self._set_state("STOPPED (unloaded)", "#dc2626")
        def _run():
            self._log("[CASSETTE] >> U  (Unload only)")
            stb = drv.unload_wafer()
            if stb == 71:
                self._log("[CASSETTE] Wafer unloaded")
            else:
                self._log(f"[CASSETTE] << STB={stb}  (unexpected)")
        threading.Thread(target=_run, daemon=True).start()

    def _manual_load_next(self):
        drv = self._drv()
        if not drv:
            self._log("[CASSETTE] Load Next Wafer: prober not connected.")
            return
        if not messagebox.askyesno(
            "Load Next Wafer",
            "This sends L (unload the current wafer, if any, and load/"
            "align the next one from the cassette) - the same command "
            "cassette automation itself uses. It does NOT advance "
            "automation's own slot tracking.\n\n"
            "If automation is armed/paused, you must press "
            "🔄 Reset to Slot #1 (or otherwise re-sync the slot list "
            "yourself) before resuming it, or it will get out of sync "
            "with what's physically in the cassette.\n\n"
            "Continue?"):
            return
        def _run():
            self._log("[CASSETTE] >> L  (Unload / Load Next Wafer)")
            stb = drv.cassette_unload_and_load_next()
            if stb == 70:
                self._log("[CASSETTE] << STB=70  (next wafer loaded, start die positioned, chuck DOWN)")
            else:
                self._log("[CASSETTE] No next wafer — cassette empty / idle / timed out "
                         "or wafer load error.")
        threading.Thread(target=_run, daemon=True).start()


    def _arm(self):
        if not self._wafers:
            messagebox.showerror("No Slots", "Add at least one cassette slot "
                                 "(Wafer ID) first.")
            return
        if not self._lot_id():
            messagebox.showerror("Lot ID Required", "Enter the Lot ID for this "
                                 "cassette first.")
            return
        if self._slot_idx >= len(self._wafers):
            messagebox.showerror("Nothing Left", "Every slot in the list is already "
                                 "done — 🔄 Reset to Slot #1 to run it again.")
            return
        if self._auto_export_var.get() and not self.ui.get_selected_export_format():
            messagebox.showerror("No Export Format", "Pick an export format above, or "
                                 "turn off auto-export.")
            return
        current_hook = getattr(self.ui, "_exec_on_run_finished", None)
        autoexport_hook = getattr(self.ui, "_on_autoexport_run_finished", None)
        if current_hook not in (None, self._on_wafer_finished, autoexport_hook):
            messagebox.showerror("Arm Blocked", "Another automation is already watching "
                                 "for the run to finish.")
            return

        self._armed = True
        self._set_paused_for_yield(False)
        self.ui._exec_on_run_finished = self._on_wafer_finished
        self._set_locked(True)
        self._set_state("ARMED — waiting for the current/next run to finish", "#2563eb")
        self._redraw_slots()
        self._log_event(
            self._slot_idx + 1, self._lot_id(),
            "Cassette started, go to run tab, start a run")

    def _set_paused_for_yield(self, paused: bool):
        self._paused_for_yield = paused
        self._refresh_continue_btn()

    def _set_paused_for_error(self, paused: bool, kind: str = "advance"):
        self._paused_for_error = paused
        self._error_retry_kind = kind
        self._refresh_continue_btn()

    def _refresh_continue_btn(self):
        self._continue_btn.config(
            state="normal" if (self._paused_for_yield or self._paused_for_error)
            else "disabled")

    def _continue_after_pause(self):
        if self._armed:
            return
        if self._paused_for_error:
            self._continue_after_error()
            return
        if self._paused_for_yield:
            self._continue_after_yield()

    def _continue_after_yield(self):
        self._set_paused_for_yield(False)
        lot_id = self._lot_id()
        self._slot_idx += 1
        if self._slot_idx >= len(self._wafers):
            self._log_event(self._slot_idx, lot_id,
                            "All slots in the list are complete — cassette automation finished.")
            self._set_state("CASSETTE COMPLETE", "#16a34a")
            self._redraw_slots()
            self._show_lot_summary()
            return
        self._armed = True
        self.ui._exec_on_run_finished = self._on_wafer_finished
        self._set_locked(True)
        self._set_state("SWAPPING CASSETTE", "#f97316")
        self._redraw_slots()
        self._log_event(self._slot_idx + 1, lot_id,
                        "Continuing past the low-yield wafer.")
        threading.Thread(target=self._advance_thread, daemon=True).start()

    def _continue_after_error(self):
        self._set_paused_for_error(False)
        lot_id = self._lot_id()
        self._armed = True
        self.ui._exec_on_run_finished = self._on_wafer_finished
        self._set_locked(True)
        self._redraw_slots()
        if self._error_retry_kind == "start":
            self._set_state("RETRYING — re-attempting to start this slot's run", "#f97316")
            self._log_event(self._slot_idx + 1, lot_id,
                            "Retrying the run start — operator confirmed the issue is fixed.")
            self._start_next_run()
        else:
            self._set_state("RETRYING — re-attempting the failed slot", "#f97316")
            self._log_event(self._slot_idx + 1, lot_id,
                            "Retrying — operator confirmed the issue is fixed.")
            threading.Thread(target=self._advance_thread, daemon=True).start()

    def _disarm(self, reason: str = ""):
        self._armed = False
        self._set_paused_for_yield(False)
        self._set_paused_for_error(False)
        if getattr(self.ui, "_exec_on_run_finished", None) is self._on_wafer_finished:
            self.ui._exec_on_run_finished = None
            claim = getattr(self.ui, "_autoexport_claim_hook", None)
            if claim is not None:
                claim()
        self._set_locked(False)
        self._redraw_slots()
        if reason:
            self._log_event(self._slot_idx + 1, "", reason)

    def _on_wafer_finished(self, pass_n: int, fail_n: int, total_n: int, aborted: bool,
                           run_mode: str = "full"):
        if not self._armed:
            return
        if run_mode:
            self._run_mode = run_mode
        lot_id, wafer_id = self._lot_id(), self._wafers[self._slot_idx]

        if aborted:
            self._log_event(self._slot_idx + 1, lot_id,
                            "Run was aborted — cassette automation stopped.")
            self._disarm()
            self._set_state("STOPPED (run aborted)", "#dc2626")
            return

        tested = pass_n + fail_n
        pct = (pass_n / tested * 100) if tested else 0.0
        self._log_event(self._slot_idx + 1, lot_id,
                        f"Run finished — {pass_n}/{tested} pass ({pct:.1f}%), "
                        f"{total_n} die(s) on the wafer map.")
        self._lot_summary.append({"wafer_id": wafer_id, "pass_n": pass_n,
                                  "fail_n": fail_n, "tested": tested, "pct": pct})

        if self._auto_export_var.get():
            self._export_current(lot_id, wafer_id)

        threshold = self._yield_threshold()
        if tested and pct < threshold:
            self._log_event(self._slot_idx + 1, lot_id,
                            f"Yield {pct:.1f}% is below the {threshold:g}% threshold — "
                            f"PAUSING cassette automation (wafer left loaded).")
            self._disarm()
            self._set_paused_for_yield(True)
            self._set_state(f"PAUSED — yield {pct:.1f}% < {threshold:g}% — "
                            "▶ Continue to proceed anyway", "#f97316")
            return

        self._slot_idx += 1
        if self._slot_idx >= len(self._wafers):
            self._log_event(self._slot_idx, lot_id,
                            "All slots in the list are complete — cassette automation finished.")
            self._disarm()
            self._set_state("CASSETTE COMPLETE", "#16a34a")
            self._show_lot_summary()
            return

        self._set_state("SWAPPING CASSETTE", "#f97316")
        self._redraw_slots()
        threading.Thread(target=self._advance_thread, daemon=True).start()

    def _show_lot_summary(self):
        if self._lot_summary:
            lines = [f"{s['wafer_id']}:  {s['pass_n']}/{s['tested']} pass "
                    f"({s['pct']:.1f}%)" for s in self._lot_summary]
        else:
            lines = ["(no wafers finished this lot)"]
        body = (f"Lot {self._lot_id()!r} — {len(self._lot_summary)} wafer(s):\n\n"
               + "\n".join(lines)
               + f"\n\nExported to: {self.ui.export_path_var.get()}")

        dlg = tk.Toplevel(self)
        dlg.title("Cassette Lot Complete")
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=body, justify="left").pack(anchor="w")

        status_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=status_var, foreground="#6b7280",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 0))

        def _unload_last():
            drv = self._drv()
            if not drv:
                status_var.set("Prober not connected.")
                return
            unload_btn.config(state="disabled")
            status_var.set("Unloading…")

            def _run():
                self._log("[CASSETTE] >> U  (Unload last wafer)")
                stb = drv.unload_wafer()
                msg = (f"Unloaded (STB={stb})." if stb == 71
                      else f"Unexpected STB={stb}.")
                self._log(f"[CASSETTE] << STB={stb}")
                self.after(0, lambda: status_var.set(msg))
                self.after(0, lambda: unload_btn.config(state="normal"))
            threading.Thread(target=_run, daemon=True).start()

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        unload_btn = ttk.Button(btns, text="⏏ Unload Last Wafer", command=_unload_last)
        unload_btn.pack(side="left")
        ttk.Button(btns, text="Close", command=dlg.destroy).pack(side="right")

        dlg.update_idletasks()
        dlg.grab_set()

    def _export_current(self, lot_id: str, wafer_id: str):
        self.ui.lot_id.set(lot_id)
        self.ui.wafer_id_var.set(wafer_id)
        try:
            path = self.controller.cmd_export_sql()
        except Exception as e:
            path = None
            self._log_event(self._slot_idx + 1, lot_id, f"Auto-export error: {e}")
        self._log_event(
            self._slot_idx + 1, lot_id,
            f"Format export -> {path}" if path else
            "⚠ Format export produced no file - see the Run tab log for why.")
        if self._auto_export_csv_var.get():
            try:
                csv_path = self.controller.cmd_save_csv()
            except Exception as e:
                csv_path = None
                self._log_event(self._slot_idx + 1, lot_id, f"Auto CSV export error: {e}")
            self._log_event(
                self._slot_idx + 1, lot_id,
                f"Plain CSV -> {csv_path}" if csv_path else
                "⚠ Plain CSV export produced no file - see the Run tab log for why.")

    def _advance_thread(self):
        drv = self._drv()
        try:
            if drv is None:
                self._log("[CASSETTE] Unload/load-next error: prober not connected")
                next_ready = False
            else:
                self._log("[CASSETTE] >> L  (Unload / Load Next Wafer)")
                next_ready = drv.cassette_unload_and_load_next() == 70
        except Exception as e:
            self._log(f"[CASSETTE] Unload/load-next error: {e}")
            next_ready = False

        if not next_ready:
            self.after(0, lambda: self._log_event(
                self._slot_idx + 1, "", "No next wafer (cassette empty/idle/error) — "
                "cassette automation stopped. Fix the physical issue, then press "
                "▶ Continue to retry this slot."))
            self.after(0, self._disarm)
            self.after(0, lambda: self._set_paused_for_error(True, "advance"))
            self.after(0, lambda: self._set_state(
                f"PAUSED (slot {self._slot_idx + 1} failed to load) — fix the issue, "
                "then ▶ Continue to retry", "#dc2626"))
            return

        wafer_id = self._wafers[self._slot_idx]
        lot_id = self._lot_id()
        self.after(0, lambda: self.ui.lot_id.set(lot_id))
        self.after(0, lambda: self.ui.wafer_id_var.set(wafer_id))
        self.after(0, lambda: self._log_event(
            self._slot_idx + 1, lot_id,
            "Next wafer ready (STB=70) — auto-starting its run."))
        self.after(0, self._start_next_run)

    def _start_next_run(self):
        if not self._armed:
            return
        try:
            if self._run_mode == "test":
                sites = list(getattr(self.ui, "_exec_last_test_sites", None) or [])
                if not sites:
                    self._log_event(self._slot_idx + 1, "",
                                    "Could not auto-start the next run: no remembered "
                                    "Test Selected sites from the first wafer. Fix "
                                    "and press ▶ Continue to retry starting this slot.")
                    self._disarm()
                    self._set_paused_for_error(True, "start")
                    self._set_state(
                        f"PAUSED (slot {self._slot_idx + 1} — auto-start failed) — "
                        "fix the issue, then ▶ Continue to retry", "#dc2626")
                    return
                self.ui._exec_start_site_list(sites, "Test Die", "test")
            elif self._run_mode == "run":
                self.ui._exec_start_run()
            else:
                self.ui._exec_start_full_die()
        except Exception as e:
            self._log_event(self._slot_idx + 1, "",
                            f"Could not auto-start the next run: {e}. Fix and press "
                            "▶ Continue to retry starting this slot.")
            self._disarm()
            self._set_paused_for_error(True, "start")
            self._set_state(
                f"PAUSED (slot {self._slot_idx + 1} — auto-start failed) — "
                "fix the issue, then ▶ Continue to retry", "#dc2626")
