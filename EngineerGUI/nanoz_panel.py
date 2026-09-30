from __future__ import annotations
from map_nav import bind_middle_pan_mpl

import bisect
import collections
import csv
import datetime as dt
import os
import queue
import random
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from wafer_map_view import WaferMapPanel
from pma_wafer_panel import centroid_offset
from cassette_panel import save_yield_threshold, load_yield_threshold
from prober_debug_panel import ProberDebugPanel
from eg_prober_debug_panel import EgProberDebugPanel
import instruments.nanoz_board as nzb

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
    _MPL = True
except ImportError:
    _MPL = False

_Q_RESPONSE_RE = re.compile(r'Y\s*([+-]?\d+)\s*X\s*([+-]?\d+)')


def _parse_q_response(raw: str):
    raw = (raw or "").strip()
    m = _Q_RESPONSE_RE.search(raw)
    if m:
        return float(m.group(2)), float(m.group(1))
    parts = re.findall(r'[+-]?\d+\.?\d*', raw)
    if len(parts) >= 2:
        return float(parts[1]), float(parts[0])
    raise ValueError(f"Cannot parse Q response: {raw!r}")


class NanoZPanel(ttk.Frame):
    _CHIP_LABELS = {"0": "1 (right)", "1": "2 (left)"}
    _CHIP_LABEL_TO_VALUE = {v: k for k, v in _CHIP_LABELS.items()}

    def __init__(self, parent, controller, main_layout, system: str = "accretech"):
        super().__init__(parent)
        self.controller = controller
        self._main_layout = main_layout
        self._system = system

        self._boards: dict[str, nzb.NanoZBoard] = {}
        self._board_rows: dict[str, str] = {}
        self._board_label_to_port: dict[str, str] = {}
        self._queue: "queue.Queue" = queue.Queue()

        self._running = False
        self._run_mode: str | None = None
        self._lot_thread: threading.Thread | None = None
        self._current_rc = (None, None)
        self._eg_origin_offset: "tuple | None" = None
        self._xy_refresh_lock = threading.Lock()
        self._position_window_items: list = []
        self._position_window_dies: list = []
        self._touchdown_errors = 0
        self._touchdown_packets = 0
        self._spl_total = 0
        self._env_total = 0
        self._pass_count = 0
        self._fail_count = 0
        self._on_wafer_finished = None
        self._spl_path: str | None = None
        self._env_path: str | None = None
        self._latest_spl: dict[tuple[str, str], dict] = {}
        self._pf_metric_var = tk.StringVar(value="Current")
        self._pf_limit_vars: dict[int, tuple] = {
            s: (tk.StringVar(value=""), tk.StringVar(value="")) for s in (1, 2, 3, 4)
        }
        self._latest_env: dict[str, dict] = {}
        self._latest_eep: dict[str, dict] = {}
        self._spl_history: dict[tuple[str, str], "collections.deque"] = {}
        self._env_history: dict[str, "collections.deque"] = {}
        self._SETTLING_SKIP_COUNT = 2
        self._skip_spl_count: dict[tuple[str, str], int] = {}
        self._cycle_start_time: "dt.datetime | None" = None
        self._chart_follow_live = True
        self._chart_pinned_time: "dt.datetime | None" = None
        self._chart_t0_by_port: dict[str, "dt.datetime"] = {}
        self._mark_cycle_start()
        self._shots: list[dict] = []
        self._touchdowns: list[dict] = []
        self._recipe_name_var = tk.StringVar(value="")
        self._current_recipe_name: str | None = None
        self._wafer_plan: "nzb.WaferPlan | None" = None
        self._wafer_plan_path: str | None = None
        self._nzmap_dies_by_rc: dict[tuple[int, int], dict] = {}
        self._show_nzmap_labels_var = tk.BooleanVar(value=True)
        self._nzmap_label_artists: list = []
        self._nzmap_view_debounce_id = None
        self._nzmap_current_labels: list = []
        self._overlay_items: list = []

        self._build_ui()
        self.after(50, self._check_queue)
        self.after(300, self._refresh_charts_loop)
        self.after(500, self._refresh_results_loop)
        self.after(1000, self._auto_refresh_board_status)

    @property
    def _nanoz_ata_folder(self):
        return getattr(self._main_layout, "_ata_folder", None)

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        outer = ttk.PanedWindow(self, orient="vertical")
        outer.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self._outer_pane = outer

        sub_nb = ttk.Notebook(outer)
        self._sub_nb = sub_nb
        outer.add(sub_nb, weight=3)

        self._build_setup_tab(sub_nb)
        self._build_recipe_tab(sub_nb)
        self._build_run_tab(sub_nb)
        self._build_charts_tab(sub_nb)
        self._build_results_tab(sub_nb)
        if self._system == "accretech":
            self._build_cassette_tab(sub_nb)
        self._build_nanoz_ek_tab(sub_nb)
        self._build_prober_debug_tab(sub_nb)

        log_frame = ttk.LabelFrame(outer, text="NanoZ Log")
        outer.add(log_frame, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, bg="#1e1e1e", fg="#7CFC00",
                                font=("Consolas", 9), wrap="word", state="disabled", height=8)
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        log_sb.grid(row=0, column=1, sticky="ns", pady=2)
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(2, 0), pady=2)

    def _build_prober_debug_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Prober Debug")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        cls = EgProberDebugPanel if self._system == "electroglas" else ProberDebugPanel
        self.prober_debug = cls(tab, controller=self.controller)
        self.prober_debug.grid(row=0, column=0, sticky="nsew")

    def _make_scrollable_tab(self, nb, title: str) -> ttk.Frame:
        outer = ttk.Frame(nb)
        nb.add(outer, text=title)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))

        def _wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        canvas.bind("<MouseWheel>", _wheel)
        inner.bind("<MouseWheel>", _wheel)
        inner.nb_page = outer
        return inner

    def _build_setup_tab(self, nb):
        tab = self._make_scrollable_tab(nb, "Setup")
        tab.columnconfigure(0, weight=1)

        boards_lf = ttk.LabelFrame(tab, text="NanoZ Boards")
        boards_lf.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        brow = ttk.Frame(boards_lf)
        brow.pack(fill="x", padx=6, pady=(6, 2))
        self._btn_discover = ttk.Button(brow, text="Discover Boards", command=self._discover_boards)
        self._btn_discover.pack(side="left", padx=(0, 4))
        self._btn_connect_boards = ttk.Button(brow, text="Connect All", command=self._connect_boards)
        self._btn_connect_boards.pack(side="left", padx=4)
        self._btn_disconnect_boards = ttk.Button(brow, text="Disconnect Boards",
                                                 command=self._disconnect_boards)
        self._btn_disconnect_boards.pack(side="left", padx=4)
        self._btn_refresh_status = ttk.Button(brow, text="Refresh Status",
                                              command=self._refresh_board_status)
        self._btn_refresh_status.pack(side="left", padx=4)
        ttk.Separator(brow, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(brow, text="ENV interval (s):").pack(side="left")
        self.env_interval_var = tk.StringVar(value="1.0")
        ttk.Entry(brow, textvariable=self.env_interval_var, width=6).pack(side="left", padx=(4, 0))
        ttk.Separator(brow, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(brow, text="Number of dies:").pack(side="left")
        self._probe_height_var = tk.IntVar(value=nzb.DEFAULT_PROBE_HEIGHT)
        self._probe_height_spin = ttk.Spinbox(
            brow, from_=1, to=20, width=4, textvariable=self._probe_height_var,
            command=self._on_probe_height_change)
        self._probe_height_spin.pack(side="left", padx=(4, 0))
        self._probe_height_spin.bind("<Return>", lambda _e: self._on_probe_height_change())
        self._probe_height_spin.bind("<FocusOut>", lambda _e: self._on_probe_height_change())

        cols = ("port", "sn", "sig", "slot0", "slot1", "status")
        self._board_tree = ttk.Treeview(boards_lf, columns=cols, show="headings", height=11)
        heads = [("port", "Port", 70), ("sn", "S/N", 130),
                 ("sig", "Signature", 70), ("slot0", "Slot (chip 0)", 90),
                 ("slot1", "Slot (chip 1)", 90), ("status", "Status", 280)]
        for cid, text, width in heads:
            self._board_tree.heading(cid, text=text)
            self._board_tree.column(cid, width=width, anchor="center" if cid != "sn" else "w")
        self._board_tree.pack(fill="x", padx=6, pady=6)
        self._board_tree.bind("<Double-1>", self._on_board_tree_double_click)

        console_lf = ttk.LabelFrame(tab, text="Board Console")
        console_lf.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 8))
        console_lf.columnconfigure(0, weight=1)

        pick = ttk.Frame(console_lf)
        pick.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 4))
        ttk.Label(pick, text="Board:").pack(side="left")
        self.console_board_var = tk.StringVar(value="")
        self._console_board_label_var = tk.StringVar(value="")
        self._console_board_cb = ttk.Combobox(
            pick, textvariable=self._console_board_label_var, state="readonly", width=26)
        self._console_board_cb.pack(side="left", padx=(4, 12))
        self._console_board_cb.bind("<<ComboboxSelected>>", self._on_console_board_picked)
        ttk.Label(pick, text="Chip:").pack(side="left")
        self.console_chip_var = tk.StringVar(value="0")
        self._console_chip_label_var = tk.StringVar(value=self._CHIP_LABELS["0"])
        self._console_chip_cb = ttk.Combobox(
            pick, textvariable=self._console_chip_label_var, state="readonly", width=14,
            values=list(self._CHIP_LABELS.values()))
        self._console_chip_cb.pack(side="left", padx=(4, 2))
        self._console_chip_cb.bind("<<ComboboxSelected>>", self._on_console_chip_picked)

        cmds = ttk.LabelFrame(console_lf, text="Commands")
        cmds.grid(row=1, column=0, sticky="ew", padx=6)
        crow = ttk.Frame(cmds)
        crow.pack(fill="x", padx=6, pady=6)
        ttk.Button(crow, text="ver", width=10,
                  command=lambda: self._console_send("ver")).pack(side="left", padx=2)
        ttk.Button(crow, text="whoami", width=10,
                  command=lambda: self._console_send("whoami")).pack(side="left", padx=2)
        ttk.Button(crow, text="#env?", width=10,
                  command=lambda: self._console_send("#env?")).pack(side="left", padx=2)
        ttk.Button(crow, text="calib ?", width=10,
                  command=lambda: self._console_send("calib ?")).pack(side="left", padx=2)
        ttk.Button(crow, text="calib!", width=10,
                  command=self._console_calib_bang).pack(side="left", padx=2)
        ttk.Button(crow, text="cleep", width=10,
                  command=self._console_cleep).pack(side="left", padx=2)

        ttk.Separator(crow, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(crow, text="Cycle #:").pack(side="left")
        self.console_cycle_var = tk.StringVar(value="0")
        ttk.Entry(crow, textvariable=self.console_cycle_var, width=5).pack(side="left", padx=(4, 8))
        ttk.Button(crow, text="run", command=self._console_run).pack(side="left", padx=2)
        ttk.Button(crow, text="pause",
                  command=lambda: self._console_send("pause")).pack(side="left", padx=2)

        ttk.Separator(crow, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(crow, text="Raw command:").pack(side="left")
        self.console_raw_var = tk.StringVar(value="")
        ttk.Entry(crow, textvariable=self.console_raw_var, width=16).pack(side="left", padx=(4, 4))
        ttk.Button(crow, text="Send", command=self._console_send_raw).pack(side="left", padx=2)

        ttk.Separator(crow, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(crow, text="Read EEPROM — addr:").pack(side="left")
        self.console_eep_addr_var = tk.StringVar(value="0")
        ttk.Entry(crow, textvariable=self.console_eep_addr_var, width=8).pack(
            side="left", padx=(4, 8))
        ttk.Label(crow, text="len:").pack(side="left")
        self.console_eep_len_var = tk.StringVar(value="64")
        ttk.Entry(crow, textvariable=self.console_eep_len_var, width=6).pack(
            side="left", padx=(4, 8))
        ttk.Button(crow, text="Read", command=self._console_read_eeprom).pack(side="left", padx=2)

        reading_lf = ttk.LabelFrame(console_lf, text="Latest Reading")
        reading_lf.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        reading_lf.columnconfigure(0, weight=1)

        reading_split = ttk.PanedWindow(reading_lf, orient="horizontal")
        reading_split.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

        spl_frame = ttk.Frame(reading_split)
        reading_split.add(spl_frame, weight=1)
        spl_frame.rowconfigure(1, weight=1)
        spl_frame.columnconfigure(0, weight=1)
        ttk.Label(spl_frame, text="SPL", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w")
        self.console_spl_text = tk.Text(spl_frame, wrap="none", state="disabled",
                                        height=14, font=("Consolas", 9))
        self.console_spl_text.grid(row=1, column=0, sticky="nsew")
        spl_sb = ttk.Scrollbar(spl_frame, orient="vertical", command=self.console_spl_text.yview)
        spl_sb.grid(row=1, column=1, sticky="ns")
        self.console_spl_text.configure(yscrollcommand=spl_sb.set)

        env_frame = ttk.Frame(reading_split)
        reading_split.add(env_frame, weight=1)
        env_frame.rowconfigure(1, weight=1)
        env_frame.columnconfigure(0, weight=1)
        ttk.Label(env_frame, text="ENV", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w")
        self.console_env_text = tk.Text(env_frame, wrap="none", state="disabled",
                                        height=14, font=("Consolas", 9))
        self.console_env_text.grid(row=1, column=0, sticky="nsew")
        env_sb = ttk.Scrollbar(env_frame, orient="vertical", command=self.console_env_text.yview)
        env_sb.grid(row=1, column=1, sticky="ns")
        self.console_env_text.configure(yscrollcommand=env_sb.set)

        eep_frame = ttk.Frame(reading_split)
        reading_split.add(eep_frame, weight=1)
        eep_frame.rowconfigure(1, weight=1)
        eep_frame.columnconfigure(0, weight=1)
        ttk.Label(eep_frame, text="EEPROM (hex)", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w")
        self.console_eep_text = tk.Text(eep_frame, wrap="word", state="disabled",
                                        height=14, font=("Consolas", 9))
        self.console_eep_text.grid(row=1, column=0, sticky="nsew")
        eep_sb = ttk.Scrollbar(eep_frame, orient="vertical", command=self.console_eep_text.yview)
        eep_sb.grid(row=1, column=1, sticky="ns")
        self.console_eep_text.configure(yscrollcommand=eep_sb.set)

    def _build_recipe_tab(self, nb):
        tab = self._make_scrollable_tab(nb, "Recipe")
        self._recipe_tab = tab.nb_page
        tab.columnconfigure(0, weight=1)

        name_row = ttk.Frame(tab)
        name_row.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 4))
        ttk.Label(name_row, text="Recipe:").pack(side="left")
        self._recipe_name_cb = ttk.Combobox(
            name_row, textvariable=self._recipe_name_var, state="readonly", width=26)
        self._recipe_name_cb.pack(side="left", padx=(4, 4))
        self._recipe_name_cb.bind(
            "<<ComboboxSelected>>", lambda _e: self._load_named_recipe())
        ttk.Button(name_row, text="＋ New", command=self._new_named_recipe).pack(
            side="left", padx=2)
        ttk.Button(name_row, text="✎ Rename", command=self._rename_named_recipe).pack(
            side="left", padx=2)
        ttk.Button(name_row, text="Delete", command=self._delete_named_recipe).pack(
            side="left", padx=2)
        ttk.Separator(name_row, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(name_row, text="💾 Save", command=self._save_named_recipe).pack(
            side="left", padx=2)
        self._recipe_active_lbl = ttk.Label(name_row, text="(no recipe saved yet)",
                                            foreground="#6b7280")
        self._recipe_active_lbl.pack(side="left", padx=(12, 0))


        td_lf = ttk.LabelFrame(tab, text="Touchdowns (which dies this recipe probes)", padding=6)
        td_lf.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 4))
        td_lf.columnconfigure(0, weight=1)

        self._nz_td_var = tk.StringVar()

        td_bar = ttk.Frame(td_lf)
        td_bar.grid(row=0, column=0, sticky="ew", pady=(4, 4))
        ttk.Button(td_bar, text="⬅ Take from map selection",
                  command=self._nz_td_from_map).pack(side="left")
        ttk.Button(td_bar, text="Take die IDs",
                  command=self._nz_td_from_die_ids).pack(side="left", padx=(6, 0))
        ttk.Button(td_bar, text="➡ Push to map",
                  command=self._nz_td_to_map).pack(side="left", padx=(6, 0))
        ttk.Button(td_bar, text="Remove selected",
                  command=self._nz_td_remove).pack(side="left", padx=(16, 0))
        ttk.Button(td_bar, text="Clear all",
                  command=self._nz_td_clear).pack(side="left", padx=(6, 0))
        ttk.Button(td_bar, text="🔎 Find all",
                  command=self._nz_td_find_all).pack(side="left", padx=(16, 0))
        self._nz_td_find_var = tk.StringVar(value="")
        ttk.Entry(td_bar, textvariable=self._nz_td_find_var, width=14).pack(
            side="left", padx=(4, 0))
        ttk.Separator(td_bar, orient="vertical").pack(side="left", fill="y", padx=12)
        self._btn_compute_recipe = ttk.Button(td_bar, text="🧮 Compute Recipe",
                                              command=self._compute_recipe)
        self._btn_compute_recipe.pack(side="left")

        td_cols = ("n", "die_id", "row", "col")
        self._nz_td_tree = ttk.Treeview(td_lf, columns=td_cols, show="headings", height=6)
        for cid, text, width, anchor in (("n", "#", 40, "center"),
                                         ("die_id", "Die ID", 220, "w"),
                                         ("row", "Row", 60, "center"),
                                         ("col", "Col", 60, "center")):
            self._nz_td_tree.heading(cid, text=text)
            self._nz_td_tree.column(cid, width=width, anchor=anchor, stretch=(cid == "die_id"))
        self._nz_td_tree.grid(row=1, column=0, sticky="ew")
        td_sb = ttk.Scrollbar(td_lf, orient="vertical", command=self._nz_td_tree.yview)
        td_sb.grid(row=1, column=1, sticky="ns")
        self._nz_td_tree.configure(yscrollcommand=td_sb.set)

        bar = ttk.Frame(tab)
        bar.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 4))
        self._btn_recipe_up = ttk.Button(bar, text="▲", width=3,
                                         command=lambda: self._move_shot(-1))
        self._btn_recipe_up.pack(side="left", padx=(0, 2))
        self._btn_recipe_down = ttk.Button(bar, text="▼", width=3,
                                           command=lambda: self._move_shot(1))
        self._btn_recipe_down.pack(side="left", padx=2)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self._btn_recipe_enable_all = ttk.Button(bar, text="Enable All Boards",
                                                 command=lambda: self._set_selected_boards(True))
        self._btn_recipe_enable_all.pack(side="left", padx=4)
        self._btn_recipe_disable_all = ttk.Button(bar, text="Disable All Boards",
                                                  command=lambda: self._set_selected_boards(False))
        self._btn_recipe_disable_all.pack(side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self._recipe_boards_lbl = ttk.Label(bar, text="", foreground="#6b7280")
        self._recipe_boards_lbl.pack(side="left", padx=(12, 0))

        tree_frame = ttk.Frame(tab)
        tree_frame.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 8))
        tree_frame.columnconfigure(0, weight=1)
        self._recipe_tree = ttk.Treeview(tree_frame, columns=("seq",), show="headings", height=8,
                                         selectmode="extended")
        self._recipe_tree.grid(row=0, column=0, sticky="ew")
        rvsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._recipe_tree.yview)
        rvsb.grid(row=0, column=1, sticky="ns")
        rhsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._recipe_tree.xview)
        rhsb.grid(row=1, column=0, sticky="ew")
        self._recipe_tree.configure(yscrollcommand=rvsb.set, xscrollcommand=rhsb.set)
        self._recipe_tree.bind("<Button-1>", self._on_recipe_click)
        self._recipe_tree.bind("<Double-1>", self._on_recipe_double_click)

        pf_lf = ttk.LabelFrame(tab, text="Pass/Fail Limits")
        pf_lf.grid(row=6, column=0, sticky="ew", padx=8, pady=(0, 8))
        pf_row = ttk.Frame(pf_lf)
        pf_row.pack(fill="x", padx=6, pady=6)
        ttk.Label(pf_row, text="Metric:").pack(side="left")
        ttk.Combobox(pf_row, textvariable=self._pf_metric_var, state="readonly", width=10,
                    values=("Current", "Resistance")).pack(side="left", padx=(4, 4))
        self._pf_unit_var = tk.StringVar(value="")
        ttk.Label(pf_row, textvariable=self._pf_unit_var, foreground="#6b7280").pack(
            side="left", padx=(0, 16))
        self._pf_metric_var.trace_add("write", lambda *_: self._pf_unit_var.set(
            f"({self._SENSOR_METRIC_UNITS.get(self._pf_metric_var.get(), '')})"))
        self._pf_unit_var.set(f"({self._SENSOR_METRIC_UNITS.get(self._pf_metric_var.get(), '')})")
        for s in (1, 2, 3, 4):
            ttk.Label(pf_row, text=f"S{s}:").pack(side="left", padx=(0, 2))
            mn_var, mx_var = self._pf_limit_vars[s]
            ttk.Entry(pf_row, textvariable=mn_var, width=7).pack(side="left")
            ttk.Label(pf_row, text="–").pack(side="left", padx=2)
            ttk.Entry(pf_row, textvariable=mx_var, width=7).pack(side="left", padx=(0, 12))
        self._rebuild_recipe_columns()

    def _recipe_ports(self) -> list:
        return sorted(self._boards.keys())

    def _board_label(self, port: str) -> str:
        board = self._boards.get(port)
        ident = board.identity if board else None
        sn = ident.serial_number if ident and ident.serial_number else ""
        real_port = (ident.port if ident else "") or "not yet connected"
        label = f"SN {sn} ({real_port})" if sn else port
        if ident and (ident.slot0 or ident.slot1):
            s0 = ident.slot0 if ident.slot0 else "—"
            s1 = ident.slot1 if ident.slot1 else "—"
            label += f" · slots {s0}/{s1}"
        return label

    def _port_header(self, port: str) -> str:
        return self._board_label(port)

    def _rebuild_recipe_columns(self):
        ports = self._recipe_ports()
        cols = ("seq", "label", "active") + tuple(ports)
        self._recipe_tree.configure(columns=cols)
        heads = [("seq", "#", 36), ("label", "Label", 220), ("active", "Active", 60)]
        heads += [(p, self._port_header(p), 100) for p in ports]
        for cid, text, width in heads:
            self._recipe_tree.heading(cid, text=text)
            self._recipe_tree.column(cid, width=width, anchor="w" if cid == "label" else "center")
        self._recipe_boards_lbl.config(
            text=f"{len(ports)} board(s) known" if ports
            else "no boards known yet — discover/connect on the Setup tab first")
        self._redraw_recipe_tree()

    def _redraw_recipe_tree(self):
        for iid in self._recipe_tree.get_children():
            self._recipe_tree.delete(iid)
        ports = self._recipe_ports()
        for i, shot in enumerate(self._shots, 1):
            excluded = shot["excluded_boards"]
            active_n = sum(1 for p in ports if p not in excluded)
            vals = [str(i), shot["label"] or f"Shot {i}", f"{active_n}/{len(ports)}"]
            vals += ["·" if p in excluded else "✓" for p in ports]
            self._recipe_tree.insert("", "end", values=vals)
        self._redraw_touchdown_list()

    def _redraw_touchdown_list(self):
        tree = getattr(self, "_touchdown_tree", None)
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        ports = self._recipe_ports()
        for i, shot in enumerate(self._shots):
            excluded = shot["excluded_boards"]
            active_n = sum(1 for p in ports if p not in excluded)
            tree.insert("", "end", iid=str(i), values=(
                str(i + 1), shot["label"] or f"Shot {i + 1}", f"{active_n}/{len(ports)}"))

    def _select_touchdown_row(self, idx: int):
        tree = getattr(self, "_touchdown_tree", None)
        if tree is None:
            return
        iid = str(idx)
        if tree.exists(iid):
            tree.selection_set(iid)
            tree.see(iid)

    def _on_touchdown_double_click(self, event):
        tree = self._touchdown_tree
        iid = tree.identify_row(event.y)
        if not iid:
            return
        self._goto_shot(int(iid))

    def _selected_shot_indices(self) -> list:
        return sorted(self._recipe_tree.index(iid) for iid in self._recipe_tree.selection())

    def _selected_shot_index(self):
        idxs = self._selected_shot_indices()
        return idxs[0] if idxs else None

    def _persist_recipe(self):
        folder = self._nanoz_ata_folder
        if not folder or not self._current_recipe_name:
            return
        try:
            nzb.save_named_recipe(folder, self._current_recipe_name, self._shots,
                                  wafer_plan_path=self._wafer_plan_path,
                                  touchdowns=self._touchdowns)
        except OSError as e:
            self._log(f"Could not save NanoZ recipe: {e}")

    def _refresh_recipe_name_cb(self):
        folder = self._nanoz_ata_folder
        names = nzb.list_recipe_names(folder) if folder else []
        self._recipe_name_cb.config(values=names)
        self._run_recipe_name_cb.config(values=names)
        self._recipe_name_var.set(self._current_recipe_name or "")
        active_text = (f"active: {self._current_recipe_name}" if self._current_recipe_name
                      else "(unsaved — ＋ New to save this recipe)")
        self._recipe_active_lbl.config(text=active_text)
        self._run_recipe_active_lbl.config(text=active_text)

    def _new_named_recipe(self):
        folder = self._nanoz_ata_folder
        if not folder:
            messagebox.showerror("No ATA Folder",
                                 "Load an ATA folder from the toolbar first.")
            return
        name = simpledialog.askstring("New Recipe", "Recipe name:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in nzb.list_recipe_names(folder):
            messagebox.showerror("Duplicate", f"Recipe '{name}' already exists.")
            return
        nzb.save_named_recipe(folder, name, self._shots, wafer_plan_path=self._wafer_plan_path,
                              touchdowns=self._touchdowns)
        self._current_recipe_name = name
        nzb.set_active_recipe(folder, name)
        self._refresh_recipe_name_cb()
        self._log_main(f"Created recipe '{name}' — {len(self._shots)} shot(s)"
                       + (" (copy of whatever was loaded)." if self._shots else "."))

    def _rename_named_recipe(self):
        folder = self._nanoz_ata_folder
        old_name = self._current_recipe_name
        if not folder or not old_name:
            messagebox.showerror("No Recipe Loaded",
                                 "Load (or ＋ New) a recipe first — there is nothing "
                                 "named yet to rename.")
            return
        new_name = simpledialog.askstring("Rename Recipe", "New recipe name:",
                                          initialvalue=old_name, parent=self)
        if not new_name or new_name == old_name:
            return
        new_name = new_name.strip()
        if not new_name:
            return
        if new_name in nzb.list_recipe_names(folder):
            messagebox.showerror("Duplicate", f"Recipe '{new_name}' already exists.")
            return
        nzb.save_named_recipe(folder, new_name, self._shots, wafer_plan_path=self._wafer_plan_path,
                              touchdowns=self._touchdowns)
        nzb.delete_named_recipe(folder, old_name)
        self._current_recipe_name = new_name
        nzb.set_active_recipe(folder, new_name)
        self._refresh_recipe_name_cb()
        self._log_main(f"Renamed '{old_name}' -> '{new_name}'.")

    def _save_named_recipe(self):
        folder = self._nanoz_ata_folder
        if not folder:
            messagebox.showerror("No ATA Folder",
                                 "Load an ATA folder from the toolbar first.")
            return
        if not self._current_recipe_name:
            self._new_named_recipe()
            return
        nzb.save_named_recipe(folder, self._current_recipe_name, self._shots,
                              wafer_plan_path=self._wafer_plan_path, touchdowns=self._touchdowns)
        self._refresh_recipe_name_cb()
        self._log_main(f"Saved '{self._current_recipe_name}' — {len(self._shots)} shot(s).")

    def _load_named_recipe(self, name: str | None = None):
        folder = self._nanoz_ata_folder
        if not folder:
            messagebox.showerror("No ATA Folder",
                                 "Load an ATA folder from the toolbar first.")
            return
        name = name or self._recipe_name_var.get()
        if not name:
            messagebox.showinfo("No Recipe Selected", "Pick a recipe from the dropdown first.")
            return
        self._shots = nzb.load_named_recipe(folder, name)
        self._touchdowns = nzb.load_named_touchdowns(folder, name)
        self._current_recipe_name = name
        nzb.set_active_recipe(folder, name)
        self._redraw_recipe_tree()
        self._nz_refresh_td()
        self._refresh_recipe_name_cb()
        self._log_main(f"Recipe '{name}' loaded — {len(self._shots)} shot(s), "
                       f"{len(self._touchdowns)} touchdown(s).")
        if self._touchdowns:
            self._nz_td_to_map()
        self._autoload_wafer_plan_for_recipe(folder, name)

    def _autoload_wafer_plan_for_recipe(self, folder: str, name: str):
        path = nzb.get_recipe_wafer_plan_path(folder, name)
        if path:
            self._autoload_wafer_plan(path, note=f"Recipe '{name}' remembers wafer plan ")

    def _autoload_wafer_plan(self, path: str, note: str = "Remembered wafer plan "):
        if not path:
            return
        if not os.path.isfile(path):
            self._log_main(f"{note}'{os.path.basename(path)}' but that file is no longer there — "
                           "Wafer Map tab left as-is.")
            return
        probe_height = self._probe_height()
        threading.Thread(target=self._autoload_wafer_plan_thread, args=(path, probe_height),
                         daemon=True).start()

    def _autoload_wafer_plan_thread(self, path: str, probe_height: int):
        try:
            plan = nzb.load_wafer_plan(path, probe_height=probe_height)
        except Exception as e:
            self.after(0, lambda e=e: self._log_main(
                f"Could not auto-reload wafer plan '{os.path.basename(path)}': {e}"))
            return

        def _finish():
            self._wafer_plan = plan
            self._wafer_plan_path = path
            lbl = getattr(self, "_recipe_plan_status_lbl", None)
            if lbl is not None:
                lbl.config(text=f"{os.path.basename(path)} — {len(plan.dies)} die(s), "
                                f"{len(plan.touchdowns)} touchdown(s)", foreground="black")
            self._log_main(f"Wafer map auto-loaded from '{os.path.basename(path)}'.")
        self.after(0, _finish)

    def _delete_named_recipe(self):
        folder = self._nanoz_ata_folder
        name = self._recipe_name_var.get()
        if not folder or not name:
            return
        if not messagebox.askyesno("Delete Recipe",
                                   f"Delete recipe '{name}'? This cannot be undone."):
            return
        nzb.delete_named_recipe(folder, name)
        if self._current_recipe_name == name:
            self._current_recipe_name = None
        self._refresh_recipe_name_cb()
        self._log_main(f"Recipe '{name}' deleted.")

    def _move_shot(self, delta: int):
        idx = self._selected_shot_index()
        if idx is None:
            return
        new_idx = idx + delta
        if not (0 <= new_idx < len(self._shots)):
            return
        self._shots[idx], self._shots[new_idx] = self._shots[new_idx], self._shots[idx]
        self._redraw_recipe_tree()
        self._persist_recipe()
        children = self._recipe_tree.get_children()
        self._recipe_tree.selection_set(children[new_idx])
        self._recipe_tree.see(children[new_idx])

    def _rename_shot(self, idx: int):
        if not (0 <= idx < len(self._shots)):
            return
        current = self._shots[idx]["label"]
        new = simpledialog.askstring("Rename Shot", "Label for this shot:",
                                     initialvalue=current, parent=self)
        if new is None:
            return
        self._shots[idx]["label"] = new.strip()
        self._redraw_recipe_tree()
        children = self._recipe_tree.get_children()
        if 0 <= idx < len(children):
            self._recipe_tree.selection_set(children[idx])
        self._persist_recipe()

    def _toggle_shot_board(self, idx: int, port: str):
        if not (0 <= idx < len(self._shots)):
            return
        excluded = self._shots[idx]["excluded_boards"]
        if port in excluded:
            excluded.discard(port)
        else:
            excluded.add(port)
        self._redraw_recipe_tree()
        children = self._recipe_tree.get_children()
        if 0 <= idx < len(children):
            self._recipe_tree.selection_set(children[idx])
        self._persist_recipe()

    def _set_selected_boards(self, included: bool):
        idxs = self._selected_shot_indices()
        if not idxs:
            self._log_main("Select at least one shot first.")
            return
        ports = self._recipe_ports()
        for i in idxs:
            self._shots[i]["excluded_boards"] = set() if included else set(ports)
        self._redraw_recipe_tree()
        children = self._recipe_tree.get_children()
        self._recipe_tree.selection_set([children[i] for i in idxs if 0 <= i < len(children)])
        self._persist_recipe()

    def _on_recipe_click(self, event):
        if self._recipe_tree.identify_region(event.x, event.y) != "cell":
            return
        row_iid = self._recipe_tree.identify_row(event.y)
        col_id = self._recipe_tree.identify_column(event.x)
        if not row_iid or not col_id:
            return
        cols = self._recipe_tree["columns"]
        col_idx = int(col_id[1:]) - 1
        if not (0 <= col_idx < len(cols)):
            return
        port = cols[col_idx]
        if port not in self._recipe_ports():
            return
        self._toggle_shot_board(self._recipe_tree.index(row_iid), port)

    def _on_recipe_double_click(self, event):
        if self._recipe_tree.identify_region(event.x, event.y) != "cell":
            return
        row_iid = self._recipe_tree.identify_row(event.y)
        col_id = self._recipe_tree.identify_column(event.x)
        if not row_iid or not col_id:
            return
        cols = self._recipe_tree["columns"]
        col_idx = int(col_id[1:]) - 1
        if not (0 <= col_idx < len(cols)) or cols[col_idx] != "label":
            return
        self._rename_shot(self._recipe_tree.index(row_iid))

    def _compute_recipe(self):
        if self._system == "electroglas":
            self._eg_refresh_wafer_plan_from_wafer_builder(silent=True)
        if not self._wafer_plan:
            messagebox.showerror(
                "No Wafer Plan",
                "Compute Recipe needs a wafer plan to tell product dies apart from "
                "reference/alignment dies and off-wafer positions, and none is "
                "available for this ATA folder."
                + ("" if self._system == "electroglas" else
                   " Import one on the Wafer Builder tab, or place a wafer plan "
                   f".xlsx in this folder ({nzb.WAFER_PLAN_XLSX_FILENAME})."))
            return
        sites = sorted(((t["row"], t["col"]) for t in self._touchdowns),
                       key=lambda rc: (rc[0], rc[1]))
        if not sites:
            messagebox.showerror(
                "No Touchdowns",
                "The touchdown list above is empty. Pick dies on the Run tab's map, "
                "then ⬅ Take from map selection (or 🏷 Take die IDs / 🔎 Find all), "
                "then Compute Recipe.")
            return
        if self._shots and not messagebox.askyesno(
            "Replace Recipe",
            f"This will replace the current recipe ({len(self._shots)} shot(s)) with "
            f"{len(sites)} shot(s) computed from the selected dies. Continue?"):
            return
        ports = self._recipe_ports()
        slots_by_port = {p: self._boards[p].identity.chip_slots() for p in ports}
        row_off, col_off = self._wafer_plan_offset()
        shots = nzb.build_shots_from_windows(self._wafer_plan, sites, ports, slots_by_port,
                                             row_off, col_off)
        self._shots = shots
        active_name = self._current_recipe_name
        if active_name:
            self._persist_recipe()
        self._redraw_recipe_tree()
        self._refresh_recipe_name_cb()
        self._sub_nb.select(self._recipe_tab)
        self._log_main(
            f"Compute Recipe: built {len(shots)} shot(s) from {len(sites)} selected die(s)"
            + (f" and saved to '{active_name}'." if active_name
               else " — not saved yet."))


    def _nz_refresh_td(self):
        tree = self._nz_td_tree
        for iid in tree.get_children():
            tree.delete(iid)
        for i, t in enumerate(self._touchdowns, 1):
            tree.insert("", "end", values=(
                i, t.get("die_id", "") or "—", t.get("row", ""), t.get("col", "")))
        n = len(self._touchdowns)
        if not n:
            self._nz_td_var.set(
                "No touchdowns yet — pick dies on the Run tab's map, then "
                "⬅ Take from map selection.")
        else:
            named = sum(1 for t in self._touchdowns if t.get("die_id"))
            self._nz_td_var.set(
                f"{n} touchdown{'' if n == 1 else 's'} — press 🧮 Compute Recipe to build "
                f"the recipe from these. {named} carry a die ID.")

    def _nz_td_set(self, touchdowns: list, verb: str):
        self._touchdowns[:] = touchdowns
        self._nz_refresh_td()
        if self._current_recipe_name:
            self._persist_recipe()
        self._log_main(
            f"Touchdown list {verb} {len(touchdowns)} die(s)"
            + (f" — saved to '{self._current_recipe_name}'." if self._current_recipe_name
               else " — not saved yet."))

    def _nz_td_from_map(self):
        picks = list(self.wafer_map.get_picked())
        if not picks:
            messagebox.showinfo(
                "Touchdowns",
                "No dies are selected on the Run tab's map.")
            return
        picks.sort()
        touchdowns = [{"die_id": self.wafer_map.die_ids.get(rc, ""),
                       "row": rc[0], "col": rc[1]} for rc in picks]
        self._nz_td_set(touchdowns, "set to")

    def _nz_td_from_die_ids(self):
        ided = {rc: did for rc, did in self.wafer_map.die_ids.items() if did}
        if not ided:
            messagebox.showinfo(
                "Touchdowns",
                "No dies on the loaded map carry a die ID.")
            return
        picks = sorted(ided.keys())
        touchdowns = [{"die_id": ided[rc], "row": rc[0], "col": rc[1]} for rc in picks]
        self._nz_td_set(touchdowns, "set to")

    def _nz_td_find_all(self):
        target = (self._nz_td_find_var.get() or "").strip()
        if not target:
            messagebox.showinfo("Touchdowns", "Type a die ID to search for first.")
            return
        picks = sorted(rc for rc, did in self.wafer_map.die_ids.items() if did == target)
        if not picks:
            messagebox.showinfo("Touchdowns",
                                f"No dies on the loaded map are labeled '{target}'.")
            return
        touchdowns = [{"die_id": target, "row": rc[0], "col": rc[1]} for rc in picks]
        self._nz_td_set(touchdowns, "set to")

    def _nz_td_to_map(self):
        if not self._touchdowns:
            messagebox.showinfo("Touchdowns", "This recipe has no touchdowns yet.")
            return
        picks = [(t["row"], t["col"]) for t in self._touchdowns]
        missing = [rc for rc in picks if rc not in self.wafer_map.dies]
        self.wafer_map.set_picked(picks)
        self._on_sites_changed(picks)
        note = (f" ({len(missing)} not on the loaded map)" if missing else "")
        self._log_main(f"Highlighted {len(picks)} touchdown(s) on the Run map.{note}")

    def _nz_td_remove(self):
        sel = self._nz_td_tree.selection()
        if not sel:
            return
        for idx in sorted((self._nz_td_tree.index(i) for i in sel), reverse=True):
            if 0 <= idx < len(self._touchdowns):
                del self._touchdowns[idx]
        self._nz_refresh_td()
        if self._current_recipe_name:
            self._persist_recipe()

    def _nz_td_clear(self):
        if not self._touchdowns:
            return
        if not messagebox.askokcancel(
                "Clear touchdowns",
                f"Remove all {len(self._touchdowns)} touchdown(s)?"):
            return
        self._touchdowns.clear()
        self._nz_refresh_td()
        if self._current_recipe_name:
            self._persist_recipe()

    def _eg_refresh_wafer_plan_from_wafer_builder(self, silent: bool = False):
        def _warn(msg):
            if silent:
                self._log_main(f"Wafer Builder auto-refresh: {msg}")
            else:
                messagebox.showwarning("Wafer Builder", msg)
        gen = getattr(self._main_layout, "recipe_gen", None)
        if gen is None:
            _warn("Wafer Builder tab not available.")
            return
        try:
            dpx, dpy = gen._die_pitch()
        except Exception:
            dpx = dpy = None
        if not dpx or not dpy:
            _warn("No Wafer Builder die map yet.")
            return
        dies, serial_to_rc = {}, {}
        for d in gen._die_positions():
            if d.get("status") != "normal" or not d.get("die_id"):
                continue
            row, col = round(d["y"] / dpy), round(d["x"] / dpx)
            serial = str(d["die_id"])
            dies[(row, col)] = {"serial": serial, "status": "product"}
            serial_to_rc[serial.upper()] = (row, col)
        if not dies:
            _warn("No Wafer Builder die map yet.")
            return
        probe_height = self._probe_height()
        by_col: dict = {}
        for (row, col) in dies:
            by_col.setdefault(col, []).append(row)
        touchdowns = []
        for col, rows in sorted(by_col.items()):
            rows.sort()
            start = rows[0]
            while start <= rows[-1]:
                touchdowns.append((start, col))
                start += probe_height
        plan = nzb.WaferPlan(dies=dies, serial_to_rc=serial_to_rc,
                             touchdowns=touchdowns, probe_height=probe_height)
        self._wafer_plan = plan
        self._wafer_plan_path = None
        stats = nzb.wafer_plan_stats(plan)
        status = (f"Wafer Builder — {len(plan.dies)} die(s), "
                 f"{len(plan.touchdowns)} touchdown(s), probe head {plan.probe_height} — "
                 f"{stats['product']} product, {stats['reference']} reference, "
                 f"{stats['off_wafer']} off-wafer")
        lbl = getattr(self, "_recipe_plan_status_lbl", None)
        if lbl is not None:
            lbl.config(text=status, foreground="black")
        self._log_main(f"Wafer data refreshed: {len(plan.dies)} die(s), "
                       f"{len(plan.touchdowns)} touchdown(s).")
        self.refresh_eg_anchor_choices()

    def _build_wafer_map_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Wafer Map")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        src_row = ttk.Frame(tab)
        src_row.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 4))
        ttk.Label(src_row, text="View: Run Tab").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(src_row, text="🏷 Die Labels", variable=self._show_nzmap_labels_var,
                       command=self._update_visible_nzmap_labels).pack(side="left", padx=(12, 0))

        if _MPL:
            self._nzmap_fig = Figure(figsize=(8, 8), dpi=100)
            self._nzmap_ax = self._nzmap_fig.add_subplot(111)
            self._nzmap_ax.set_aspect("equal")
            self._nzmap_canvas = FigureCanvasTkAgg(self._nzmap_fig, master=tab)
            self._nzmap_canvas.get_tk_widget().grid(
                row=2, column=0, sticky="nsew", padx=8, pady=(0, 0))
            toolbar = NavigationToolbar2Tk(self._nzmap_canvas, tab, pack_toolbar=False)
            toolbar.update()
            toolbar.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 4))
            self._nzmap_canvas.mpl_connect("button_press_event", self._on_nzmap_click)
            self._nzmap_canvas.mpl_connect("scroll_event", self._on_nzmap_scroll_zoom)
            bind_middle_pan_mpl(self._nzmap_canvas, lambda: self._nzmap_ax)

            info_lf = ttk.LabelFrame(tab, text="Selected Die")
            info_lf.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 8))
            self._nzmap_die_var = tk.StringVar(value="Click a die to see its row/col/serial.")
            ttk.Label(info_lf, textvariable=self._nzmap_die_var,
                     font=("Consolas", 10)).pack(anchor="w", padx=6, pady=6)
            self._draw_empty_nzmap()
        else:
            ttk.Label(tab, text="matplotlib not installed — install it to view the wafer map.",
                     foreground="red").grid(row=2, column=0, sticky="nw", padx=10, pady=10)

    def _draw_empty_nzmap(self, message: str | None = None):
        if not _MPL:
            return
        self._nzmap_current_labels = []
        self._nzmap_label_artists = []
        self._nzmap_ax.clear()
        self._nzmap_ax.set_aspect("equal")
        self._nzmap_ax.text(
            0.5, 0.5, message or "No wafer plan imported yet — "
                                 "Recipe tab → Import Wafer Plan (.xlsx)",
            ha="center", va="center", transform=self._nzmap_ax.transAxes, color="#999999")
        self._nzmap_canvas.draw_idle()

    def _redraw_nanoz_wafer_map(self):
        if not _MPL:
            return
        self._nzmap_dies_by_rc = {}
        self._draw_run_map_nzmap()

    def _draw_run_map_nzmap(self):
        wm = getattr(self._main_layout, "_exec_wafer_map", None)
        rcs = sorted(wm.dies.keys()) if wm is not None else []
        if not rcs:
            self._draw_empty_nzmap(
                "No wafer map loaded on the Run tab yet.")
            return
        die_ids = wm.die_ids or {}
        self._nzmap_dies_by_rc = {
            rc: {"row": rc[0], "col": rc[1], "serial": die_ids.get(rc, ""),
                "status": "run_tab"}
            for rc in rcs}
        self._nzmap_ax.clear()
        self._nzmap_ax.set_aspect("equal")
        patches = [Rectangle((c - 0.5, -r - 0.5), 1, 1) for r, c in rcs]
        coll = PatchCollection(patches, edgecolor="#1e293b", linewidths=0.4)
        coll.set_facecolor("#7aaec8")
        self._nzmap_ax.add_collection(coll)
        cols = [c for _r, c in rcs]
        rows = [r for r, _c in rcs]
        self._nzmap_ax.set_xlim(min(cols) - 1, max(cols) + 1)
        self._nzmap_ax.set_ylim(-(max(rows) + 1), -(min(rows) - 1))
        n_ided = sum(1 for rc in rcs if die_ids.get(rc))
        self._nzmap_ax.set_title(
            f"Run Tab — {len(rcs)} die(s), {n_ided} with an ID — "
            "click a die to see it", fontsize=9)
        self._nzmap_current_labels = [
            {"x": c, "y": -r, "label": die_ids.get((r, c), "") or f"R{r}C{c}",
             "color": "black"} for r, c in rcs
        ]
        self._connect_nzmap_view_callbacks()
        self._update_visible_nzmap_labels()
        self._nzmap_canvas.draw_idle()

    def _draw_overlay_labels_on(self, wm, die_ids_by_rc: dict) -> list:
        items = []
        for rc, label_text in die_ids_by_rc.items():
            item = wm.dies.get(rc)
            if item is None:
                continue
            coords = wm.canvas.coords(item)
            if len(coords) < 4:
                continue
            cx, cy = (coords[0] + coords[2]) / 2, (coords[1] + coords[3]) / 2
            items.append(wm.canvas.create_text(
                cx, cy, text=label_text, font=("Consolas", 7), fill="#1e293b"))
        return items

    def _clear_overlay_labels(self, wm, items: list):
        for item in items:
            try:
                wm.canvas.delete(item)
            except tk.TclError:
                pass
        items.clear()

    def _redraw_overlay_on_run_map(self):
        self._clear_overlay_labels(self.wafer_map, self._overlay_items)
        die_ids = self.wafer_map.die_ids
        if not die_ids:
            self._overlay_items = []
        else:
            self._overlay_items = self._draw_overlay_labels_on(self.wafer_map, die_ids)
            self._update_overlay_visibility()
        self._update_position_window()

    _OVERLAY_MIN_DIE_PX = 22

    def _update_overlay_visibility(self):
        if not self._overlay_items:
            return
        wm = self.wafer_map
        sample_rc = next(iter(wm.die_ids), None)
        item = wm.dies.get(sample_rc) if sample_rc else None
        bbox = wm.canvas.bbox(item) if item is not None else None
        if not bbox:
            return
        width_px = bbox[2] - bbox[0]
        state = "normal" if width_px >= self._OVERLAY_MIN_DIE_PX else "hidden"
        for it in self._overlay_items:
            try:
                wm.canvas.itemconfigure(it, state=state)
            except tk.TclError:
                pass

    def _clear_position_window(self):
        wm = self.wafer_map
        for item in self._position_window_items:
            try:
                wm.canvas.delete(item)
            except tk.TclError:
                pass
        self._position_window_items = []

    def _die_pitch(self):
        wm = self.wafer_map
        by_col: dict = {}
        for (r, c) in wm.dies:
            by_col.setdefault(c, []).append(r)
        for c, rows in by_col.items():
            rows_sorted = sorted(rows)
            for a, b in zip(rows_sorted, rows_sorted[1:]):
                if b - a == 1:
                    ca = wm.canvas.coords(wm.dies[(a, c)])
                    cb = wm.canvas.coords(wm.dies[(b, c)])
                    if ca and cb:
                        return (cb[0] - ca[0], cb[1] - ca[1])
        return None

    def _nearest_known_in_col(self, col: int, row: int):
        wm = self.wafer_map
        best_rc, best_item, best_dist = None, None, None
        for (r, c), item in wm.dies.items():
            if c != col:
                continue
            d = abs(r - row)
            if best_dist is None or d < best_dist:
                best_dist, best_rc, best_item = d, (r, c), item
        return best_rc, best_item

    def _update_position_window(self):
        self._clear_position_window()
        self._position_window_dies = []
        row, col = self._current_rc
        if row is None or col is None:
            self._position_window_var.set("Position window: XY not read yet")
            return

        wm = self.wafer_map
        pitch = self._die_pitch()
        present_n = 0
        all_coords = []
        window_size = self._probe_height()
        for i in range(window_size):
            r, c = row + i, col
            item = wm.dies.get((r, c))
            coords = wm.canvas.coords(item) if item is not None else None
            if coords is None and pitch is not None:
                anchor_rc, anchor_item = self._nearest_known_in_col(c, r)
                if anchor_item is not None:
                    acoords = wm.canvas.coords(anchor_item)
                    if acoords:
                        dr = r - anchor_rc[0]
                        coords = [acoords[0] + pitch[0] * dr, acoords[1] + pitch[1] * dr,
                                  acoords[2] + pitch[0] * dr, acoords[3] + pitch[1] * dr]
            present = item is not None
            if present:
                present_n += 1
            self._position_window_dies.append({
                "row": r, "col": c, "present": present,
                "die_id": wm.die_ids.get((r, c), ""),
            })
            if coords:
                all_coords.append(coords)

        if all_coords:
            x1 = min(c[0] for c in all_coords)
            y1 = min(c[1] for c in all_coords)
            x2 = max(c[2] for c in all_coords)
            y2 = max(c[3] for c in all_coords)
            rect = wm.canvas.create_rectangle(x1, y1, x2, y2, outline="#2563eb", width=3)
            wm.canvas.tag_raise(rect)
            self._position_window_items.append(rect)

        self._position_window_var.set(
            f"Position window R{row}C{col} ↓{window_size}: "
            f"{present_n}/{window_size} dies present")

    def _wafer_plan_offset(self) -> tuple:
        if not self._wafer_plan or not self.wafer_map.dies:
            return (0, 0)
        plan_grid = nzb.wafer_plan_die_grid(self._wafer_plan)
        return centroid_offset(plan_grid, self.wafer_map.dies.keys())

    def _select_plan(self):
        window_height = self._probe_height()
        die_keys = list(self.wafer_map.dies.keys())
        if not die_keys:
            messagebox.showerror(
                "No Wafer Map", "No wafer map is loaded — load an ATA folder first.")
            return
        picks = nzb.tile_windows_covering_wafer(die_keys, window_height)
        self.wafer_map.set_picked(picks)
        self._on_sites_changed(picks)
        self._log_main(f"NanoZ Run: selected {len(picks)} die(s)")

    _NZMAP_MAX_VISIBLE_LABELS = 900

    def _connect_nzmap_view_callbacks(self):
        self._nzmap_ax.callbacks.connect("xlim_changed", self._on_nzmap_view_changed)
        self._nzmap_ax.callbacks.connect("ylim_changed", self._on_nzmap_view_changed)

    def _on_nzmap_view_changed(self, _ax=None):
        if self._nzmap_view_debounce_id is not None:
            try:
                self.after_cancel(self._nzmap_view_debounce_id)
            except Exception:
                pass
        self._nzmap_view_debounce_id = self.after(120, self._update_visible_nzmap_labels)

    def _clear_nzmap_labels(self):
        for t in self._nzmap_label_artists:
            try:
                t.remove()
            except Exception:
                pass
        self._nzmap_label_artists = []

    def _fit_nzmap_fontsize(self, box_w_px: float, box_h_px: float, text_len: int) -> float:
        text_len = max(text_len, 1)
        dpi = self._nzmap_fig.dpi
        by_width = box_w_px * 72.0 / dpi / (0.62 * text_len)
        by_height = box_h_px * 72.0 / dpi * 0.75
        return max(3.0, min(by_width, by_height, 24.0))

    def _update_visible_nzmap_labels(self):
        self._nzmap_view_debounce_id = None
        self._clear_nzmap_labels()
        if not (_MPL and self._show_nzmap_labels_var.get()):
            self._nzmap_canvas.draw_idle()
            return
        labels = self._nzmap_current_labels
        if not labels:
            return
        xlim = sorted(self._nzmap_ax.get_xlim())
        ylim = sorted(self._nzmap_ax.get_ylim())
        visible = [d for d in labels
                  if xlim[0] <= d["x"] <= xlim[1] and ylim[0] <= d["y"] <= ylim[1]]
        if not visible or len(visible) > self._NZMAP_MAX_VISIBLE_LABELS:
            self._nzmap_canvas.draw_idle()
            return
        bbox = self._nzmap_ax.get_window_extent()
        span_x = (xlim[1] - xlim[0]) or 1.0
        span_y = (ylim[1] - ylim[0]) or 1.0
        box_w_px = bbox.width / span_x
        box_h_px = bbox.height / span_y
        for d in visible:
            fs = self._fit_nzmap_fontsize(box_w_px, box_h_px, len(d["label"]))
            t = self._nzmap_ax.text(d["x"], d["y"], d["label"], fontsize=fs,
                                    ha="center", va="center", color=d["color"],
                                    zorder=6, clip_on=True)
            self._nzmap_label_artists.append(t)
        self._nzmap_canvas.draw_idle()

    def _on_nzmap_click(self, event):
        if event.xdata is None or event.ydata is None:
            return
        rc = (round(-event.ydata), round(event.xdata))
        d = self._nzmap_dies_by_rc.get(rc)
        if d is None:
            return
        self._nzmap_die_var.set(
            f"Row {d['row']}, Col {d['col']} — serial {d['serial']}  ({d['status']})")

    def _on_nzmap_scroll_zoom(self, event):
        if event.inaxes != self._nzmap_ax or event.xdata is None or event.ydata is None:
            return
        factor = 0.85 if event.button == "up" else (1 / 0.85)
        xlim = self._nzmap_ax.get_xlim()
        ylim = self._nzmap_ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        self._nzmap_ax.set_xlim(xd - (xd - xlim[0]) * factor, xd + (xlim[1] - xd) * factor)
        self._nzmap_ax.set_ylim(yd - (yd - ylim[0]) * factor, yd + (ylim[1] - yd) * factor)
        self._nzmap_canvas.draw_idle()

    def _build_run_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Run")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        ctrl = tk.Frame(tab, bg="#f1f5f9", relief="solid", bd=1)
        ctrl.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        tk.Label(ctrl, text="Recipe:", bg="#f1f5f9").pack(side="left", padx=(10, 2), pady=6)
        self._run_recipe_name_cb = ttk.Combobox(
            ctrl, textvariable=self._recipe_name_var, state="readonly", width=22)
        self._run_recipe_name_cb.pack(side="left", pady=6)
        self._run_recipe_name_cb.bind(
            "<<ComboboxSelected>>", lambda _e: self._load_named_recipe())
        self._run_recipe_active_lbl = tk.Label(ctrl, text="(no recipe saved yet)",
                                               bg="#f1f5f9", fg="#6b7280")
        self._run_recipe_active_lbl.pack(side="left", padx=(4, 10))

        ttk.Separator(ctrl, orient="vertical").pack(side="left", fill="y", padx=10, pady=4)

        tk.Label(ctrl, text="Cycle #:", bg="#f1f5f9").pack(side="left", padx=(0, 2), pady=6)
        self.cycle_var = tk.StringVar(value="0")
        self._cycle_entry = ttk.Entry(ctrl, textvariable=self.cycle_var, width=5)
        self._cycle_entry.pack(side="left", pady=6)
        tk.Label(ctrl, text="Duration (s):", bg="#f1f5f9").pack(side="left", padx=(8, 2), pady=6)
        self.duration_var = tk.StringVar(value="7")
        self._duration_entry = ttk.Entry(ctrl, textvariable=self.duration_var, width=5)
        self._duration_entry.pack(side="left", pady=6)

        ttk.Separator(ctrl, orient="vertical").pack(side="left", fill="y", padx=10, pady=4)

        self.start_btn = ttk.Button(ctrl, text="▶  Start", command=self._start_recipe_run)
        self.start_btn.pack(side="left", padx=4, pady=5)
        self.test_btn = ttk.Button(ctrl, text="▶  Test Die", command=self._start_test_die)
        self.recipe_btn = ttk.Button(ctrl, text="▶  Run Recipe", command=self._start_recipe_run)

        ttk.Separator(ctrl, orient="vertical").pack(side="left", fill="y", padx=10, pady=4)

        self._btn_test_active = ttk.Button(ctrl, text="▶  Run Cycle (Active)",
                                           command=self._test_active_boards)
        self._btn_test_active.pack(side="left", padx=2, pady=5)
        self._btn_pause_active = ttk.Button(ctrl, text="⏸  Pause (Active)",
                                            command=self._pause_active_boards)
        self._btn_pause_active.pack(side="left", padx=2, pady=5)

        ttk.Separator(ctrl, orient="vertical").pack(side="left", fill="y", padx=10, pady=4)

        self.stop_btn = ttk.Button(ctrl, text="⏹  Stop Run", command=self._stop_lot, state="disabled")
        self.stop_btn.pack(side="left", padx=4, pady=5)

        self._btn_manual_unload = ttk.Button(ctrl, text="⏏  Unload (U)", command=self._manual_unload)
        self._btn_manual_unload.pack(side="left", padx=4, pady=5)

        self.state_var = tk.StringVar(value="IDLE")
        tk.Label(ctrl, textvariable=self.state_var, bg="#f1f5f9", fg="#6b7280",
                font=("Segoe UI", 11, "bold")).pack(side="right", padx=12)
        self.counts_var = tk.StringVar(value="SPL: 0   ENV: 0")
        tk.Label(ctrl, textvariable=self.counts_var, bg="#f1f5f9", fg="#0077cc").pack(
                 side="right", padx=(0, 4))
        self.die_var = tk.StringVar(value="Die: —")
        tk.Label(ctrl, textvariable=self.die_var, bg="#f1f5f9").pack(side="right", padx=(0, 12))

        body = ttk.PanedWindow(tab, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        left_col = ttk.Frame(body)
        body.add(left_col, weight=1)
        left_col.rowconfigure(1, weight=1)
        left_col.columnconfigure(0, weight=1)

        pos_lf = ttk.LabelFrame(left_col, text="Manual Control", padding=6)
        pos_lf.grid(row=0, column=0, sticky="new", pady=(0, 4))
        pos_lf.columnconfigure(0, weight=1)
        pos_lf.columnconfigure(1, weight=1)

        self.manual_xy_var = tk.StringVar(value="X: —  Y: —")
        ttk.Label(pos_lf, textvariable=self.manual_xy_var,
                  font=("Consolas", 11, "bold"), foreground="#0077cc",
                  justify="center").grid(row=0, column=0, columnspan=2, pady=(0, 4))

        self._btn_manual_zup = ttk.Button(pos_lf, text="⬆ Z Up", command=self._manual_z_up)
        self._btn_manual_zup.grid(row=1, column=0, sticky="ew", padx=(0, 1), pady=1)
        self._btn_manual_zdown = ttk.Button(pos_lf, text="⬇ Z Down", command=self._manual_z_down)
        self._btn_manual_zdown.grid(row=1, column=1, sticky="ew", padx=(1, 0), pady=1)
        if self._system == "electroglas":
            self._btn_manual_first_die = ttk.Button(
                pos_lf, text="⚓ Chuck Is Set", command=self._eg_set_anchor)
            self._btn_manual_first_die.grid(row=2, column=0, sticky="ew", padx=(0, 1), pady=1)
        else:
            self._btn_manual_first_die = ttk.Button(pos_lf, text="⏮ First Die (G)", command=self._manual_first_die)
            self._btn_manual_first_die.grid(row=2, column=0, sticky="ew", padx=(0, 1), pady=1)
        self._btn_manual_xy = ttk.Button(
            pos_lf, text="↻ Sync ?P" if self._system == "electroglas" else "↻ Refresh XY",
            command=self._manual_xy)
        self._btn_manual_xy.grid(row=2, column=1, sticky="ew", padx=(1, 0), pady=1)
        if self._system == "electroglas":
            anchor_row = ttk.Frame(pos_lf)
            anchor_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=1)
            ttk.Label(anchor_row, text="Chuck is on die:", font=("Segoe UI", 8)
                     ).pack(side="left")
            self._eg_anchor_var = tk.StringVar()
            self._eg_anchor_cb = ttk.Combobox(anchor_row, textvariable=self._eg_anchor_var, width=14)
            self._eg_anchor_cb.pack(side="left", padx=(2, 0))
            self._eg_anchor_state_var = tk.StringVar(value="not anchored")
            ttk.Label(pos_lf, textvariable=self._eg_anchor_state_var, foreground="#b45309",
                     font=("Segoe UI", 8), wraplength=260, justify="left").grid(
                     row=4, column=0, columnspan=2, sticky="ew", pady=(0, 2))

            pitch_row = ttk.Frame(pos_lf)
            pitch_row.grid(row=5, column=0, columnspan=2, sticky="ew", pady=1)
            ttk.Label(pitch_row, text="Pitch X/Y (mm):", font=("Segoe UI", 8)).pack(side="left")
            self._eg_pitch_x_var = tk.StringVar()
            ttk.Entry(pitch_row, textvariable=self._eg_pitch_x_var, width=6).pack(
                side="left", padx=(2, 2))
            self._eg_pitch_y_var = tk.StringVar()
            ttk.Entry(pitch_row, textvariable=self._eg_pitch_y_var, width=6).pack(side="left")
            ttk.Button(pos_lf, text="Set/Verify Pitch on Prober", command=self._eg_pitch_action
                      ).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(1, 4))
        self._btn_reset_counts = ttk.Button(pos_lf, text="Reset Counts", command=self._reset_counts)
        self._btn_reset_counts.grid(row=7, column=0, columnspan=2, sticky="ew", pady=1)
        self._btn_manual_next_die = ttk.Button(pos_lf, text="▶▶ Next Die (Recipe)",
                                               command=self._manual_next_die)
        self._btn_manual_next_die.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(1, 0))
        self._btn_manual_move_selected = ttk.Button(
            pos_lf, text="➡ Move to Selected",
            command=self._manual_move_to_selected, state="disabled")
        self._btn_manual_move_selected.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(1, 0))
        self._btn_measure = ttk.Button(pos_lf, text="Measure", command=self._manual_measure)

        td_lf = ttk.LabelFrame(left_col, text="Recipe — Touchdown List "
                               "(double-click to move there)")
        td_lf.grid(row=1, column=0, sticky="new", pady=(4, 0))
        td_cols = ("seq", "label", "active")
        self._touchdown_tree = ttk.Treeview(td_lf, columns=td_cols, show="headings", height=6)
        td_heads = [("seq", "#", 32), ("label", "Touchdown", 190), ("active", "Active", 55)]
        for cid, text, width in td_heads:
            self._touchdown_tree.heading(cid, text=text)
            self._touchdown_tree.column(cid, width=width, anchor="w" if cid == "label" else "center")
        td_sb = ttk.Scrollbar(td_lf, orient="vertical", command=self._touchdown_tree.yview)
        self._touchdown_tree.configure(yscrollcommand=td_sb.set)
        self._touchdown_tree.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=6)
        td_sb.pack(side="left", fill="y", pady=6, padx=(0, 6))
        self._touchdown_tree.bind("<Double-1>", self._on_touchdown_double_click)

        shot_lf = ttk.LabelFrame(left_col, text="Recipe — Current Shot")
        shot_lf.grid(row=2, column=0, sticky="new", pady=(4, 0))
        self.recipe_shot_var = tk.StringVar(value="No recipe run active — see the Recipe tab.")
        ttk.Label(shot_lf, textvariable=self.recipe_shot_var, wraplength=280,
                 justify="left").pack(anchor="w", padx=6, pady=(6, 2))
        sd_cols = ("port", "slots", "decision", "reason")
        self._shot_decision_tree = ttk.Treeview(shot_lf, columns=sd_cols, show="headings", height=5)
        sd_heads = [("port", "Board", 60), ("slots", "Slots", 70),
                   ("decision", "Decision", 65), ("reason", "Reason", 140)]
        for cid, text, width in sd_heads:
            self._shot_decision_tree.heading(cid, text=text)
            self._shot_decision_tree.column(cid, width=width, anchor="center" if cid != "reason" else "w")
        self._shot_decision_tree.pack(fill="x", padx=6, pady=(0, 6))
        self._shot_decision_tree.bind("<Double-1>", self._on_shot_decision_double_click)

        map_lf = ttk.LabelFrame(body, text="Wafer Map")
        body.add(map_lf, weight=2)
        map_lf.rowconfigure(2, weight=1)
        map_lf.columnconfigure(0, weight=1)

        map_bar = ttk.Frame(map_lf)
        map_bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self.sites_var = tk.StringVar(value="Test sites: 0 picked (click dies to add/remove)")
        ttk.Label(map_bar, textvariable=self.sites_var, foreground="#6b7280",
                 font=("Segoe UI", 8)).pack(side="left", padx=8)
        ttk.Separator(map_bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self._select_all_btn = ttk.Button(
            map_bar, text="☑ Select All", command=self._toggle_select_all)
        self._select_all_btn.pack(side="left", padx=(6, 0))
        ttk.Button(map_bar, text="☑ Select Plan",
                  command=self._select_plan).pack(side="left", padx=(6, 0))

        pos_bar = ttk.Frame(map_lf)
        pos_bar.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))
        self._position_window_var = tk.StringVar(value="Position window: XY not read yet")
        ttk.Label(pos_bar, textvariable=self._position_window_var, foreground="#2563eb",
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=8)

        self.wafer_map = WaferMapPanel(map_lf)
        self.wafer_map.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.wafer_map.enable_picking(on_change=self._on_sites_changed)
        self.wafer_map.on_redraw = self._redraw_overlay_on_run_map
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Double-Button-1>"):
            self.wafer_map.canvas.bind(seq, lambda _e: self._update_overlay_visibility(), add="+")

        stat_lf = ttk.LabelFrame(body, text="Pass / Fail", padding=10)
        body.add(stat_lf, weight=1)
        stat_lf.columnconfigure(0, weight=1)

        self.pass_var = tk.StringVar(value="0")
        self.fail_var = tk.StringVar(value="0")
        for var, label, color in [
            (self.pass_var, "PASS", "#16a34a"),
            (self.fail_var, "FAIL", "#dc2626"),
        ]:
            row_f = ttk.Frame(stat_lf)
            row_f.pack(fill="x", pady=4)
            ttk.Label(row_f, text=label, width=6,
                      font=("Segoe UI", 10, "bold"),
                      foreground=color).pack(side="left")
            ttk.Label(row_f, textvariable=var,
                      font=("Consolas", 24, "bold"),
                      foreground=color).pack(side="left", padx=8)

        ttk.Separator(stat_lf, orient="horizontal").pack(fill="x", pady=8)

        self.yield_var = tk.StringVar(value="Yield: —")
        ttk.Label(stat_lf, textvariable=self.yield_var,
                  font=("Consolas", 13, "bold"), foreground="#374151").pack()

    def _build_charts_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Charts")
        self._charts_tab = tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        pick = ttk.Frame(tab)
        pick.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ttk.Label(pick, text="Board:").pack(side="left")
        self._chart_board_cb = ttk.Combobox(
            pick, textvariable=self._console_board_label_var, state="readonly", width=26)
        self._chart_board_cb.pack(side="left", padx=(4, 12))
        self._chart_board_cb.bind(
            "<<ComboboxSelected>>",
            lambda _e: (self._on_console_board_picked(_e), self._redraw_charts()))
        ttk.Button(pick, text="▶ Run Cycle", command=self._chart_run_cycle).pack(
            side="left", padx=(0, 12))
        ttk.Label(pick, text="Sensors:").pack(side="left", padx=(12, 0))
        self._chart_sensor_metric_var = tk.StringVar(value="Current")
        ttk.Combobox(pick, textvariable=self._chart_sensor_metric_var, state="readonly", width=10,
                    values=("Current", "Resistance")).pack(side="left", padx=(4, 12))
        self._chart_sensor_metric_var.trace_add(
            "write", lambda *_: self._redraw_charts(preserve_view=True))
        ttk.Label(pick, text="Heaters:").pack(side="left")
        self._chart_heater_metric_var = tk.StringVar(value="Voltage")
        ttk.Combobox(pick, textvariable=self._chart_heater_metric_var, state="readonly", width=10,
                    values=("Voltage", "Current", "Power", "Resistance")).pack(side="left", padx=(4, 12))
        self._chart_heater_metric_var.trace_add(
            "write", lambda *_: self._redraw_charts(preserve_view=True))
        self._chart_live_btn = ttk.Button(pick, text="▶ Jump to Live",
                                          command=self._chart_resume_live)
        self._chart_live_btn.pack(side="left", padx=(12, 0))

        channels_row = ttk.Frame(tab)
        channels_row.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 4))
        ttk.Label(channels_row, text="Show chips:").pack(side="left")
        self._chart_chip_visible_vars = {}
        for chip, text in (("0", f"Chip {self._CHIP_LABELS['0']} (solid)"),
                          ("1", f"Chip {self._CHIP_LABELS['1']} (dashed)")):
            var = tk.BooleanVar(value=True)
            self._chart_chip_visible_vars[chip] = var
            ttk.Checkbutton(channels_row, text=text, variable=var,
                            command=lambda: self._redraw_charts(preserve_view=True)).pack(
                            side="left", padx=(6, 0))
        ttk.Label(channels_row, text="   Show channels:").pack(side="left")
        self._chart_visible_vars = {}
        for key in ("s1", "s2", "s3", "s4", "h1", "h2"):
            var = tk.BooleanVar(value=True)
            self._chart_visible_vars[key] = var
            ttk.Checkbutton(channels_row, text=key.upper(), variable=var,
                            command=lambda: self._redraw_charts(preserve_view=True)).pack(
                            side="left", padx=(6, 0))

        if _MPL:
            self._chart_fig = Figure(figsize=(8, 7), dpi=100)
            self._chart_ax_v = self._chart_fig.add_subplot(311)
            self._chart_ax_i = self._chart_fig.add_subplot(312, sharex=self._chart_ax_v)
            self._chart_ax_t = self._chart_fig.add_subplot(313, sharex=self._chart_ax_v)
            self._chart_fig.tight_layout(pad=2.2)
            self._chart_canvas = FigureCanvasTkAgg(self._chart_fig, master=tab)
            self._chart_canvas.get_tk_widget().grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 0))
            toolbar = NavigationToolbar2Tk(self._chart_canvas, tab, pack_toolbar=False)
            toolbar.update()
            toolbar.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
            self._chart_follow_live = True
            self._chart_programmatic_xlim = False
            self._chart_ax_v.callbacks.connect("xlim_changed", self._on_chart_xlim_changed)
            self._chart_canvas.mpl_connect("button_press_event", self._on_chart_button_press)
            self._chart_canvas.mpl_connect("scroll_event", self._on_chart_scroll_zoom)
            bind_middle_pan_mpl(self._chart_canvas)
            self._draw_empty_charts()
        else:
            ttk.Label(tab, text="matplotlib not installed — install it to view live charts.",
                     foreground="red").grid(row=2, column=0, sticky="nw", padx=10, pady=10)

    def _draw_empty_charts(self):
        for ax, title in ((self._chart_ax_v, "Heater Voltage (mV) — SPL"),
                          (self._chart_ax_i, "Sensor Current (mA) — SPL"),
                          (self._chart_ax_t, "Temperature (°C) — ENV")):
            ax.clear()
            ax.set_title(title, fontsize=9)
            ax.text(0.5, 0.5, "no data yet", ha="center", va="center",
                    transform=ax.transAxes, color="#999999")
        self._chart_canvas.draw_idle()

    def _build_results_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Results")
        self._results_tab = tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        export_frame = ttk.LabelFrame(tab, text="Data Export")
        export_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))

        ttk.Label(
            export_frame,
            text="Output filename:  <Lot ID>_<Wafer ID>_nanoz_results.csv  "
                 "(Wafer ID omitted if blank)"
        ).pack(anchor="w", padx=10, pady=(8, 4))

        file_row = ttk.Frame(export_frame)
        file_row.pack(fill="x", padx=10, pady=4)
        ttk.Label(file_row, text="Lot ID:").pack(side="left")
        self._nz_lot_id_var = tk.StringVar(value="")
        ttk.Entry(file_row, textvariable=self._nz_lot_id_var, width=22).pack(side="left", padx=6)
        ttk.Label(file_row, text="Wafer ID:").pack(side="left", padx=(12, 0))
        self._nz_wafer_id_var = tk.StringVar(value="")
        ttk.Entry(file_row, textvariable=self._nz_wafer_id_var, width=22).pack(side="left", padx=6)

        path_row = ttk.Frame(export_frame)
        path_row.pack(fill="x", padx=10, pady=(4, 12))
        ttk.Label(path_row, text="Export Path:").pack(side="left")
        self._nz_export_path_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Downloads"))
        ttk.Entry(path_row, textvariable=self._nz_export_path_var, width=40).pack(side="left", padx=6)
        ttk.Button(path_row, text="Browse...", command=self._nz_browse_export_path).pack(
            side="left", padx=4)
        ttk.Button(path_row, text="Save to CSV", command=self._nz_save_results_csv).pack(
            side="left", padx=10)
        ttk.Button(path_row, text="Export Raw", command=self._nz_export_raw).pack(
            side="left", padx=(0, 10))
        ttk.Button(path_row, text="Clear", command=self._nz_clear_results).pack(
            side="left", padx=(0, 4))

        results_frame = ttk.Frame(tab)
        results_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        results_frame.rowconfigure(0, weight=1)
        results_frame.columnconfigure(0, weight=1)

        cols = ("port", "chip", "die", "channel", "v_now", "i_now", "r_now",
               "v_avg", "i_avg", "r_avg", "n", "updated")
        self._results_tree = ttk.Treeview(results_frame, columns=cols, show="headings", height=16)
        heads = [("port", "Port", 70), ("chip", "Chip", 50), ("die", "Die ID", 100),
                 ("channel", "Channel", 60),
                 ("v_now", "V now (mV)", 100), ("i_now", "I now (mA)", 100),
                 ("r_now", "R now", 100),
                 ("v_avg", "V avg (mV)", 100), ("i_avg", "I avg (mA)", 100),
                 ("r_avg", "R avg", 100),
                 ("n", "N", 50), ("updated", "Updated", 140)]
        for cid, text, width in heads:
            self._results_tree.heading(cid, text=text)
            self._results_tree.column(cid, width=width, anchor="center")
        self._results_tree.grid(row=0, column=0, sticky="nsew")
        results_vsb = ttk.Scrollbar(results_frame, orient="vertical",
                                    command=self._results_tree.yview)
        results_vsb.grid(row=0, column=1, sticky="ns")
        results_hsb = ttk.Scrollbar(results_frame, orient="horizontal",
                                    command=self._results_tree.xview)
        results_hsb.grid(row=1, column=0, sticky="ew")
        self._results_tree.configure(yscrollcommand=results_vsb.set,
                                     xscrollcommand=results_hsb.set)

    def _nz_browse_export_path(self):
        path = filedialog.askdirectory(title="Choose Export Folder")
        if path:
            self._nz_export_path_var.set(path)

    _RAW_EXPORT_DROP_FIELDS = {"die_row", "die_col", "header_time_ms", "header_bfr",
                               "len", "checksum_expected", "ppms", "header_chip", "reserved",
                               "cycle_start"}

    @staticmethod
    def _raw_resistance(v_str, i_str) -> str:
        try:
            v, i = float(v_str), float(i_str)
        except (TypeError, ValueError):
            return ""
        if not i:
            return ""
        return f"{v / i:.4g}"

    _RAW_EXPORT_ENV_FIELDS = ("temp_h_c", "humidity_percent", "temp_p_c",
                              "pressure_hpa_minus_1013", "mcu_temperature_c")

    def _load_env_by_port(self) -> dict:
        by_port: dict = {}
        if not self._env_path or not os.path.isfile(self._env_path):
            return by_port
        try:
            with open(self._env_path, "r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    ts = row.get("host_timestamp", "")
                    try:
                        parsed = dt.datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                    by_port.setdefault(row.get("port"), []).append((parsed, row))
        except OSError:
            return by_port
        for port in by_port:
            by_port[port].sort(key=lambda pr: pr[0])
        return by_port

    @staticmethod
    def _nearest_env_reading(env_by_port: dict, port: str, host_timestamp: str) -> "dict | None":
        samples = env_by_port.get(port)
        if not samples:
            return None
        try:
            target = dt.datetime.fromisoformat(host_timestamp)
        except (ValueError, TypeError):
            return samples[-1][1]
        times = [t for t, _ in samples]
        i = bisect.bisect_left(times, target)
        candidates = [c for c in (i - 1, i) if 0 <= c < len(samples)]
        best = min(candidates, key=lambda c: abs((samples[c][0] - target).total_seconds()))
        return samples[best][1]

    @staticmethod
    def _raw_die_key(row: dict) -> str:
        return row.get("die_id") or f"{row.get('die_row')},{row.get('die_col')}"

    def _raw_latest_cycle_only(self, rows: list) -> list:
        latest: dict[str, str] = {}
        for row in rows:
            key = self._raw_die_key(row)
            cs = row.get("cycle_start") or ""
            if cs and cs > latest.get(key, ""):
                latest[key] = cs
        return [row for row in rows if row.get("cycle_start", "") == latest.get(
            self._raw_die_key(row), "")]

    def _nz_export_raw(self):
        if not self._spl_path or not os.path.isfile(self._spl_path):
            messagebox.showerror(
                "No Raw Data Yet",
                "No SPL data has been logged this session — run a cycle first "
                "(Run Cycle (Active), a recipe run, a double-clicked board, ...), "
                "then Export Raw.")
            return
        folder = self._nz_export_path_var.get().strip() or self._nanoz_ata_folder
        if not folder:
            messagebox.showerror("No Export Path", "Choose an export path first.")
            return

        try:
            with open(self._spl_path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                src_fields = reader.fieldnames or []
                rows = list(reader)
        except OSError as e:
            messagebox.showerror("Export Failed", str(e))
            return
        rows = self._raw_latest_cycle_only(rows)
        n_before = len(rows)
        rows = [r for r in rows if (r.get("die_id") or "").strip()]
        n_unassigned = n_before - len(rows)
        env_by_port = self._load_env_by_port()

        kept_fields = [c for c in src_fields if c not in self._RAW_EXPORT_DROP_FIELDS]
        r_fields = [f"r_s{s}_kohm" for s in (1, 2, 3, 4)] + ["r_h1_ohm", "r_h2_ohm"]
        env_fields = [f"env_{f}" for f in self._RAW_EXPORT_ENV_FIELDS]
        out_fields = kept_fields + r_fields + env_fields

        lot_id = self._nz_lot_id_var.get().strip()
        wafer_id = self._nz_wafer_id_var.get().strip()
        name_parts = [p for p in (lot_id, wafer_id) if p] or ["nanoz"]
        filename = "_".join(name_parts) + "_nanoz_raw.csv"
        dest = os.path.join(folder, filename)
        try:
            with open(dest, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=out_fields)
                writer.writeheader()
                for row in rows:
                    out = {k: row.get(k, "") for k in kept_fields}
                    for s in (1, 2, 3, 4):
                        out[f"r_s{s}_kohm"] = self._raw_resistance(
                            row.get(f"dac_mv_s{s}"), row.get(f"adc_current_ma_s{s}"))
                    for h in (1, 2):
                        out[f"r_h{h}_ohm"] = self._raw_resistance(
                            row.get(f"heater{h}_voltage_mv"), row.get(f"heater{h}_current_ma"))
                    env_row = self._nearest_env_reading(
                        env_by_port, row.get("port"), row.get("host_timestamp", ""))
                    for f in self._RAW_EXPORT_ENV_FIELDS:
                        out[f"env_{f}"] = (env_row or {}).get(f, "")
                    writer.writerow(out)
        except OSError as e:
            messagebox.showerror("Export Failed", str(e))
            return
        skipped_note = (f", {n_unassigned} unassigned-slot sample(s) skipped (no die ID)"
                        if n_unassigned else "")
        self._log_main(f"NanoZ Export Raw: {len(rows)} raw sample(s){skipped_note}")

    def _nz_save_results_csv(self):
        folder = self._nz_export_path_var.get().strip() or self._nanoz_ata_folder
        if not folder:
            messagebox.showerror("No Export Path", "Choose an export path first.")
            return
        lot_id = self._nz_lot_id_var.get().strip()
        wafer_id = self._nz_wafer_id_var.get().strip()
        name_parts = [p for p in (lot_id, wafer_id) if p] or ["nanoz"]
        filename = "_".join(name_parts) + "_nanoz_results.csv"
        path = os.path.join(folder, filename)
        cols = ("port", "chip", "die", "channel", "v_now", "i_now", "r_now",
                "v_avg", "i_avg", "r_avg", "n", "updated")
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(cols)
                for iid in self._results_tree.get_children():
                    writer.writerow(self._results_tree.item(iid, "values"))
        except OSError as e:
            messagebox.showerror("Save Failed", str(e))
            return
        self._log_main(f"NanoZ Results: saved {len(self._results_tree.get_children())} row(s)")

    def _nz_clear_results(self):
        n = len(self._latest_spl)
        self._latest_spl = {}
        for iid in self._results_tree.get_children():
            self._results_tree.delete(iid)
        for path in (self._spl_path, self._env_path):
            if path and os.path.isfile(path):
                try:
                    open(path, "w", encoding="utf-8").close()
                except OSError as e:
                    self._log_main(f"NanoZ Results: could not clear log file: {e}")
        self._log_main(f"NanoZ Results: cleared")

    def _results_tab_visible(self):
        try:
            return self._sub_nb.select() == str(self._results_tab)
        except Exception:
            return False

    def _refresh_results_loop(self):
        if self._results_tab_visible():
            self._redraw_results()
        self.after(500, self._refresh_results_loop)

    def _redraw_results(self):
        for iid in self._results_tree.get_children():
            self._results_tree.delete(iid)

        cutoff = self._cycle_start_time
        for port, chip in sorted(self._latest_spl.keys()):
            key = (port, chip)
            latest = self._latest_spl[key]
            hist = [h for h in self._spl_history.get(key, ()) if h.get("_settled", True)]
            windowed = [h for h in hist if cutoff and (self._pkt_time(h) or cutoff) >= cutoff]
            if not windowed:
                windowed = hist
            updated = latest.get("host_timestamp", "")
            updated = updated.split("T")[-1] if "T" in updated else updated
            die_row, die_col = latest.get("die_row"), latest.get("die_col")
            die = latest.get("die_id") or (
                f"R{die_row}C{die_col}" if die_row is not None and die_col is not None else "—")
            channels = ([(f"s{s}", f"dac_mv_s{s}", f"adc_current_ma_s{s}") for s in (1, 2, 3, 4)]
                       + [(f"h{h}", f"heater{h}_voltage_mv", f"heater{h}_current_ma")
                          for h in (1, 2)])
            for label, v_field, i_field in channels:
                is_sensor = label.startswith("s")
                v_now, i_now = latest.get(v_field), latest.get(i_field)
                v_vals = [h[v_field] for h in windowed if v_field in h]
                i_vals = [h[i_field] for h in windowed if i_field in h]
                v_avg = sum(v_vals) / len(v_vals) if v_vals else None
                i_avg = sum(i_vals) / len(i_vals) if i_vals else None
                r_unit = "kΩ" if is_sensor else "Ω"
                r_now = v_now / i_now if (v_now is not None and i_now) else None
                r_avg = v_avg / i_avg if (v_avg is not None and i_avg) else None
                self._results_tree.insert("", "end", values=(
                    port, chip, die, label,
                    f"{v_now:.2f}" if v_now is not None else "—",
                    f"{i_now:.5f}" if i_now is not None else "—",
                    f"{r_now:.3g} {r_unit}" if r_now is not None else "—",
                    f"{v_avg:.2f}" if v_avg is not None else "—",
                    f"{i_avg:.5f}" if i_avg is not None else "—",
                    f"{r_avg:.3g} {r_unit}" if r_avg is not None else "—",
                    len(windowed), updated,
                ))


    def _build_cassette_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Cassette")
        self._cst_wafers: list[dict] = []
        self._cst_slot_idx = 0
        self._cst_armed = False
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)

        bar = ttk.Frame(tab, padding=(6, 4))
        bar.grid(row=0, column=0, sticky="ew")
        self._cst_go_btn = ttk.Button(bar, text="▶  Cassette Automation",
                                      command=self._cst_arm)
        self._cst_go_btn.pack(side="left", padx=4)
        self._cst_stop_btn = ttk.Button(bar, text="⏹  Stop Automation", state="disabled",
                                        command=lambda: self._cst_disarm("Stopped by user."))
        self._cst_stop_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Reset to Slot #1", command=self._cst_reset_slot).pack(
            side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(bar, text="Pass yield ≥").pack(side="left")
        self._cst_yield_var = tk.StringVar(value="0")
        self._cst_yield_folder: str | None = None
        cst_yield_ent = ttk.Entry(bar, textvariable=self._cst_yield_var, width=5)
        cst_yield_ent.pack(side="left", padx=(2, 0))
        cst_yield_ent.bind("<Return>", lambda _e: self._on_cst_yield_edited())
        cst_yield_ent.bind("<FocusOut>", lambda _e: self._on_cst_yield_edited())
        ttk.Label(bar, text="% to auto-continue, else pause").pack(side="left", padx=(2, 0))
        self._cst_state_var = tk.StringVar(value="IDLE")
        self._cst_state_lbl = ttk.Label(bar, textvariable=self._cst_state_var,
                                        font=("Consolas", 11, "bold"), foreground="#6b7280")
        self._cst_state_lbl.pack(side="right", padx=8)

        lf = ttk.LabelFrame(tab, text="Cassette Slots", padding=6)
        lf.grid(row=1, column=0, sticky="ew", padx=6, pady=(4, 2))
        lf.columnconfigure(0, weight=1)
        btns = ttk.Frame(lf)
        btns.grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Button(btns, text="Add Slot", command=self._cst_add_slot).pack(side="left", padx=2)
        ttk.Button(btns, text="Edit", command=self._cst_edit_slot).pack(side="left", padx=2)
        ttk.Button(btns, text="Remove", command=self._cst_remove_slot).pack(side="left", padx=2)
        ttk.Button(btns, text="▲", width=3, command=lambda: self._cst_move_slot(-1)).pack(
            side="left", padx=(10, 2))
        ttk.Button(btns, text="▼", width=3, command=lambda: self._cst_move_slot(1)).pack(
            side="left", padx=2)
        ttk.Button(btns, text="Clear All", command=self._cst_clear_slots).pack(
            side="left", padx=(10, 2))
        cols = ("slot", "lot", "wafer")
        self._cst_tree = ttk.Treeview(lf, columns=cols, show="headings", height=5,
                                      selectmode="browse")
        heads = [("slot", "Slot #", 60), ("lot", "Lot ID", 160), ("wafer", "Wafer ID", 160)]
        for cid, text, width in heads:
            self._cst_tree.heading(cid, text=text)
            self._cst_tree.column(cid, width=width, anchor="center" if cid == "slot" else "w")
        self._cst_tree.grid(row=1, column=0, sticky="ew")
        self._cst_tree.bind("<Double-1>", lambda _e: self._cst_edit_slot())

        ef = ttk.Frame(lf)
        ef.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._cst_auto_export_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ef, text="Auto Export",
                       variable=self._cst_auto_export_var).pack(side="left", padx=(0, 16))
        ttk.Label(ef, text="Export Directory:").pack(side="left")
        ttk.Entry(ef, textvariable=self._nz_export_path_var, width=32).pack(side="left", padx=6)
        ttk.Button(ef, text="Browse...", command=self._nz_browse_export_path).pack(side="left")
        export_dir_choices = getattr(self._main_layout, "_export_dir_choices", None)
        if export_dir_choices:
            export_dir_var = tk.StringVar(value=next(iter(export_dir_choices)))
            export_dir_cb = ttk.Combobox(
                ef, textvariable=export_dir_var, state="readonly",
                width=16, values=list(export_dir_choices.keys()))
            export_dir_cb.pack(side="left", padx=(4, 0))
            export_dir_cb.bind(
                "<<ComboboxSelected>>",
                lambda _e: self._nz_export_path_var.set(
                    export_dir_choices[export_dir_var.get()]))

        pf = ttk.LabelFrame(tab, text="Cassette Automation Log", padding=6)
        pf.grid(row=3, column=0, sticky="nsew", padx=6, pady=(2, 6))
        pf.rowconfigure(0, weight=1)
        pf.columnconfigure(0, weight=1)
        cst_cols = ("timestamp", "slot", "lot", "event")
        self._cst_log_tree = ttk.Treeview(pf, columns=cst_cols, show="headings", height=10,
                                          selectmode="browse")
        cst_heads = [("timestamp", "Time", 150), ("slot", "Slot", 50),
                    ("lot", "Lot ID", 120), ("event", "Event", 400)]
        for cid, text, width in cst_heads:
            self._cst_log_tree.heading(cid, text=text)
            self._cst_log_tree.column(cid, width=width, anchor="center" if cid == "slot" else "w")
        self._cst_log_tree.grid(row=0, column=0, sticky="nsew")
        cst_sb = ttk.Scrollbar(pf, orient="vertical", command=self._cst_log_tree.yview)
        cst_sb.grid(row=0, column=1, sticky="ns")
        self._cst_log_tree.configure(yscrollcommand=cst_sb.set)

    def _cst_set_state(self, text: str, color: str = "#6b7280"):
        self._cst_state_var.set(text)
        self._cst_state_lbl.config(foreground=color)

    def _cst_set_locked(self, locked: bool):
        self._cst_go_btn.config(state="disabled" if locked else "normal")
        self._cst_stop_btn.config(state="normal" if locked else "disabled")

    def _cst_log_event(self, slot_num, lot_id: str, event: str):
        self._log_main(f"Slot {slot_num}: {event}" if slot_num else event)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        def _ui():
            self._cst_log_tree.insert("", "end", values=(ts, slot_num, lot_id, event))
            children = self._cst_log_tree.get_children()
            if children:
                self._cst_log_tree.see(children[-1])
        self.after(0, _ui)

    def _cst_yield_threshold(self) -> float:
        try:
            return float(self._cst_yield_var.get())
        except ValueError:
            return 0.0

    def _on_cst_yield_edited(self):
        if not self._cst_yield_folder:
            return
        save_yield_threshold(self._cst_yield_folder, self._cst_yield_threshold())

    def _cst_redraw_slots(self):
        self._cst_tree.delete(*self._cst_tree.get_children())
        for i, w in enumerate(self._cst_wafers):
            marker = " (current)" if self._cst_armed and i == self._cst_slot_idx else ""
            self._cst_tree.insert("", "end", iid=str(i), values=(
                f"{i + 1}{marker}", w["lot_id"], w["wafer_id"]))

    def _cst_add_slot(self):
        lot_id = simpledialog.askstring("Add Slot", "Lot ID:", parent=self)
        if lot_id is None:
            return
        lot_id = lot_id.strip()
        if not lot_id:
            messagebox.showerror("Lot ID Required", "Lot ID can't be blank.")
            return
        wafer_id = simpledialog.askstring("Add Slot", "Wafer ID (optional):", parent=self) or ""
        self._cst_wafers.append({"lot_id": lot_id, "wafer_id": wafer_id.strip()})
        self._cst_redraw_slots()

    def _cst_selected_slot_index(self):
        sel = self._cst_tree.selection()
        return int(sel[0]) if sel else None

    def _cst_edit_slot(self):
        idx = self._cst_selected_slot_index()
        if idx is None:
            return
        w = self._cst_wafers[idx]
        lot_id = simpledialog.askstring("Edit Slot", "Lot ID:", initialvalue=w["lot_id"],
                                        parent=self)
        if lot_id is None:
            return
        lot_id = lot_id.strip()
        if not lot_id:
            messagebox.showerror("Lot ID Required", "Lot ID can't be blank.")
            return
        wafer_id = simpledialog.askstring("Edit Slot", "Wafer ID (optional):",
                                          initialvalue=w["wafer_id"], parent=self)
        if wafer_id is None:
            return
        self._cst_wafers[idx] = {"lot_id": lot_id, "wafer_id": wafer_id.strip()}
        self._cst_redraw_slots()

    def _cst_remove_slot(self):
        idx = self._cst_selected_slot_index()
        if idx is None:
            return
        del self._cst_wafers[idx]
        if self._cst_slot_idx > idx:
            self._cst_slot_idx -= 1
        self._cst_redraw_slots()

    def _cst_move_slot(self, direction: int):
        idx = self._cst_selected_slot_index()
        if idx is None:
            return
        new_idx = idx + direction
        if not (0 <= new_idx < len(self._cst_wafers)):
            return
        self._cst_wafers[idx], self._cst_wafers[new_idx] = (
            self._cst_wafers[new_idx], self._cst_wafers[idx])
        self._cst_redraw_slots()
        self._cst_tree.selection_set(str(new_idx))

    def _cst_clear_slots(self):
        if self._cst_armed:
            messagebox.showerror("Automation Armed", "Stop automation before clearing the list.")
            return
        if self._cst_wafers and not messagebox.askyesno(
            "Clear All", f"Remove all {len(self._cst_wafers)} slot(s)?"):
            return
        self._cst_wafers = []
        self._cst_slot_idx = 0
        self._cst_redraw_slots()

    def _cst_reset_slot(self):
        if self._cst_armed:
            messagebox.showerror("Automation Armed", "Stop automation before resetting.")
            return
        self._cst_slot_idx = 0
        self._cst_redraw_slots()
        self._cst_log_event(1, "", "Reset — next Arm will start tracking from slot #1.")

    def _cst_arm(self):
        if not self._cst_wafers:
            messagebox.showerror("No Slots", "Add at least one cassette slot "
                                 "(Lot ID/Wafer ID) first.")
            return
        if self._cst_slot_idx >= len(self._cst_wafers):
            messagebox.showerror("Nothing Left", "Every slot in the list is already "
                                 "done — 🔄 Reset to Slot #1 to run it again.")
            return
        if self._on_wafer_finished not in (None, self._cst_on_wafer_finished):
            messagebox.showerror("Arm Blocked", "Another automation is already watching "
                                 "for the run to finish.")
            return
        self._cst_armed = True
        self._on_wafer_finished = self._cst_on_wafer_finished
        self._cst_set_locked(True)
        self._cst_set_state("ARMED — waiting for the current/next run to finish", "#2563eb")
        slot = self._cst_wafers[self._cst_slot_idx]
        self._cst_redraw_slots()
        self._cst_log_event(
            self._cst_slot_idx + 1, slot["lot_id"],
            "Armed — if this wafer's run isn't already going, start it normally "
            "(▶ Start on the Run tab) and this panel will take over from there.")

    def _cst_disarm(self, reason: str = ""):
        self._cst_armed = False
        if self._on_wafer_finished is self._cst_on_wafer_finished:
            self._on_wafer_finished = None
        self._cst_set_locked(False)
        self._cst_redraw_slots()
        if reason:
            self._cst_log_event(self._cst_slot_idx + 1, "", reason)

    def _cst_on_wafer_finished(self, pass_n: int, fail_n: int, aborted: bool):
        if not self._cst_armed:
            return
        slot = self._cst_wafers[self._cst_slot_idx]
        lot_id, wafer_id = slot["lot_id"], slot["wafer_id"]

        if aborted:
            self._cst_log_event(self._cst_slot_idx + 1, lot_id,
                                "Run was stopped/aborted — cassette automation stopped.")
            self._cst_disarm()
            self._cst_set_state("STOPPED (run aborted)", "#dc2626")
            return

        tested = pass_n + fail_n
        pct = (pass_n / tested * 100) if tested else 0.0
        self._cst_log_event(self._cst_slot_idx + 1, lot_id,
                            f"Run finished — {pass_n}/{tested} pass ({pct:.1f}%).")

        if self._cst_auto_export_var.get():
            self._cst_export_current(lot_id, wafer_id)

        threshold = self._cst_yield_threshold()
        if tested and pct < threshold:
            self._cst_log_event(self._cst_slot_idx + 1, lot_id,
                                f"Yield {pct:.1f}% is below the {threshold:g}% threshold — "
                                f"PAUSING cassette automation (wafer left loaded).")
            self._cst_disarm()
            self._cst_set_state(f"PAUSED — yield {pct:.1f}% < {threshold:g}%", "#f97316")
            return

        self._cst_slot_idx += 1
        if self._cst_slot_idx >= len(self._cst_wafers):
            self._cst_log_event(self._cst_slot_idx, lot_id,
                                "All slots in the list are complete — cassette automation "
                                "finished.")
            self._cst_disarm()
            self._cst_set_state("CASSETTE COMPLETE", "#16a34a")
            return

        self._cst_set_state("SWAPPING CASSETTE", "#f97316")
        self._cst_redraw_slots()
        threading.Thread(target=self._cst_advance_thread, daemon=True).start()

    def _cst_export_current(self, lot_id: str, wafer_id: str):
        self._nz_lot_id_var.set(lot_id)
        self._nz_wafer_id_var.set(wafer_id)
        try:
            self._nz_save_results_csv()
        except Exception as e:
            self._cst_log_event(self._cst_slot_idx + 1, lot_id, f"Auto-export error: {e}")

    def _cst_advance_thread(self):
        prober = self.controller.drivers.get("prober")
        drv = prober if (prober and prober.inst) else None
        try:
            if drv is None:
                self.after(0, lambda: self._log_main(
                    "Unload/load-next error: prober not connected"))
                next_ready = False
            else:
                self.after(0, lambda: self._log_main(">> L  (Unload / Load Next Wafer)"))
                next_ready = drv.cassette_unload_and_load_next(timeout_s=180) == 70
        except Exception as e:
            self.after(0, lambda e=e: self._log_main(f"Unload/load-next error: {e}"))
            next_ready = False

        if not next_ready:
            self.after(0, lambda: self._cst_log_event(
                self._cst_slot_idx + 1, "", "No next wafer (cassette empty/idle/error) — "
                "cassette automation stopped."))
            self.after(0, self._cst_disarm)
            self.after(0, lambda: self._cst_set_state("STOPPED (no next wafer)", "#dc2626"))
            return

        slot = self._cst_wafers[self._cst_slot_idx]
        self.after(0, lambda: self._nz_lot_id_var.set(slot["lot_id"]))
        self.after(0, lambda: self._nz_wafer_id_var.set(slot["wafer_id"]))
        self.after(0, lambda: self._cst_log_event(
            self._cst_slot_idx + 1, slot["lot_id"],
            "Next wafer ready (STB=70) — auto-starting its recipe run."))
        self.after(0, self._cst_start_next_run)

    def _cst_start_next_run(self):
        if not self._cst_armed:
            return
        try:
            self._start_recipe_run()
        except Exception as e:
            self._cst_log_event(self._cst_slot_idx + 1, "", f"Could not auto-start the next run: {e}")
            self._cst_disarm()
            self._cst_set_state("STOPPED (auto-start failed)", "#dc2626")

    def _build_nanoz_ek_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="NanoZ_EK")
        tab.columnconfigure(0, weight=1)

        pick = ttk.Frame(tab)
        pick.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
        ttk.Label(pick, text="Board:").pack(side="left")
        self._ek_board_var = tk.StringVar(value="")
        self._ek_board_label_var = tk.StringVar(value="")
        self._ek_board_cb = ttk.Combobox(
            pick, textvariable=self._ek_board_label_var, state="readonly", width=26)
        self._ek_board_cb.pack(side="left", padx=(4, 12))
        self._ek_board_cb.bind("<<ComboboxSelected>>", self._on_ek_board_picked)
        self._btn_ek_read = ttk.Button(pick, text="Read Configuration",
                                       command=self._ek_read_configuration)
        self._btn_ek_read.pack(side="left")
        self._btn_ek_write = ttk.Button(pick, text="Write Sequence to Device",
                                        command=self._ek_write_sequence, state="disabled")
        self._btn_ek_write.pack(side="left", padx=(6, 0))
        self._ek_status_var = tk.StringVar(value="")
        ttk.Label(pick, textvariable=self._ek_status_var, foreground="#6b7280").pack(
                  side="left", padx=(10, 0))

        body = ttk.Frame(tab)
        body.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        tab.rowconfigure(2, weight=1)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        cfg_lf = ttk.LabelFrame(body, text="B — Configuration")
        cfg_lf.grid(row=0, column=0, sticky="new", padx=(0, 6), pady=(0, 6))
        self._ek_cycles_count_var = tk.StringVar(value="")
        self._ek_sequences_count_var = tk.StringVar(value="")
        self._ek_periodicity_var = tk.StringVar(value="")
        self._ek_signature_var = tk.StringVar(value="—")
        self._ek_chip1_var = tk.StringVar(value="—")
        self._ek_chip2_var = tk.StringVar(value="—")
        r = 0
        for label, var, editable in (
            ("Cycles:", self._ek_cycles_count_var, True),
            ("Sequences:", self._ek_sequences_count_var, True),
            ("Periodicity (ms):", self._ek_periodicity_var, True),
        ):
            ttk.Label(cfg_lf, text=label).grid(row=r, column=0, sticky="e", padx=4, pady=2)
            ttk.Entry(cfg_lf, textvariable=var, width=10).grid(row=r, column=1, sticky="w", padx=(0, 4))
            r += 1
        ttk.Label(cfg_lf, text="Signature:").grid(row=r, column=0, sticky="e", padx=4, pady=2)
        ttk.Label(cfg_lf, textvariable=self._ek_signature_var).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(cfg_lf, text="Chip 1 (ID / Age):").grid(row=r, column=0, sticky="e", padx=4, pady=2)
        ttk.Label(cfg_lf, textvariable=self._ek_chip1_var).grid(row=r, column=1, columnspan=3, sticky="w")
        r += 1
        ttk.Label(cfg_lf, text="Chip 2 (ID / Age):").grid(row=r, column=0, sticky="e", padx=4, pady=2)
        ttk.Label(cfg_lf, textvariable=self._ek_chip2_var).grid(row=r, column=1, columnspan=3, sticky="w")

        cyc_lf = ttk.LabelFrame(body, text="C — Cycle")
        cyc_lf.grid(row=0, column=1, sticky="new", padx=(6, 0), pady=(0, 6))
        self._ek_cycle_index_var = tk.StringVar(value="1")
        self._ek_cycle_numseq_var = tk.StringVar(value="")
        self._ek_cycle_seqorder_var = tk.StringVar(value="")
        self._ek_cycle_loopback_var = tk.BooleanVar(value=False)
        ttk.Label(cyc_lf, text="Index:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self._ek_cycle_index_spin = ttk.Spinbox(
            cyc_lf, from_=1, to=nzb.MAX_CYCLES_NB, width=6, textvariable=self._ek_cycle_index_var,
            command=self._ek_on_cycle_index_changed)
        self._ek_cycle_index_spin.grid(row=0, column=1, sticky="w")
        self._ek_cycle_index_spin.bind("<Return>", self._ek_on_cycle_index_changed)
        self._ek_cycle_index_spin.bind("<FocusOut>", self._ek_on_cycle_index_changed)
        ttk.Label(cyc_lf, text="Number of sequences:").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(cyc_lf, textvariable=self._ek_cycle_numseq_var, width=10).grid(row=1, column=1, sticky="w")
        ttk.Label(cyc_lf, text="Sequence order (comma-sep, UI index):").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(cyc_lf, textvariable=self._ek_cycle_seqorder_var, width=24).grid(
            row=2, column=1, columnspan=2, sticky="w")
        ttk.Checkbutton(cyc_lf, text="Loop back (not yet located in EEPROM — disabled)",
                        variable=self._ek_cycle_loopback_var, state="disabled").grid(
                        row=3, column=0, columnspan=3, sticky="w", padx=4, pady=(4, 2))

        seq_lf = ttk.LabelFrame(body, text="D.a — Sequence settings")
        seq_lf.grid(row=1, column=0, sticky="new", padx=(0, 6), pady=(0, 6))
        self._ek_seq_index_var = tk.StringVar(value="1")
        self._ek_seq_duration_var = tk.StringVar(value="")
        self._ek_seq_delay_var = tk.StringVar(value="")
        self._ek_seq_chip_var = tk.StringVar(value="1")
        self._ek_seq_sensor_var = tk.StringVar(value="")
        ttk.Label(seq_lf, text="Index:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self._ek_seq_index_spin = ttk.Spinbox(
            seq_lf, from_=1, to=nzb.MAX_SEQUENCE_NB, width=6, textvariable=self._ek_seq_index_var,
            command=self._ek_on_seq_index_changed)
        self._ek_seq_index_spin.grid(row=0, column=1, sticky="w")
        self._ek_seq_index_spin.bind("<Return>", self._ek_on_seq_index_changed)
        self._ek_seq_index_spin.bind("<FocusOut>", self._ek_on_seq_index_changed)
        ttk.Label(seq_lf, text="Duration (s):").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(seq_lf, textvariable=self._ek_seq_duration_var, width=10).grid(row=1, column=1, sticky="w")
        ttk.Label(seq_lf, text="Delay (s):").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(seq_lf, textvariable=self._ek_seq_delay_var, width=10).grid(row=2, column=1, sticky="w")
        ttk.Label(seq_lf, text="Chip:").grid(row=3, column=0, sticky="e", padx=4, pady=2)
        ttk.Combobox(seq_lf, textvariable=self._ek_seq_chip_var, values=("1", "2"),
                    state="readonly", width=4).grid(row=3, column=1, sticky="w")
        ttk.Label(seq_lf, text="Sensors-NZG2 (mV, all sensors):").grid(row=4, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(seq_lf, textvariable=self._ek_seq_sensor_var, width=10).grid(row=4, column=1, sticky="w")

        heat_lf = ttk.LabelFrame(body, text="D.b — Heater settings  (Table 2: Heater control parameters)")
        heat_lf.grid(row=1, column=1, sticky="new", padx=(6, 0), pady=(0, 6))
        for c, text in enumerate(("Parameter", "Value", "Unit", "Min", "Max")):
            ttk.Label(heat_lf, text=text, font=("TkDefaultFont", 8, "bold")).grid(
                row=0, column=c, sticky="w", padx=4)
        self._ek_heater_vars = {}
        heater_rows = (
            ("heater1_low_mv", "1. Heater 1 — low state", "mV", "0", "2200"),
            ("heater1_high_mv", "1. Heater 1 — high state", "mV", "0", "2200"),
            ("heater2_low_mv", "2. Heater 2 — low state", "mV", "0", "2200"),
            ("heater2_high_mv", "2. Heater 2 — high state", "mV", "0", "2200"),
            ("ramp_up_ms", "3. Ramp up time", "ms", "0", "60000"),
            ("high_duration_ms", "4. High state duration", "ms", "0", "60000"),
            ("ramp_down_ms", "5. Ramp down time", "ms", "0", "60000"),
            ("low_duration_ms", "6. Low state duration", "ms", "0", "60000"),
            ("phase_shift_ms", "7. Phase shift Heater 1/2", "ms", "0", "60000"),
            ("resolution_ms", "8. Time resolution", "ms", "0", "10000"),
        )
        for i, (key, label, unit, lo, hi) in enumerate(heater_rows, start=1):
            var = tk.StringVar(value="")
            self._ek_heater_vars[key] = var
            ttk.Label(heat_lf, text=label).grid(row=i, column=0, sticky="w", padx=4, pady=1)
            ttk.Entry(heat_lf, textvariable=var, width=8).grid(row=i, column=1, padx=4)
            ttk.Label(heat_lf, text=unit).grid(row=i, column=2, sticky="w")
            ttk.Label(heat_lf, text=lo, foreground="#9ca3af").grid(row=i, column=3)
            ttk.Label(heat_lf, text=hi, foreground="#9ca3af").grid(row=i, column=4)

        raw_lf = ttk.LabelFrame(body, text="Raw sequence record (debug)")
        raw_lf.grid(row=2, column=0, columnspan=2, sticky="new")
        self._ek_seq_raw_var = tk.StringVar(value="(no data read yet)")
        ttk.Label(raw_lf, textvariable=self._ek_seq_raw_var, font=("Consolas", 8),
                 foreground="#6b7280").pack(anchor="w", padx=8, pady=4)

        self._ek_cycles_by_index = {}
        self._ek_sequences_by_index = {}
        self._ek_write_board = None

    def _ek_refresh_board_list(self):
        ports = sorted(self._boards.keys())
        labels = [self._board_label(p) for p in ports]
        self._ek_board_label_to_port = dict(zip(labels, ports))
        self._ek_board_cb.config(values=labels)
        if self._ek_board_var.get() not in ports and ports:
            self._ek_board_var.set(ports[0])
        current = self._ek_board_var.get()
        if current in self._boards:
            self._ek_board_label_var.set(self._board_label(current))

    def _on_ek_board_picked(self, _event=None):
        port = getattr(self, "_ek_board_label_to_port", {}).get(self._ek_board_label_var.get())
        if port:
            self._ek_board_var.set(port)

    def _ek_read_configuration(self):
        self._ek_refresh_board_list()
        port = self._ek_board_var.get()
        board = self._boards.get(port)
        if not board or board.state != "connected":
            messagebox.showerror("No Board Connected",
                                 "Pick a connected board first (Setup tab -> Connect All).")
            return
        self._ek_write_board = board
        self._btn_ek_read.config(state="disabled")
        self._btn_ek_write.config(state="disabled")
        self._ek_status_var.set("Reading...")
        threading.Thread(target=self._ek_read_thread, args=(board,), daemon=True).start()

    def _ek_request_eeprom_sync(self, board: "nzb.NanoZBoard", addr: int, length: int,
                                timeout_s: float = 3.0) -> "bytes | None":
        self._latest_eep.pop(board.port, None)
        board.request_eeprom(addr, length)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            item = self._latest_eep.get(board.port)
            if item and item.get("addr") == addr and item.get("len") == length:
                return bytes.fromhex(item.get("data_hex", ""))
            time.sleep(0.05)
        return None

    def _ek_read_thread(self, board: "nzb.NanoZBoard"):
        try:
            params_bytes = bytearray()
            for addr in (0, 64, 128):
                chunk = self._ek_request_eeprom_sync(board, addr, 64)
                if chunk is None:
                    self.after(0, lambda a=addr: self._ek_read_failed(f"PARAMS @ {a}"))
                    return
                params_bytes += chunk
                time.sleep(0.05)
            params = nzb.parse_params_block(bytes(params_bytes))

            cycles_nb = max(1, min(params["cycles_configured"], nzb.MAX_CYCLES_NB))
            cycles = []
            for i in range(cycles_nb):
                addr = nzb.EEPROM_CYCLES_ADDR + i * nzb.EEPROM_CYCLE_RECORD_SIZE
                chunk = self._ek_request_eeprom_sync(board, addr, nzb.EEPROM_CYCLE_RECORD_SIZE)
                if chunk is None:
                    self.after(0, lambda a=addr: self._ek_read_failed(f"CYCLES @ {a}"))
                    return
                rec = nzb.parse_cycle_record(chunk)
                if rec:
                    cycles.append(rec)
                time.sleep(0.05)

            seq_bytes = bytearray()
            for i in range(4):
                addr = nzb.EEPROM_SEQUENCES_ADDR + i * 64
                chunk = self._ek_request_eeprom_sync(board, addr, 64)
                if chunk is None:
                    self.after(0, lambda a=addr: self._ek_read_failed(f"SEQUENCES @ {a}"))
                    return
                seq_bytes += chunk
                time.sleep(0.05)
            sequences = nzb.parse_sequence_records(bytes(seq_bytes))

            self.after(0, lambda: self._ek_display_results(params, cycles, sequences))
        except Exception as e:
            self.after(0, lambda e=e: self._ek_read_failed(str(e)))

    def _ek_read_failed(self, where: str):
        self._btn_ek_read.config(state="normal")
        self._ek_status_var.set(f"Read failed/timed out at {where}.")
        self._log_main(f"NanoZ_EK: EEPROM read failed at {where}.")

    def _ek_display_results(self, params: dict, cycles: list, sequences: list):
        self._btn_ek_read.config(state="normal")
        self._ek_status_var.set(f"Read OK — {len(cycles)} cycle(s), {len(sequences)} sequence(s).")

        def age_str(seconds):
            h, rem = divmod(int(seconds), 3600)
            m, s = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"

        c1, c2 = params["chip1"], params["chip2"]
        self._ek_cycles_count_var.set(str(params["cycles_configured"]))
        self._ek_sequences_count_var.set(str(len(sequences)))
        self._ek_periodicity_var.set(str(params["periodicity_ms"]))
        self._ek_signature_var.set(params["signature"])
        self._ek_chip1_var.set(f"{c1['id']}  /  {age_str(c1['age_s'])}")
        self._ek_chip2_var.set(f"{c2['id']}  /  {age_str(c2['age_s'])}")

        self._ek_cycles_by_index = {c["wire_index"] + 1: c for c in cycles}
        self._ek_sequences_by_index = {s["wire_index"] + 1: s for s in sequences}

        cyc_max = max(self._ek_cycles_by_index.keys(), default=1)
        self._ek_cycle_index_spin.config(to=max(cyc_max, 1))
        first_cycle = min(self._ek_cycles_by_index.keys(), default=1)
        self._ek_cycle_index_var.set(str(first_cycle))
        self._ek_load_cycle(first_cycle)

        seq_max = max(self._ek_sequences_by_index.keys(), default=1)
        self._ek_seq_index_spin.config(to=max(seq_max, 1))
        first_seq = min(self._ek_sequences_by_index.keys(), default=1)
        self._ek_seq_index_var.set(str(first_seq))
        self._ek_load_sequence(first_seq)

    def _ek_on_cycle_index_changed(self, _event=None):
        try:
            idx = int(self._ek_cycle_index_var.get())
        except ValueError:
            return
        self._ek_load_cycle(idx)

    def _ek_load_cycle(self, idx: int):
        c = self._ek_cycles_by_index.get(idx)
        if not c:
            self._ek_cycle_numseq_var.set("—")
            self._ek_cycle_seqorder_var.set("—")
            return
        self._ek_cycle_numseq_var.set(str(c["num_sequences"]))
        self._ek_cycle_seqorder_var.set(
            ", ".join(str(r + 1) for r in c["sequence_refs"]) or "—")

    def _ek_on_seq_index_changed(self, _event=None):
        try:
            idx = int(self._ek_seq_index_var.get())
        except ValueError:
            return
        self._ek_load_sequence(idx)

    def _ek_load_sequence(self, idx: int):
        s = self._ek_sequences_by_index.get(idx)
        if not s:
            self._ek_seq_duration_var.set("—")
            self._ek_seq_delay_var.set("—")
            self._ek_seq_sensor_var.set("—")
            for var in self._ek_heater_vars.values():
                var.set("—")
            self._ek_seq_raw_var.set("(no sequence at this index)")
            self._btn_ek_write.config(state="disabled")
            return

        def fmt(v):
            return "—" if v is None else str(v)

        self._ek_seq_duration_var.set(fmt(s.get("duration_s")))
        self._ek_seq_delay_var.set(fmt(s.get("delay_s")))
        self._ek_seq_chip_var.set(fmt(s.get("chip")))
        self._ek_seq_sensor_var.set(fmt(s.get("sensor_mv")))
        for key, var in self._ek_heater_vars.items():
            var.set(fmt(s.get(key)))
        cc = s.get("chip_candidates")
        rc = s.get("resolution_candidates")
        self._ek_seq_raw_var.set(
            f"chip candidates: {cc}   resolution candidates: {rc}\n{s.get('raw_hex', '')}")
        self._btn_ek_write.config(
            state="normal" if self._ek_write_board is not None else "disabled")

    def _ek_d_field_specs(self):
        return [
            ("duration_s", self._ek_seq_duration_var, "Duration (s)", 0, 60000),
            ("delay_s", self._ek_seq_delay_var, "Delay (s)", 0, 60000),
            ("sensor_mv", self._ek_seq_sensor_var, "Sensors-NZG2 (mV)", -800, 800),
            ("ramp_up_ms", self._ek_heater_vars["ramp_up_ms"], "Ramp up time (ms)", 0, 60000),
            ("high_duration_ms", self._ek_heater_vars["high_duration_ms"], "High state duration (ms)", 0, 60000),
            ("ramp_down_ms", self._ek_heater_vars["ramp_down_ms"], "Ramp down time (ms)", 0, 60000),
            ("low_duration_ms", self._ek_heater_vars["low_duration_ms"], "Low state duration (ms)", 0, 60000),
            ("phase_shift_ms", self._ek_heater_vars["phase_shift_ms"], "Phase shift H1/H2 (ms)", 0, 60000),
            ("heater1_low_mv", self._ek_heater_vars["heater1_low_mv"], "Heater 1 low state (mV)", 0, 2200),
            ("heater2_low_mv", self._ek_heater_vars["heater2_low_mv"], "Heater 2 low state (mV)", 0, 2200),
            ("heater1_high_mv", self._ek_heater_vars["heater1_high_mv"], "Heater 1 high state (mV)", 0, 2200),
            ("heater2_high_mv", self._ek_heater_vars["heater2_high_mv"], "Heater 2 high state (mV)", 0, 2200),
        ]

    def _ek_write_sequence(self):
        try:
            idx = int(self._ek_seq_index_var.get())
        except ValueError:
            messagebox.showerror("Invalid Index", "Sequence index must be a number.")
            return
        s = self._ek_sequences_by_index.get(idx)
        board = self._ek_write_board
        if not s or board is None or board.state != "connected":
            messagebox.showerror("Not Ready", "Read this sequence from a connected board first.")
            return

        fields, errors, changes = {}, [], []
        for key, var, label, lo, hi in self._ek_d_field_specs():
            raw = var.get().strip()
            try:
                val = int(raw)
            except ValueError:
                errors.append(f"{label}: '{raw}' is not a whole number")
                continue
            if not (lo <= val <= hi):
                errors.append(f"{label}: {val} is outside the documented range [{lo}, {hi}]")
                continue
            fields[key] = val
            old = s.get(key)
            if old != val:
                changes.append(f"  {label}: {old} -> {val}")

        if errors:
            messagebox.showerror("Out of Range", "Fix these before writing:\n\n" + "\n".join(errors))
            return
        if not changes:
            messagebox.showinfo("Nothing Changed", "No D.a/D.b field differs from the last read - nothing to write.")
            return

        cc = s.get("chip_candidates")
        rc = s.get("resolution_candidates")
        proceed = messagebox.askyesno(
            "Confirm Write to Real Hardware",
            "This sends a real wreep write to the board's EEPROM. This is NOT simulated.\n\n"
            f"Sequence (UI index {idx}) changes:\n" + "\n".join(changes) + "\n\n"
            "Untouched (preserved exactly as last read): wire_index, sensor/heater padding "
            f"bytes, Chip (candidates {cc}, offset ambiguous), Resolution (candidates {rc}, "
            "offset ambiguous), and everything in the Cycle/Configuration sections.\n\n"
            "If the checksum this app computes doesn't match what the board expects, the "
            "board rejects the write outright (per the protocol doc) rather than corrupting "
            "anything - but there is no undo if the write DOES succeed with a wrong value.\n\n"
            "Proceed?",
            icon="warning")
        if not proceed:
            return

        self._btn_ek_write.config(state="disabled")
        self._ek_status_var.set("Writing...")
        threading.Thread(target=self._ek_write_thread, args=(board, s, fields, idx), daemon=True).start()

    def _ek_write_thread(self, board: "nzb.NanoZBoard", s: dict, fields: dict, idx: int):
        try:
            original = bytes.fromhex(s["raw_hex"])
            patched = nzb.encode_sequence_patch(original, fields)
            addr = nzb.EEPROM_SEQUENCES_ADDR + s["blob_offset"]
            board.write_eeprom(addr, bytes(patched))
            time.sleep(1.0)
            length = s.get("record_len", len(patched))
            readback = self._ek_request_eeprom_sync(board, addr, length, timeout_s=3.0)
            ok = readback is not None and bytes(readback) == bytes(patched)
            self.after(0, lambda: self._ek_write_done(idx, ok, readback))
        except Exception as e:
            self.after(0, lambda e=e: self._ek_write_failed(str(e)))

    def _ek_write_done(self, idx: int, ok: bool, readback):
        self._btn_ek_write.config(state="normal")
        if ok:
            self._ek_status_var.set(f"Write OK — verified by readback (sequence UI index {idx}).")
            self._log_main(f"NanoZ_EK: wrote sequence {idx}, readback matches.")
        else:
            self._ek_status_var.set("Write sent, but readback did NOT match — check Console tab log for an error line, then re-read.")
            self._log_main(f"NanoZ_EK: wrote sequence {idx}, readback MISMATCH — "
                           f"got {readback.hex() if readback else None}.")

    def _ek_write_failed(self, msg: str):
        self._btn_ek_write.config(state="normal")
        self._ek_status_var.set(f"Write failed: {msg}")
        self._log_main(f"NanoZ_EK: write failed — {msg}")

    def _charts_tab_visible(self):
        if not _MPL:
            return False
        try:
            return self._sub_nb.select() == str(self._charts_tab)
        except Exception:
            return False

    def _refresh_charts_loop(self):
        if self._charts_tab_visible() and (self._chart_follow_live
                                           or self._chart_pinned_time is not None):
            self._redraw_charts()
        self.after(300, self._refresh_charts_loop)

    @staticmethod
    def _pkt_time(item: dict):
        ts = item.get("host_timestamp")
        if not ts:
            return None
        try:
            return dt.datetime.fromisoformat(ts)
        except ValueError:
            return None

    def _elapsed_seconds(self, hist: list, t0: "dt.datetime"):
        out = []
        for item in hist:
            t = self._pkt_time(item)
            out.append((t - t0).total_seconds() if t else float("nan"))
        return out

    def _break_gaps(self, xs: list, ys: list):
        out_x, out_y = [], []
        prev = None
        for x, y in zip(xs, ys):
            if prev is not None and (x - prev) > self._CHART_GAP_THRESHOLD_S:
                out_x.append(float("nan"))
                out_y.append(float("nan"))
            out_x.append(x)
            out_y.append(y)
            prev = x
        return out_x, out_y

    def _plot_series(self, ax, xs: list, hist: list, field: str, label: str, linestyle: str = "-"):
        ys = [r.get(field, 0) for r in hist]
        gx, gy = self._break_gaps(xs, ys)
        ax.plot(gx, gy, label=label, linestyle=linestyle)

    def _plot_computed(self, ax, xs: list, hist: list, value_fn, label: str, linestyle: str = "-"):
        ys = [value_fn(r) for r in hist]
        ys = [float("nan") if y is None else y for y in ys]
        gx, gy = self._break_gaps(xs, ys)
        ax.plot(gx, gy, label=label, linestyle=linestyle)

    _SENSOR_METRIC_UNITS = {"Current": "mA", "Resistance": "kΩ"}
    _HEATER_METRIC_UNITS = {"Voltage": "mV", "Current": "mA", "Power": "mW", "Resistance": "Ω"}

    @staticmethod
    def _sensor_metric_value_for(rec: dict, s: int, metric: str):
        if metric == "Resistance":
            v, i = rec.get(f"dac_mv_s{s}"), rec.get(f"adc_current_ma_s{s}")
            return v / i if (v is not None and i) else None
        return rec.get(f"adc_current_ma_s{s}")

    def _sensor_metric_value(self, rec: dict, s: int):
        return self._sensor_metric_value_for(rec, s, self._chart_sensor_metric_var.get())

    def _evaluate_die_pass_fail(self, port: str, chip: str) -> "bool | None":
        rec = self._latest_spl.get((port, chip))
        if rec is None:
            return None
        metric = self._pf_metric_var.get()
        for s in (1, 2, 3, 4):
            mn_var, mx_var = self._pf_limit_vars[s]
            mn, mx = mn_var.get().strip(), mx_var.get().strip()
            if not mn and not mx:
                continue
            value = self._sensor_metric_value_for(rec, s, metric)
            if value is None:
                return False
            try:
                if mn and value < float(mn):
                    return False
                if mx and value > float(mx):
                    return False
            except ValueError:
                continue
        return True

    def _heater_metric_value(self, rec: dict, h: int):
        metric = self._chart_heater_metric_var.get()
        v, i = rec.get(f"heater{h}_voltage_mv"), rec.get(f"heater{h}_current_ma")
        if metric == "Voltage":
            return v
        if metric == "Current":
            return i
        if metric == "Power":
            return v * i / 1000 if (v is not None and i is not None) else None
        if metric == "Resistance":
            return v / i if (v is not None and i) else None
        return None

    def _on_chart_xlim_changed(self, _ax):
        if self._chart_programmatic_xlim:
            return
        self._chart_follow_live = False
        self._chart_pinned_time = None

    def _on_chart_button_press(self, _event):
        self._chart_follow_live = False
        self._chart_pinned_time = None

    def _on_chart_scroll_zoom(self, event):
        if event.inaxes not in (self._chart_ax_v, self._chart_ax_i, self._chart_ax_t):
            return
        if event.xdata is None:
            return
        factor = 0.85 if event.button == "up" else (1 / 0.85)
        xlim = self._chart_ax_v.get_xlim()
        xd = event.xdata
        self._chart_ax_v.set_xlim(xd - (xd - xlim[0]) * factor, xd + (xlim[1] - xd) * factor)
        self._chart_canvas.draw_idle()

    def _chart_resume_live(self):
        self._chart_follow_live = True
        self._chart_pinned_time = None
        self._redraw_charts()

    def _redraw_charts(self, preserve_view: bool = False):
        if not _MPL:
            return
        port = self.console_board_var.get()
        hist_by_chip = {
            "0": [h for h in self._spl_history.get((port, "0"), ()) if h.get("_settled", True)],
            "1": [h for h in self._spl_history.get((port, "1"), ()) if h.get("_settled", True)],
        }
        env_hist = list(self._env_history.get(port, ()))

        prev_xlim = self._chart_ax_v.get_xlim()
        self._chart_ax_v.clear()
        self._chart_ax_i.clear()
        self._chart_ax_t.clear()

        if port and port not in self._chart_t0_by_port:
            candidates = [self._pkt_time(h[0]) for h in (*hist_by_chip.values(), env_hist) if h]
            candidates = [t for t in candidates if t is not None]
            if candidates:
                self._chart_t0_by_port[port] = min(candidates)
        t0 = self._chart_t0_by_port.get(port, dt.datetime.now())
        t_max = 0.0
        visible = self._chart_visible_vars
        chip_visible = self._chart_chip_visible_vars

        any_spl = False
        for chip, hist, linestyle in (("0", hist_by_chip["0"], "-"), ("1", hist_by_chip["1"], "--")):
            if not hist or not chip_visible[chip].get():
                continue
            any_spl = True
            xs = self._elapsed_seconds(hist, t0)
            t_max = max(t_max, max(xs, default=0.0))
            chip_disp = self._CHIP_LABELS[chip].split()[0]
            for h in (1, 2):
                if visible[f"h{h}"].get():
                    self._plot_computed(self._chart_ax_v, xs, hist,
                                        lambda r, h=h: self._heater_metric_value(r, h),
                                        f"chip{chip_disp}-h{h}", linestyle=linestyle)
            for s in (1, 2, 3, 4):
                if visible[f"s{s}"].get():
                    self._plot_computed(self._chart_ax_i, xs, hist,
                                        lambda r, s=s: self._sensor_metric_value(r, s),
                                        f"chip{chip_disp}-s{s}", linestyle=linestyle)
        if any_spl:
            self._chart_ax_v.legend(fontsize=6, loc="upper left", ncol=2)
            self._chart_ax_i.legend(fontsize=6, loc="upper left", ncol=4)
        else:
            for ax in (self._chart_ax_v, self._chart_ax_i):
                ax.text(0.5, 0.5, "no SPL data yet (needs an active run)",
                        ha="center", va="center", transform=ax.transAxes, color="#999999")

        if env_hist:
            xs2 = self._elapsed_seconds(env_hist, t0)
            t_max = max(t_max, max(xs2, default=0.0))
            self._plot_series(self._chart_ax_t, xs2, env_hist, "temp_h_c", "temp_h_c")
            self._plot_series(self._chart_ax_t, xs2, env_hist, "mcu_temperature_c", "mcu_temp")
            self._chart_ax_t.legend(fontsize=7, loc="upper left")
        else:
            self._chart_ax_t.text(0.5, 0.5, "no ENV data yet", ha="center", va="center",
                                  transform=self._chart_ax_t.transAxes, color="#999999")

        h_metric = self._chart_heater_metric_var.get()
        s_metric = self._chart_sensor_metric_var.get()
        self._chart_ax_v.set_title(
            f"Heater {h_metric} ({self._HEATER_METRIC_UNITS[h_metric]}) — SPL (both chips)", fontsize=9)
        self._chart_ax_i.set_title(
            f"Sensor {s_metric} ({self._SENSOR_METRIC_UNITS[s_metric]}) — SPL (both chips)", fontsize=9)
        self._chart_ax_t.set_title("Temperature (°C) — ENV", fontsize=9)
        if self._chart_follow_live:
            live_note = ""
        elif self._chart_pinned_time is not None:
            live_note = "  [PAUSED at cycle start — ▶ Jump to Live to resume auto-scroll]"
        else:
            live_note = "  [PAUSED — ▶ Jump to Live to resume auto-scroll]"
        self._chart_ax_t.set_xlabel(f"time (s, board {port or '—'}){live_note}")

        self._chart_programmatic_xlim = True
        try:
            if self._chart_follow_live and not preserve_view:
                self._chart_ax_v.set_xlim(max(0.0, t_max - self._CHART_WINDOW_S), max(t_max, self._CHART_WINDOW_S))
            elif self._chart_pinned_time is not None and not preserve_view:
                pin_elapsed = max(0.0, (self._chart_pinned_time - t0).total_seconds())
                self._chart_ax_v.set_xlim(pin_elapsed, pin_elapsed + self._CHART_WINDOW_S)
            else:
                self._chart_ax_v.set_xlim(prev_xlim)
        finally:
            self._chart_programmatic_xlim = False
        self._chart_canvas.draw_idle()

    _LOCKABLE_WIDGETS = ("_cycle_entry", "_duration_entry", "_btn_discover",
                        "_btn_connect_boards", "_btn_disconnect_boards",
                        "_btn_manual_zup", "_btn_manual_zdown", "_btn_manual_first_die",
                        "_btn_manual_next_die", "_btn_manual_xy", "_btn_manual_unload",
                        "_btn_measure",
                        "_btn_test_active", "_btn_pause_active",
                        "_btn_recipe_up", "_btn_recipe_down",
                        "_btn_recipe_enable_all", "_btn_recipe_disable_all")

    _CHART_HISTORY_LEN = 300
    _CHART_GAP_THRESHOLD_S = 3.0
    _CHART_WINDOW_S = 15.0

    def _set_locked(self, locked: bool):
        state = "disabled" if locked else "normal"
        for attr in self._LOCKABLE_WIDGETS:
            getattr(self, attr).config(state=state)

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"{ts}  {msg}"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_main(self, msg: str):
        self._log(msg)
        if hasattr(self.controller, "log"):
            self.controller.log(f"[NANOZ] {msg}")

    def _probe_height(self) -> int:
        try:
            n = int(self._probe_height_var.get())
        except (tk.TclError, ValueError):
            return nzb.DEFAULT_PROBE_HEIGHT
        return n if n > 0 else nzb.DEFAULT_PROBE_HEIGHT

    def _on_probe_height_change(self):
        n = self._probe_height()
        if int(self._probe_height_var.get() or 0) != n:
            self._probe_height_var.set(n)
        folder = self._nanoz_ata_folder
        if folder:
            try:
                nzb.save_probe_height(folder, n)
            except OSError as e:
                self._log_main(f"Could not save probe head slot count: {e}")
        if self._wafer_plan is not None:
            if self._system == "electroglas":
                self._eg_refresh_wafer_plan_from_wafer_builder(silent=True)
            else:
                self._wafer_plan.probe_height = n
                self._log_main(
                    f"Probe head slots set to {n} - re-import the wafer plan "
                    "(.xlsx) to rebuild it against the new slot count.")
        self._update_position_window()
        self._log_main(f"Probe head slots set to {n}.")

    def _discover_boards(self):
        threading.Thread(target=self._discover_boards_thread, daemon=True).start()

    def _discover_boards_thread(self):
        self.after(0, lambda: self._log("Scanning COM ports for NanoZ boards..."))
        found = nzb.discover_boards(log=lambda m: self.after(0, lambda m=m: self._log(m)))
        self.after(0, lambda: self._on_discovered(found))

    @staticmethod
    def _synthetic_board_key(serial_number: str) -> str:
        return f"SN:{serial_number}"

    @staticmethod
    def _board_port_display(ident: "nzb.BoardIdentity") -> str:
        if ident.port:
            return ident.port
        if ident.last_port:
            return f"{ident.last_port} (last known)"
        return "—"

    @staticmethod
    def _sn_display(serial_number: str) -> str:
        sn = serial_number or ""
        hex_digits = "".join(ch for ch in sn if ch in "0123456789abcdefABCDEF")
        last4 = hex_digits[-4:]
        if not last4:
            return sn
        try:
            return f"{sn} ({int(last4, 16)})"
        except ValueError:
            return sn

    def _add_board(self, ident: "nzb.BoardIdentity"):
        existing_key = None
        if ident.serial_number:
            for key, b in self._boards.items():
                if b.identity.serial_number == ident.serial_number:
                    existing_key = key
                    break
        new_key = ident.port or self._synthetic_board_key(ident.serial_number)

        if existing_key is not None:
            if existing_key == new_key:
                return None
            board = self._boards.pop(existing_key)
            old_iid = self._board_rows.pop(existing_key, None)
            old_identity = board.identity
            board.identity = nzb.BoardIdentity(
                port=ident.port, serial_number=ident.serial_number,
                firmware=ident.firmware, signature=ident.signature,
                raw_ver=ident.raw_ver, raw_whoami=ident.raw_whoami, usb_id=ident.usb_id,
                slot0=old_identity.slot0 if old_identity.slot0 is not None else ident.slot0,
                slot1=old_identity.slot1 if old_identity.slot1 is not None else ident.slot1,
                last_port=ident.port or old_identity.last_port or ident.last_port,
            )
            board.port = ident.port
            self._boards[new_key] = board
            if old_iid is not None:
                self._board_tree.item(old_iid, values=(
                    self._board_port_display(board.identity),
                    self._sn_display(board.identity.serial_number),
                    board.identity.signature,
                    board.identity.slot0 if board.identity.slot0 else "—",
                    board.identity.slot1 if board.identity.slot1 else "—",
                    self._board_status_text(board)))
                self._board_rows[new_key] = old_iid
            return board

        if new_key in self._boards:
            return None
        try:
            env_interval_s = float(self.env_interval_var.get())
        except ValueError:
            env_interval_s = 1.0
        board = nzb.NanoZBoard(ident, self._queue, env_interval_s=env_interval_s)
        board._die_provider = lambda chip: self._die_provider(board.port, chip)
        self._boards[new_key] = board
        iid = self._board_tree.insert("", "end", values=(
            self._board_port_display(ident), self._sn_display(ident.serial_number),
            ident.signature,
            ident.slot0 if ident.slot0 else "—",
            ident.slot1 if ident.slot1 else "—",
            self._board_status_text(board)))
        self._board_rows[new_key] = iid
        return board

    def _persist_boards(self):
        folder = self._nanoz_ata_folder
        if not folder:
            return
        try:
            nzb.save_known_boards(folder, [b.identity for b in self._boards.values()])
        except OSError as e:
            self._log(f"Could not save NanoZ board memory: {e}")

    def _on_discovered(self, found: list):
        added = sum(1 for ident in found if self._add_board(ident))
        self._log_main(f"Discovery complete — {len(found)} board(s) found, "
                       f"{added} new, {len(self._boards)} total known.")
        self._refresh_console_boards()
        self._rebuild_recipe_columns()
        self._persist_boards()

    def _connect_boards(self):
        targets = [b for b in self._boards.values() if b.state != "connected"]
        if not targets:
            self._log_main("Connect All: nothing to connect.")
            return
        threading.Thread(target=self._connect_boards_thread, args=(targets,), daemon=True).start()

    def _connect_boards_thread(self, targets: list):
        no_port = [b for b in targets if not b.port and b.identity.last_port]
        for board in no_port:
            board.port = board.identity.last_port
            board.identity.port = board.identity.last_port
            self.after(0, lambda ident=board.identity: self._add_board(ident))

        still_missing = []
        for board in targets:
            if not board.port:
                continue
            was_error = board.state == "error"
            try:
                board.reconnect() if was_error else board.start()
                verb = "reconnected" if was_error else "connected"
                self.after(0, lambda p=board.port, b=board: self._set_board_status(
                    p, self._board_status_text(b)))
                self.after(0, lambda p=board.port, v=verb: self._log(f"{p}: {v}, reader running"))
            except Exception as e:
                self.after(0, lambda p=board.port, e=e: self._set_board_status(
                    p, self._error_status_text(e)))
                self.after(0, lambda p=board.port, e=e: self._log(f"{p}: connect failed — {e}"))
                if board in no_port:
                    still_missing.append(board)
                    board.port = ""

        if still_missing:
            self.after(0, lambda n=len(still_missing): self._log_main(
                f"Connect All: {n} known board(s) didn't respond on their last-known port"))
            found = nzb.discover_boards(
                log=lambda m: self.after(0, lambda m=m: self._log(m)))
            found_by_sn = {f.serial_number: f for f in found if f.serial_number}
            for board in still_missing:
                ident = found_by_sn.get(board.identity.serial_number)
                if not ident:
                    self.after(0, lambda b=board: self._log_main(
                        f"Connect All: {b.identity.serial_number or '(no S/N)'} not found on "
                        f"any COM port."))
                    continue
                board.port = ident.port
                self.after(0, lambda ident=ident: self._add_board(ident))
                try:
                    board.reconnect() if board.state == "error" else board.start()
                    self.after(0, lambda p=board.port, b=board: self._set_board_status(
                        p, self._board_status_text(b)))
                    self.after(0, lambda p=board.port: self._log(f"{p}: connected, reader running"))
                except Exception as e:
                    self.after(0, lambda p=board.port, e=e: self._set_board_status(
                        p, self._error_status_text(e)))
                    self.after(0, lambda p=board.port, e=e: self._log(f"{p}: connect failed — {e}"))
            self.after(0, self._persist_boards)

        self.after(0, lambda: self._log_main(
            f"{sum(1 for b in targets if b.state == 'connected')}/{len(targets)} board(s) connected."))
        self.after(0, self._refresh_console_boards)
        self.after(0, self._rebuild_recipe_columns)

    def _disconnect_boards(self):
        if self._running:
            messagebox.showerror("Lot Running", "Stop the lot before disconnecting boards.")
            return
        targets = [b for b in self._boards.values() if b.is_running]
        if not targets:
            self._log_main("Disconnect Boards: nothing connected.")
            return
        threading.Thread(target=self._disconnect_boards_thread, args=(targets,), daemon=True).start()

    def _disconnect_boards_thread(self, targets: list):
        for board in targets:
            board.stop()
            self.after(0, lambda p=board.port, b=board: self._set_board_status(
                p, self._board_status_text(b)))
        self.after(0, lambda: self._log_main(
            f"{len(targets)} board(s) disconnected (ports closed)."))
        self.after(0, self._refresh_console_boards)
        self.after(0, self._rebuild_recipe_columns)

    def _set_board_status(self, port: str, status: str):
        iid = self._board_rows.get(port)
        if not iid:
            return
        vals = list(self._board_tree.item(iid, "values"))
        vals[5] = status
        self._board_tree.item(iid, values=vals)

    def _on_board_tree_double_click(self, event):
        if self._board_tree.identify_region(event.x, event.y) != "cell":
            return
        row_iid = self._board_tree.identify_row(event.y)
        col_id = self._board_tree.identify_column(event.x)
        if not row_iid or not col_id:
            return
        cols = self._board_tree["columns"]
        col_idx = int(col_id[1:]) - 1
        if not (0 <= col_idx < len(cols)) or cols[col_idx] not in ("slot0", "slot1"):
            return
        chip = "0" if cols[col_idx] == "slot0" else "1"
        vals_idx = col_idx
        key = next((k for k, v in self._board_rows.items() if v == row_iid), None)
        board = self._boards.get(key) if key is not None else None
        if not board:
            return
        label = board.identity.port or f"SN {board.identity.serial_number}"
        current = board.identity.slot0 if chip == "0" else board.identity.slot1
        max_slot = self._probe_height()
        new_slot = simpledialog.askinteger(
            "Assign Probe-Card Slot",
            f"Physical slot for {label}'s chip {chip} (1-{max_slot}, top to bottom "
            "of the probe head — see the Setup tab's Probe head slots):",
            initialvalue=min(current, max_slot) if current else 1,
            minvalue=1, maxvalue=max_slot, parent=self)
        if new_slot is None:
            return
        if chip == "0":
            board.identity.slot0 = new_slot
        else:
            board.identity.slot1 = new_slot
        vals = list(self._board_tree.item(row_iid, "values"))
        vals[vals_idx] = str(new_slot)
        self._board_tree.item(row_iid, values=vals)
        self._persist_boards()
        self._rebuild_recipe_columns()
        self._refresh_console_boards()
        self._log_main(f"{label} chip {chip} assigned to probe-card slot {new_slot}.")

    @staticmethod
    def _error_status_text(err) -> str:
        return f"⚠ error: {err}"[:80]

    def _board_status_text(self, board: "nzb.NanoZBoard") -> str:
        state = board.state
        if state == "error":
            return self._error_status_text(board.last_error)
        if state == "connected":
            return "✅ connected"
        return "— not connected"

    def _refresh_board_status_quiet(self):
        for port, board in self._boards.items():
            self._set_board_status(port, self._board_status_text(board))

    def _auto_refresh_board_status(self):
        self._refresh_board_status_quiet()
        self.after(1000, self._auto_refresh_board_status)

    def _refresh_board_status(self):
        self._refresh_board_status_quiet()
        connected = sum(1 for b in self._boards.values() if b.state == "connected")
        errored = sum(1 for b in self._boards.values() if b.state == "error")
        idle = len(self._boards) - connected - errored
        self._log_main(f"Refresh Status — {connected} connected, {errored} error(s), "
                       f"{idle} not connected ({len(self._boards)} known).")

    def on_ata_folder_loaded(self, folder_path: str):
        self._probe_height_var.set(nzb.load_probe_height(folder_path))
        self._cst_yield_folder = folder_path
        self._cst_yield_var.set(f"{load_yield_threshold(folder_path):g}")
        if self._system == "electroglas":
            self._eg_refresh_wafer_plan_from_wafer_builder(silent=True)
            plan = self._wafer_plan
            dies = ([{"row": r, "col": c, "x_um": float(c), "y_um": -float(r),
                     "die_id": d["serial"]} for (r, c), d in plan.dies.items()]
                   if plan else [])
            n = self.wafer_map.load_die_list(dies, label="dies")
        else:
            n = self.wafer_map.load_from_ata(folder_path, filename="ata_wafer_map_accretech.csv")
            overlay_ids = getattr(self._main_layout, "_exec_overlay_die_ids", None)
            if overlay_ids:
                self.wafer_map.die_ids.update(overlay_ids)
                self._redraw_overlay_on_run_map()
        if n:
            self._log_main(f"Wafer map auto-loaded from "
                           f"'{os.path.basename(folder_path)}' — {n} die(s).")
        self.wafer_map.clear_picks()

        remembered = nzb.load_known_boards(folder_path)
        added = sum(1 for ident in remembered if self._add_board(ident))
        if added:
            self._log_main(f"Remembered {added} NanoZ board(s) from this ATA folder "
                           f"— Connect All once they're plugged in.")
            self._refresh_console_boards()

        migrated = nzb.migrate_legacy_recipe(folder_path)
        if migrated:
            self._log_main(f"Migrated the old unnamed NanoZ recipe into '{migrated}'.")
        name, shots, wafer_plan_path = nzb.load_active_recipe(folder_path)
        self._shots = shots
        self._touchdowns = nzb.load_named_touchdowns(folder_path, name) if name else []
        self._current_recipe_name = name
        self._wafer_plan_path = wafer_plan_path
        if shots:
            self._log_main(f"Recipe '{name}' auto-loaded from "
                           f"'{os.path.basename(folder_path)}' — {len(shots)} shot(s).")
        self._refresh_recipe_name_cb()
        self._rebuild_recipe_columns()
        self._nz_refresh_td()

        plan_path = nzb.wafer_plan_path_in_folder(folder_path)
        if not os.path.isfile(plan_path) and name:
            plan_path = nzb.get_recipe_wafer_plan_path(folder_path, name)
        if plan_path and os.path.isfile(plan_path):
            self._autoload_wafer_plan(plan_path)
        else:
            self._wafer_plan = None
            lbl = getattr(self, "_recipe_plan_status_lbl", None)
            if lbl is not None:
                lbl.config(text="No wafer plan imported yet.", foreground="#6b7280")

    def _refresh_console_boards(self):
        ports = sorted(self._boards.keys())
        labels = [self._board_label(p) for p in ports]
        self._board_label_to_port = dict(zip(labels, ports))
        self._console_board_cb.config(values=labels)
        self._chart_board_cb.config(values=labels)
        if self.console_board_var.get() not in ports and ports:
            self.console_board_var.set(ports[0])
        current = self.console_board_var.get()
        if current in self._boards:
            self._console_board_label_var.set(self._board_label(current))
        self._refresh_console_reading()
        if hasattr(self, "_ek_board_cb"):
            self._ek_refresh_board_list()

    def _on_console_board_picked(self, _event=None):
        port = self._board_label_to_port.get(self._console_board_label_var.get())
        if port:
            self.console_board_var.set(port)
        self._refresh_console_reading()

    def _on_console_chip_picked(self, _event=None):
        value = self._CHIP_LABEL_TO_VALUE.get(self._console_chip_label_var.get())
        if value:
            self.console_chip_var.set(value)
        self._refresh_console_reading()

    def _console_selected_board(self):
        return self._boards.get(self.console_board_var.get())

    def _console_send(self, cmd: str):
        board = self._console_selected_board()
        if not board or not board.is_running:
            messagebox.showerror("No Board Selected",
                                 "Pick a connected board first (Setup tab -> Connect All).")
            return
        board.send_raw(cmd)
        self._log(f"{board.port}: >> {cmd}")

    def _console_send_raw(self):
        cmd = self.console_raw_var.get().strip()
        if not cmd:
            return
        self._console_send(cmd)

    def _console_run(self):
        try:
            cycle = int(self.console_cycle_var.get())
        except ValueError:
            messagebox.showerror("Invalid Cycle", "Cycle # must be a whole number.")
            return
        self._ensure_xy_then(self._console_run_body, cycle)

    def _console_run_body(self, cycle: int):
        self._mark_cycle_start(pin_chart=True)
        self._ensure_csv_paths()
        self._console_send(f"run {cycle}")

    def _console_calib_bang(self):
        if not messagebox.askyesno(
            "Run Calibration",
            "calib! runs the EK-IV's calibration routine and REQUIRES the "
            "10K-resistor calibration kit to be mounted in place of the "
            "normal sensors. Running it with real sensors attached will "
            "produce meaningless calibration offsets.\n\nContinue?"):
            return
        self._console_send("calib!")

    def _console_cleep(self):
        if not messagebox.askyesno(
            "Erase EEPROM",
            "cleep erases every stored cycle/sequence on this board's "
            "non-volatile memory. This cannot be undone from here — the "
            "board will need to be reprogrammed with Nanoz_EK before it "
            "can run a cycle again.\n\nContinue?"):
            return
        self._console_send("cleep")

    def _console_read_eeprom(self):
        board = self._console_selected_board()
        if not board or not board.is_running:
            messagebox.showerror("No Board Selected",
                                 "Pick a connected board first (Setup tab -> Connect All).")
            return
        try:
            addr = int(self.console_eep_addr_var.get())
            length = int(self.console_eep_len_var.get())
        except ValueError:
            messagebox.showerror("Invalid Input", "Address and length must be whole numbers.")
            return
        board.request_eeprom(addr, length)
        self._log(f"{board.port}: >> rdeep {addr} {length}")

    @staticmethod
    def _format_reading_lines(item: "dict | None"):
        if not item:
            return ["(none yet)"]
        return [f"{k}: {v}" for k, v in item.items() if k not in ("kind", "port")]

    @staticmethod
    def _format_eep_lines(item: "dict | None"):
        if not item:
            return ["(none yet)"]
        data_hex = item.get("data_hex", "")
        rows = [f"{k}: {v}" for k, v in item.items() if k not in ("kind", "port", "data_hex")]
        rows.append("")
        rows.append("data:")
        for i in range(0, len(data_hex), 32):
            offset = i // 2
            rows.append(f"  +{offset:04d}  {data_hex[i:i + 32]}")
        return rows

    def _refresh_console_reading(self):
        port = self.console_board_var.get()
        chip = self.console_chip_var.get()
        spl_lines = self._format_reading_lines(self._latest_spl.get((port, chip)))
        env_lines = self._format_reading_lines(self._latest_env.get(port))
        for widget, lines in ((self.console_spl_text, spl_lines),
                              (self.console_env_text, env_lines)):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", "\n".join(lines))
            widget.configure(state="disabled")
        self._refresh_console_eep_display()

    def _refresh_console_eep_display(self):
        port = self.console_board_var.get()
        lines = self._format_eep_lines(self._latest_eep.get(port))
        self.console_eep_text.configure(state="normal")
        self.console_eep_text.delete("1.0", "end")
        self.console_eep_text.insert("1.0", "\n".join(lines))
        self.console_eep_text.configure(state="disabled")

    def _new_csv_paths(self):
        folder = (self._nanoz_ata_folder
                 or self._main_layout.export_path_var.get()
                 or os.getcwd())
        os.makedirs(folder, exist_ok=True)
        run_id = time.strftime("%Y%m%d_%H%M%S")
        return (os.path.join(folder, f"ata_nanoz_spl_{run_id}.csv"),
               os.path.join(folder, f"ata_nanoz_env_{run_id}.csv"))

    def _check_queue(self):
        drained = 0
        while drained < 500:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            self._handle_packet(item)
        if drained:
            self.counts_var.set(f"SPL: {self._spl_total}   ENV: {self._env_total}")
        self.after(50, self._check_queue)

    def _ensure_csv_paths(self):
        if not self._spl_path:
            self._spl_path, self._env_path = self._new_csv_paths()

    def _arm_settling_skip(self, boards: list):
        self._ensure_csv_paths()
        for board in boards:
            self._skip_spl_count[(board.port, "0")] = self._SETTLING_SKIP_COUNT
            self._skip_spl_count[(board.port, "1")] = self._SETTLING_SKIP_COUNT
            board.set_active_die({
                "0": self._die_provider(board.port, "0"),
                "1": self._die_provider(board.port, "1"),
                None: self._die_provider(board.port, None),
            })

    def _handle_packet(self, item: dict):
        kind = item.get("kind")
        port = item.get("port")
        board = self._boards.get(port)
        if kind == "spl":
            chip = str(item.get("header_chip", ""))
            key = (port, chip)
            self._spl_total += 1
            self._touchdown_packets += 1
            if "parse_error" in item:
                self._touchdown_errors += 1
            remaining = self._skip_spl_count.get(key, 0)
            settled = remaining <= 0
            if not settled:
                self._skip_spl_count[key] = remaining - 1
            item["_settled"] = settled
            item["cycle_start"] = (self._cycle_start_time.isoformat()
                                   if self._cycle_start_time else "")
            self._spl_history.setdefault(
                key, collections.deque(maxlen=self._CHART_HISTORY_LEN)).append(item)
            if settled:
                self._latest_spl[key] = item
            if settled and self._spl_path:
                row = {k: v for k, v in item.items() if k not in ("kind", "_settled")}
                try:
                    nzb.append_csv_row(self._spl_path, row)
                except OSError as e:
                    self._log(f"SPL CSV write error: {e}")
            if port == self.console_board_var.get() and chip == self.console_chip_var.get():
                self._refresh_console_reading()
        elif kind == "env":
            self._env_total += 1
            self._touchdown_packets += 1
            if "parse_error" in item:
                self._touchdown_errors += 1
            self._latest_env[port] = item
            self._env_history.setdefault(
                port, collections.deque(maxlen=self._CHART_HISTORY_LEN)).append(item)
            if self._env_path:
                row = {k: v for k, v in item.items() if k != "kind"}
                try:
                    nzb.append_csv_row(self._env_path, row)
                except OSError as e:
                    self._log(f"ENV CSV write error: {e}")
            if port == self.console_board_var.get():
                self._refresh_console_reading()
        elif kind == "text":
            self._log(f"{port}: {item.get('text', '')}")
        elif kind == "unrecognized":
            self._log(f"{port}: UNRECOGNIZED HEADER: {item.get('raw')!r}")
        elif kind == "eep":
            self._latest_eep[port] = item
            status = "OK" if item.get("checksum_ok") else "CHECKSUM MISMATCH"
            self._log(f"{port}: EEPROM read addr={item['addr']} len={item['len']} ({status})")
            if port == self.console_board_var.get():
                self._refresh_console_eep_display()

    def _stop_lot(self):
        if not self._running:
            return
        self._running = False
        self._log_main("Stop requested.")
        for board in self._boards.values():
            try:
                board.pause()
            except Exception:
                pass

    def _run_guard(self, name: str) -> bool:
        if self._running:
            messagebox.showerror("Run Active", f"{name}: stop the current run first.")
            return True
        return False

    def _do_manual_call(self, name: str, fn, log_cmd: str, refresh_xy: bool = False) -> bool:
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self.after(0, lambda: self._log_main(f"{name}: prober not connected."))
            return False
        try:
            self.after(0, lambda: self._log(log_cmd))
            stb = fn(prober)
            self.after(0, lambda stb=stb: self._log(f"<< STB={stb}  ({name} complete)"))
            if refresh_xy:
                self._manual_xy_thread()
            return True
        except Exception as e:
            self.after(0, lambda e=e: self._log_main(f"{name} error: {e}"))
            return False

    def _manual_z_up(self):
        if self._run_guard("Z Up"):
            return
        threading.Thread(target=self._manual_z_up_thread, daemon=True).start()

    def _manual_z_up_thread(self):
        self._do_manual_call("Z Up", lambda p: p.z_up(), ">> Z  (Contact)")

    def _manual_z_down(self):
        if self._run_guard("Z Down"):
            return
        threading.Thread(target=self._manual_z_down_thread, daemon=True).start()

    def _manual_z_down_thread(self):
        self._do_manual_call("Z Down", lambda p: p.z_down(), ">> D  (Separate)")

    def _manual_first_die(self):
        if self._run_guard("First Die"):
            return
        threading.Thread(target=self._manual_first_die_thread, daemon=True).start()

    def _manual_first_die_thread(self):
        self._do_manual_call("First Die", lambda p: p.move_to_start_die(),
                             ">> G  (Position start die)", refresh_xy=True)


    def refresh_eg_anchor_choices(self):
        if self._system != "electroglas" or not hasattr(self, "_eg_anchor_cb"):
            return
        plan = self._wafer_plan
        serials = sorted(plan.serial_to_rc.keys()) if plan else []
        self._eg_anchor_cb.config(values=serials)

    def _eg_set_anchor(self):
        if self._run_guard("Chuck Is Set"):
            return
        plan = self._wafer_plan
        if plan is None:
            messagebox.showwarning("Anchor", "Import a wafer plan first (Recipe tab).")
            return
        serial = self._eg_anchor_var.get().strip().upper()
        rc = plan.serial_to_rc.get(serial)
        if rc is None:
            messagebox.showwarning("Anchor", f"'{serial}' is not on the wafer plan's Die Map.")
            return
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            messagebox.showwarning("Anchor", "Prober not connected.")
            return
        threading.Thread(target=self._eg_set_anchor_thread, args=(prober, rc, serial),
                         daemon=True).start()

    def _eg_set_anchor_thread(self, prober, rc, serial):
        try:
            real = prober.get_die_position()
        except Exception as e:
            self.after(0, lambda: self._log_main(f"Anchor: could not read ?P — {e}"))
            return
        row, col = rc
        offset = (real[0] - col, real[1] - row)
        self._eg_origin_offset = offset
        self._current_rc = (row, col)

        def _finish():
            self._eg_anchor_state_var.set(
                f"anchored at {serial} (row {row}, col {col}) — real "
                f"X{real[0]}Y{real[1]} — offset {offset}")
            self.manual_xy_var.set(f"X: {col:.0f}  Y: {row:.0f}")
            self._log_main(f"Anchored: {serial} is real X{real[0]}Y{real[1]}, offset {offset}.")
            self.wafer_map.update_die(row, col, "CURRENT")
            self._update_position_window()
        self.after(0, _finish)

    def _eg_pitch_action(self):
        if self._run_guard("Pitch"):
            return
        try:
            x_mm, y_mm = float(self._eg_pitch_x_var.get()), float(self._eg_pitch_y_var.get())
        except ValueError:
            messagebox.showerror("Pitch", "X/Y must be numbers (mm).")
            return
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            messagebox.showwarning("Pitch", "Prober not connected.")
            return
        threading.Thread(target=self._eg_pitch_action_thread, args=(prober, x_mm, y_mm),
                         daemon=True).start()

    def _eg_pitch_action_thread(self, prober, x_mm, y_mm):
        try:
            prober.set_die_size_mm(x_mm, y_mm)
        except Exception as e:
            self.after(0, lambda: self._log_main(f"Pitch: could not set — {e}"))
            return
        self.after(0, lambda: self._log_main(f"Die pitch set: {x_mm} x {y_mm} mm."))
        try:
            size_x_um, size_y_um = prober.infer_die_size()
        except Exception as e:
            self.after(0, lambda: self._log_main(f"Pitch: could not verify — {e}"))
            return
        got_x_mm, got_y_mm = size_x_um / 1000.0, size_y_um / 1000.0
        match = abs(got_x_mm - x_mm) < 0.001 and abs(got_y_mm - y_mm) < 0.001
        self.after(0, lambda: self._log_main(
            f"Pitch verify: prober reports {got_x_mm:.3f} x {got_y_mm:.3f} mm — "
            f"{'MATCHES' if match else 'DOES NOT MATCH'} entered pitch."))

    def _manual_move_to_selected(self):
        if self._run_guard("Move to Selected"):
            return
        sites = self.wafer_map.get_picked()
        if len(sites) != 1:
            self._log_main("Move to Selected:")
            return
        threading.Thread(target=self._manual_move_to_selected_thread,
                         args=(sites[0],), daemon=True).start()

    def _manual_move_to_selected_thread(self, rc):
        row, col = rc
        if not self._do_manual_call("Separate", lambda p: p.z_down(), ">> D  (Separate)"):
            return
        if self._system == "electroglas":
            if self._eg_origin_offset is None:
                self.after(0, lambda: self._log_main(
                    "Move to Selected: set the anchor (Chuck Is Set) first."))
                return
            ox, oy = self._eg_origin_offset
            target_x, target_y = col + ox, row + oy
            self._do_manual_call(
                "Move to Selected", lambda p: p.goto_die(target_x, target_y),
                f">> goto_die(X={target_x}, Y={target_y})", refresh_xy=True)
            return
        self._do_manual_call(
            "Move to Selected", lambda p: p.move_to_die_xy(col, row),
            f">> J  (X={col} Y={row})", refresh_xy=True)

    def _manual_next_die(self):
        if self._run_guard("Next Die"):
            return
        if not self._shots:
            messagebox.showerror(
                "No Recipe",
                "No recipe shots to step through — Compute Recipe (or import a recipe) "
                "first.")
            return
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            messagebox.showerror("Prober Not Connected", "Connect Prober first.")
            return
        threading.Thread(target=self._manual_next_die_thread, daemon=True).start()

    def _next_recipe_shot_index(self) -> int:
        row, col = self._current_rc
        if row is not None and col is not None:
            for i, shot in enumerate(self._shots):
                if shot.get("td_start_row") == row and shot.get("die_column") == col:
                    return i + 1
        return 0

    def _manual_next_die_thread(self):
        idx = self._next_recipe_shot_index()
        self._move_to_shot_thread(idx, label="Next Die")

    def _goto_shot(self, idx: int):
        if self._run_guard("Go to Touchdown"):
            return
        if not (0 <= idx < len(self._shots)):
            return
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            messagebox.showerror("Prober Not Connected", "Connect Prober first.")
            return
        threading.Thread(target=self._move_to_shot_thread, args=(idx,),
                         kwargs={"label": "Go to Touchdown"}, daemon=True).start()

    def _move_to_shot_thread(self, idx: int, label: str = "Next Die"):
        prober = self.controller.drivers.get("prober")
        if idx >= len(self._shots):
            self.after(0, lambda: self._log_main(f"{label}: already at the last recipe shot."))
            return
        shot = self._shots[idx]
        row, die_col = shot.get("td_start_row"), shot.get("die_column")
        if row is None or die_col is None:
            self.after(0, lambda: self._log_main(
                f"{label}: shot {idx + 1} ('{shot.get('label', '')}') has no touchdown "
                "position."))
            return
        self.after(0, lambda i=idx, s=shot: self._show_current_shot(i, s))
        if self._system == "electroglas":
            if self._eg_origin_offset is None:
                self.after(0, lambda: self._log_main(
                    f"{label}: set the anchor (Chuck Is Set) first."))
                return
            ox, oy = self._eg_origin_offset
            target_x, target_y = die_col + ox, row + oy
            try:
                self.after(0, lambda: self._log(
                    f">> goto_die(X={target_x}, Y={target_y})"))
                prober.goto_die(target_x, target_y)
                self.after(0, lambda: self._log(f"{label} complete."))
            except Exception as e:
                self.after(0, lambda e=e: self._log_main(f"{label} error: {e}"))
                return
            self._current_rc = (row, die_col)
            self.after(0, lambda: self.die_var.set(f"Die: R{row}C{die_col}"))
            self.after(0, lambda: self.manual_xy_var.set(f"X: {die_col:.0f}  Y: {row:.0f}"))
            self.after(0, lambda r=row, c=die_col: self.wafer_map.update_die(r, c, "CURRENT"))
            self.after(0, self._update_position_window)
            self.after(0, lambda i=idx: self._select_touchdown_row(i))
            return
        try:
            self.after(0, lambda: self._log(">> D  (Separate)"))
            prober.z_down()
            self.after(0, lambda r=row, c=die_col: self._log(
                f">> J  (Position die X={c} Y={r})"))
            stb = prober.move_to_die_xy(die_col, row)
            if stb == 81:
                self.after(0, lambda: self._log_main("STB=81 — wafer end, stopping."))
                return
            if stb == 90:
                self.after(0, lambda: self._log_main(
                    "STB=90 — probing stop (<STOP> pushed), stopping."))
                return
            self.after(0, lambda stb=stb: self._log(f"<< STB={stb}  ({label} complete)"))
            self._ensure_separated(prober, stb)
        except Exception as e:
            self.after(0, lambda e=e: self._log_main(f"{label} error: {e}"))
            return
        self._current_rc = (row, die_col)
        self.after(0, lambda: self.die_var.set(f"Die: R{row}C{die_col}"))
        self.after(0, lambda: self.manual_xy_var.set(f"X: {die_col:.0f}  Y: {row:.0f}"))
        self.after(0, lambda r=row, c=die_col: self.wafer_map.update_die(r, c, "CURRENT"))
        self.after(0, self._update_position_window)
        self.after(0, lambda i=idx: self._select_touchdown_row(i))

    def _manual_unload(self):
        if self._run_guard("Unload"):
            return
        threading.Thread(target=self._manual_unload_thread, daemon=True).start()

    def _manual_unload_thread(self):
        ok = self._do_manual_call("Unload", lambda p: p.unload_wafer(), ">> U  (Unload wafer)")
        if not ok:
            return
        self._current_rc = (None, None)
        self.after(0, lambda: self.manual_xy_var.set("X: —  Y: —"))
        self.after(0, lambda: self.die_var.set("Die: —"))

    def _manual_xy(self):
        if self._run_guard("XY"):
            return
        threading.Thread(target=self._manual_xy_thread, daemon=True).start()

    def _manual_xy_thread(self):
        with self._xy_refresh_lock:
            self._query_xy_thread_body()

    def _query_xy_thread_body(self) -> bool:
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self.after(0, lambda: self.manual_xy_var.set("X: —  Y: —"))
            self.after(0, lambda: self._log_main("XY: prober not connected."))
            return False
        try:
            if self._system == "electroglas":
                real_x, real_y = prober.get_die_position()
                cmd_label = "?P"
                if self._eg_origin_offset is not None:
                    ox, oy = self._eg_origin_offset
                    x, y = real_x - ox, real_y - oy
                else:
                    x, y = real_x, real_y
                log_line = (f"{cmd_label} -> real X={real_x:.0f} Y={real_y:.0f}  "
                           f"(die col={x:.0f} row={y:.0f})")
            else:
                raw = prober.get_xy_position()
                x, y = _parse_q_response(raw)
                cmd_label = "Q"
                log_line = f"{cmd_label} -> die X={x:.0f} Y={y:.0f}"
            self._current_rc = (int(round(y)), int(round(x)))
            self.after(0, lambda: self.manual_xy_var.set(f"X: {x:.0f}  Y: {y:.0f}"))
            self.after(0, lambda: self._log(log_line))
            self.after(0, lambda: self.wafer_map.update_die(int(round(y)), int(round(x)), "CURRENT"))
            self.after(0, self._update_position_window)
            return True
        except Exception as e:
            self.after(0, lambda e=e: self._log_main(f"XY error: {e}"))
            self.after(0, lambda: self.manual_xy_var.set("X: ERROR  Y: ERROR"))
            return False

    def _ensure_xy_then(self, fn, *args, **kwargs):
        def _run():
            with self._xy_refresh_lock:
                if self._current_rc == (None, None):
                    self.after(0, lambda: self._log_main(
                        "XY position not known yet — auto-refreshing before this cycle."))
                    self._query_xy_thread_body()
            self.after(0, lambda: fn(*args, **kwargs))
        threading.Thread(target=_run, daemon=True).start()

    def _manual_measure(self):
        if self._run_guard("Measure"):
            return
        active = [b for b in self._boards.values() if b.state == "connected"]
        if not active:
            messagebox.showerror("No Boards Connected",
                                 "Connect All (Setup tab) — no NanoZ boards are connected.")
            return
        try:
            cycle = int(self.cycle_var.get())
            duration_s = float(self.duration_var.get())
        except ValueError:
            messagebox.showerror("Invalid Parameters", "Cycle # and duration must be numeric.")
            return
        threading.Thread(target=self._manual_measure_thread, args=(active, cycle, duration_s),
                         daemon=True).start()

    def _manual_measure_thread(self, active: list, cycle: int, duration_s: float):
        prober = self.controller.drivers.get("prober")
        if prober and prober.inst:
            try:
                self.after(0, lambda: self._log(
                    ">> Z  (Touchdown — chuck rises, wafer CONTACTS probe card)"))
                stb = prober.z_up()
                self.after(0, lambda stb=stb: self._log(f"<< STB={stb}  (touchdown complete)"))
            except Exception as e:
                self.after(0, lambda e=e: self._log_main(
                    f"Measure: touchdown error: {e} — measuring anyway"))
        else:
            self.after(0, lambda: self._log_main(
                "Measure: prober not connected — measuring at current state."))

        self._trigger_cycle_and_wait(active, cycle, duration_s, "Measure")
        self.after(0, lambda: self._log(
            "Measure complete — chuck still in contact; use Z Down to release."))

    def _active_boards_for_window(self) -> list:
        connected = {b.port: b for b in self._boards.values() if b.state == "connected"}
        if not connected:
            return []
        row, col = self._current_rc
        if not self._wafer_plan or row is None or col is None:
            return list(connected.values())
        ports = sorted(connected.keys())
        slots_by_port = {p: connected[p].identity.chip_slots() for p in ports}
        row_off, col_off = self._wafer_plan_offset()
        active_ports = nzb.active_ports_for_window(self._wafer_plan, col, row, ports,
                                                    slots_by_port, row_off, col_off)
        return [connected[p] for p in active_ports]

    def _test_active_boards(self):
        if self._run_guard("Run Cycle"):
            return
        self._ensure_xy_then(self._test_active_boards_body)

    def _test_active_boards_body(self):
        active = self._active_boards_for_window()
        if not active:
            messagebox.showerror(
                "No Active Boards",
                "Connect All (Setup tab) — no NanoZ boards are connected and allowed to "
                "run (per the wafer plan) at the current position window.")
            return
        self._mark_cycle_start(pin_chart=True)
        self._arm_settling_skip(active)
        for board in active:
            board.run_cycle(0)
        self._log_main(f"Run Cycle 0 triggered on {len(active)} active board(s) for this "
                       f"window: " + ", ".join(b.port for b in active))

    def _pause_active_boards(self):
        if self._run_guard("Pause"):
            return
        active = self._active_boards_for_window()
        if not active:
            self._log_main("Pause (Active Boards): nothing connected/active for this window.")
            return
        for board in active:
            board.pause()
        self._log_main(f"Paused {len(active)} active board(s) for this window: "
                       + ", ".join(b.port for b in active))

    def _on_sites_changed(self, picks: list):
        self.sites_var.set(f"Test sites: {len(picks)} picked (click dies to add/remove)")
        btn = getattr(self, "_select_all_btn", None)
        dies = self.wafer_map._last_dies
        if btn and dies:
            all_rc = {(d["row"], d["col"]) for d in dies}
            is_all = bool(all_rc) and set(picks) == all_rc
            btn.config(text="☐ Deselect All" if is_all else "☑ Select All")
        move_btn = getattr(self, "_btn_manual_move_selected", None)
        if move_btn is not None:
            move_btn.config(state="normal" if len(picks) == 1 else "disabled")

    def _toggle_select_all(self):
        dies = self.wafer_map._last_dies
        if not dies:
            self._log_main("No wafer map loaded")
            return
        all_rc = [(d["row"], d["col"]) for d in dies]
        already_all = set(self.wafer_map.get_picked()) == set(all_rc)
        if already_all:
            self.wafer_map.set_picked([])
            self._on_sites_changed([])
            self._log_main("Deselected all dies.")
        else:
            self.wafer_map.set_picked(all_rc)
            self._on_sites_changed(all_rc)
            self._log_main(f"Selected all {len(all_rc)} die(s)")

    def _randomize_sites(self):
        if self._run_guard("Randomize"):
            return
        dies = list(self.wafer_map.dies.keys())
        n = min(5, len(dies))
        picks = random.sample(dies, n) if n else []
        self.wafer_map.set_picked(picks)
        self._on_sites_changed(picks)

    def _ensure_separated(self, prober, stb: int):
        if stb != 67:
            return
        self.after(0, lambda: self._log("finished chuck UP (STB=67 — contact) >> D  (Separate)"))
        prober.z_down()

    def _zup_measure_zdown(self, prober, boards: list, cycle: int, duration_s: float, label: str) -> bool:
        try:
            self.after(0, lambda: self._log(f"{label}: >> Z  (Contact)"))
            stb = prober.z_up()
            if stb == 67:
                self.after(0, lambda: self._log(f"{label}: << STB=67 (contact confirmed)"))
            else:
                self.after(0, lambda stb=stb: self._log_main(
                    f"{label}: Z Up returned STB={stb} (expected 67)"))
        except Exception as e:
            self.after(0, lambda e=e: self._log_main(
                f"{label}: touchdown error: {e} — measuring anyway"))

        ok = self._trigger_cycle_and_wait(boards, cycle, duration_s, label)

        z_down_confirmed = True
        try:
            self.after(0, lambda: self._log(f"{label}: >> D  (Separate)"))
            stb = prober.z_down()
            if stb != 68:
                z_down_confirmed = False
                self.after(0, lambda stb=stb: self._log_main(
                    f"{label}: Z Down returned STB={stb} (expected 68) — separation NOT confirmed"))
        except Exception as e:
            z_down_confirmed = False
            self.after(0, lambda e=e: self._log_main(f"{label}: separate error: {e}"))
        if not z_down_confirmed:
            self._running = False
            self.after(0, lambda: self._log_main(
                "Z Down not confirmed — stopping."))
        return ok

    def _start_test_die(self):
        if self._running:
            self._log_main("A run is already active.")
            return
        if self._system == "electroglas":
            self._log_main("Test Die:")
            return
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            messagebox.showerror("Prober Not Connected", "Connect Prober first.")
            return
        active = [b for b in self._boards.values() if b.state == "connected"]
        if not active:
            messagebox.showerror("No Boards Connected",
                                 "Connect All (Setup tab) — no NanoZ boards are connected.")
            return
        sites = self.wafer_map.get_picked()
        if not sites:
            self._randomize_sites()
            sites = self.wafer_map.get_picked()
        if not sites:
            messagebox.showerror("No Dies", "No dies available to pick test sites from — "
                                 "load a wafer map first.")
            return
        try:
            cycle = int(self.cycle_var.get())
            duration_s = float(self.duration_var.get())
        except ValueError:
            messagebox.showerror("Invalid Parameters", "Cycle # and duration must be numeric.")
            return

        self._spl_path, self._env_path = self._new_csv_paths()
        self._log_main(f"Starting Test Die — cycle {cycle}, {duration_s:g}s/touchdown, "
                       f"{len(sites)} site(s): " + ", ".join(f"R{r}C{c}" for r, c in sites))
        self._log(f"SPL CSV: {self._spl_path}")
        self._log(f"ENV CSV: {self._env_path}")

        self._reset_counts()
        self._running = True
        self._run_mode = "test"
        self.start_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.recipe_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.state_var.set("RUNNING (Test Die)")
        self.wafer_map.enable_picking(0)
        self._set_locked(True)
        self._lot_thread = threading.Thread(
            target=self._test_die_thread_body, args=(prober, active, sites, cycle, duration_s),
            daemon=True)
        self._lot_thread.start()

    def _test_die_thread_body(self, prober, boards: list, sites: list, cycle: int, duration_s: float):
        try:
            self.after(0, lambda: self._log(">> D  (Separate)"))
            prober.z_down()

            row, col = sites[0]
            self.after(0, lambda: self._log(f">> J  (Position die X={col} Y={row})"))
            stb = prober.move_to_die_xy(col, row)
            if stb == 81:
                self.after(0, lambda: self._log_main("STB=81 — wafer end, stopping."))
                return
            if stb == 90:
                self.after(0, lambda: self._log_main(
                    "STB=90 — probing stop (<STOP> pushed), stopping."))
                return
            self.after(0, lambda stb=stb: self._log(f"<< STB={stb}"))
            self._ensure_separated(prober, stb)

            idx = 0
            while self._running and idx < len(sites):
                row, col = sites[idx]
                die_label = f"R{row}C{col}"
                self._current_rc = (row, col)
                self.after(0, lambda dl=die_label: self.die_var.set(f"Die: {dl}"))
                self.after(0, lambda r=row, c=col: self.wafer_map.update_die(r, c, "CURRENT"))
                self.after(0, self._update_position_window)

                ok = self._zup_measure_zdown(prober, boards, cycle, duration_s, die_label)
                if not self._running:
                    break
                status = "PASS" if ok else "FAIL"
                if status == "PASS":
                    self._pass_count += 1
                else:
                    self._fail_count += 1
                self.after(0, self._update_pass_fail_display)
                self.after(0, lambda r=row, c=col, s=status: self.wafer_map.update_die(r, c, s))

                idx += 1
                if not self._running or idx >= len(sites):
                    break

                row, col = sites[idx]
                self.after(0, lambda r=row, c=col: self._log(f">> J  (Position die X={c} Y={r})"))
                stb = prober.move_to_die_xy(col, row)
                if stb == 81:
                    self.after(0, lambda: self._log_main("STB=81 — wafer end, stopping."))
                    break
                if stb == 90:
                    self.after(0, lambda: self._log_main(
                        "STB=90 — probing stop (<STOP> pushed), stopping."))
                    break
                self.after(0, lambda stb=stb: self._log(f"<< STB={stb}"))
                self._ensure_separated(prober, stb)
        except Exception as e:
            self.after(0, lambda e=e: self._log_main(f"ERROR: {e}"))
        finally:
            for board in boards:
                try:
                    board.pause()
                except Exception:
                    pass
            self._running = False
            self._run_mode = None
            self.after(0, lambda: self._finish_lot("TEST DIE COMPLETE"))

    def _die_id_for(self, row: int, col: int) -> "str | None":
        die_id = self.wafer_map.die_ids.get((row, col))
        if die_id:
            return die_id
        if self._wafer_plan:
            row_off, col_off = self._wafer_plan_offset()
            plan_die = self._wafer_plan.dies.get((row - row_off, col - col_off))
            if plan_die:
                return plan_die.get("serial")
        return None

    def _die_provider(self, port: str, chip: "str | None"):
        row, col = self._current_rc
        if row is None or col is None:
            return (None, None, None)
        board = self._boards.get(port)
        if chip in ("0", "1"):
            slot = (board.identity.slot0 if chip == "0" else board.identity.slot1) if board else None
            if not slot:
                return (row, col, None)
            row = row + slot - 1
        return (row, col, self._die_id_for(row, col))

    def _shot_active_boards(self, shot: dict) -> list:
        excluded = shot.get("excluded_boards", set())
        return [b for p, b in self._boards.items() if p not in excluded and b.state == "connected"]

    def _show_current_shot(self, idx: int, shot: dict):
        total = len(self._shots)
        self.recipe_shot_var.set(f"Shot {idx + 1}/{total}: {shot['label']}")
        for iid in self._shot_decision_tree.get_children():
            self._shot_decision_tree.delete(iid)
        excluded = shot.get("excluded_boards", set())
        reasons = shot.get("board_reasons") or {}
        chip_reasons = shot.get("chip_reasons") or {}

        def _top_slot(port: str):
            ident = self._boards[port].identity if port in self._boards else None
            slots = [s for s in ((ident.slot0, ident.slot1) if ident else ()) if s is not None]
            return min(slots) if slots else float("inf")

        for port in sorted(self._recipe_ports(), key=_top_slot):
            board = self._boards.get(port)
            ident = board.identity if board else None
            s0 = ident.slot0 if ident and ident.slot0 else "—"
            s1 = ident.slot1 if ident and ident.slot1 else "—"
            slots = f"{s0}/{s1}"
            per_chip = chip_reasons.get(port)
            if port in excluded:
                decision = "SKIP"
                reason = reasons.get(port) or "excluded (manual)"
            elif not board or board.state != "connected":
                decision = "SKIP"
                reason = "not connected"
            else:
                decision = "RUN"
                if per_chip:
                    reason = "; ".join(
                        f"chip{c}: {r or 'product'}" for c, r in per_chip.items())
                else:
                    reason = "—"
            self._shot_decision_tree.insert("", "end", iid=port,
                                            values=(port, slots, decision, reason))

    def _on_shot_decision_double_click(self, event):
        tree = self._shot_decision_tree
        port = tree.identify_row(event.y)
        if not port:
            return
        self._run_single_board_cycle(port, context=" (double-clicked in current shot)")

    def _run_single_board_cycle(self, port: str, context: str = ""):
        if self._run_guard("Run Cycle"):
            return
        board = self._boards.get(port)
        if not board or board.state != "connected":
            messagebox.showerror("Board Not Connected",
                                 f"{port}: not connected — Connect All (Setup tab) first.")
            return
        try:
            cycle = int(self.cycle_var.get())
        except ValueError:
            messagebox.showerror("Invalid Cycle", "Cycle # must be a whole number.")
            return
        self._ensure_xy_then(self._run_single_board_cycle_body, board, port, cycle, context)

    def _run_single_board_cycle_body(self, board, port: str, cycle: int, context: str):
        self._mark_cycle_start(pin_chart=True)
        self._arm_settling_skip([board])
        board.run_cycle(cycle)
        self._log_main(f"Run Cycle {cycle} triggered on {port}{context}.")

    def _chart_run_cycle(self):
        port = self.console_board_var.get()
        if not port:
            messagebox.showerror("No Board Selected", "Pick a board first.")
            return
        self._run_single_board_cycle(port, context=" (Charts tab)")

    def _start_recipe_run(self):
        if self._running:
            self._log_main("A run is already active.")
            return
        if self._system == "electroglas":
            self._log_main("Run Recipe:")
            return
        if not self._shots:
            messagebox.showerror("No Recipe",
                                 "No recipe shots defined — import a wafer plan or add "
                                 "shots on the Recipe tab first.")
            return
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            messagebox.showerror("Prober Not Connected", "Connect Prober first.")
            return
        if not any(b.state == "connected" for b in self._boards.values()):
            messagebox.showerror("No Boards Connected",
                                 "Connect All (Setup tab) — no NanoZ boards are connected.")
            return
        try:
            cycle = int(self.cycle_var.get())
            duration_s = float(self.duration_var.get())
        except ValueError:
            messagebox.showerror("Invalid Parameters", "Cycle # and duration must be numeric.")
            return

        self._spl_path, self._env_path = self._new_csv_paths()
        self._log_main(f"Starting Run Recipe — {len(self._shots)} shot(s), cycle {cycle}, "
                       f"{duration_s:g}s/touchdown.")
        self._log(f"SPL CSV: {self._spl_path}")
        self._log(f"ENV CSV: {self._env_path}")

        self._reset_counts()
        self._running = True
        self._run_mode = "recipe"
        self.start_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.recipe_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.state_var.set("RUNNING (Recipe)")
        self.wafer_map.enable_picking(0)
        self._set_locked(True)
        self._lot_thread = threading.Thread(
            target=self._recipe_thread_body, args=(prober, cycle, duration_s), daemon=True)
        self._lot_thread.start()

    def _recipe_thread_body(self, prober, cycle: int, duration_s: float):
        shots = self._shots
        idx = 0
        try:
            self.after(0, lambda: self._log(">> D  (Separate)"))
            prober.z_down()

            while self._running and idx < len(shots):
                shot = shots[idx]
                die_col = shot.get("die_column")
                row = shot.get("td_start_row")
                self.after(0, lambda i=idx, s=shot: self._show_current_shot(i, s))
                active_boards = self._shot_active_boards(shot)
                self.after(0, lambda i=idx, n=len(shots), s=shot, ab=active_boards: self._log_main(
                    f"Shot {i + 1}/{n}: {s['label']} — "
                    + (f"{len(ab)} board(s) active: " + ", ".join(b.port for b in ab)
                       if ab else "no boards active, skipping touchdown")))

                if die_col is None or row is None or not active_boards:
                    idx += 1
                    continue

                self.after(0, lambda r=row, c=die_col: self._log(f">> J  (Position die X={c} Y={r})"))
                stb = prober.move_to_die_xy(die_col, row)
                if stb == 81:
                    self.after(0, lambda: self._log_main("STB=81 — wafer end, stopping."))
                    break
                if stb == 90:
                    self.after(0, lambda: self._log_main(
                        "STB=90 — probing stop (<STOP> pushed), stopping."))
                    break
                self.after(0, lambda stb=stb: self._log(f"<< STB={stb}"))
                self._ensure_separated(prober, stb)

                self._current_rc = (row, die_col)
                die_label = f"R{row}C{die_col}"
                self.after(0, lambda dl=die_label: self.die_var.set(f"Die: {dl}"))
                self.after(0, lambda r=row, c=die_col: self.wafer_map.update_die(r, c, "CURRENT"))
                self.after(0, self._update_position_window)

                ok = self._zup_measure_zdown(prober, active_boards, cycle, duration_s, shot["label"])
                if not self._running:
                    break
                for board in active_boards:
                    for chip, slot in board.identity.chip_slots().items():
                        if not slot:
                            continue
                        r, c = row + slot - 1, die_col
                        verdict = self._evaluate_die_pass_fail(board.port, chip)
                        status = "PASS" if (ok and verdict is True) else "FAIL"
                        if status == "PASS":
                            self._pass_count += 1
                        else:
                            self._fail_count += 1
                        self.after(0, lambda r=r, c=c, s=status: self.wafer_map.update_die(r, c, s))
                self.after(0, self._update_pass_fail_display)

                idx += 1
        except Exception as e:
            self.after(0, lambda e=e: self._log_main(f"ERROR: {e}"))
        finally:
            for board in self._boards.values():
                try:
                    board.pause()
                except Exception:
                    pass
            self._running = False
            self._run_mode = None
            self.after(0, lambda: self._finish_lot("RECIPE RUN COMPLETE"))
            if self._on_wafer_finished:
                aborted = idx < len(shots)
                p, f = self._pass_count, self._fail_count
                hook = self._on_wafer_finished
                self.after(0, lambda: hook(p, f, aborted))

    def _finish_lot(self, msg: str = "LOT COMPLETE"):
        self.start_btn.config(state="normal")
        self.test_btn.config(state="normal")
        self.recipe_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.state_var.set(msg)
        self._set_locked(False)
        self.wafer_map.enable_picking(on_change=self._on_sites_changed)
        self._log_main(f"{msg} — heaters paused on all boards.")

    def _trigger_cycle_and_wait(self, boards: list, cycle: int, duration_s: float, label: str) -> bool:
        self._touchdown_errors = 0
        self._touchdown_packets = 0
        self._mark_cycle_start(pin_chart=True)
        self._arm_settling_skip(boards)
        for board in boards:
            board.run_cycle(cycle)
        self.after(0, lambda: self._log_main(
            f"{label} — triggered run {cycle} on {len(boards)} board(s)."))

        t0 = time.time()
        while self._running and time.time() - t0 < duration_s:
            time.sleep(0.05)

        for board in boards:
            board.pause()
        self.after(0, lambda: self._log(f"{label}: heaters paused."))
        return self._touchdown_packets > 0 and self._touchdown_errors == 0

    def _mark_cycle_start(self, pin_chart: bool = False):
        self._cycle_start_time = dt.datetime.now() - dt.timedelta(milliseconds=5)
        if pin_chart:
            self._chart_follow_live = False
            self._chart_pinned_time = self._cycle_start_time
            if hasattr(self, "_chart_canvas"):
                self.after(0, self._redraw_charts)

    def _reset_counts(self):
        self._pass_count = 0
        self._fail_count = 0
        self._mark_cycle_start()
        self._update_pass_fail_display()

    def _update_pass_fail_display(self):
        self.pass_var.set(str(self._pass_count))
        self.fail_var.set(str(self._fail_count))
        total = self._pass_count + self._fail_count
        if total:
            pct = 100.0 * self._pass_count / total
            self.yield_var.set(f"Yield: {pct:.1f}%  ({self._pass_count}/{total})")
        else:
            self.yield_var.set("Yield: —")
