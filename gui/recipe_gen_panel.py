from __future__ import annotations
from map_nav import bind_middle_pan_mpl

import csv
import json
import os
from collections import Counter
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from typing import Any, Dict, List, Optional, Tuple

from electroglas_pma import fmt_num, shot_geometry, slot_names, slot_grid, \
    parse_pma_file, load_touchdowns
from pma_wafer_panel import ATA_CSV_MAP_FILENAME, read_moves_grid
from wafer_map_view import WAFER_MAP_SOURCES

try:
    import xlrd
    _XLRD = True
    _XLRD_ERR = ""
except ImportError as _e:
    _XLRD = False
    _XLRD_ERR = f"{type(_e).__name__}: {_e}"

try:
    import matplotlib
    try:
        matplotlib.use("TkAgg")
    except Exception:
        pass
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle
    from matplotlib.collections import PatchCollection
    from matplotlib.colors import to_rgba
    _MPL = True
except ImportError:
    _MPL = False


_COLOR_BLANK = "#93c5fd"
_COLOR_HAS_ID = "#22c55e"
_COLOR_SKIP = "#9ca3af"
_COLOR_ALIGN = "#ef4444"
_COLOR_ABSENT = "#e2e8f0"
_COLOR_SELECTED = "#f59e0b"

_ALIGN_KEYWORDS = {"target", "pcm", "align", "alignment", "ref", "reference"}


def _die_id_shape(text: str) -> str:
    out = []
    for ch in text:
        marker = "@" if ch.isalpha() else "#" if ch.isdigit() else ch
        if out and out[-1] == marker and marker in "@#":
            continue
        out.append(marker)
    return "".join(out)


def _find_alignment_ids(die_ids) -> set:
    texts = [d.strip() for d in die_ids if d and d.strip()]
    outliers = {d for d in texts
               if any(d.lower().startswith(kw) for kw in _ALIGN_KEYWORDS)}
    if len(texts) >= 4:
        shapes = Counter(_die_id_shape(d) for d in texts)
        majority_shape, _count = shapes.most_common(1)[0]
        outliers |= {d for d in texts if _die_id_shape(d) != majority_shape}
    return outliers


