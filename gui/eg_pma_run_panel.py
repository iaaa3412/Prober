"""Drive a .PMA recipe on the Electroglas as relative die steps."""

import os
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from electroglas_pma import (format_quad, expand_touchdowns_to_dies,
                             die_grid_index, QUAD_ORDER,
                             shot_geometry, slot_names, slot_grid,
                             quad_positions, serpentine_order)
from recipe_gen_panel import shot_die_rc

_POS_RE = re.compile(r"X(-?\d+)Y(-?\d+)")

MOTION_DIE = "die"
MOTION_UM = "um"

_MAX_UM_HOP = 150000


def parse_position(reply) -> tuple:
    m = _POS_RE.match(str(reply or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def chunk_step(dx: int, dy: int, cap: int) -> list:
    hops = []
    while dx or dy:
        hop_x = max(-cap, min(cap, dx))
        hop_y = max(-cap, min(cap, dy))
        hops.append((hop_x, hop_y))
        dx -= hop_x
        dy -= hop_y
    return hops


class EgPmaRunPanel(ttk.Frame):
    def __init__(self, parent, controller, main_layout=None):
        super().__init__(parent)
        self.controller = controller
        self._main_layout = main_layout

        self._recipe_path = None
        self._fields = {}
        self._touchdowns = []
        self._pma_raw_touchdowns = []
        self._die_um = (0.0, 0.0)
        self._index = None
        self._anchored = False
        self._origin_offset = (0, 0)
        self._size_confirmed = False
        self._running = False
        self._abort = False
        self._paused = False
        self._rc = {}
        self._cells = {}
        self._results = {}
        self._die_results = {}
        self._slot_rc = {}
        self._last_seq = None

        self._selected = None
        self._seq_at_rc = {}
        self._die_at_rc = {}
        self._sel_rc = None
        self._shot_window_items = []
        self._sel_window_items = []
        self._move_armed = False
        self._goto_btn = None
        self._um_residual = [0.0, 0.0]

        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        self._build_recipe_row()
        self._build_anchor()
        self._build_controls()
        self._build_selection()
        self._build_table()


    def _log(self, msg: str):
        self.controller.log(msg)

    def _prober(self):
        drv = self.controller.drivers.get("prober")
        return drv if (drv and drv.inst) else None

    def _ui(self, fn):
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass


    def _build_recipe_row(self):
        self._recipe_var = tk.StringVar(value="(none loaded)")

    def _build_anchor(self):
        self._info_var = tk.StringVar(value="Load a .PMA to begin.")

    def _build_controls(self):
        lf = ttk.LabelFrame(self, text="Run", padding=6)
        lf.grid(row=2, column=0, sticky="ew", padx=6, pady=2)

        anchor = ttk.Frame(lf)
        anchor.pack(fill="x")
        anchor.columnconfigure(1, weight=1)
        ttk.Label(anchor, text="Chuck is on:").grid(row=0, column=0, sticky="w")
        self._anchor_var = tk.StringVar()
        self._anchor_cb = ttk.Combobox(anchor, textvariable=self._anchor_var,
                                       width=44)
        self._anchor_cb.grid(row=0, column=1, sticky="ew", padx=6)
        self._anchor_cb.bind("<KeyRelease>", self._on_anchor_typed)
        ttk.Button(anchor, text="Set", command=self._set_anchor).grid(row=0, column=2)

        self._anchor_state_var = tk.StringVar(value="not set")
        ttk.Label(anchor, textvariable=self._anchor_state_var,
                  font=("Consolas", 8), foreground="#b45309").grid(
                  row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        btns = ttk.Frame(lf)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="↻ Sync", command=self._sync_position).pack(side="left")

        mode = ttk.Frame(lf)
        mode.pack(fill="x", pady=(6, 0))
        ttk.Label(mode, text="Move by:").pack(side="left")
        self._motion_var = tk.StringVar(value=MOTION_UM)
        ttk.Radiobutton(mode, text="die steps (MD)", value=MOTION_DIE,
                        variable=self._motion_var,
                        command=self._on_motion_mode).pack(side="left", padx=(6, 0))
        self._um_radio = ttk.Radiobutton(
            mode, text="microns (MM)",
            value=MOTION_UM, variable=self._motion_var,
            command=self._on_motion_mode)
        self._um_radio.pack(side="left", padx=(8, 0))
        self._on_motion_mode()

        self._status_var = tk.StringVar(value="idle")
        self._pos_var = tk.StringVar(value="—")
        self._shot_window_var = tk.StringVar(value="Shot window: chuck not set")

    def _build_selection(self):
        pass

    def _build_table(self):
        lf = ttk.LabelFrame(self, text="Die list", padding=4)
        lf.grid(row=5, column=0, sticky="nsew", padx=6, pady=(2, 6))
        lf.rowconfigure(1, weight=1)
        lf.columnconfigure(0, weight=1)

        bar = ttk.Frame(lf)
        bar.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        self._table_count_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._table_count_var, foreground="#6b7280",
                  font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))

        cols = ("seq", "grid", "run", "step", "devices")
        self._tree = ttk.Treeview(lf, columns=cols, show="headings", height=10)
        for col, head, width, stretch in (("seq", "#", 46, False),
                                          ("grid", "grid x,y", 74, False),
                                          ("run", "run", 40, False),
                                          ("step", "MD", 64, False),
                                          ("devices", "devices", 260, True)):
            self._tree.heading(col, text=head)
            self._tree.column(col, width=width, anchor="w", stretch=stretch)
        self._tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.tag_configure("here", background="#fde68a")
        self._tree.tag_configure("done", foreground="#9ca3af")
        self._tree.tag_configure("offrun", foreground="#9ca3af")
        self._tree.bind("<<TreeviewSelect>>", self._on_table_click)


    def adopt_from_wafer_builder(self, quiet: bool = True) -> bool:
        self._builder_shot_cache = None
        self._builder_pitch_cache = None
        self._builder_slots_cache = None
        self._builder_grid_cache = None
        self._builder_offset_cache = None
        wm = self._run_map()
        dies = list(getattr(wm, "_last_dies", None) or [])
        if not dies:
            if not quiet:
                messagebox.showinfo(
                    "Wafer Builder", "No wafer map published yet - build "
                    "the Die Map on the Wafer Builder tab first.")
            return False
        shot_rows, shot_cols = self._builder_shot_layout()
        dx, dy = self._builder_die_pitch()
        gen = getattr(self._main_layout, "recipe_gen", None)
        if (shot_rows <= 0 or shot_cols <= 0 or dx <= 0 or dy <= 0) and gen is not None:
            try:
                if shot_rows <= 0 or shot_cols <= 0:
                    shot_rows, shot_cols = gen._shot_dims()
                if dx <= 0 or dy <= 0:
                    dx, dy = gen._die_pitch()
            except Exception as e:
                if not quiet:
                    messagebox.showerror("Wafer Builder", f"Could not read the "
                                         f"Shot tab's dims/pitch: {e}")
                return False
        if shot_rows <= 0 or shot_cols <= 0 or dx <= 0 or dy <= 0:
            if not quiet:
                messagebox.showinfo(
                    "Wafer Builder", "The published map does not say what "
                    "shape a shot is or how far apart the dies are, and the "
                    "Wafer Builder tab has no project open to ask instead.")
            return False

        die_id_by_rc = {
            (d["row"], d["col"]): (d.get("die_id") or "").strip()
            for d in dies
            if d.get("row") is not None and d.get("col") is not None}
        if not die_id_by_rc:
            if not quiet:
                messagebox.showinfo("Wafer Builder", "No dies are marked "
                                    "present on the Wafer Builder map yet.")
            return False

        by_shot, _rc_to_shot = self._builder_shot_slots()
        die_by_rc = {(d["row"], d["col"]): d for d in dies
                     if d.get("row") is not None and d.get("col") is not None}
        order = slot_names(shot_rows, shot_cols)
        touchdowns = []
        seq = 1
        for map_seq in sorted(by_shot):
            slots = by_shot[map_seq]
            devices = [die_id_by_rc.get(slots.get(q)) or "NA" for q in order]
            for q in order:
                rc = slots.get(q)
                if rc is None:
                    continue
                d = die_by_rc[rc]
                die_id = die_id_by_rc.get(rc) or ""
                if not die_id:
                    die_id = f"({rc[0]},{rc[1]})"
                x = float(d.get("x_um") or 0.0)
                y = -float(d.get("y_um") or 0.0)
                touchdowns.append({
                    "seq": seq,
                    "major_index": seq,
                    "minor_index": 1,
                    "device_id": die_id,
                    "device_id_major": die_id,
                    "devices": devices,
                    "slot": q,
                    "map_row": rc[0],
                    "map_col": rc[1],
                    "x": x,
                    "y": y,
                    "major_x": x,
                    "major_y": y,
                })
                seq += 1

        if not touchdowns:
            if not quiet:
                messagebox.showinfo("Wafer Builder", "No dies are marked "
                                    "present on the Wafer Builder map yet.")
            return False

        same = (len(touchdowns) == len(self._touchdowns) and
                all(a["seq"] == b["seq"] and a["device_id"] == b["device_id"]
                    and a["x"] == b["x"] and a["y"] == b["y"]
                    for a, b in zip(touchdowns, self._touchdowns)))
        if same:
            if not quiet:
                self._log(f"[RUN] Die list already matches the Wafer Builder "
                          f"map ({len(touchdowns)} dies) - nothing to rebuild.")
            return True

        self._adopt("(Wafer Builder map — no .PMA)",
                    {"DieSizeX": dx, "DieSizeY": dy}, touchdowns)
        self._log(
            f"[RUN] Built {len(touchdowns)} die(s) directly from the Wafer "
            f"Builder map ({shot_rows}x{shot_cols} shot, die pitch "
            f"{dx:.0f} x {dy:.0f} um) - no .PMA file used.")
        return True

    def _adopt(self, path: str, fields: dict, touchdowns: list):
        self._builder_grid_cache = None
        self._builder_offset_cache = None
        self._grid_fallback_warned = False
        self._shot_corner_warned = False
        self._recipe_path = path
        self._fields = fields
        self._touchdowns = touchdowns
        self._die_um = (float(fields["DieSizeX"]), float(fields["DieSizeY"]))
        self._pma_order_keys = [self._grid_xy(t) for t in touchdowns]
        self._pma_raw_touchdowns = touchdowns
        self._index = None
        self._anchored = False
        self._origin_offset = (0, 0)
        self._size_confirmed = False
        self._rc = {}
        self._cells = {}
        self._builder_grid_cache = None
        self._builder_offset_cache = None
        self._results = {}
        self._die_results = {}
        self._last_seq = None
        self._build_rc_index()
        self._recipe_var.set(os.path.basename(path))
        self._fill_info()
        self._fill_anchor_choices()
        self._fill_table()
        self._anchor_state_var.set("not set — pick where the chuck is, then Set")
        self._log(f"[RUN] Loaded {os.path.basename(path)}: {len(touchdowns)} touchdowns, "
                  f"die {self._die_um[0]:.0f} x {self._die_um[1]:.0f} um")

    def forget_recipe(self):
        self._recipe_path = ""
        self._fields = {}
        self._touchdowns = []
        self._pma_order_keys = []
        self._pma_raw_touchdowns = []
        self._index = None
        self._anchored = False
        self._origin_offset = (0, 0)
        self._size_confirmed = False
        self._rc = {}
        self._cells = {}
        self._builder_grid_cache = None
        self._builder_offset_cache = None
        self._slot_rc = {}
        self._results = {}
        self._die_results = {}
        self._last_seq = None
        self._anchor_choices = []
        try:
            self._disarm_move()
        except Exception:
            self._move_armed = False
        self._selected = None
        self._sel_rc = None
        try:
            self._recipe_var.set("(none)")
            self._anchor_cb.config(values=[])
            self._anchor_var.set("")
            self._anchor_state_var.set("not set — load a recipe for this wafer")
            self._info_var.set("No recipe loaded.")
            self._tree.delete(*self._tree.get_children())
            self._clear_selection_window()
            self.update_shot_window()
            self._update_move_button()
        except Exception:
            pass
        self._log("[RUN] Run tab cleared — the ATA folder changed, so the "
                  "previous wafer's touchdowns no longer apply.")

    def _fill_info(self):
        dx, dy = self._die_um
        n = len(self._touchdowns)
        rows, cols = self.shot_layout()
        lines = [
            f"shot pitch             {dx:.0f} x {dy:.0f} um   "
            f"= {dx / 1000:.3f} x {dy / 1000:.3f} mm",
            f"shot                   {rows} x {cols} dies",
            f"touchdowns this run    {len(self._enabled_indices())}",
        ]
        if len(self._enabled_indices()) != n:
            lines.append(f"wafer positions        {n}  (the chuck can be set "
                         "to, or moved to, any of them)")
        self._info_var.set("\n".join(lines))


    def _builder_grid_lookup(self) -> dict:
        cached = getattr(self, "_builder_grid_cache", None)
        if cached is not None:
            return cached
        wm = self._run_map()
        dies = getattr(wm, "_last_dies", None) or []
        cached = {}
        for d in dies:
            die_id = (d.get("die_id") or "").strip()
            if die_id and d.get("row") is not None and d.get("col") is not None:
                cached.setdefault(die_id, (int(d["col"]), int(d["row"])))
        self._builder_grid_cache = cached
        return cached

    def _builder_grid_xy(self, t):
        if t.get("map_row") is not None and t.get("map_col") is not None:
            return (int(t["map_col"]), int(t["map_row"]))
        lut = self._builder_grid_lookup()
        if not lut:
            return None
        rows, cols = self.shot_layout()
        grid = slot_grid(rows, cols)
        corners = []
        for ent in quad_positions(t["device_id"], rows, cols):
            cell = lut.get((ent["device"] or "").strip())
            if cell is None:
                continue
            slot_c, slot_r = grid.get(ent["pos"], (0, 0)) if ent["pos"] else (0, 0)
            corners.append(((cell[0] - slot_c, cell[1] - slot_r), ent["device"]))
        if not corners:
            return None
        distinct = {c for c, _ in corners}
        if len(distinct) > 1 and not getattr(self, "_shot_corner_warned", False):
            self._shot_corner_warned = True
            detail = ", ".join(f"{d}->{c}" for c, d in corners)
            self._log(
                f"[RUN] ⚠ shot '{t.get('device_id')}' does not sit on the Wafer "
                f"Builder map as one block ({detail}). The recipe groups those "
                "dies into one touchdown but the published map spaces them "
                "differently, so this shot's position is a best guess. Republish "
                "the map from the definition this recipe was built for.")
        best = max(distinct, key=lambda c: sum(1 for x, _ in corners if x == c))
        return best

    def _grid_xy(self, t) -> tuple:
        cell = self._builder_grid_xy(t)
        if cell is not None:
            return cell
        raw = (round(t["x"] / self._die_um[0]), round(t["y"] / self._die_um[1]))
        ox, oy = self._builder_frame_offset()
        if (ox, oy) != (0, 0) and not getattr(self, "_grid_fallback_warned", False):
            self._grid_fallback_warned = True
            self._log(
                f"[RUN] '{t.get('device_id')}' (and possibly others) is not on the "
                f"Wafer Builder map; placing it from the .xls, shifted by "
                f"({ox:+d},{oy:+d}) into the map's frame. Republish the map to "
                "include those dies rather than relying on this.")
        return (raw[0] + ox, raw[1] + oy)

    def _builder_frame_offset(self) -> tuple:
        cached = getattr(self, "_builder_offset_cache", None)
        if cached is not None:
            return cached
        offset, votes = (0, 0), {}
        if self._builder_grid_lookup():
            for t in self._touchdowns or []:
                cell = self._builder_grid_xy(t)
                if cell is None:
                    continue
                raw = (round(t["x"] / self._die_um[0]),
                       round(t["y"] / self._die_um[1]))
                d = (cell[0] - raw[0], cell[1] - raw[1])
                votes[d] = votes.get(d, 0) + 1
            if votes:
                offset = max(votes, key=votes.get)
        self._builder_offset_cache = offset
        return offset

    def _expected_position(self, t) -> tuple:
        qx, qy = self._grid_xy(t)
        ox, oy = self._origin_offset
        return (qx + ox, qy + oy)

    def _fill_anchor_choices(self):
        choices = []
        for t in self._touchdowns:
            choices.append(f"#{t['seq']} {t['device_id']}")
        if not self._anchored:
            want = self._recipe_align_die()
            if want:
                want_id = want.split(" ", 1)[-1].strip() if want.startswith("#") else want
                hit = next((c for c in choices
                            if c == want
                            or c.split(" ", 1)[-1].strip() in (want, want_id)),
                           None)
                if hit is not None:
                    choices.remove(hit)
                    choices.insert(0, hit)
        self._anchor_choices = choices
        self._anchor_cb.config(values=choices)
        self._anchor_var.set(choices[0] if choices else "")

    def _recipe_align_die(self) -> str:
        panel = getattr(self._main_layout, "recipe_panel", None)
        getter = getattr(panel, "get_align_die", None)
        if getter is None:
            return ""
        try:
            return (getter() or "").strip()
        except Exception:
            return ""

    _ANCHOR_MAX_LISTED = 300

    def _on_anchor_typed(self, event=None):
        if event is not None and event.keysym in (
                "Up", "Down", "Left", "Right", "Return", "Escape", "Tab"):
            return
        text = self._anchor_var.get().strip().lower()
        if not text:
            matches = self._anchor_choices
        else:
            matches = [c for c in self._anchor_choices if text in c.lower()]
        self._anchor_cb.config(values=matches[:self._ANCHOR_MAX_LISTED])

    def _table_position(self, t) -> tuple:
        if self._motion_var.get() == MOTION_UM:
            return (round(t["x"]), round(t["y"]))
        return self._grid_xy(t)

    def _display_run_order(self):
        loaded_name = getattr(self._main_layout, "_exec_loaded_recipe_name", None)
        if not (loaded_name and loaded_name()):
            return []
        return self._enabled_indices()

    def _fill_table(self):
        um_mode = self._motion_var.get() == MOTION_UM
        run_order = self._display_run_order()
        anchored = (self._anchored and self._index is not None
                   and 0 <= self._index < len(self._touchdowns))
        signature = (id(self._touchdowns), len(self._touchdowns),
                    tuple(run_order), um_mode, anchored, self._index)
        if signature == getattr(self, "_fill_table_signature", None):
            return
        self._fill_table_signature = signature

        self._tree.delete(*self._tree.get_children())
        self._tree.heading("grid", text="µm x,y" if um_mode else "grid x,y")
        self._tree.heading("step", text="MM (µm)" if um_mode else "MD")
        in_run = set(run_order)
        prev = self._table_position(self._touchdowns[self._index]) if anchored else None
        for i in run_order:
            if self._tree.exists(str(i)):
                continue
            t = self._touchdowns[i]
            qx, qy = self._table_position(t)
            step = f"{qx - prev[0]:+d},{qy - prev[1]:+d}" if anchored else ""
            self._tree.insert("", "end", iid=str(i),
                              values=(t["seq"], f"{qx},{qy}", "✓", step,
                                      t["device_id"]))
            prev = (qx, qy)
        n_off = 0
        for i, t in enumerate(self._touchdowns):
            if i in in_run or self._tree.exists(str(i)):
                continue
            qx, qy = self._table_position(t)
            self._tree.insert("", "end", iid=str(i), tags=("offrun",),
                              values=(t["seq"], f"{qx},{qy}", "", "",
                                      t["device_id"]))
            n_off += 1
        self._table_count_var.set(
            f"{len(run_order)} probed by this recipe"
            + (f", {n_off} more on the wafer" if n_off else ""))


    def _resolve_anchor(self, choice: str):
        text = (choice or "").strip()
        if not text:
            messagebox.showwarning("Anchor", "Pick or type where the chuck is first.")
            return None

        m = re.search(r"#(\d+)", text)
        if m:
            seq = int(m.group(1))
            idx = next((i for i, t in enumerate(self._touchdowns)
                        if t["seq"] == seq), None)
            if idx is None:
                messagebox.showwarning("Anchor", f"No touchdown #{seq} in this recipe.")
            return idx

        want = text.upper()
        exact = [i for i, t in enumerate(self._touchdowns)
                 if want in {d.strip().upper()
                             for d in (t.get("devices") or [t["device_id"]])}
                 or want == t["device_id"].strip().upper()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            seqs = ", ".join(f"#{self._touchdowns[i]['seq']}" for i in exact[:6])
            messagebox.showwarning(
                "Anchor", f"'{text}' appears at {len(exact)} touchdowns ({seqs}"
                          f"{'…' if len(exact) > 6 else ''}).\n\n"
                          "Pick the one you want from the list instead.")
            return None
        messagebox.showwarning(
            "Anchor", f"No touchdown matches '{text}'.\n\n"
                      "Type a die ID, or pick an entry from the list.")
        return None

    def _set_anchor(self):
        if not self._touchdowns:
            return
        choice = self._anchor_var.get()
        idx = self._resolve_anchor(choice)
        if idx is None:
            return

        drv = self._prober()

        dx, dy = self._die_um
        if not self._size_confirmed:
            if not self._confirm_die_size(dx, dy, drv):
                return
            self._size_confirmed = True

        if not drv:
            messagebox.showwarning(
                "Anchor", "Prober not connected - cannot read its real "
                          "position to anchor against.")
            return

        def _work():
            real = self._read_position(drv)
            self._ui(lambda: self._finish_anchor(idx, real))
        threading.Thread(target=_work, daemon=True).start()

    def _confirm_die_size(self, dx: float, dy: float, drv) -> bool:
        result = {"ok": False}
        dlg = tk.Toplevel(self)
        dlg.title("Confirm die size")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        dlg.resizable(False, False)

        mm_mode = self._motion_var.get() == MOTION_UM
        body = (
            f"This recipe steps by {dx:.0f} x {dy:.0f} um "
            f"({dx / 1000:.3f} x {dy / 1000:.3f} mm).\n\n"
            + ("MM moves in real microns, so a mismatch will not send a "
               "step to the wrong place by itself - but the prober's own "
               "?P position reply still counts in ITS die size, not this "
               "one, and the software trusts ?P to say where the chuck "
               "really is between every move. A mismatch there does not "
               "break the move, it breaks the software's belief about "
               "where the chuck is.\n\n"
               if mm_mode else
               "MD moves by the PROBER'S configured die size, not this "
               "one. They must match, or every step lands between "
               "quads.\n\n")
        )
        ttk.Label(dlg, text=body, wraplength=380, justify="left").pack(
            padx=16, pady=(16, 10))

        btns = ttk.Frame(dlg)
        btns.pack(padx=16, pady=(0, 16), fill="x")

        def _send_now():
            if not drv:
                messagebox.showwarning(
                    "Confirm die size",
                    "Prober not connected - cannot send the die size.",
                    parent=dlg)
                return
            try:
                drv.set_die_size(dx, dy)
                self._log(f"[RUN] >> SP1X{dx:.0f}Y{dy:.0f}  "
                          "(die size sent to prober)")
            except Exception as e:
                messagebox.showerror(
                    "Confirm die size", f"Could not send die size: {e}",
                    parent=dlg)
                return
            result["ok"] = True
            dlg.destroy()

        def _already_set():
            result["ok"] = True
            dlg.destroy()

        def _cancel():
            result["ok"] = False
            dlg.destroy()

        ttk.Button(btns, text="📤 Send to Prober Now", command=_send_now).pack(
            side="left", padx=(0, 6))
        ttk.Button(btns, text="✓ Already Set", command=_already_set).pack(
            side="left", padx=(0, 6))
        ttk.Button(btns, text="Cancel", command=_cancel).pack(side="right")

        dlg.protocol("WM_DELETE_WINDOW", _cancel)
        dlg.update_idletasks()
        pw = self.winfo_toplevel()
        x = pw.winfo_x() + (pw.winfo_width() - dlg.winfo_width()) // 2
        y = pw.winfo_y() + (pw.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")
        dlg.wait_window()
        return result["ok"]

    def _finish_anchor(self, idx: int, real):
        if real is None:
            messagebox.showwarning(
                "Anchor", "Could not read the prober's real position (?P) - "
                          "cannot anchor. Check the link and try again.")
            return
        t = self._touchdowns[idx]
        theoretical = self._grid_xy(t)
        self._origin_offset = (real[0] - theoretical[0], real[1] - theoretical[1])
        self._index = idx
        self._anchored = True
        self._um_residual = [0.0, 0.0]
        qx, qy = theoretical
        offset_note = (f", origin offset {self._origin_offset}"
                       if self._origin_offset != (0, 0) else "")
        self._anchor_state_var.set(
            f"anchored at #{t['seq']} grid ({qx},{qy}) — real X{real[0]}Y{real[1]}"
            f"{offset_note} — {t['device_id']}")
        self._mark_current()
        self._refresh_position()
        self._fill_table()
        try:
            wmap = self._run_map()
            if wmap is not None:
                wmap.canvas.update_idletasks()
        except Exception:
            pass
        self._log(f"[RUN] Anchored at #{t['seq']} {t['device_id']} grid ({qx},{qy})"
                  f"{offset_note}")

    def _mark_current(self):
        run_order = self._enabled_indices()
        in_run = set(run_order)
        done_upto = run_order.index(self._index) if self._index in in_run else None
        for iid in self._tree.get_children():
            i = int(iid)
            tags = () if i in in_run else ("offrun",)
            if self._index is not None:
                if i == self._index:
                    tags = ("here",)
                elif done_upto is not None and i in in_run \
                        and run_order.index(i) < done_upto:
                    tags = ("done",)
            self._tree.item(iid, tags=tags)
        if self._index is not None:
            self._tree.see(str(self._index))

    def _refresh_position(self):
        if self._index is None:
            self._pos_var.set("—")
            self._mark_on_wafer_map(None)
            return
        t = self._touchdowns[self._index]
        qx, qy = self._grid_xy(t)
        self._pos_var.set(f"#{t['seq']}/{len(self._touchdowns)}  grid ({qx},{qy})  "
                          f"{t['device_id']}")
        self._mark_on_wafer_map(t)
        self._highlight(self._index)


    def _builder_shot_layout(self) -> tuple:
        cached = getattr(self, "_builder_shot_cache", None)
        if cached is not None:
            return cached
        wm = self._run_map()
        dies = getattr(wm, "_last_dies", None) or []
        max_r = max_c = -1
        for d in dies:
            pos = (d.get("quad_pos") or "").strip()
            if not pos:
                continue
            if pos.upper() in QUAD_ORDER:
                max_r = max_c = 1
                break
            m = re.fullmatch(r"R(\d+)C(\d+)", pos, re.IGNORECASE)
            if m:
                max_r = max(max_r, int(m.group(1)))
                max_c = max(max_c, int(m.group(2)))
        out = (0, 0) if (max_r < 0 or max_c < 0) else (max_r + 1, max_c + 1)
        self._builder_shot_cache = out
        return out

    def _builder_shot_slots(self) -> tuple:
        cached = getattr(self, "_builder_slots_cache", None)
        if cached is not None:
            return cached
        wm = self._run_map()
        dies = getattr(wm, "_last_dies", None) or []
        rows, cols = self.shot_layout()
        names = slot_names(rows, cols)
        grid = slot_grid(rows, cols)
        by_cell = {cr: nm for nm, cr in grid.items()}
        by_shot, rc_to_shot = {}, {}
        for d in dies:
            seq = d.get("seq")
            r, c = d.get("row"), d.get("col")
            if seq is None or r is None or c is None:
                continue
            pos = (d.get("quad_pos") or "").strip()
            if pos in names:
                slot = pos
            else:
                m = re.fullmatch(r"R(\d+)C(\d+)", pos, re.IGNORECASE)
                slot = by_cell.get((int(m.group(2)), int(m.group(1)))) if m else None
                if slot is None:
                    slot = names[0] if len(names) == 1 else None
                    if slot is None:
                        continue
            by_shot.setdefault(seq, {})[slot] = (r, c)
            rc_to_shot[(r, c)] = seq
        cached = (by_shot, rc_to_shot)
        self._builder_slots_cache = cached
        return cached

    def _builder_die_pitch(self) -> tuple:
        cached = getattr(self, "_builder_pitch_cache", None)
        if cached is not None:
            return cached
        wm = self._run_map()
        dies = getattr(wm, "_last_dies", None) or []

        def spacing(key):
            vals = sorted({round(float(d[key])) for d in dies
                           if d.get(key) is not None})
            gaps = {}
            for a, b in zip(vals, vals[1:]):
                if b > a:
                    gaps[b - a] = gaps.get(b - a, 0) + 1
            return max(gaps, key=gaps.get) if gaps else 0

        try:
            out = (float(spacing("x_um")), float(spacing("y_um")))
        except (TypeError, ValueError):
            out = (0.0, 0.0)
        self._builder_pitch_cache = out
        return out

    def shot_layout(self) -> tuple:
        rows, cols = self._builder_shot_layout()
        if rows <= 0 or cols <= 0:
            gen = getattr(self._main_layout, "recipe_gen", None)
            if gen is not None:
                try:
                    gr, gc = gen._shot_dims()
                except Exception:
                    gr, gc = 0, 0
                if gr * gc > 1:
                    rows, cols = gr, gc
        widest = max(
            (len(str(t.get("device_id", "")).split("/"))
             for t in (self._touchdowns or [])), default=1)
        return shot_geometry(widest, rows, cols)


    def _wafer_builder_rc_lookup(self) -> dict:
        wm = self._run_map()
        dies = getattr(wm, "_last_dies", None) or []
        return {(round(d["x_um"]), round(-d["y_um"])): (d["row"], d["col"])
                for d in dies
                if d.get("row") is not None and d.get("col") is not None
                and d.get("x_um") is not None and d.get("y_um") is not None}

    def _wafer_builder_die_id_lookup(self) -> dict:
        wm = self._run_map()
        dies = getattr(wm, "_last_dies", None) or []
        return {(d["row"], d["col"]): d["die_id"] for d in dies if d.get("die_id")}

    def _build_rc_index(self):
        self._builder_shot_cache = None
        self._builder_pitch_cache = None
        self._builder_slots_cache = None
        self._grid_index_cache = None
        rows, cols = self.shot_layout()
        rc_lookup = self._wafer_builder_rc_lookup()
        die_id_lookup = self._wafer_builder_die_id_lookup()
        dies = expand_touchdowns_to_dies(self._touchdowns, *self._die_um,
                                         rows=rows, cols=cols)

        self._cells = {}
        self._builder_grid_cache = None
        self._builder_offset_cache = None
        self._rc = {}
        self._seq_at_rc = {}
        self._die_at_rc = {}
        self._anchor_rc = {}
        self._slot_rc = {}
        missing = 0
        by_id = {}
        for rc_key, wb_id in die_id_lookup.items():
            if wb_id:
                by_id.setdefault(wb_id.strip(), rc_key)
        td_rc = {t["seq"]: (t["map_row"], t["map_col"])
                 for t in self._touchdowns
                 if t.get("map_row") is not None and t.get("map_col") is not None}
        for d in dies:
            rc = td_rc.get(d["seq"])
            if rc is None:
                rc = by_id.get((d.get("device_id") or "").strip())
            if rc is None:
                rc = rc_lookup.get((round(d["x"]), round(d["y"])))
            if rc is None:
                missing += 1
                continue
            wb_id = die_id_lookup.get(rc)
            if wb_id:
                d = dict(d, device_id=wb_id)
            self._cells.setdefault(d["seq"], []).append(rc)
            self._slot_rc.setdefault(d["seq"], {})[d["quad_pos"]] = rc
            self._seq_at_rc[rc] = d["seq"]
            self._die_at_rc[rc] = d
            self._anchor_rc.setdefault(d["seq"], rc)
        if missing:
            self._log(f"[RUN] {missing} of {len(dies)} recipe dies are not on "
                      "the Wafer Builder map — the .PMA and the published map "
                      "look like they are for different wafers, or the map has "
                      "not been (re)published since this recipe loaded.")
        self._rc = {seq: cells[0] for seq, cells in self._cells.items()}

        by_shot, rc_to_shot = self._builder_shot_slots()
        order = slot_names(rows, cols)
        for t in self._touchdowns:
            rc = self._anchor_rc.get(t["seq"])
            shot = by_shot.get(rc_to_shot.get(rc)) if rc is not None else None
            if not shot:
                continue
            self._slot_rc[t["seq"]] = dict(shot)
            t["devices"] = [die_id_lookup.get(shot.get(q)) or "NA" for q in order]

        for t in self._touchdowns:
            anchor = self._anchor_rc.get(t["seq"])
            if anchor is None:
                continue
            wb_id = die_id_lookup.get(anchor)
            if wb_id and wb_id != t["device_id"]:
                t["device_id"] = wb_id
                t["devices"] = [wb_id]

    def _run_map(self):
        return getattr(self._main_layout, "_exec_wafer_map", None)

    def _results_map(self):
        return getattr(self._main_layout, "_results_wafer_map", None)

    def _paint(self, seq, status: str, also_results: bool = False):
        self._paint_cells(self._cells.get(seq), status, also_results)

    def _paint_cells(self, cells, status: str, also_results: bool = False):
        if not cells:
            return
        maps = [self._run_map()]
        if also_results:
            maps.append(self._results_map())
        for wmap in maps:
            if wmap is None:
                continue
            for rc in cells:
                try:
                    if rc in wmap.dies:
                        wmap.update_die(rc[0], rc[1], status)
                except Exception:
                    pass


    def _clear_shot_window(self):
        self._clear_items(self._shot_window_items)
        self._shot_window_items = []

    def _clear_selection_window(self):
        self._clear_items(self._sel_window_items)
        self._sel_window_items = []

    def _clear_items(self, items):
        wmap = self._run_map()
        for item in items:
            try:
                wmap.canvas.delete(item)
            except Exception:
                pass

    def _cell_pitch(self, wmap):
        px = py = None
        for (r, c) in wmap.dies:
            if px is None and (r, c + 1) in wmap.dies:
                a = wmap.canvas.coords(wmap.dies[(r, c)])
                b = wmap.canvas.coords(wmap.dies[(r, c + 1)])
                if a and b:
                    px = b[0] - a[0]
            if py is None and (r + 1, c) in wmap.dies:
                a = wmap.canvas.coords(wmap.dies[(r, c)])
                b = wmap.canvas.coords(wmap.dies[(r + 1, c)])
                if a and b:
                    py = b[1] - a[1]
            if px is not None and py is not None:
                break
        return px, py

    def _cell_box(self, wmap, rc, pitch):
        item = wmap.dies.get(rc)
        if item is not None:
            coords = wmap.canvas.coords(item)
            if len(coords) >= 4:
                return coords
        px, py = pitch
        if px is None or py is None:
            return None
        best = None
        for (r, c), it in wmap.dies.items():
            d = abs(r - rc[0]) + abs(c - rc[1])
            if best is None or d < best[0]:
                best = (d, (r, c), it)
        if best is None:
            return None
        _d, (br, bc), it = best
        base = wmap.canvas.coords(it)
        if len(base) < 4:
            return None
        ox, oy = (rc[1] - bc) * px, (rc[0] - br) * py
        return [base[0] + ox, base[1] + oy, base[2] + ox, base[3] + oy]

    def _block_box(self, wmap, cells):
        pitch = self._cell_pitch(wmap)
        boxes = [b for b in (self._cell_box(wmap, rc, pitch) for rc in cells) if b]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def update_shot_window(self):
        self._draw_shot_window()
        self.update_selection_window()

    _MOVE_TARGET_COLOR = "#1e3a8a"

    def update_selection_window(self):
        self._clear_selection_window()
        wmap = self._run_map()
        idx = self._selected
        if wmap is None or not self._cells or idx is None \
                or not (0 <= idx < len(self._touchdowns)):
            return
        seq = self._touchdowns[idx]["seq"]
        cells = self._cells.get(seq) or []
        box = self._block_box(wmap, cells) if cells else None
        if not box:
            return
        rect = wmap.canvas.create_rectangle(*box, outline=self._MOVE_TARGET_COLOR,
                                            width=2, dash=(4, 3))
        wmap.canvas.tag_raise(rect)
        self._sel_window_items.append(rect)
        if self._sel_rc is not None:
            cell = self._cell_box(wmap, self._sel_rc, self._cell_pitch(wmap))
            if cell:
                inner = wmap.canvas.create_rectangle(
                    *cell, outline=self._MOVE_TARGET_COLOR, width=3)
                wmap.canvas.tag_raise(inner)
                self._sel_window_items.append(inner)

    def _slots_anchored_at(self, rc) -> dict:
        if rc is None:
            return {}
        rows, cols = self.shot_layout()
        if rows * cols <= 1:
            return {}
        order = slot_names(rows, cols)
        grid = slot_grid(rows, cols)
        c1, r1 = grid[order[0]]
        row0, col0 = rc
        out = {}
        for name in order:
            c, r = grid[name]
            out[name] = (row0 + (r - r1), col0 + (c - c1))
        return out

    def _shot_window_cells(self, seq) -> list:
        slots = self._slots_anchored_at(self._anchor_rc.get(seq))
        if slots:
            return list(slots.values())
        return self._cells.get(seq) or []

    def _draw_shot_window(self):
        self._clear_shot_window()
        wmap = self._run_map()
        if wmap is None or self._index is None or not self._cells:
            self._shot_window_var.set("Shot window: chuck not set")
            return
        seq = self._touchdowns[self._index]["seq"]
        cells = self._shot_window_cells(seq)
        if not cells:
            self._shot_window_var.set("Shot window: not on the map")
            return
        box = self._block_box(wmap, cells)
        if not box:
            self._shot_window_var.set("Shot window: off the drawn map")
            return
        rect = wmap.canvas.create_rectangle(*box, outline="#2563eb", width=3)
        wmap.canvas.tag_raise(rect)
        self._shot_window_items.append(rect)

        t = self._touchdowns[self._index]
        drawn = sum(1 for rc in cells if rc in wmap.dies)
        self._shot_window_var.set(
            f"Shot window #{t['seq']} {len(cells)}-up "
            f"({drawn} die{'' if drawn == 1 else 's'} on the map):  "
            f"{format_quad(t['device_id'], *self.shot_layout())}")

    def mark_die_result(self, seq, quad_pos: str, passed: bool):
        rc = self._slots_anchored_at(self._anchor_rc.get(seq)).get(quad_pos)
        if rc is None:
            return
        key = (seq, quad_pos)
        was = self._die_results.get(key)
        self._die_results[key] = "PASS" if passed else "FAIL"
        self._paint_cells([rc], self._die_results[key], also_results=True)
        self._tally(was, self._die_results[key])
        try:
            self.controller.die_status[rc] = self._die_results[key]
        except Exception:
            pass

    def mark_result(self, seq, passed: bool):
        was = self._results.get(seq)
        self._results[seq] = "PASS" if passed else "FAIL"
        self._paint(seq, self._results[seq], also_results=True)
        self._tally(was, self._results[seq])
        try:
            for rc in self._cells.get(seq) or []:
                self.controller.die_status[rc] = self._results[seq]
        except Exception:
            pass

    def _tally(self, was: str, now: str):
        if was == now:
            return
        layout = self._main_layout
        add_pass = getattr(layout, "_exec_add_pass", None)
        add_fail = getattr(layout, "_exec_add_fail", None)
        if not (add_pass and add_fail):
            return
        try:
            if was in ("PASS", "FAIL"):
                var = (layout._exec_pass_var if was == "PASS"
                       else layout._exec_fail_var)
                var.set(max(0, var.get() - 1))
            (add_pass if now == "PASS" else add_fail)()
        except Exception as e:
            self._log(f"[MEASURE] Could not update pass/fail counts — "
                      f"{type(e).__name__}: {e}")

    def reset_results(self):
        for seq in list(self._results):
            self._paint(seq, "UNTESTED", also_results=True)
        self._results.clear()
        for (seq, quad), _v in list(self._die_results.items()):
            rc = self._slots_anchored_at(self._anchor_rc.get(seq)).get(quad)
            if rc is not None:
                self._paint_cells([rc], "UNTESTED", also_results=True)
        self._die_results.clear()
        if self._index is not None:
            self._last_seq = None
            self._highlight(self._index)

    def _restore_colours(self, seq):
        slots = self._slots_anchored_at(self._anchor_rc.get(seq))
        per_die = {quad: self._die_results.get((seq, quad)) for quad in slots}
        if any(per_die.values()):
            for quad, rc in slots.items():
                self._paint_cells([rc], per_die.get(quad) or "UNTESTED")
            return
        self._paint(seq, self._results.get(seq, "UNTESTED"))

    def _highlight(self, index):
        if self._last_seq is not None and self._last_seq != (
                self._touchdowns[index]["seq"] if index is not None else None):
            self._restore_colours(self._last_seq)
        if index is None:
            self._last_seq = None
            self.update_shot_window()
            return
        seq = self._touchdowns[index]["seq"]
        self._paint(seq, "PROBING")
        self._last_seq = seq
        self.update_shot_window()


    def _mark_on_wafer_map(self, touchdown):
        wafer = getattr(self._main_layout, "pma_wafer", None)
        if wafer is None:
            return
        try:
            if touchdown is None:
                wafer.clear_current_shot()
            else:
                label = touchdown["device_id"]
                wafer.mark_current_shot(touchdown["x"], touchdown["y"],
                                        f"#{touchdown['seq']}  {label}")
        except Exception as e:
            self._log(f"[RUN] wafer map marker skipped — {type(e).__name__}: {e}")

    def _push_xy_display(self, xy):
        var = getattr(self._main_layout, "_exec_xy_var", None)
        if var is None:
            return
        if not xy:
            var.set("X: ?\nY: ?")
            return
        var.set(f"X: {xy[0]:.0f} die\nY: {xy[1]:.0f} die")

    def _grid_index_map(self) -> dict:
        cached = getattr(self, "_grid_index_cache", None)
        if cached is not None:
            return cached
        cached = {}
        for i, t in enumerate(self._touchdowns or []):
            try:
                cached.setdefault(self._grid_xy(t), i)
            except Exception:
                continue
        self._grid_index_cache = cached
        return cached

    def _locate_real(self, real):
        if real is None or not self._anchored:
            return None, None
        ox, oy = self._origin_offset
        grid = (real[0] - ox, real[1] - oy)
        return self._grid_index_map().get(grid), grid

    def _sync_position(self):
        drv = self._prober()
        if not drv:
            self._log("[RUN] Prober not connected")
            return

        def _work():
            try:
                self._sync_position_work(drv)
            except Exception as e:
                self._ui(lambda: self._log(
                    f"[RUN] Sync ?P failed - {type(e).__name__}: {e}"))

        threading.Thread(target=_work, daemon=True).start()

    def _sync_position_work(self, drv):
        drv.recover()
        pos = drv.get_xy_position()
        status = drv.decode_status(drv.get_prober_status())
        note = ""
        real = parse_position(pos)
        moved_to = None
        if real is None:
            note = "  (could not parse ?P - position unknown)"
        elif self._anchored:
            idx, grid = self._locate_real(real)
            if idx is None:
                note = (f"  grid ({grid[0]},{grid[1]}) - no touchdown in this "
                        "recipe sits there; the chuck is on a die the recipe "
                        "does not visit. Still anchored.")
            elif idx == self._index:
                note = f"  on touchdown #{self._touchdowns[idx]['seq']}, as expected."
            else:
                was = (self._touchdowns[self._index]['seq']
                       if self._index is not None
                       and 0 <= self._index < len(self._touchdowns) else "?")
                moved_to = idx
                note = (f"  RE-LOCATED: chuck is on touchdown "
                        f"#{self._touchdowns[idx]['seq']} "
                        f"({self._touchdowns[idx]['device_id']}), not #{was} - "
                        "the software has followed it; no re-anchor needed.")
        else:
            note = "  (not anchored yet - Set Initial to place this on the map)"

        def _apply():
            if moved_to is not None:
                self._index = moved_to
                self._um_residual = [0.0, 0.0]
                self._mark_current()
                self._refresh_position()
                self._fill_table()
            self._status_var.set(f"?P={pos}  {status}")
            self._push_xy_display(real)
            self._log(f"[RUN] ?P={pos}  {status}{note}")
        self._ui(_apply)


    def _guard(self) -> bool:
        matches = getattr(self._main_layout, "_exec_wafer_map_matches_recipe", None)
        if matches is not None and not matches():
            return False
        if not self._anchored or self._index is None:
            messagebox.showwarning("Run", "Set where the chuck is first.")
            return False
        if not self._prober():
            self._log("[RUN] Prober not connected")
            return False
        if self._running:
            self._log("[RUN] Already running")
            return False
        return True


    def _probe_seqs(self):
        panel = getattr(self._main_layout, "recipe_panel", None)
        get_records = getattr(panel, "get_site_records", None)
        if not get_records:
            return None
        try:
            records = list(get_records())
        except Exception:
            return None
        if not records:
            return None
        resolve = getattr(self._main_layout, "_exec_resolve_site_cells", None)
        sites = resolve(records) if resolve else [
            (r["row"], r["col"]) for r in records]
        seqs = {self._seq_at_rc[rc] for rc in sites if rc in self._seq_at_rc}
        return seqs or None

    def _pma_order(self):
        index_of = {self._grid_xy(t): i for i, t in enumerate(self._touchdowns)}
        seen = set()
        order = []
        dropped = []
        for k in getattr(self, "_pma_order_keys", []):
            i = index_of.get(k)
            if i is None:
                continue
            if i in seen:
                dropped.append(i)
                continue
            seen.add(i)
            order.append(i)
        if dropped and dropped != getattr(self, "_pma_order_dropped", None):
            self._pma_order_dropped = dropped
            uniq = sorted({self._touchdowns[i]["seq"] for i in dropped})
            seqs = ", ".join(f"#{s}" for s in uniq[:8])
            more = f" (+{len(uniq) - 8} more)" if len(uniq) > 8 else ""
            self._log(
                f"[RUN] {len(dropped)} entr(ies) in the run order resolved to a "
                f"grid position already taken, at {len(uniq)} position(s): "
                f"{seqs}{more}. This is the ambiguous-shot-corner case "
                "_builder_grid_xy warns about — those dies will NOT be probed.")
        return order

    def _enabled_indices(self):
        order = self._pma_order() or list(range(len(self._touchdowns)))
        seqs = self._probe_seqs()
        if seqs is None:
            return order
        chosen = [i for i in order if self._touchdowns[i]["seq"] in seqs]
        seen = set(chosen)
        chosen.extend(i for i, t in enumerate(self._touchdowns)
                      if t["seq"] in seqs and i not in seen)
        return chosen

    def _next_enabled_index(self, after):
        order = self._enabled_indices()
        if not order:
            return None
        if after is None:
            return order[0]
        try:
            pos = order.index(after)
        except ValueError:
            return order[0]
        return order[pos + 1] if pos + 1 < len(order) else None

    def _step_once(self):
        if self._guard():
            self._start(1)

    def _run_all(self):
        rp = getattr(self._main_layout, "recipe_panel", None)
        if rp is not None and getattr(rp, "is_minor_moves", None) and rp.is_minor_moves():
            self._run_minor_moves()
            return
        if not self._guard():
            return
        enabled = self._enabled_indices()
        if not enabled:
            self._log("[RUN] Run: this recipe has no touchdowns to probe.")
            return
        total = len(self._touchdowns)

        restart = not getattr(self, "_paused", False)
        if not restart:
            try:
                ahead = enabled[enabled.index(self._index) + 1:]
            except ValueError:
                ahead = enabled
            restart = not ahead

        if restart:
            remaining = len(enabled)
            first = enabled[0]
            if self._index != first:
                self._log("[RUN] The chuck is not on the first touchdown of "
                          "this run — it will move back there before probing "
                          "starts.")
        else:
            remaining = len(ahead)
        if len(enabled) != total:
            self._log(f"[RUN] The loaded recipe restricts this run to "
                      f"{len(enabled)} of the {total} dies on the map; the "
                      "rest are skipped.")

        if not messagebox.askokcancel("Run", f"Probe {remaining} Dies?"):
            return
        if restart:
            self._needs_restart = True
        self._start(remaining)


    def _run_minor_moves(self):
        matches = getattr(self._main_layout, "_exec_wafer_map_matches_recipe", None)
        if matches is not None and not matches():
            return
        if self._running:
            self._log("[RUN] Already running")
            return
        drv = self._prober()
        if drv is None:
            self._log("[RUN] Prober not connected")
            return
        rp = self._main_layout.recipe_panel
        origin = rp.get_shot_origin()
        if origin is None:
            self._log("[RUN] Minor Moves: no shot origin set for this recipe "
                      "— press Set Shot Origin on the Recipe tab (with the "
                      "chuck on shot R0C0's die R0C0), then Run again.")
            return
        gen = getattr(self._main_layout, "recipe_gen", None)
        if gen is None:
            self._log("[RUN] Minor Moves: the Wafer Builder tab is not available.")
            return
        shot_rows, shot_cols = gen._shot_dims()
        shot_cells = dict(gen._shot_cells)
        shots = sorted({(d["row"], d["col"]) for d in gen.shots_as_die_list()})
        if not shots:
            self._log("[RUN] Minor Moves: no shots on the Wafer Builder map.")
            return
        steps = self._main_layout.recipe_panel.get_steps()
        if not steps:
            self._log("[RUN] Minor Moves: the loaded recipe has no steps.")
            return
        if not messagebox.askokcancel(
                "Run (Minor Moves)", f"Probe {len(shots)} Shots?"):
            return
        self._running = True
        try:
            self._main_layout._exec_set_running_buttons(True)
        except Exception:
            pass
        self._abort = False
        self._set_run_state("RUNNING (Minor Moves)", "#2563eb")
        self._log(f"[RUN] Run (Minor Moves) — {len(shots)} shot(s).")
        threading.Thread(
            target=self._minor_move_thread,
            args=(shots, origin, shot_rows, shot_cols, shot_cells),
            daemon=True).start()

    def _minor_move_thread(self, shots: list, origin: tuple,
                           shot_rows: int, shot_cols: int, shot_cells: dict):
        drv = self._prober()
        origin_x, origin_y = origin
        layout = self._main_layout
        error_msg = None

        die1_rc = shot_die_rc(shot_cells, shot_rows, shot_cols, 1)
        if die1_rc is None:
            self._ui(lambda: self._log(
                "[RUN] Minor Moves: this shot has no die #1 - treating "
                "grid cell (0,0) as the reference instead."))
            die1_rc = (0, 0)
        r1, c1 = die1_rc

        class _Stop(Exception):
            pass

        def goto_shot_die(shot_row, shot_col, die_num):
            rc = shot_die_rc(shot_cells, shot_rows, shot_cols, die_num)
            if rc is None:
                raise RuntimeError(f"die #{die_num} is not on shot "
                                   f"R{shot_row}C{shot_col}")
            r, c = rc
            die_x = origin_x + shot_col * shot_cols + (c - c1)
            die_y = origin_y + shot_row * shot_rows + (r - r1)
            label = f"shot R{shot_row}C{shot_col} die #{die_num} (X{die_x} Y{die_y})"
            self._ui(lambda lab=label: self._status_var.set(f"moving to {lab}"))
            drv.z_down()
            self._ui(lambda lab=label: self._log(f"[RUN] >> goto_die X={die_x} Y={die_y}"))
            drv.goto_die(die_x, die_y)
            drv.z_up()

        try:
            for shot_row, shot_col in shots:
                if self._abort:
                    break
                self._ui(lambda sr=shot_row, sc=shot_col: self._log(
                    f"[RUN] Shot R{sr}C{sc}: landing on die #1"))
                try:
                    goto_shot_die(shot_row, shot_col, 1)
                except _Stop:
                    break
                layout._exec_move_fn = (
                    lambda die_num, sr=shot_row, sc=shot_col: goto_shot_die(sr, sc, die_num))
                try:
                    ok = bool(layout._exec_run_steps_once())
                finally:
                    layout._exec_move_fn = None
                drv.z_down()
                self._ui(lambda p=ok, sr=shot_row, sc=shot_col: self._log(
                    f"[RESULTS] {'PASS' if p else 'FAIL'}  shot R{sr}C{sc}"))
        except Exception as e:
            error_msg = str(e)
            self._ui(lambda: self._log(f"[RUN] ERROR: {e}"))
        finally:
            layout._exec_move_fn = None
            self._running = False
            self._ui(lambda: self._main_layout._exec_set_running_buttons(False))
            try:
                self._make_safe(drv)
            except Exception:
                pass
            if error_msg:
                self._set_run_state(f"ERROR: {error_msg[:60]}", "#dc2626")
            else:
                self._set_run_state("FINISHED (Minor Moves)", "#16a34a")

    def _pause(self):
        if not self._running:
            return
        self._paused = True
        self._status_var.set("pausing after this touchdown…")
        self._set_run_state("PAUSING…", "#b45309")

    def _stop(self):
        self._abort = True
        self._paused = False
        self._status_var.set("stopping…")
        self._set_run_state("STOPPING…", "#dc2626")

    def _set_run_state(self, text: str, color: str):
        layout = self._main_layout
        setter = getattr(layout, "_exec_set_state", None)
        if setter is None:
            return
        try:
            self._ui(lambda: setter(text, color))
        except Exception:
            pass

    def _make_safe(self, drv):
        layout = self._main_layout
        opener = getattr(layout, "_exec_open_all_channels", None)
        if opener is not None:
            try:
                opener()
            except Exception as e:
                self._ui(lambda: self._log(
                    f"[RUN] Could not open the switch channels — "
                    f"{type(e).__name__}: {e}"))
        if drv is not None:
            try:
                drv.z_down()
                self._ui(lambda: self._log("[RUN] Chuck separated (Z down)."))
            except Exception as e:
                self._ui(lambda: self._log(
                    f"[RUN] Could not separate the chuck — "
                    f"{type(e).__name__}: {e}  Check Z before moving."))

    def _publish_total_dies(self) -> int:
        total = 0
        for i in self._enabled_indices():
            seq = self._touchdowns[i]["seq"]
            slots = self._slots_anchored_at(self._anchor_rc.get(seq))
            if slots:
                total += len(slots)
            else:
                total += 1
        try:
            self._main_layout._exec_total_dies = total
            self._main_layout._exec_push_stats()
        except Exception as e:
            self._log(f"[RUN] Could not publish the die total — "
                      f"{type(e).__name__}: {e}")
        return total

    def _start(self, count: int):
        self._running = True
        try:
            self._main_layout._exec_set_running_buttons(True)
        except Exception:
            pass
        self._abort = False
        self._paused = False
        try:
            self._main_layout._exec_aborted = False
        except Exception:
            pass
        self._publish_total_dies()
        self._set_run_state("RUNNING", "#2563eb")
        drv = self._prober()
        cap = getattr(drv, "max_die_step", 5)

        def _work():
            done = 0
            err = None
            try:
                for _ in range(count):
                    if self._abort or self._paused:
                        break
                    if not self._move_next(drv, cap):
                        break
                    if not self._measure_here(drv):
                        break
                    done += 1
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:80]}"
                self._ui(lambda: self._log(f"[RUN] run aborted — {err}"))
            finally:
                self._make_safe(drv)
                self._running = False
                self._ui(lambda: self._main_layout._exec_set_running_buttons(False))

            stopped, paused = self._abort, self._paused
            if stopped:
                word, state, colour = "stopped", "STOPPED", "#dc2626"
            elif paused:
                word, state, colour = "paused", "PAUSED", "#b45309"
            elif err:
                word, state, colour = "error", "ERROR", "#dc2626"
            else:
                word, state, colour = "finished", "FINISHED", "#16a34a"
            self._set_run_state(state, colour)

            def _settle():
                self._status_var.set(f"{word} — {done} touchdown(s)")
                if not paused:
                    self._needs_restart = True
                self._mark_current()
                self._refresh_position()
            self._ui(_settle)

        threading.Thread(target=_work, daemon=True).start()

    def _ensure_contact(self, drv) -> bool:
        if drv is None:
            return True
        try:
            status = (drv.get_prober_status() or "").upper()
        except Exception as e:
            self._ui(lambda: self._log(
                f"[RUN] Could not read the Z state ({type(e).__name__}: {e}) — "
                "stopping rather than measuring blind."))
            return False
        if "ZU" in status:
            return True
        try:
            drv.z_up()
            return True
        except Exception as e:
            self._ui(lambda: self._log(
                f"[RUN] Chuck is not in contact ({status or 'no status'}) and ZU "
                f"failed — {e}  Run stopped; nothing was measured."))
            return False

    def _measure_here(self, drv) -> bool:
        layout = self._main_layout
        run_steps = getattr(layout, "_exec_run_steps_once", None)
        if run_steps is None:
            self._ui(lambda: self._log(
                "[MEASURE] No measurement engine on this layout — run stopped."))
            return False
        if not self._ensure_contact(drv):
            return False
        t = self._touchdowns[self._index]
        seq, dev = t["seq"], t["device_id"]
        rc = self._anchor_rc.get(seq)
        if rc is not None:
            layout._exec_current_rc = rc
        layout._exec_die_id_override = dev
        slots = self._slots_anchored_at(rc)
        order = slot_names(*self.shot_layout())
        wm = self._run_map()
        layout._exec_die_ids_by_slot = [
            (wm.die_ids.get(slots[q], "") if wm is not None and q in slots else "")
            for q in order]
        layout._exec_die_rc_by_slot = [slots.get(q) for q in order]
        self._ui(lambda: layout._exec_die_var.set(f"Die: {dev}"))
        try:
            ok = bool(run_steps())
        except Exception as e:
            self._ui(lambda: self._log(
                f"[MEASURE] Measurement error at #{seq} {dev} — "
                f"{type(e).__name__}: {e}"))
            return False
        slot_verdicts = dict(getattr(layout, "_exec_slot_verdicts", None) or {})
        if slot_verdicts:
            ids = list(getattr(layout, "_exec_die_ids_by_slot", None) or [])

            def _mark():
                for slot, passed in sorted(slot_verdicts.items()):
                    order = slot_names(*self.shot_layout())
                    quad = order[slot - 1] if 1 <= slot <= len(order) else None
                    if quad is None:
                        continue
                    die = ids[slot - 1] if slot - 1 < len(ids) else ""
                    self.mark_die_result(seq, quad, passed)
                    self._log(f"[MEASURE] #{seq} {quad} {die or '(unnamed)'}: "
                              f"{'PASS' if passed else 'FAIL'}")
            self._ui(_mark)
        else:
            self._ui(lambda: (self.mark_result(seq, ok),
                              self._log(f"[MEASURE] #{seq} {dev}: "
                                        f"{'PASS' if ok else 'FAIL'}")))
        return True

    def _read_position(self, drv):
        pos = None
        try:
            pos = parse_position(drv.get_xy_position())
        except Exception:
            try:
                drv.recover()
                pos = parse_position(drv.get_xy_position())
            except Exception:
                pos = None
        self._ui(lambda p=pos: self._push_xy_display(p))
        return pos

    def _move_next(self, drv, cap: int) -> bool:
        if getattr(self, "_needs_restart", False):
            self._needs_restart = False
            order = self._enabled_indices()
            nxt = order[0] if order else None
        else:
            nxt = self._next_enabled_index(self._index)
        if nxt is None:
            self._ui(lambda: self._log(
                "[RUN] No further touchdowns in this recipe's list."))
            return False
        return self._move_to_index(drv, cap, nxt)

    def _move_to_index(self, drv, cap: int, target: int) -> bool:
        if not 0 <= target < len(self._touchdowns):
            return False
        i = self._index
        cur, nxt = self._touchdowns[i], self._touchdowns[target]
        cx, cy = self._grid_xy(cur)
        nx, ny = self._grid_xy(nxt)
        dx, dy = nx - cx, ny - cy

        real = self._read_position(drv)
        if real is None:
            self._ui(lambda: self._log(
                f"[RUN] STOPPED at #{nxt['seq']}: could not read ?P to verify "
                "the chuck's real position before moving, even after a "
                "recover() - re-anchor once the link is back rather than "
                "assuming."))
            return False
        expected = self._expected_position(cur)
        if real != expected:
            here, grid = self._locate_real(real)
            self._um_residual = [0.0, 0.0]
            if here is not None:
                self._index = here
                cur = self._touchdowns[here]
                where = f"touchdown #{cur['seq']} ({cur['device_id']})"
            else:
                where = (f"grid ({grid[0]},{grid[1]}), which no touchdown in "
                         "this recipe covers")
            self._ui(lambda r=real, w=where, c=expected, s=nxt['seq']: self._log(
                f"[RUN] Re-located before moving to #{s}: chuck is really at "
                f"X{r[0]}Y{r[1]} — {w} — not X{c[0]}Y{c[1]} as assumed. "
                "Stepping from where it actually is; no re-anchor needed."))
            self._ui(self._fill_table)
            tx, ty = self._expected_position(nxt)
            dx, dy = tx - real[0], ty - real[1]
            if (dx, dy) == (0, 0):
                self._index = target
                self._ui(lambda: (self._mark_current(), self._refresh_position()))
                return True
            if here is None and self._motion_var.get() == MOTION_UM:
                self._ui(lambda: self._log(
                    "[RUN] STOPPED: in µm (MM) mode the step is the recipe's "
                    "own micron delta between two touchdowns, and the chuck is "
                    "not on one. Switch to die steps (MD), or re-anchor."))
                return False
        elif (dx, dy) == (0, 0):
            self._index = target
            self._ui(lambda: (self._mark_current(), self._refresh_position()))
            return True

        if self._motion_var.get() == MOTION_UM:
            return self._move_um(drv, cur, nxt, target, (nx, ny), before=real)

        before = real
        self._ui(lambda: self._status_var.set(
            f"#{nxt['seq']}  MD {dx:+d},{dy:+d}  {nxt['device_id']}"))

        for hop_x, hop_y in chunk_step(dx, dy, cap):
            if self._abort:
                return False
            try:
                drv.move_relative_die(hop_x, hop_y)
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e).splitlines()[0][:70]}"
                self._ui(lambda: self._log(
                    f"[RUN] #{nxt['seq']} MD {hop_x:+d},{hop_y:+d} FAILED — {msg}"))
                return False

        after = self._read_position(drv)
        if before is None or after is None:
            self._ui(lambda: self._log(
                f"[RUN] STOPPED at #{nxt['seq']}: could not read ?P to confirm the "
                "move, even after a recover(). The move itself may well have "
                "landed — re-anchor once the link is back rather than assuming."))
            return False
        got = (after[0] - before[0], after[1] - before[1])
        if got != (dx, dy):
            self._ui(lambda: self._log(
                f"[RUN] STOPPED at #{nxt['seq']}: commanded ({dx:+d},{dy:+d}) "
                f"but ?P moved ({got[0]:+d},{got[1]:+d}) — "
                f"{before} -> {after}. Map and machine have diverged."))
            return False

        self._index = target
        self._ui(lambda: (self._mark_current(), self._refresh_position()))
        self._ui(lambda: self._log(
            f"[RUN] #{nxt['seq']} MD {dx:+d},{dy:+d} -> grid ({nx},{ny})  "
            f"{nxt['device_id']}"))
        return True

    def _move_um(self, drv, cur, nxt, target, grid_xy, before) -> bool:
        want_x = (nxt["x"] - cur["x"]) + self._um_residual[0]
        want_y = (nxt["y"] - cur["y"]) + self._um_residual[1]
        dx_um = int(round(want_x))
        dy_um = int(round(want_y))
        self._ui(lambda: self._status_var.set(
            f"#{nxt['seq']}  MM {dx_um:+d},{dy_um:+d} um  {nxt['device_id']}"))

        for hop_x, hop_y in chunk_step(dx_um, dy_um, _MAX_UM_HOP):
            if self._abort:
                return False
            try:
                drv.move_relative_um(hop_x, hop_y)
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e).splitlines()[0][:70]}"
                self._ui(lambda: self._log(
                    f"[RUN] #{nxt['seq']} MM {hop_x:+d},{hop_y:+d} um FAILED — {msg}"))
                return False

        after = self._read_position(drv)
        note = ""
        if before is not None and after is not None:
            got = (after[0] - before[0], after[1] - before[1])
            dxq, dyq = grid_xy[0] - self._grid_xy(cur)[0], grid_xy[1] - self._grid_xy(cur)[1]
            if got != (dxq, dyq):
                note = (f"   [?P moved ({got[0]:+d},{got[1]:+d}) dies, recipe step "
                        f"is ({dxq:+d},{dyq:+d}) — the prober's die size differs "
                        f"from the recipe's, which does not affect a micron move]")

        unit = float(getattr(drv, "MM_UNIT_UM", 1.0)) or 1.0
        self._um_residual = [want_x - round(dx_um / unit) * unit,
                             want_y - round(dy_um / unit) * unit]

        self._index = target
        self._ui(lambda: (self._mark_current(), self._refresh_position()))
        self._ui(lambda: self._log(
            f"[RUN] #{nxt['seq']} MM {dx_um:+d},{dy_um:+d} um -> grid "
            f"({grid_xy[0]},{grid_xy[1]})  {nxt['device_id']}{note}"))
        return True

    def _on_motion_mode(self):
        if hasattr(self, "_tree"):
            self._fill_table()


    def toggle_move_armed(self):
        if not self._move_armed:
            self._move_armed = True
            wmap = self._run_map()
            if wmap is not None:
                self._prev_click_handler = wmap._click_handler
                self._prev_picking_enabled = wmap._picking_enabled
                self._click_state_saved = True
                wmap._picking_enabled = False
                wmap.set_click_handler(self._on_map_click)
            self._select(None)
            return
        if self._selected is None:
            self._disarm_move()
            return
        self._goto_selected()

    def _disarm_move(self):
        wmap = self._run_map()
        if wmap is not None and getattr(self, "_click_state_saved", False):
            wmap.set_click_handler(self._prev_click_handler)
            wmap._picking_enabled = self._prev_picking_enabled
        self._click_state_saved = False
        self._move_armed = False
        self._select(None)

    def _update_move_button(self):
        btn = self._goto_btn
        if btn is None:
            return
        if not self._move_armed:
            btn.config(text="→ Move to Selected")
            return
        if self._selected is None:
            btn.config(text="✕ Cancel Move")
            return
        t = self._touchdowns[self._selected]
        here = "" if self._index is None else \
            f"  ({self._selected - self._index:+d} from here)"
        btn.config(text=f"→ Move to #{t['seq']}{here}")

    def _on_map_click(self, row: int, col: int):
        if not self._move_armed:
            return
        rc = (row, col)
        if rc == self._sel_rc:
            self._select(None)
            return
        seq = self._seq_at_rc.get(rc)
        if seq is None:
            return
        idx = next((i for i, t in enumerate(self._touchdowns) if t["seq"] == seq), None)
        self._select(idx, clicked_rc=rc)
        if idx is not None:
            self._tree.selection_set(str(idx))
            self._tree.see(str(idx))

    def _on_table_click(self, _event=None):
        if not self._move_armed:
            return
        sel = self._tree.selection()
        idx = int(sel[0]) if sel else None
        if idx == self._selected:
            return
        rc = self._rc.get(self._touchdowns[idx]["seq"]) if idx is not None else None
        self._select(idx, clicked_rc=rc)

    def _select(self, index, clicked_rc=None):
        self._selected = index
        self._sel_rc = clicked_rc
        self.update_selection_window()
        self._update_move_button()

    def _goto_selected(self):
        if self._selected is None:
            return
        if not self._guard():
            return
        target = self._selected
        t = self._touchdowns[target]
        cur = self._touchdowns[self._index]
        cx, cy = self._grid_xy(cur)
        nx, ny = self._grid_xy(t)
        if self._motion_var.get() == MOTION_UM:
            step = (f"MM {t['x'] - cur['x']:+.0f},{t['y'] - cur['y']:+.0f} µm")
        else:
            step = f"MD {nx - cx:+d},{ny - cy:+d} die steps"
        if not messagebox.askokcancel(
                "Move", f"Move from #{cur['seq']} to #{t['seq']}?\n\n"
                        f"{step}\n{t['device_id']}"):
            return
        drv = self._prober()
        cap = getattr(drv, "max_die_step", 5)
        self._running = True
        self._abort = False
        self._disarm_move()

        def _work():
            try:
                ok = self._move_to_index(drv, cap, target)
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:80]}"
                self._ui(lambda: self._log(f"[RUN] move failed — {err}"))
                ok = False
            finally:
                self._running = False
            self._ui(lambda: self._status_var.set("moved" if ok else "move stopped"))

        threading.Thread(target=_work, daemon=True).start()

    def _step_back(self):
        if not self._guard():
            return
        if self._index <= 0:
            self._log("[RUN] Already at the first touchdown")
            return
        target = self._index - 1
        drv = self._prober()
        cap = getattr(drv, "max_die_step", 5)
        self._running = True
        self._abort = False

        def _work():
            try:
                self._move_to_index(drv, cap, target)
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:80]}"
                self._ui(lambda: self._log(f"[RUN] back failed — {err}"))
            finally:
                self._running = False
            self._ui(lambda: self._status_var.set("idle"))

        threading.Thread(target=_work, daemon=True).start()