def _to_float(text, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _to_int(text, default: int = 0) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def present_slots(cells: Dict[tuple, dict], rows: int, cols: int) -> Dict[tuple, int]:
    present = [(r, c) for c in range(cols) for r in range(rows)
              if cells.get((r, c), {}).get("present")]
    out, used = {}, set()
    for rc in present:
        o = cells.get(rc, {}).get("order")
        if isinstance(o, int) and o >= 1 and o not in used:
            out[rc] = o
            used.add(o)
    n = 0
    for rc in present:
        if rc in out:
            continue
        n += 1
        while n in used:
            n += 1
        out[rc] = n
        used.add(n)
    return out


def shot_die_rc(cells: Dict[tuple, dict], rows: int, cols: int,
                die_num: int) -> Optional[tuple]:
    for rc, num in present_slots(cells, rows, cols).items():
        if num == die_num:
            return rc
    return None


def _resize_cells(cells: Dict[tuple, dict], new_rows: int, new_cols: int,
                  default: dict) -> Dict[tuple, dict]:
    out = {}
    for r in range(new_rows):
        for c in range(new_cols):
            out[(r, c)] = dict(cells.get((r, c), default))
    return out


class RecipeGenPanel(ttk.Frame):

    def __init__(self, parent, controller, main_layout, system: str = "electroglas"):
        super().__init__(parent)
        self.controller = controller
        self._main_layout = main_layout
        self._system = system

        self.map_name_var = tk.StringVar(value="")

        self._shot_rows_var = tk.StringVar(value="1")
        self._shot_cols_var = tk.StringVar(value="1")
        self._die_pitch_x_var = tk.StringVar(value="1000")
        self._die_pitch_y_var = tk.StringVar(value="1000")
        self._shot_pitch_x_var = tk.StringVar(value="")
        self._shot_pitch_y_var = tk.StringVar(value="")
        self._shot_cells: Dict[tuple, dict] = {(0, 0): {"present": True}}
        self._shot_selected: Optional[tuple] = None
        self._shot_status_var = tk.StringVar(value="")

        self._shotmap_rows_var = tk.StringVar(value="4")
        self._shotmap_cols_var = tk.StringVar(value="4")
        self._shotmap_cells: Dict[tuple, bool] = {
            (r, c): True for r in range(4) for c in range(4)}
        self._shotmap_status_var = tk.StringVar(value="")

        self._die_status: Dict[tuple, dict] = {}
        self._diemap_mode_var = tk.StringVar(value="id")
        self._diemap_status_var = tk.StringVar(value="")
        self._diemap_label_min_px_var = main_layout._exec_label_min_px_var
        self._diemap_label_min_px_var.trace_add("write", self._on_diemap_label_min_px_change)
        self._die_editor: Optional[tk.Entry] = None
        self._die_editor_key: Optional[tuple] = None
        self._die_boxes: List[dict] = []
        self._die_id_labels: list = []
        self._selected_die_patch = None

        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_toolbar()
        self._sub_nb = ttk.Notebook(self)
        self._sub_nb.grid(row=1, column=0, sticky="nsew")

        shot_tab = ttk.Frame(self._sub_nb)
        self._sub_nb.add(shot_tab, text="Shot")
        self._build_shot_tab(shot_tab)

        shotmap_tab = ttk.Frame(self._sub_nb)
        self._sub_nb.add(shotmap_tab, text="Shot Map")
        self._build_shotmap_tab(shotmap_tab)

        diemap_tab = ttk.Frame(self._sub_nb)
        self._sub_nb.add(diemap_tab, text="Die Map")
        self._build_diemap_tab(diemap_tab)

        self._shot_tab_widget = shot_tab
        self._shotmap_tab_widget = shotmap_tab
        self._diemap_tab_widget = diemap_tab

        self._sub_nb.bind("<<NotebookTabChanged>>", self._on_subtab_changed)

        self._hidden_pma_wafer_parent = ttk.Frame(self)

    def _log(self, msg: str):
        try:
            self.controller.log(msg)
        except Exception:
            pass

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=6)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="Map:").pack(side="left")
        self._map_picker_cb = ttk.Combobox(
            bar, textvariable=self.map_name_var, state="readonly", width=16,
            postcommand=self._refresh_map_picker)
        self._map_picker_cb.pack(side="left", padx=(4, 8))
        self._map_picker_cb.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._load_named_map(self.map_name_var.get()))
        ttk.Button(bar, text="New", command=self._new_named_map).pack(
            side="left", padx=1)
        ttk.Button(bar, text="Rename", command=self._rename_named_map).pack(
            side="left", padx=1)
        ttk.Button(bar, text="Delete", command=self._delete_named_map).pack(
            side="left", padx=1)
        ttk.Button(bar, text="Set Default", command=self._set_default_map).pack(
            side="left", padx=(6, 12))
        ttk.Button(bar, text="Import CSV…", command=self._import_csv).pack(
            side="left", padx=(0, 6))
        ttk.Button(bar, text="Load PMA…", command=self._import_pma).pack(
            side="left", padx=(0, 6))
        ttk.Button(bar, text="Load Recipe Gen (.xls)…",
                  command=self._import_recipe_gen_xls).pack(side="left", padx=(0, 6))

    _CELL = 78
    _GAP = 6

    def _build_shot_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        top = ttk.Frame(tab, padding=6)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="Shot size:").pack(side="left")
        ttk.Entry(top, textvariable=self._shot_rows_var, width=3).pack(side="left")
        ttk.Label(top, text="x").pack(side="left", padx=1)
        ttk.Entry(top, textvariable=self._shot_cols_var, width=3).pack(side="left")
        ttk.Button(top, text="Apply", width=7, command=self._shot_apply_size).pack(
            side="left", padx=(4, 16))
        ttk.Label(top, text="Die pitch X/Y (µm):").pack(side="left")
        ttk.Entry(top, textvariable=self._die_pitch_x_var, width=8).pack(
            side="left", padx=(4, 2))
        ttk.Entry(top, textvariable=self._die_pitch_y_var, width=8).pack(
            side="left", padx=(2, 16))
        ttk.Label(top, text="Shot pitch X/Y:").pack(side="left")
        ttk.Entry(top, textvariable=self._shot_pitch_x_var, width=8).pack(
            side="left", padx=(4, 2))
        ttk.Entry(top, textvariable=self._shot_pitch_y_var, width=8).pack(
            side="left", padx=(2, 4))
        ttk.Button(top, text="Apply", width=7, command=self._draw_shot).pack(
            side="left", padx=(4, 0))

        body = ttk.Frame(tab)
        body.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self._shot_canvas = tk.Canvas(body, background="#f8fafc",
                                      highlightthickness=1,
                                      highlightbackground="#cbd5e1")
        self._shot_canvas.grid(row=0, column=0, sticky="nsew")
        self._shot_canvas.bind("<Button-1>", self._on_shot_click)
        self._shot_canvas.bind("<Shift-Button-1>", self._on_shot_shift_click)
        self._shot_canvas.bind("<Button-3>", self._on_shot_right_click)
        self._shot_canvas.bind("<Configure>", lambda _e: self._draw_shot())

        side = ttk.Frame(body, padding=(8, 0, 0, 0))
        side.grid(row=0, column=1, sticky="ns")
        ttk.Label(side, text="Die order in the shot", font=("Segoe UI", 9, "bold")
                 ).pack(anchor="w")
        ttk.Label(side, text="Right click to remove, Left click to add back.\n"
                            "Left click to edit die ordering, Shift Click to "
                            "set a global die id\n"
                            "Use die order number in recipe tab to sync "
                            "measurements \nTo correct dies in shots, refer "
                            "to manual for more details\n\n"
                            "Continue to shot map to create overall shape, "
                            "then \nContinue to die map to assign die ids,\n"
                            "Then overlay to map this map to accretech and "
                            "save to run tab",
                 foreground="#6b7280",
                 wraplength=220, justify="left").pack(anchor="w", pady=(2, 6))
        ttk.Label(side, textvariable=self._shot_status_var, foreground="#374151",
                 wraplength=220, justify="left").pack(anchor="w", pady=(6, 0))

        self._draw_shot()

    def _shot_dims(self) -> tuple:
        return (max(1, _to_int(self._shot_rows_var.get(), 1)),
               max(1, _to_int(self._shot_cols_var.get(), 1)))

    def _shot_apply_size(self):
        rows, cols = self._shot_dims()
        self._shot_cells = _resize_cells(self._shot_cells, rows, cols,
                                         {"present": True})
        self._draw_shot()

    def _shot_cell_rects(self) -> Dict[tuple, tuple]:
        rows, cols = self._shot_dims()
        w = int(self._shot_canvas.winfo_width() or 1)
        h = int(self._shot_canvas.winfo_height() or 1)
        avail_w = (w - 20 - self._GAP * cols) // max(1, cols)
        avail_h = (h - 20 - self._GAP * rows) // max(1, rows)
        cell = max(4, min(self._CELL, avail_w, avail_h))
        span_w = cols * cell + (cols - 1) * self._GAP
        span_h = rows * cell + (rows - 1) * self._GAP
        x0 = max(8, (w - span_w) // 2)
        y0 = max(8, (h - span_h) // 2)
        out = {}
        for r in range(rows):
            for c in range(cols):
                left = x0 + c * (cell + self._GAP)
                top = y0 + r * (cell + self._GAP)
                out[(r, c)] = (left, top, left + cell, top + cell)
        return out

    def _draw_shot(self):
        cv = getattr(self, "_shot_canvas", None)
        if cv is None:
            return
        cv.delete("all")
        rows, cols = self._shot_dims()
        slots = present_slots(self._shot_cells, rows, cols)
        for (r, c), (x0, y0, x1, y1) in self._shot_cell_rects().items():
            present = self._shot_cells.get((r, c), {}).get("present")
            slot = slots.get((r, c))
            fill, outline = (_COLOR_BLANK, "#1d4ed8") if present else (_COLOR_ABSENT, "#94a3b8")
            width = 1.5
            if (r, c) == self._shot_selected:
                outline, width = "#b45309", 3
            cv.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline,
                               width=width)
            if present:
                die_id = self._shot_cells.get((r, c), {}).get("die_id", "")
                label = f"die {slot}\n{die_id}" if die_id else f"die {slot}"
                cv.create_text((x0 + x1) / 2, (y0 + y1) / 2,
                              text=label, font=("Segoe UI", 9, "bold"),
                              fill="#0f172a")
        n_dies = len(slots)
        n_named = sum(1 for cell in self._shot_cells.values()
                     if cell.get("present") and cell.get("die_id"))
        self._shot_status_var.set(
            f"{n_dies} die(s) in this shot."
            + (f"  {n_named} named (applied to every shot on the wafer)."
               if n_named else ""))

    def _on_shot_click(self, event):
        for (r, c), (x0, y0, x1, y1) in self._shot_cell_rects().items():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self._shot_selected = (r, c)
                if not self._shot_cells.get((r, c), {}).get("present"):
                    self._shot_cells[(r, c)] = {"present": True}
                    self._draw_shot()
                    return
                self._draw_shot()
                self._set_shot_order_dialog(r, c)
                return

    def _on_shot_shift_click(self, event):
        for (r, c), (x0, y0, x1, y1) in self._shot_cell_rects().items():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self._shot_selected = (r, c)
                self._draw_shot()
                self._set_shot_die_id_dialog(r, c)
                return

    def _on_shot_right_click(self, event):
        for (r, c), (x0, y0, x1, y1) in self._shot_cell_rects().items():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self._shot_cells[(r, c)] = {"present": False}
                if self._shot_selected == (r, c):
                    self._shot_selected = None
                self._draw_shot()
                return

    def _set_shot_order_dialog(self, row: int, col: int):
        rows, cols = self._shot_dims()
        slots = present_slots(self._shot_cells, rows, cols)
        cur = slots.get((row, col))
        if cur is None:
            return
        new = simpledialog.askinteger(
            "Die Order", f"Which die is this (row {row}, col {col}) in the "
            f"shot?\n\nCurrently die {cur} of {len(slots)}.",
            initialvalue=cur, minvalue=1, maxvalue=len(slots), parent=self)
        if new is None or new == cur:
            return
        self._set_shot_order(row, col, new)
        self._draw_shot()

    def _set_shot_die_id_dialog(self, row: int, col: int):
        cell = self._shot_cells.get((row, col))
        if not cell or not cell.get("present"):
            return
        cur = cell.get("die_id", "")
        new = simpledialog.askstring(
            "Name Die (whole wafer)",
            f"Die ID for this slot (row {row}, col {col}) - applied to "
            "every shot's die at this same slot across the wafer, unless "
            "a specific die has its own individual ID set on the Die Map "
            "tab.\n\nLeave blank to clear.",
            initialvalue=cur, parent=self)
        if new is None:
            return
        self._shot_cells[(row, col)]["die_id"] = new.strip()
        self._draw_shot()
        self._redraw_diemap()

    def _set_shot_order(self, row: int, col: int, new_order: int):
        rows, cols = self._shot_dims()
        slots = present_slots(self._shot_cells, rows, cols)
        for rc, n in slots.items():
            if n == new_order and rc != (row, col):
                self._shot_cells[rc]["order"] = slots[(row, col)]
                break
        self._shot_cells[(row, col)]["order"] = new_order

    def _die_pitch(self) -> tuple:
        return (_to_float(self._die_pitch_x_var.get(), 1.0) or 1.0,
               _to_float(self._die_pitch_y_var.get(), 1.0) or 1.0)

    def _shot_pitch(self) -> tuple:
        rows, cols = self._shot_dims()
        dx, dy = self._die_pitch()
        spx = _to_float(self._shot_pitch_x_var.get(), 0.0) or cols * dx
        spy = _to_float(self._shot_pitch_y_var.get(), 0.0) or rows * dy
        return spx, spy

    def shots_as_die_list(self) -> list:
        spx, spy = self._shot_pitch()
        return [{"row": r, "col": c, "x_um": c * spx, "y_um": -r * spy,
                "die_id": ""}
               for (r, c), present in self._shotmap_cells.items() if present]

    def _build_shotmap_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        top = ttk.Frame(tab, padding=6)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="Wafer size:").pack(side="left")
        ttk.Entry(top, textvariable=self._shotmap_rows_var, width=4).pack(side="left")
        ttk.Label(top, text="x").pack(side="left", padx=1)
        ttk.Entry(top, textvariable=self._shotmap_cols_var, width=4).pack(side="left")
        ttk.Label(top, text="touchdowns").pack(side="left", padx=(2, 8))
        ttk.Button(top, text="Apply", width=7, command=self._shotmap_apply_size).pack(
            side="left", padx=(0, 16))
        self._shotmap_fill_btn = ttk.Button(top, text="☑ Fill All",
                                            command=self._shotmap_toggle_all)
        self._shotmap_fill_btn.pack(side="left")
        ttk.Label(top, textvariable=self._shotmap_status_var,
                 foreground="#374151").pack(side="left", padx=12)

        body = ttk.Frame(tab)
        body.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self._shotmap_canvas = tk.Canvas(body, background="#f8fafc",
                                         highlightthickness=1,
                                         highlightbackground="#cbd5e1")
        self._shotmap_canvas.grid(row=0, column=0, sticky="nsew")
        self._shotmap_canvas.bind("<Button-1>", self._on_shotmap_click)
        self._shotmap_canvas.bind("<Configure>", lambda _e: self._draw_shotmap())

        self._draw_shotmap()

    def _shotmap_dims(self) -> tuple:
        return (max(1, _to_int(self._shotmap_rows_var.get(), 1)),
               max(1, _to_int(self._shotmap_cols_var.get(), 1)))

    def _shotmap_apply_size(self):
        rows, cols = self._shotmap_dims()
        cells = {}
        for r in range(rows):
            for c in range(cols):
                cells[(r, c)] = self._shotmap_cells.get((r, c), True)
        self._shotmap_cells = cells
        self._draw_shotmap()

    def _shotmap_set_all(self, present: bool):
        for k in self._shotmap_cells:
            self._shotmap_cells[k] = present
        self._draw_shotmap()

    def _shotmap_toggle_all(self):
        all_filled = bool(self._shotmap_cells) and all(self._shotmap_cells.values())
        self._shotmap_set_all(not all_filled)

    _SM_CELL = 26
    _SM_GAP = 3

    def _shotmap_cell_rects(self) -> Dict[tuple, tuple]:
        rows, cols = self._shotmap_dims()
        w = int(self._shotmap_canvas.winfo_width() or 1)
        h = int(self._shotmap_canvas.winfo_height() or 1)
        avail_w = (w - 20 - self._SM_GAP * cols) // max(1, cols)
        avail_h = (h - 20 - self._SM_GAP * rows) // max(1, rows)
        cell = max(2, min(self._SM_CELL, avail_w, avail_h))
        span_w = cols * cell + (cols - 1) * self._SM_GAP
        span_h = rows * cell + (rows - 1) * self._SM_GAP
        x0 = max(22, (w - span_w) // 2)
        y0 = max(16, (h - span_h) // 2)
        out = {}
        for r in range(rows):
            for c in range(cols):
                left = x0 + c * (cell + self._SM_GAP)
                top = y0 + r * (cell + self._SM_GAP)
                out[(r, c)] = (left, top, left + cell, top + cell)
        return out

    def _draw_shotmap(self):
        cv = getattr(self, "_shotmap_canvas", None)
        if cv is None:
            return
        cv.delete("all")
        rects = self._shotmap_cell_rects()
        n = 0
        for (r, c), (x0, y0, x1, y1) in rects.items():
            present = self._shotmap_cells.get((r, c), False)
            if present:
                n += 1
            fill = "#60a5fa" if present else "#f1f5f9"
            outline = "#1d4ed8" if present else "#cbd5e1"
            cv.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline)
        self._draw_shotmap_axis_labels(rects)
        self._shotmap_status_var.set(f"{n} touchdown(s) on the wafer.")
        if hasattr(self, "_shotmap_fill_btn"):
            all_filled = bool(self._shotmap_cells) and all(self._shotmap_cells.values())
            self._shotmap_fill_btn.config(text="☐ Clear All" if all_filled else "☑ Fill All")

    def _draw_shotmap_axis_labels(self, rects: Dict[tuple, tuple]):
        cv = self._shotmap_canvas
        rows, cols = self._shotmap_dims()
        font = ("TkDefaultFont", 7)
        step_r = max(1, round(rows / 20))
        step_c = max(1, round(cols / 20))
        for r in range(0, rows, step_r):
            x0, y0, x1, y1 = rects[(r, 0)]
            cv.create_text(max(4, x0 - 6), (y0 + y1) / 2, text=str(r),
                           fill="#64748b", anchor="e", font=font)
        for c in range(0, cols, step_c):
            x0, y0, x1, y1 = rects[(0, c)]
            cv.create_text((x0 + x1) / 2, max(4, y0 - 6), text=str(c),
                           fill="#64748b", anchor="s", font=font)

    def _on_shotmap_click(self, event):
        for (r, c), (x0, y0, x1, y1) in self._shotmap_cell_rects().items():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self._shotmap_cells[(r, c)] = not self._shotmap_cells.get((r, c), False)
                self._draw_shotmap()
                return

    def _build_diemap_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        top = ttk.Frame(tab, padding=6)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="Click mode:").pack(side="left")
        for value, text in (("id", "Set Die ID"), ("skip", "Mark Skip"),
                           ("align", "Mark Align")):
            ttk.Radiobutton(top, text=text, value=value,
                           variable=self._diemap_mode_var).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="right click to clear",
                 foreground="#6b7280").pack(side="left", padx=(10, 0))
        ttk.Button(top, text="Save Wafer Map",
                  command=self._save_wafer_map).pack(side="left", padx=(16, 0))
        ttk.Button(top, text="Export CSV",
                  command=self._export_diemap_csv).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="Label min width (px):",
                 foreground="#6b7280").pack(side="left", padx=(16, 0))
        ttk.Spinbox(top, from_=4, to=200, increment=1, width=4,
                   textvariable=self._diemap_label_min_px_var).pack(side="left", padx=(4, 0))
        ttk.Label(top, textvariable=self._diemap_status_var,
                 foreground="#374151").pack(side="left", padx=10)

        legend = ttk.Frame(tab)
        legend.grid(row=1, column=0, sticky="ew", padx=6)
        for color, text in [(_COLOR_HAS_ID, "has ID"), (_COLOR_BLANK, "blank"),
                           (_COLOR_SKIP, "skip"), (_COLOR_ALIGN, "align")]:
            sw = tk.Canvas(legend, width=12, height=12, highlightthickness=0)
            sw.create_rectangle(0, 0, 12, 12, fill=color, outline="")
            sw.pack(side="left", padx=(0, 3))
            ttk.Label(legend, text=text).pack(side="left", padx=(0, 10))

        body = ttk.Frame(tab)
        body.grid(row=2, column=0, sticky="nsew", padx=6, pady=(4, 6))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        if _MPL:
            self.fig = Figure(figsize=(6, 6), dpi=100)
            self.ax = self.fig.add_subplot(111)
            self.canvas = FigureCanvasTkAgg(self.fig, master=body)
            toolbar = NavigationToolbar2Tk(self.canvas, body, pack_toolbar=False)
            toolbar.grid(row=1, column=0, sticky="ew")
            self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
            self.canvas.mpl_connect("button_press_event", self._on_diemap_click)
            self.canvas.mpl_connect("scroll_event", self._on_diemap_scroll_zoom)
            bind_middle_pan_mpl(self.canvas, lambda: getattr(self, "ax", None))
            self._diemap_xlim_cid = None
        else:
            ttk.Label(body, text="matplotlib not installed — install it to "
                                "view/edit the die map.", foreground="red").grid(
                row=0, column=0, sticky="w", padx=10, pady=10)

        self._redraw_diemap()

    def _die_positions(self) -> List[dict]:
        shot_rows, shot_cols = self._shot_dims()
        dpx, dpy = self._die_pitch()
        spx, spy = self._shot_pitch()
        out = []
        for (sr, sc), present in self._shotmap_cells.items():
            if not present:
                continue
            ox, oy = sc * spx, sr * spy
            for (slr, slc), cell in self._shot_cells.items():
                if not cell.get("present"):
                    continue
                key = (sr, sc, slr, slc)
                info = self._die_status.get(key, {})
                die_id = info.get("die_id") or cell.get("die_id", "")
                out.append({
                    "key": key, "x": ox + slc * dpx, "y": oy + slr * dpy,
                    "w": dpx, "h": dpy, "shot_r": sr, "shot_c": sc,
                    "slot_r": slr, "slot_c": slc,
                    "die_id": die_id,
                    "status": info.get("status", "normal"),
                })
        return out

    def _die_color(self, box: dict) -> str:
        if box["status"] == "align":
            return _COLOR_ALIGN
        if box["status"] == "skip":
            return _COLOR_SKIP
        return _COLOR_HAS_ID if box["die_id"] else _COLOR_BLANK

    def _redraw_diemap(self, reset_view: bool = True):
        if not _MPL or not hasattr(self, "ax"):
            return
        self._close_die_editor(commit=True)
        prev_xlim = self.ax.get_xlim()
        prev_ylim = self.ax.get_ylim()
        self.ax.clear()
        if self._diemap_xlim_cid is not None:
            try:
                self.ax.callbacks.disconnect(self._diemap_xlim_cid)
            except Exception:
                pass
        self._diemap_xlim_cid = self.ax.callbacks.connect(
            "xlim_changed", lambda _ax: self._diemap_debounced_label_visibility())
        self._selected_die_patch = None
        self._die_boxes = self._die_positions()
        self._die_id_labels = []
        self._diemap_coll = None
        self._diemap_box_index = {b["key"]: i for i, b in enumerate(self._die_boxes)}
        self._diemap_label_by_key = {}
        if self._die_boxes:
            patches = [Rectangle((b["x"], b["y"]), b["w"], b["h"])
                      for b in self._die_boxes]
            coll = PatchCollection(patches, edgecolor="#0f172a", linewidths=0.3)
            coll.set_facecolor([self._die_color(b) for b in self._die_boxes])
            self.ax.add_collection(coll)
            self._diemap_coll = coll
            if reset_view:
                xs = [b["x"] for b in self._die_boxes]
                ys = [b["y"] for b in self._die_boxes]
                dpx, dpy = self._die_pitch()
                self.ax.set_xlim(min(xs) - dpx, max(xs) + 2 * dpx)
                self.ax.set_ylim(min(ys) - dpy, max(ys) + 2 * dpy)
        if reset_view:
            self.ax.invert_yaxis()
        else:
            self.ax.set_xlim(prev_xlim)
            self.ax.set_ylim(prev_ylim)
        n_id = sum(1 for b in self._die_boxes if b["die_id"] and b["status"] == "normal")
        n_skip = sum(1 for b in self._die_boxes if b["status"] == "skip")
        n_align = sum(1 for b in self._die_boxes if b["status"] == "align")
        self.ax.set_title(f"{self.map_name_var.get()} — {len(self._die_boxes)} die(s), "
                          f"{n_id} with ID, {n_skip} skip, {n_align} align")
        self.ax.set_aspect("equal")
        if self._die_boxes:
            self.ax.set_axis_on()
            self._draw_diemap_axis_ticks()
        else:
            self.ax.set_axis_off()
        self._diemap_sync_visible_labels()
        self.canvas.draw_idle()

    def _draw_diemap_axis_ticks(self):
        shot_rows, shot_cols = self._shot_dims()
        row_y, col_x = {}, {}
        for b in self._die_boxes:
            row = b["shot_r"] * shot_rows + b["slot_r"]
            col = b["shot_c"] * shot_cols + b["slot_c"]
            row_y.setdefault(row, b["y"])
            col_x.setdefault(col, b["x"])

        def _pick(mapping, target=20):
            keys = sorted(mapping)
            if len(keys) <= target:
                return keys
            step = max(1, round(len(keys) / target))
            return keys[::step]

        rows = _pick(row_y)
        cols = _pick(col_x)
        self.ax.set_yticks([row_y[r] for r in rows])
        self.ax.set_yticklabels([str(r) for r in rows], fontsize=7)
        self.ax.set_xticks([col_x[c] for c in cols])
        self.ax.set_xticklabels([str(c) for c in cols], fontsize=7)
        self.ax.tick_params(length=3, colors="#64748b", labelcolor="#64748b")
        for spine in self.ax.spines.values():
            spine.set_visible(False)
        self.ax.grid(True, which="major", alpha=0.15, linestyle="--")

    _DIEMAP_LABEL_VIEW_MARGIN = 0.4

    def _diemap_visible_keys(self) -> set:
        if not getattr(self, "_die_boxes", None):
            return set()
        try:
            xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        except Exception:
            return set()
        x0, x1 = sorted(xlim)
        y0, y1 = sorted(ylim)
        pad_x = (x1 - x0) * self._DIEMAP_LABEL_VIEW_MARGIN
        pad_y = (y1 - y0) * self._DIEMAP_LABEL_VIEW_MARGIN
        x0, x1 = x0 - pad_x, x1 + pad_x
        y0, y1 = y0 - pad_y, y1 + pad_y
        return {b["key"] for b in self._die_boxes
               if b["die_id"]
               and x0 <= b["x"] <= x1 and y0 <= b["y"] <= y1}

    def _diemap_sync_visible_labels(self):
        if not getattr(self, "_die_boxes", None) or not hasattr(self, "ax"):
            return
        want = self._diemap_visible_keys()
        have = set(self._diemap_label_by_key)
        for key in have - want:
            txt = self._diemap_label_by_key.pop(key)
            try:
                txt.remove()
            except Exception:
                pass
            if txt in self._die_id_labels:
                self._die_id_labels.remove(txt)
        if want - have:
            box_by_key = {b["key"]: b for b in self._die_boxes}
            for key in want - have:
                b = box_by_key.get(key)
                if b is None:
                    continue
                txt = self.ax.text(
                    b["x"] + b["w"] / 2, b["y"] + b["h"] / 2, b["die_id"],
                    ha="center", va="center", fontsize=6, color="#0f172a",
                    zorder=6, clip_on=True)
                self._die_id_labels.append(txt)
                self._diemap_label_by_key[key] = txt
        self._diemap_label_visibility()
        self.canvas.draw_idle()

    def _diemap_update_one(self, key: tuple):
        if (not _MPL or not hasattr(self, "ax")
                or getattr(self, "_diemap_coll", None) is None):
            self._redraw_diemap(reset_view=False)
            return
        idx = self._diemap_box_index.get(key)
        if idx is None or idx >= len(self._die_boxes):
            self._redraw_diemap(reset_view=False)
            return

        box = self._die_boxes[idx]
        info = self._die_status.get(key, {"die_id": box["die_id"], "status": "normal"})
        box["die_id"] = info.get("die_id", "")
        box["status"] = info.get("status", "normal")

        colors = self._diemap_coll.get_facecolor()
        colors[idx] = to_rgba(self._die_color(box))
        self._diemap_coll.set_facecolor(colors)

        old_label = self._diemap_label_by_key.pop(key, None)
        if old_label is not None:
            try:
                old_label.remove()
            except Exception:
                pass
            if old_label in self._die_id_labels:
                self._die_id_labels.remove(old_label)
        if box["die_id"]:
            txt = self.ax.text(
                box["x"] + box["w"] / 2, box["y"] + box["h"] / 2, box["die_id"],
                ha="center", va="center", fontsize=6, color="#0f172a",
                zorder=6, clip_on=True)
            self._die_id_labels.append(txt)
            self._diemap_label_by_key[key] = txt

        n_id = sum(1 for b in self._die_boxes if b["die_id"] and b["status"] == "normal")
        n_skip = sum(1 for b in self._die_boxes if b["status"] == "skip")
        n_align = sum(1 for b in self._die_boxes if b["status"] == "align")
        self.ax.set_title(f"{self.map_name_var.get()} — {len(self._die_boxes)} die(s), "
                          f"{n_id} with ID, {n_skip} skip, {n_align} align")
        self._diemap_label_visibility()
        self.canvas.draw_idle()
        self._diemap_status_var.set(f"{len(self._die_boxes)} die(s) on the wafer.")

    _DIEMAP_LABEL_MIN_PX = 22

    def _diemap_debounced_label_visibility(self, delay_ms: int = 60):
        pending = getattr(self, "_diemap_visibility_after_id", None)
        if pending is not None:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
        self._diemap_visibility_after_id = self.after(
            delay_ms, self._diemap_sync_visible_labels)

    def _on_diemap_label_min_px_change(self, *_args):
        if hasattr(self, "ax"):
            self._diemap_label_visibility()

    def _diemap_label_min_px(self) -> float:
        try:
            return float(self._diemap_label_min_px_var.get())
        except (tk.TclError, ValueError):
            return self._DIEMAP_LABEL_MIN_PX

    def _diemap_label_visibility(self):
        if not getattr(self, "_die_id_labels", None):
            return
        dpx, _dpy = self._die_pitch()
        (x0, _), (x1, _) = self.ax.transData.transform([(0, 0), (dpx, 0)])
        visible = abs(x1 - x0) >= self._diemap_label_min_px()
        for txt in self._die_id_labels:
            txt.set_visible(visible)
        self.canvas.draw_idle()

    def _on_diemap_scroll_zoom(self, event):
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            return
        self._close_die_editor(commit=True)
        factor = 0.85 if event.button == "up" else (1 / 0.85)
        xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        self.ax.set_xlim(xd - (xd - xlim[0]) * factor, xd + (xlim[1] - xd) * factor)
        self.ax.set_ylim(yd - (yd - ylim[0]) * factor, yd + (ylim[1] - yd) * factor)
        self.canvas.draw_idle()

    def _hit_die(self, xdata, ydata) -> Optional[dict]:
        for b in self._die_boxes:
            if b["x"] <= xdata < b["x"] + b["w"] and b["y"] <= ydata < b["y"] + b["h"]:
                return b
        return None

    def _on_diemap_click(self, event):
        if event.button == 2:
            return
        if event.xdata is None or event.ydata is None:
            return
        die = self._hit_die(event.xdata, event.ydata)
        if die is None:
            return
        if event.button == 3:
            self._close_die_editor(commit=True)
            self._die_status[die["key"]] = {"die_id": die["die_id"], "status": "normal"}
            self._diemap_update_one(die["key"])
            return
        mode = self._diemap_mode_var.get()
        if mode == "id":
            self._select_die(die)
            self._open_die_editor(die)
        else:
            self._close_die_editor(commit=True)
            cur = self._die_status.get(die["key"], {"die_id": die["die_id"],
                                                     "status": "normal"})
            cur["status"] = "normal" if cur.get("status") == mode else mode
            self._die_status[die["key"]] = cur
            self._diemap_update_one(die["key"])

    def _select_die(self, box: dict):
        if self._selected_die_patch is not None:
            try:
                self._selected_die_patch.remove()
            except Exception:
                pass
        hl = Rectangle((box["x"], box["y"]), box["w"], box["h"], fill=False,
                      edgecolor=_COLOR_SELECTED, linewidth=2.2, zorder=7)
        self.ax.add_patch(hl)
        self._selected_die_patch = hl
        self.canvas.draw_idle()

    def _open_die_editor(self, box: dict):
        self._close_die_editor(commit=True)
        (px0, py0), (px1, py1) = self.ax.transData.transform(
            [(box["x"], box["y"]), (box["x"] + box["w"], box["y"] + box["h"])])
        canvas_h = self.fig.bbox.height
        left, right = sorted((px0, px1))
        top, bottom = sorted((canvas_h - py0, canvas_h - py1))

        entry = tk.Entry(self.canvas.get_tk_widget(), borderwidth=1,
                         relief="solid", font=("Segoe UI", 9))
        entry.insert(0, box["die_id"])
        entry.select_range(0, "end")
        entry.place(x=left, y=top, width=max(right - left, 30),
                   height=max(bottom - top, 16))
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self._close_die_editor(commit=True))
        entry.bind("<Escape>", lambda _e: self._close_die_editor(commit=False))
        entry.bind("<FocusOut>", lambda _e: self._close_die_editor(commit=True))
        self._die_editor = entry
        self._die_editor_key = box["key"]

    def _close_die_editor(self, commit: bool):
        entry, key = self._die_editor, self._die_editor_key
        if entry is None:
            return
        self._die_editor = None
        self._die_editor_key = None
        if commit and key is not None:
            text = entry.get().strip()
            cur = self._die_status.get(key, {"die_id": "", "status": "normal"})
            cur["die_id"] = text
            self._die_status[key] = cur
        try:
            entry.destroy()
        except Exception:
            pass
        if commit:
            if key is not None:
                self._diemap_update_one(key)
            else:
                self._redraw_diemap(reset_view=False)

    def _maps_dir(self, create: bool = False) -> Optional[str]:
        folder = getattr(self._main_layout, "_exec_map_folder", None) or \
            getattr(self._main_layout, "_ata_folder", None)
        if not folder:
            return None
        d = os.path.join(folder, "wafer_builder_maps")
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    def list_map_names(self) -> list:
        d = self._maps_dir()
        if not d or not os.path.isdir(d):
            return []
        return sorted(os.path.splitext(f)[0] for f in os.listdir(d)
                     if f.endswith(".json"))

    def _refresh_map_picker(self):
        d = self._maps_dir()
        names = []
        if d and os.path.isdir(d):
            names = sorted(os.path.splitext(f)[0] for f in os.listdir(d)
                           if f.endswith(".json"))
        self._map_picker_cb.config(values=names)

    @staticmethod
    def _safe_map_filename(name: str) -> str:
        return "".join(c for c in name.strip() if c.isalnum() or c in " _-").strip() or "map"

    def _state_to_dict(self) -> dict:
        def kstr(k):
            return f"{k[0]},{k[1]}"
        ml = self._main_layout
        return {
            "shot_rows": self._shot_rows_var.get(), "shot_cols": self._shot_cols_var.get(),
            "die_pitch_x": self._die_pitch_x_var.get(), "die_pitch_y": self._die_pitch_y_var.get(),
            "shot_pitch_x": self._shot_pitch_x_var.get(), "shot_pitch_y": self._shot_pitch_y_var.get(),
            "shot_cells": {kstr(k): v for k, v in self._shot_cells.items()},
            "shotmap_rows": self._shotmap_rows_var.get(),
            "shotmap_cols": self._shotmap_cols_var.get(),
            "shotmap_cells": {kstr(k): v for k, v in self._shotmap_cells.items()},
            "die_status": {",".join(str(x) for x in k): v
                          for k, v in self._die_status.items()},
            "overlay_row_offset": getattr(ml, "_exec_overlay_row_offset", 0),
            "overlay_col_offset": getattr(ml, "_exec_overlay_col_offset", 0),
            "overlay_confirmed": bool(getattr(ml, "_exec_overlay_offset_confirmed", False)),
        }

    def _state_from_dict(self, data: dict):
        def pk2(s):
            a, b = s.split(",")
            return int(a), int(b)

        def pk4(s):
            a, b, c, d = s.split(",")
            return int(a), int(b), int(c), int(d)

        self._close_die_editor(commit=False)
        self._shot_rows_var.set(data.get("shot_rows", "1"))
        self._shot_cols_var.set(data.get("shot_cols", "1"))
        self._die_pitch_x_var.set(data.get("die_pitch_x", "1000"))
        self._die_pitch_y_var.set(data.get("die_pitch_y", "1000"))
        self._shot_pitch_x_var.set(data.get("shot_pitch_x", ""))
        self._shot_pitch_y_var.set(data.get("shot_pitch_y", ""))
        self._shot_cells = {pk2(k): v for k, v in data.get("shot_cells", {}).items()}
        self._shotmap_rows_var.set(data.get("shotmap_rows", "4"))
        self._shotmap_cols_var.set(data.get("shotmap_cols", "4"))
        self._shotmap_cells = {pk2(k): v for k, v in data.get("shotmap_cells", {}).items()}
        self._die_status = {pk4(k): v for k, v in data.get("die_status", {}).items()}
        self._shot_selected = None
        self._draw_shot()
        self._draw_shotmap()
        self._redraw_diemap()
        ml = self._main_layout
        if self._system == "accretech" and hasattr(ml, "_exec_overlay_offset_confirmed"):
            try:
                ml._exec_overlay_row_offset = int(data.get("overlay_row_offset", 0) or 0)
                ml._exec_overlay_col_offset = int(data.get("overlay_col_offset", 0) or 0)
            except (TypeError, ValueError):
                ml._exec_overlay_row_offset = ml._exec_overlay_col_offset = 0
            ml._exec_overlay_offset_confirmed = bool(data.get("overlay_confirmed", False))

    def _current_folder(self) -> Optional[str]:
        return getattr(self._main_layout, "_exec_map_folder", None) or \
            getattr(self._main_layout, "_ata_folder", None)

    def _new_named_map(self):
        folder = self._current_folder()
        d = self._maps_dir(create=True)
        if not d or not folder:
            messagebox.showerror("No ATA Folder", "Load an ATA folder first.")
            return
        name = simpledialog.askstring("New Map", "Map name:", parent=self)
        if not name:
            return
        name = self._safe_map_filename(name)
        if not name:
            messagebox.showerror("Invalid Name", "Use letters, digits, space, - or _.")
            return
        path = os.path.join(d, name + ".json")
        if os.path.isfile(path):
            messagebox.showerror("Duplicate", f"A map named '{name}' already exists.")
            return
        self._close_die_editor(commit=True)
        self._shot_rows_var.set("1"); self._shot_cols_var.set("1")
        self._die_pitch_x_var.set("1000"); self._die_pitch_y_var.set("1000")
        self._shot_pitch_x_var.set(""); self._shot_pitch_y_var.set("")
        self._shot_cells = {(0, 0): {"present": True}}
        self._shotmap_rows_var.set("4"); self._shotmap_cols_var.set("4")
        self._shotmap_cells = {(r, c): True for r in range(4) for c in range(4)}
        self._die_status = {}
        self.map_name_var.set(name)
        self._draw_shot()
        self._draw_shotmap()
        self._redraw_diemap()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._state_to_dict(), f, indent=2)
        except OSError as exc:
            messagebox.showerror("Create Failed", str(exc))
            return
        self._log(f"[MAP] Created new map '{name}'")
        self._refresh_map_picker()
        self._sync_partner_after_change(folder, name)

    def _rename_named_map(self):
        old_name = self.map_name_var.get().strip()
        d = self._maps_dir()
        if not old_name or not d:
            messagebox.showerror("No Map", "No map is currently loaded.")
            return
        old_path = os.path.join(d, self._safe_map_filename(old_name) + ".json")
        if not os.path.isfile(old_path):
            messagebox.showerror("Not Found", f"'{old_name}' hasn't been saved "
                                 "yet - use ＋ New instead.")
            return
        new_name = simpledialog.askstring("Rename Map", "New name:",
                                          initialvalue=old_name, parent=self)
        if not new_name:
            return
        new_name = self._safe_map_filename(new_name)
        if not new_name or new_name == old_name:
            return
        new_path = os.path.join(d, new_name + ".json")
        if os.path.isfile(new_path):
            messagebox.showerror("Duplicate", f"A map named '{new_name}' already exists.")
            return
        try:
            os.replace(old_path, new_path)
        except OSError as exc:
            messagebox.showerror("Rename Failed", str(exc))
            return
        marker = os.path.join(d, self._DEFAULT_MARKER)
        if os.path.isfile(marker):
            try:
                with open(marker, encoding="utf-8") as f:
                    was_default = f.read().strip() == old_name
                if was_default:
                    with open(marker, "w", encoding="utf-8") as f:
                        f.write(new_name)
            except OSError:
                pass
        self.map_name_var.set(new_name)
        self._redraw_diemap()
        self._log(f"[MAP] Renamed map '{old_name}' → '{new_name}'")
        self._refresh_map_picker()
        folder = self._current_folder()
        if folder:
            self._sync_partner_after_change(folder, new_name)

    def _load_named_map(self, name: str):
        name = (name or "").strip()
        if not name:
            return
        d = self._maps_dir()
        path = os.path.join(d, self._safe_map_filename(name) + ".json") if d else ""
        if not d or not os.path.isfile(path):
            messagebox.showerror("Not Found", f"No saved map named '{name}'.")
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Load Failed", str(exc))
            return
        self.map_name_var.set(name)
        self._state_from_dict(data)
        self._log(f"[MAP] Loaded map '{name}'")

    def _delete_named_map(self):
        name = self.map_name_var.get().strip()
        if not name:
            messagebox.showerror("No Map Name", "Type/select a saved map name first.")
            return
        d = self._maps_dir()
        path = os.path.join(d, self._safe_map_filename(name) + ".json") if d else ""
        if not d or not os.path.isfile(path):
            messagebox.showerror("Not Found", f"No saved map named '{name}'.")
            return
        if not messagebox.askyesno(
                "Delete Map", f"Delete the saved map '{name}'?"):
            return
        try:
            os.remove(path)
        except OSError as exc:
            messagebox.showerror("Delete Failed", str(exc))
            return
        marker = os.path.join(d, self._DEFAULT_MARKER)
        if os.path.isfile(marker):
            try:
                with open(marker, encoding="utf-8") as f:
                    was_default = f.read().strip() == name
                if was_default:
                    os.remove(marker)
            except OSError:
                pass
        self._log(f"[MAP] Deleted map '{name}'")
        self.map_name_var.set("")
        self._refresh_map_picker()

    _DEFAULT_MARKER = "_default.txt"

    def _set_default_map(self):
        name = self.map_name_var.get().strip()
        if not name:
            messagebox.showerror("No Map Name", "Type/select a map name first.")
            return
        d = self._maps_dir(create=True)
        folder = self._current_folder()
        if not d or not folder:
            messagebox.showerror("No ATA Folder", "Load an ATA folder first.")
            return
        self._close_die_editor(commit=True)
        path = os.path.join(d, self._safe_map_filename(name) + ".json")
        if not os.path.isfile(path):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self._state_to_dict(), f, indent=2)
            except OSError as exc:
                messagebox.showerror("Save Failed", str(exc))
                return
            self._refresh_map_picker()
        marker = os.path.join(d, self._DEFAULT_MARKER)
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(name)
        except OSError as exc:
            messagebox.showerror("Set Default Failed", str(exc))
            return
        self._log(f"[MAP] '{name}' set as the default map for this "
                 f"ATA folder — it will auto-load whenever this folder opens.")
        messagebox.showinfo("Default Set", f"'{name}' will now auto-load "
                           f"whenever this ATA folder is opened.")
        self._sync_partner_after_change(folder, None)

    def autoload_map_for_folder(self, folder: str):
        self.map_name_var.set("")
        self._state_from_dict({})
        d = os.path.join(folder, "wafer_builder_maps")
        if not os.path.isdir(d):
            return
        names = sorted(os.path.splitext(f)[0] for f in os.listdir(d)
                       if f.endswith(".json"))
        if not names:
            return
        target = None
        marker = os.path.join(d, self._DEFAULT_MARKER)
        if os.path.isfile(marker):
            try:
                with open(marker, encoding="utf-8") as f:
                    marked = f.read().strip()
                if marked in names:
                    target = marked
            except OSError:
                pass
        if target is None:
            target = "Autoload" if "Autoload" in names else (
                names[0] if len(names) == 1 else None)
        if not target:
            return
        path = os.path.join(d, self._safe_map_filename(target) + ".json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            self._log(f"[MAP] Could not auto-load map '{target}': "
                     f"{type(exc).__name__}: {exc}")
            return
        self.map_name_var.set(target)
        self._state_from_dict(data)
        self._log(f"[MAP] Auto-loaded map '{target}'")

    def _autosave_named_map_quiet(self, folder: str):
        name = self.map_name_var.get().strip() or "NewMap"
        self.map_name_var.set(name)
        d = os.path.join(folder, "wafer_builder_maps")
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, self._safe_map_filename(name) + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._state_to_dict(), f, indent=2)
        except OSError as exc:
            self._log(f"[MAP] Could not auto-save map definition "
                     f"'{name}': {exc}")
            return
        self._sync_partner_after_change(folder, name)

    def _sibling_recipe_gen(self) -> Optional["RecipeGenPanel"]:
        by_system = getattr(self.controller, "_by_system", None)
        if not by_system or self._system not in ("accretech", "electroglas"):
            return None
        other = "electroglas" if self._system == "accretech" else "accretech"
        other_ui = by_system.get(other, {}).get("ui")
        return getattr(other_ui, "recipe_gen", None) if other_ui is not None else None

    def _sync_partner_after_change(self, folder: str, name: Optional[str]):
        sib = self._sibling_recipe_gen()
        if sib is None or sib is self:
            return
        sib_folder = sib._current_folder()
        if sib_folder != folder:
            return
        if name is None:
            sib.autoload_map_for_folder(folder)
        elif sib.map_name_var.get().strip() == name:
            sib._reload_named_map_quiet(folder, name)

    def _reload_named_map_quiet(self, folder: str, name: str):
        d = os.path.join(folder, "wafer_builder_maps")
        path = os.path.join(d, self._safe_map_filename(name) + ".json")
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self._state_from_dict(data)
        self.map_name_var.set(name)
        self._log(f"[MAP] Synced map '{name}' — updated on the "
                 f"other system's tab.")

    def _wafer_map_filename(self) -> str:
        return WAFER_MAP_SOURCES["Wafer Builder"]

    def _write_active_wafer_map_csv(self, folder: str, dies: list):
        shot_rows, shot_cols = self._shot_dims()
        shot_order = {(sr, sc): i + 1 for i, (sr, sc) in
                     enumerate(sorted(k for k, v in self._shotmap_cells.items() if v))}
        path = os.path.join(folder, self._wafer_map_filename())
        fields = ("row", "col", "seq", "quad_pos", "device_id",
                 "x_um", "y_um", "map_x", "map_y", "shot_x", "shot_y", "enabled")
        with open(path, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=fields)
            wr.writeheader()
            for d in dies:
                device_id = d["die_id"]
                ox, oy = d["shot_c"] * self._shot_pitch()[0], d["shot_r"] * self._shot_pitch()[1]
                wr.writerow({
                    "row": d["shot_r"] * shot_rows + d["slot_r"],
                    "col": d["shot_c"] * shot_cols + d["slot_c"],
                    "seq": shot_order.get((d["shot_r"], d["shot_c"]), 0),
                    "quad_pos": f"R{d['slot_r']}C{d['slot_c']}",
                    "device_id": device_id,
                    "x_um": fmt_num(d["x"]), "y_um": fmt_num(-d["y"]),
                    "map_x": fmt_num(d["x"]), "map_y": fmt_num(d["y"]),
                    "shot_x": fmt_num(ox), "shot_y": fmt_num(oy),
                    "enabled": 0 if d["status"] == "skip" else 1,
                })
        return path

    def _save_wafer_map(self):
        self._close_die_editor(commit=True)
        folder = getattr(self._main_layout, "_exec_map_folder", None) or \
            getattr(self._main_layout, "_ata_folder", None)
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("No ATA Folder", "Load an ATA folder first.")
            return
        dies = self._die_positions()
        if not dies:
            messagebox.showerror("Empty Map", "No touchdowns/dies to save — "
                                 "set up Shot and Shot Map first.")
            return
        n_id = sum(1 for d in dies if d["status"] == "normal" and d["die_id"])
        n_skip = sum(1 for d in dies if d["status"] == "skip")
        n_align = sum(1 for d in dies if d["status"] == "align")
        filename = self._wafer_map_filename()
        if not messagebox.askokcancel(
                "Save Wafer Map",
                f"Write {len(dies)} die(s) ({n_id} with an ID, {n_skip} skip, "
                f"{n_align} align) to {filename} in\n{folder}?\n\n"
                "This replaces the Run tab's wafer map."):
            return
        try:
            path = self._write_active_wafer_map_csv(folder, dies)
        except OSError as exc:
            messagebox.showerror("Write Failed", str(exc))
            return
        self._diemap_status_var.set(f"Wrote {len(dies)} die(s) to the Run tab's wafer map.")
        self._log(f"[MAP] Wrote {len(dies)} die(s), {n_id} with an "
                 f"ID, {n_skip} skip.")
        self._autosave_named_map_quiet(folder)
        self._refresh_map_picker()
        self._sync_views(folder)

    def _export_diemap_csv(self):
        self._close_die_editor(commit=True)
        dies = self._die_positions()
        if not dies:
            messagebox.showerror("Empty Map", "No dies to export — set up "
                                 "Shot and Shot Map first.")
            return
        shot_rows, shot_cols = self._shot_dims()
        grid = {}
        max_row = max_col = 0
        for d in dies:
            row = d["shot_r"] * shot_rows + d["slot_r"]
            col = d["shot_c"] * shot_cols + d["slot_c"]
            grid[(row, col)] = d["die_id"]
            max_row = max(max_row, row)
            max_col = max(max_col, col)
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            os.makedirs(downloads, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Export Failed", str(exc))
            return
        name = self._safe_map_filename(
            self.map_name_var.get().strip() or "wafer_builder_die_map")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(downloads, f"{name}_die_map_{ts}.csv")
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f)
                for row in range(max_row + 1):
                    wr.writerow([grid.get((row, col), "")
                                for col in range(max_col + 1)])
        except OSError as exc:
            messagebox.showerror("Export Failed", str(exc))
            return
        self._log(f"[MAP] Exported {len(dies)} die(s)")
        messagebox.showinfo("Exported", f"Wrote {len(dies)} die(s) to:\n{path}")

    def _sync_views(self, folder: str):
        layout = self._main_layout
        try:
            layout._exec_map_folder = folder
            if self._system != "accretech":
                dies = self._die_positions()
                if dies:
                    try:
                        self._write_active_wafer_map_csv(folder, dies)
                    except OSError as exc:
                        self._log(f"[MAP] Could not publish the Run "
                                 f"tab's map: {exc}")
                layout._exec_map_source_var.set("Wafer Builder")
            layout._exec_draw_wafer_map()
        except Exception as exc:
            self._log(f"[MAP] Map written, but the Run tab did not "
                     f"redraw: {type(exc).__name__}: {exc}")
        proc = getattr(layout, "pma_process", None)
        if proc is not None:
            try:
                proc.refresh_align_site()
            except Exception:
                pass
        if self._system != "accretech":
            self._push_to_pma_wafer(folder)

    def _plain_csv_rows(self) -> List[List[str]]:
        shot_rows, shot_cols = self._shot_dims()
        ordered = sorted(present_slots(self._shot_cells, shot_rows, shot_cols).items(),
                         key=lambda kv: kv[1])
        present_shots = [rc for rc, v in self._shotmap_cells.items() if v]
        max_sr = max((r for r, _ in present_shots), default=-1)
        max_sc = max((c for _, c in present_shots), default=-1)
        rows = []
        for sr in range(max_sr + 1):
            row = []
            for sc in range(max_sc + 1):
                if not self._shotmap_cells.get((sr, sc)):
                    row.append("")
                    continue
                texts = []
                for (slr, slc), _slot_no in ordered:
                    info = self._die_status.get((sr, sc, slr, slc), {})
                    status = info.get("status", "normal")
                    if status == "skip":
                        texts.append("SKIP")
                    elif status == "align":
                        texts.append("ALIGN")
                    else:
                        texts.append(info.get("die_id") or "UNNAMED")
                row.append("/".join(texts))
            rows.append(row)
        return rows

    def _push_to_pma_wafer(self, folder: str):
        wafer = getattr(self._main_layout, "pma_wafer", None)
        if wafer is None:
            return
        rows = self._plain_csv_rows()
        if not any(c for r in rows for c in r):
            return
        shot_rows, shot_cols = self._shot_dims()
        try:
            wafer._shot_rows_var.set(str(shot_rows))
            wafer._shot_cols_var.set(str(shot_cols))
            path = os.path.join(folder, ATA_CSV_MAP_FILENAME)
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
            wafer.load_csv_path(path)
        except Exception as exc:
            self._log(f"[MAP] Could not sync the .PMA-recipe "
                     f"wafer view: {type(exc).__name__}: {exc}")

    def _current_tab_kind(self) -> str:
        cur = self._sub_nb.select()
        if cur == str(self._shot_tab_widget):
            return "shot"
        if cur == str(self._shotmap_tab_widget):
            return "shotmap"
        if cur == str(self._diemap_tab_widget):
            return "die"
        return "die"

    def _on_subtab_changed(self, _event=None):
        self.update_idletasks()
        kind = self._current_tab_kind()
        if kind == "shot":
            self._draw_shot()
        elif kind == "shotmap":
            self._draw_shotmap()
        elif kind == "die":
            self._redraw_diemap()

    def _import_csv(self):
        path = filedialog.askopenfilename(
            title="Import CSV (Die Map)",
            filetypes=[("CSV or Excel", "*.csv *.xlsx *.xls"),
                      ("CSV", "*.csv"), ("Excel", "*.xlsx *.xls"),
                      ("All files", "*.*")])
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in (".xlsx", ".xls"):
            try:
                import openpyxl
            except ImportError:
                messagebox.showerror(
                    "Import Failed",
                    "openpyxl is required to import an .xlsx/.xls file here "
                    "(pip install openpyxl).")
                return
            try:
                wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
                ws = wb.active
                rows = [["" if v is None else str(v) for v in row]
                       for row in ws.iter_rows(values_only=True)]
            except Exception as exc:
                messagebox.showerror("Import Failed", f"Could not read {path}:\n{exc}")
                return
        else:
            try:
                with open(path, newline="", encoding="utf-8-sig") as fh:
                    rows = [r for r in csv.reader(fh)]
            except OSError as exc:
                messagebox.showerror("Import Failed", f"Could not read {path}:\n{exc}")
                return
        while rows and not any((c or "").strip() for c in rows[0]):
            rows.pop(0)
        while rows and not any((c or "").strip() for c in rows[-1]):
            rows.pop()
        if not rows:
            messagebox.showerror("Empty File", f"{os.path.basename(path)} has "
                                 "no data in it.")
            return

        self._import_diemap_csv(rows, os.path.basename(path))

    def _diemap_csv_cells(self, rows: List[List[str]]):
        shot_rows, shot_cols = self._shot_dims()
        out = []
        for r, row in enumerate(rows):
            for c in range(len(row)):
                text = (row[c] or "").strip()
                if not text:
                    continue
                out.append((r, c, text, r // shot_rows, c // shot_cols,
                           r % shot_rows, c % shot_cols))
        return out

    def _import_diemap_csv(self, rows: List[List[str]], name: str):
        shot_rows, shot_cols = self._shot_dims()
        max_row = len(rows) - 1
        max_col = max(len(r) for r in rows) - 1
        need_shot_r = max_row // shot_rows + 1
        need_shot_c = max_col // shot_cols + 1
        filled = self._diemap_csv_cells(rows)

        template_conflicts = [
            (r, c, text) for r, c, text, _sr, _sc, slr, slc in filled
            if not self._shot_cells.get((slr, slc), {}).get("present")]
        if template_conflicts:
            sample = ", ".join(f"row {r} col {c} ('{t}')"
                              for r, c, t in template_conflicts[:5])
            messagebox.showerror(
                "Shot/Die Map Mismatch",
                f"{len(template_conflicts)} die ID(s) in this CSV land on a "
                f"slot the Shot tab's current {shot_rows}x{shot_cols} "
                f"template marks blank (e.g. {sample}"
                f"{'…' if len(template_conflicts) > 5 else ''}).\n\n"
                "A shot map can't be safely worked out from a CSV that "
                "disagrees with Shot - define Shot to match this wafer "
                "first, then import again.")
            return

        existing_ok = all(
            self._shotmap_cells.get((sr, sc), False)
            for _r, _c, _text, sr, sc, _slr, _slc in filled)
        if existing_ok and self._shotmap_cells:
            cells = {(r, c): self._shotmap_cells.get((r, c), False)
                    for r in range(need_shot_r) for c in range(need_shot_c)}
            source = "the existing Shot Map"
        else:
            cells = {(r, c): False
                    for r in range(need_shot_r) for c in range(need_shot_c)}
            for _r, _c, _text, sr, sc, _slr, _slc in filled:
                cells[(sr, sc)] = True
            source = "the CSV's own blanks"

        align_ids = _find_alignment_ids(
            text for _r, _c, text, *_rest in filled
            if text.upper() not in ("SKIP", "ALIGN"))
        n = 0
        for r, c, text, sr, sc, slr, slc in filled:
            status = "normal"
            die_id = text
            if text.upper() == "SKIP":
                status, die_id = "skip", ""
            elif text.upper() == "ALIGN":
                status, die_id = "align", ""
            elif text in align_ids:
                status = "align"
            self._die_status[(sr, sc, slr, slc)] = {"die_id": die_id,
                                                    "status": status}
            n += 1
        self._shotmap_rows_var.set(str(need_shot_r))
        self._shotmap_cols_var.set(str(need_shot_c))
        self._shotmap_cells = cells
        self._draw_shotmap()
        self._redraw_diemap()
        self._log(f"[MAP] Imported Die Map from '{name}': {n} die(s) "
                 f"placed on a {shot_rows}x{shot_cols} shot grid "
                 f"(shot map from {source}).")
        self._sub_nb.select(2)

    def load_touchdowns_as_map(self, touchdowns: list, name: str, source_label: str,
                               save_as: Optional[str] = None):
        if not touchdowns:
            return
        save_path = None
        if save_as:
            folder = self._current_folder()
            d = self._maps_dir(create=True)
            target = self._safe_map_filename(save_as)
            if d and folder and target:
                save_path = os.path.join(d, target + ".json")
                if os.path.isfile(save_path):
                    if not messagebox.askyesno(
                            "Overwrite Map",
                            f"A Wafer Builder map named '{target}' already "
                            "exists.\n\nOverwrite it with this LOAD ALL's "
                            "wafer?"):
                        self._log(f"[MAP] LOAD ALL: kept the "
                                  f"existing map '{target}' - cancelled by "
                                  "the operator.")
                        return
                self.map_name_var.set(target)
            else:
                self._log("[MAP] LOAD ALL: could not save a "
                          f"named map for '{save_as}' - no ATA folder "
                          "loaded.")

        xs = sorted({t["x"] for t in touchdowns})
        ys = sorted({t["y"] for t in touchdowns})
        x_idx = {x: i for i, x in enumerate(xs)}
        y_idx = {y: i for i, y in enumerate(ys)}
        cells = {(y_idx[t["y"]], x_idx[t["x"]]): t["device_id"] for t in touchdowns}
        self._autofill_from_major_grid(cells, name, source_label)

        if save_path:
            try:
                with open(save_path, "w", encoding="utf-8") as f:
                    json.dump(self._state_to_dict(), f, indent=2)
                self._log(f"[MAP] Saved map '{self.map_name_var.get()}'")
                self._refresh_map_picker()
            except OSError as exc:
                self._log(f"[MAP] Could not save map: {exc}")

    def _import_pma(self):
        path = filedialog.askopenfilename(
            title="Load a .PMA recipe",
            filetypes=[("PMA recipe", "*.PMA"), ("All files", "*.*")])
        if not path:
            return
        try:
            fields = parse_pma_file(path)
            touchdowns = load_touchdowns(path, fields)
        except Exception as exc:
            messagebox.showerror("Import Failed", f"Could not read {path}:\n{exc}")
            return
        if not touchdowns:
            messagebox.showerror("Empty Recipe", "No touchdowns found — are "
                                 "the .PMV and .PMS siblings next to the .PMA?")
            return
        self.load_touchdowns_as_map(touchdowns, os.path.basename(path), "PMA recipe")

    def _import_recipe_gen_xls(self):
        if not _XLRD:
            messagebox.showerror("xlrd Not Installed",
                                 f"xlrd is not installed ({_XLRD_ERR}) — run:\n"
                                 "    .venv\\Scripts\\pip install xlrd")
            return
        path = filedialog.askopenfilename(
            title="Load a Recipe Generator (.xls)",
            filetypes=[("Excel 97-2003 Workbook", "*.xls"), ("All files", "*.*")])
        if not path:
            return
        try:
            book = xlrd.open_workbook(path, formatting_info=True)
            major_grid = read_moves_grid(book, "MajorMoves")
        except Exception as exc:
            messagebox.showerror("Import Failed", f"Could not read {path}:\n{exc}")
            return
        cells = {(s["row"], s["col"]): s["raw_text"] for s in major_grid["shots"]
                 if s["included"] and (s["raw_text"] or "").strip()}
        self._autofill_from_major_grid(cells, os.path.basename(path),
                                       "Recipe Generator workbook")

    def _autofill_from_major_grid(self, cells: Dict[tuple, str], name: str,
                                  source_label: str):
        if not cells:
            messagebox.showerror("Nothing to Import",
                                 f"{source_label} had no touchdowns.")
            return
        widest = 1
        die_lists = {}
        for rc, text in cells.items():
            parts = [d.strip() for d in text.replace(",", "/").split("/") if d.strip()]
            die_lists[rc] = parts or [text.strip()]
            widest = max(widest, len(die_lists[rc]))
        shot_rows, shot_cols = shot_geometry(widest, 0, 0)
        names = slot_names(shot_rows, shot_cols)
        grid = slot_grid(shot_rows, shot_cols)

        max_r = max(r for r, _ in cells)
        max_c = max(c for _, c in cells)
        shot_cells = {(r, c): {"present": True}
                      for r in range(shot_rows) for c in range(shot_cols)}
        shotmap_cells = {(r, c): (r, c) in cells
                         for r in range(max_r + 1) for c in range(max_c + 1)}
        align_ids = _find_alignment_ids(
            d for dies in die_lists.values() for d in dies)
        n = 0
        n_align = 0
        for (r, c), dies in die_lists.items():
            for i, die in enumerate(dies):
                if i >= len(names):
                    break
                slot_c, slot_r = grid[names[i]]
                text = die.strip()
                is_real = text.upper() not in ("NA", "")
                if not is_real:
                    die_id, status = "", "normal"
                elif text in align_ids:
                    die_id, status = text, "align"
                    n += 1
                    n_align += 1
                else:
                    die_id, status = text, "normal"
                    n += 1
                self._die_status[(r, c, slot_r, slot_c)] = {
                    "die_id": die_id, "status": status}

        self._shot_rows_var.set(str(shot_rows))
        self._shot_cols_var.set(str(shot_cols))
        self._shot_cells = shot_cells
        self._shotmap_rows_var.set(str(max_r + 1))
        self._shotmap_cols_var.set(str(max_c + 1))
        self._shotmap_cells = shotmap_cells
        self._draw_shot()
        self._draw_shotmap()
        self._redraw_diemap()
        align_note = f", {n_align} marked align" if n_align else ""
        self._log(f"[MAP] Imported '{name}' ({source_label}): "
                 f"{len(cells)} touchdown(s), {shot_rows}x{shot_cols} dies per "
                 f"touchdown, {n} die(s) with an ID{align_note}.")
        self._sub_nb.select(2)
