"""Main layout for the Operator GUI.

Every tab the Engineer GUI builds is still built here, in the same order, so
ATA-folder loading, default recipe / probe card / wafer map selection, runs,
exports and cassette automation behave exactly as they do there. Only Run,
Results and Cassette go into the visible notebook; everything else goes into
a notebook that is never displayed.

The visible tabs' widget-building methods are copied from EngineerGUI's
MainLayout so operator-facing controls can be removed in this file. All the
behaviour behind those widgets is inherited from MainLayout. Controls the
engineer code still reads or enables/disables are built into a hidden frame
rather than deleted, so that code never finds them missing.
"""
import os
import threading
import tkinter as tk
from tkinter import ttk

import app_settings
from eg_pma_run_panel import EgPmaRunPanel
from instrument_panel import MainLayout
from wafer_map_view import WaferMapPanel

from operator_cassette_panel import OperatorCassettePanel


class OperatorMainLayout(MainLayout):

    def _operator_hidden(self):
        # Never geometry-managed, so nothing inside it is ever drawn.
        holder = getattr(self, "_operator_hidden_holder", None)
        if holder is None:
            holder = self._operator_hidden_holder = ttk.Frame(self)
        return holder

    # ---- Sidebar (replaces EngineerGUI MainLayout._build_sidebar) ----

    def _build_sidebar(self, paned):
        # Operator: no sidebar. Its status line and Instruments box are in the
        # Run tab (_build_operator_bar); the rest is still built for the
        # engine, hidden - the Execution Log too (everything still logs to it).
        hidden = self._operator_hidden()
        self.prober_status_label = ttk.Label(
            hidden, text="Prober: —", foreground="orange",
            font=("Arial", 9)
        )

        self.lbl_progress = ttk.Label(hidden, text="No wafer loaded")
        self.sidebar_canvas = tk.Canvas(
            hidden, width=110, height=110, bg="#f0f0f0", highlightthickness=0
        )
        self.lbl_stats_text = ttk.Label(
            hidden, text="Pass: 0  |  Fail: 0\nUntested: 0", justify="center"
        )

        log_frame = ttk.LabelFrame(hidden, text="Execution Log")
        log_frame.pack(fill="both", expand=True, pady=4)
        self.log_text = tk.Text(
            log_frame, bg="#1e1e1e", fg="lime", font=("Consolas", 8),
            wrap="word", state="disabled", width=24
        )
        log_sb = ttk.Scrollbar(log_frame, orient="vertical",
                               command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y", pady=2)
        self.log_text.pack(fill="both", expand=True, padx=(2, 0), pady=2)

    def _build_notebook(self, paned):
        # Operator: larger tabs than the engineer's default.
        ttk.Style().configure("Operator.TNotebook.Tab", font=("Segoe UI", 11),
                              padding=(14, 5))
        visible_nb = ttk.Notebook(paned, style="Operator.TNotebook")
        paned.add(visible_nb, weight=1)

        hidden_nb = ttk.Notebook(self._operator_hidden())
        hidden_nb.pack(fill="both", expand=True)

        # Same construction order as MainLayout._build_notebook: later tabs
        # read state the earlier ones create.
        self._tab_execution2(visible_nb)
        if self._system == "accretech":
            self._tab_pma_wafer(hidden_nb)
            self._tab_probe_card(hidden_nb)
            self._tab_recipe(hidden_nb)
            self._tab_wafer_map(hidden_nb)
            self._tab_results(visible_nb)
            self._tab_cassette(visible_nb)
        else:
            self._tab_results(visible_nb)
            self._tab_recipe(hidden_nb)
            self._tab_probe_card(hidden_nb)
            self._tab_pma_process(hidden_nb)
            self._tab_recipe_gen(hidden_nb)
            self._tab_wafer_map(hidden_nb)

        if self._system == "accretech":
            self._tab_instruments(hidden_nb)
            self._tab_probe_routing(hidden_nb)
        else:
            self._tab_instruments_eg(hidden_nb)
        self._tab_setup(hidden_nb)
        self._tab_gds_parser(hidden_nb)
        self._tab_switch_settings(hidden_nb)
        self._tab_prober_debug(hidden_nb)
        self._tab_gpib_trace(hidden_nb)
        self._tab_nanoz_switch(hidden_nb)

    def _autoexport_release_hook(self):
        # MainLayout's version compares with "is", but a bound method is a new
        # object on every access, so it never matched: AutoExport (and its
        # end-of-run popup) stayed on after loading a folder that has it off.
        if getattr(self, "_exec_on_run_finished", None) == getattr(
                self, "_on_autoexport_run_finished", None):
            self._exec_on_run_finished = None

    def _tab_cassette(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Cassette")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.cassette_panel = OperatorCassettePanel(tab, controller=self.controller, ui=self)
        self.cassette_panel.grid(row=0, column=0, sticky="nsew")

    # ---- Run tab (copied from EngineerGUI MainLayout._tab_execution2) ----

    def _tab_execution2(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="▶  Run")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)

        self._exec_running  = False
        self._exec_aborted  = False
        self._exec_run_mode = None
        # Only a run started via the ▶ Run button should end with the
        # AutoExport/cassette popup - Full Die and Test Selected are quick
        # manual checks, not "the run", and used to pop the same export
        # dialog every time either was pressed. Set True/False at the top
        # of each of the three button handlers (_exec_start_run/
        # _exec_start_full_die/_exec_start_test_die); _exec_finish_run
        # reads it once, right before resetting for the next run.
        self._exec_run_via_run_button = False
        self._exec_die_num  = 0
        self._exec_step_config_cache = {}
        self._exec_avg_count_cache = {}
        self._exec_die_id_override = ""
        self._exec_total_dies = 0
        self._exec_run_token = 0
        self._exec_lot_thread: threading.Thread | None = None
        self._exec_on_run_finished = None
        self._exec_last_run_start_idx = 0
        self._exec_steps    = []
        self._exec_current_rc = None
        self._exec_last_test_sites: list = []
        self._exec_overlay_row_offset = 0
        self._exec_overlay_col_offset = 0
        self._exec_overlay_offset_confirmed = False
        self._exec_overlay_items: list = []
        self._exec_overlay_result_items: list = []
        self._exec_overlay_die_ids: dict = {}
        self._exec_shot_window_items: list = []
        self._exec_move_armed = False
        self._exec_move_target_rc = None
        self._exec_move_target_prev_fill = None
        self._exec_move_prev_click_handler = None
        self._exec_move_prev_picking_enabled = True

        ctrl = tk.Frame(tab, bg="#f1f5f9", relief="solid", bd=1)
        ctrl.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))

        tk.Label(ctrl, text="Recipe:", bg="#f1f5f9").pack(side="left", padx=(10, 2), pady=6)
        self._exec_recipe_var = tk.StringVar()
        self._exec_recipe_cb = ttk.Combobox(
            ctrl, textvariable=self._exec_recipe_var, width=20, state="readonly",
            postcommand=lambda: self._exec_recipe_cb.config(
                values=self.recipe_panel.get_recipe_names()))
        self._exec_recipe_cb.pack(side="left", pady=6)
        self._exec_recipe_cb.bind(
            "<<ComboboxSelected>>", lambda _e: self._exec_load_recipe())

        tk.Label(ctrl, text="Probe Card:", bg="#f1f5f9").pack(side="left", padx=(10, 2), pady=6)
        self._exec_card_var = tk.StringVar(value="")
        self._exec_card_cb = ttk.Combobox(
            ctrl, textvariable=self._exec_card_var, width=14, state="readonly")
        self._exec_card_cb.pack(side="left", pady=6)
        self._exec_card_cb.bind("<<ComboboxSelected>>",
                                 lambda _e: self._exec_on_card_picked())

        tk.Label(ctrl, text="Wafer Map:", bg="#f1f5f9").pack(side="left", padx=(10, 2), pady=6)
        self._exec_wafer_map_var = tk.StringVar(value="")
        # Operator: read-only. The map is the ATA folder's default, or the one
        # the chosen recipe names.
        ttk.Entry(ctrl, textvariable=self._exec_wafer_map_var, width=16,
                  state="readonly").pack(side="left", pady=6)

        ttk.Separator(ctrl, orient="vertical").pack(side="left", fill="y", padx=10, pady=4)

        self._exec_test_btn = ttk.Button(
            ctrl, text="▶  Test Die", command=self._exec_start_test_die)

        run_border = tk.Frame(ctrl, background="#15803d")
        run_border.pack(side="left", padx=(2, 6), pady=5)
        self._exec_run_btn = ttk.Button(
            run_border, text="▶  Run",
            command=(lambda: self.eg_pma_run._run_all())
                    if self._system == "electroglas"
                    else self._exec_start_run)
        self._exec_run_btn.pack(padx=2, pady=2)

        for label, cmd, attr in [
            ("⏏  Unload (U)",  self._exec_manual_unload, "_exec_unload_btn"),
            ("⏸  Pause",       self._exec_pause, "_exec_pause_btn"),
            ("⏹  Stop Run",       self._exec_abort, "_exec_stop_btn"),
        ]:
            btn = ttk.Button(ctrl, text=label, command=cmd)
            btn.pack(side="left", padx=3, pady=5)
            if attr:
                setattr(self, attr, btn)
        # Operator: the window's Abort (the engineer toolbar's), next to Stop Run.
        ttk.Style().configure("Abort.TButton", foreground="red", font=("Arial", 9, "bold"))
        ttk.Button(ctrl, text="⏹ Abort", style="Abort.TButton",
                   command=self.controller.cmd_abort).pack(side="left", padx=3, pady=5)
        self._exec_set_running_buttons(False)

        self._exec_state_lbl = tk.Label(
            ctrl, text="IDLE", bg="#f1f5f9", fg="#6b7280",
            font=("Segoe UI", 11, "bold"))
        self._exec_state_lbl.pack(side="right", padx=12)

        body = ttk.PanedWindow(tab, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 6))

        if self._system == "electroglas":
            self.eg_pma_run = EgPmaRunPanel(body, controller=self.controller,
                                            main_layout=self)
            body.add(self.eg_pma_run, weight=25)

        left_col = ttk.Frame(body)
        body.add(left_col, weight=25 if self._system == "electroglas" else 1)
        left_col.rowconfigure(0, weight=0)
        left_col.rowconfigure(1, weight=1)
        left_col.columnconfigure(0, weight=1)

        pos_row = ttk.Frame(left_col)
        pos_row.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
        pos_row.columnconfigure(0, weight=1)
        pos_row.columnconfigure(1, weight=1)
        pos_row.rowconfigure(2, weight=1)

        # Operator: Chuck Position and Pass / Fail sit in row 2, under the
        # Instruments and ATA Folder boxes (_build_operator_bar).
        pos_lf = ttk.LabelFrame(pos_row, text="Chuck Position", padding=6)
        pos_lf.grid(row=2, column=0, sticky="nsew", padx=(0, 3), pady=(6, 0))
        pos_lf.columnconfigure(0, weight=1)
        pos_lf.columnconfigure(1, weight=1)

        self._exec_xy_var = tk.StringVar(value="X: —\nY: —")
        ttk.Label(pos_lf, textvariable=self._exec_xy_var,
                  font=("Consolas", 13, "bold"), foreground="#0077cc",
                  justify="center").grid(row=0, column=0, columnspan=2, pady=(0, 2))

        self._exec_die_var = tk.StringVar(value="Die: —")
        ttk.Label(pos_lf, textvariable=self._exec_die_var,
                  font=("Consolas", 9), foreground="#374151",
                  justify="center").grid(row=1, column=0, columnspan=2, pady=(0, 4))

        if self._system == "electroglas":
            self._exec_die_size_var = tk.StringVar(value="Prober Die size: unknown")
            ttk.Label(pos_lf, textvariable=self._exec_die_size_var,
                     font=("Consolas", 8), foreground="#6b7280",
                     justify="center").grid(row=2, column=0, columnspan=2,
                                            pady=(0, 4))

        ttk.Separator(pos_lf, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=3)

        # Operator: only First Die and Refresh XY are shown. The other manual
        # moves, the Recipe Steps table and its buttons, and Select All are
        # built into the hidden frame - the run engine still enables/disables
        # and fills them.
        hidden = self._operator_hidden()
        self._exec_first_die_btn = ttk.Button(
            pos_lf, text="◀ First Die", command=self._exec_manual_go_to_start)
        self._exec_first_die_btn.grid(
                   row=4, column=0, columnspan=2, sticky="ew", pady=1)
        self._exec_zup_btn = ttk.Button(
            hidden, text="↑ Z Up", command=self._exec_manual_z_up)
        self._exec_zdown_btn = ttk.Button(
            hidden, text="↓ Z Down", command=self._exec_manual_z_down)
        if self._system == "electroglas":
            self._exec_back_btn = ttk.Button(
                hidden, text="◀ Back", command=lambda: self.eg_pma_run._step_back())
            self._exec_next_btn = ttk.Button(
                hidden, text="▶ Next", command=lambda: self.eg_pma_run._step_once())
            self.eg_pma_run._goto_btn = ttk.Button(
                hidden, text="→ Move to Selected",
                command=self.eg_pma_run.toggle_move_armed)
        else:
            self._exec_back_btn = ttk.Button(
                hidden, text="◀ Back", command=self._exec_manual_prev_die)
            self._exec_next_btn = ttk.Button(
                hidden, text="▶ Next", command=self._exec_manual_next_die)
            self._exec_prev_shot_btn = ttk.Button(
                hidden, text="◀◀ Previous Shot", command=self._exec_manual_prev_shot)
            self._exec_next_shot_btn = ttk.Button(
                hidden, text="▶▶ Next Shot", command=self._exec_manual_next_shot)
            self._exec_move_selected_btn = ttk.Button(
                hidden, text="→ Move to Selected",
                command=self._exec_move_selected_button)
            self._exec_refresh_xy_btn = ttk.Button(
                pos_lf, text="↻ Refresh XY", command=self._exec_get_xy)
            self._exec_refresh_xy_btn.grid(
                row=9, column=0, columnspan=2, sticky="ew", pady=1)

        self._build_operator_bar(pos_row)

        steps_lf = ttk.LabelFrame(hidden, text="Recipe Steps", padding=(6, 4))
        steps_lf.rowconfigure(0, weight=1)
        steps_lf.columnconfigure(0, weight=1)

        self._exec_steps_var = tk.StringVar(value="No recipe loaded")

        cols = ("n", "name", "type", "conn")
        self._exec_steps_tree = ttk.Treeview(
            steps_lf, columns=cols, show="headings", height=5, selectmode="browse")
        for cid, text, width in (("n", "#", 24), ("name", "Name", 78),
                                 ("type", "Type", 68), ("conn", "Conn", 100)):
            self._exec_steps_tree.heading(cid, text=text)
            self._exec_steps_tree.column(cid, width=width,
                                          anchor="center" if cid == "n" else "w")
        self._exec_steps_tree.grid(row=0, column=0, sticky="nsew")
        ssb = ttk.Scrollbar(steps_lf, orient="vertical",
                            command=self._exec_steps_tree.yview)
        ssb.grid(row=1, column=1, sticky="ns")
        self._exec_steps_tree.configure(yscrollcommand=ssb.set)

        exec_btn_row1 = ttk.Frame(steps_lf)
        exec_btn_row1.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 1))
        exec_btn_row1.columnconfigure(0, weight=1)
        exec_btn_row1.columnconfigure(1, weight=1)
        self._exec_full_btn = ttk.Button(
            exec_btn_row1, text="▶  Full Die", command=self._exec_start_full_die)
        self._exec_full_btn.grid(row=0, column=0, sticky="ew", padx=(0, 1))
        self._exec_test_selected_btn = ttk.Button(
            exec_btn_row1, text="▶  Test Selected", command=self._exec_start_test_selected)
        self._exec_test_selected_btn.grid(row=0, column=1, sticky="ew", padx=(1, 0))

        exec_btn_row2 = ttk.Frame(steps_lf)
        exec_btn_row2.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(1, 0))
        exec_btn_row2.columnconfigure(0, weight=1)
        exec_btn_row2.columnconfigure(1, weight=1)
        self._exec_measure_btn = ttk.Button(
            exec_btn_row2, text="Measure", command=self._exec_touchdown_measure)
        self._exec_measure_btn.grid(row=0, column=0, sticky="ew", padx=(0, 1))
        self._exec_local_btn = ttk.Button(
            exec_btn_row2, text="↩  Release To Local", command=self._release_all_to_local)
        self._exec_local_btn.grid(row=0, column=1, sticky="ew", padx=(1, 0))

        map_lf = ttk.LabelFrame(body, text="Wafer Map")
        body.add(map_lf, weight=50 if self._system == "electroglas" else 2)
        map_lf.rowconfigure(1, weight=1)
        map_lf.columnconfigure(0, weight=1)

        if self._system == "electroglas":
            def _apply_initial_sashes():
                w = body.winfo_width()
                if w <= 1:
                    return
                body.sashpos(0, int(w * 0.25))
                body.sashpos(1, int(w * 0.50))
            def _set_initial_sashes(_event=None):
                if body.winfo_width() <= 1:
                    return
                body.unbind("<Configure>", sash_bind_id[0])
                body.after_idle(_apply_initial_sashes)
            sash_bind_id = [body.bind("<Configure>", _set_initial_sashes)]

        map_bar = ttk.Frame(map_lf)
        map_bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self._exec_map_folder = None
        self._exec_map_source_var = tk.StringVar(
            value="Accretech" if self._system == "accretech" else "Wafer Builder")
        self._exec_map_path_var = tk.StringVar(value="No wafer map loaded")
        ttk.Label(map_bar, textvariable=self._exec_map_path_var,
                  foreground="#6b7280", font=("Segoe UI", 8)).pack(
                  side="left", padx=8)

        self._exec_sites_var = tk.StringVar(value="Test sites: 0 picked")
        ttk.Label(map_bar, textvariable=self._exec_sites_var,
                  foreground="#6b7280", font=("Segoe UI", 8)).pack(
                  side="left", padx=8)

        self._exec_select_all_btn = ttk.Button(
            hidden, text="☑ Select All", command=self._exec_toggle_select_all)

        self._exec_wafer_map = WaferMapPanel(
            map_lf, show_title=False, show_axis_grid=True)
        self._exec_wafer_map.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self._exec_wafer_map.enable_picking(on_change=self._exec_on_sites_changed)
        self._exec_wafer_map.on_redraw = self._exec_redraw_overlay_on_run_map
        self._exec_wafer_map.on_zoom = self._exec_debounced(
            "_exec_zoom_debounce_id", self._exec_redraw_overlay_on_run_map)
        self._exec_wafer_map.on_reset_request = self._exec_rebuild_run_map
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Double-Button-1>"):
            self._exec_wafer_map.canvas.bind(
                seq, lambda _e: self._exec_update_overlay_visibility(), add="+")

        stat_lf = ttk.LabelFrame(pos_row, text="Pass / Fail", padding=(8, 4))
        stat_lf.grid(row=2, column=1, sticky="nsew", padx=(3, 0), pady=(6, 0))
        count_font = ("Consolas", 18, "bold")
        stat_lf.columnconfigure(0, weight=1)

        self._exec_pass_var = tk.IntVar(value=0)
        self._exec_fail_var = tk.IntVar(value=0)

        for var, label, color in [
            (self._exec_pass_var, "PASS", "#00a800"),
            (self._exec_fail_var, "FAIL", "#dc2626"),
        ]:
            row_f = ttk.Frame(stat_lf)
            row_f.pack(fill="x", pady=4)
            ttk.Label(row_f, text=label, width=6,
                      font=("Segoe UI", 10, "bold"),
                      foreground=color).pack(side="left")
            ttk.Label(row_f, textvariable=var, font=count_font,
                      foreground=color).pack(side="left", padx=8)

        ttk.Separator(stat_lf, orient="horizontal").pack(fill="x", pady=8)

        self._exec_pct_var = tk.StringVar(value="Yield:  —")
        ttk.Label(stat_lf, textvariable=self._exec_pct_var,
                  font=("Consolas", 13, "bold"), foreground="#374151").pack()

    def _build_operator_bar(self, pos_row):
        # What was the sidebar: the status line across the top, then the
        # Instruments box (left) and an ATA Folder box (right) above Chuck
        # Position / Pass-Fail, then Lot ID / Wafer ID below them.
        # OperatorDashboard packs the ATA Folder dropdown, prober and
        # defaults warning into the displayed system's ATA Folder box.
        self.status_label = ttk.Label(
            pos_row, text="INITIALIZING", foreground="orange",
            font=("Arial", 11, "bold")
        )
        self.status_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))

        inst_frame = ttk.LabelFrame(pos_row, text="Instruments")
        inst_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 3))
        self._inst_frame = inst_frame
        for inst in self._instrument_names:
            lbl = ttk.Label(inst_frame, text=f"⏳ {inst}", foreground="orange")
            lbl.pack(anchor="w", padx=4, pady=2)
            self.status_labels[inst] = lbl
        self._refresh_conn_btn = ttk.Button(
            inst_frame, text="↻ Refresh Connections",
            command=self._init_hardware_fn)
        self._refresh_conn_btn.pack(pady=(8, 4), padx=4, fill="x")

        self._operator_controls_slot = ttk.LabelFrame(pos_row, text="ATA Folder",
                                                      padding=6)
        self._operator_controls_slot.grid(row=1, column=1, sticky="nsew", padx=(3, 0))

        # Same variables as the Results tab's Lot ID / Wafer ID, so the two
        # always match; exports and the cassette read them from there.
        for col, (text, var) in enumerate((("Lot ID:", self.lot_id),
                                           ("Wafer ID:", self.wafer_id_var))):
            cell = ttk.Frame(pos_row)
            cell.grid(row=3, column=col, sticky="ew", pady=(10, 0),
                      padx=(0, 3) if col == 0 else (3, 0))
            ttk.Label(cell, text=text).pack(side="left")
            ttk.Entry(cell, textvariable=var).pack(side="left", fill="x",
                                                   expand=True, padx=(6, 0))

    # ---- Results tab (copied from EngineerGUI MainLayout) ----

    def _tab_results(self, nb):
        page = ttk.Frame(nb)
        nb.add(page, text="Results")
        self.results_tab_frame = page
        page.rowconfigure(0, weight=1)
        page.columnconfigure(0, weight=1)

        split = ttk.PanedWindow(page, orient="vertical")
        split.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        wafer_pane = ttk.Frame(split)
        split.add(wafer_pane, weight=3)
        self._build_results_wafer_map(wafer_pane)

        export_frame = ttk.LabelFrame(split, text="Data Export")
        split.add(export_frame, weight=3)

        ttk.Label(
            export_frame,
            text="Output filename:  <Lot ID>_<Wafer ID>_results.csv"
        ).pack(anchor="w", padx=10, pady=(8, 4))

        file_row = ttk.Frame(export_frame)
        file_row.pack(fill="x", padx=10, pady=4)
        ttk.Label(file_row, text="Lot ID:").pack(side="left")
        ttk.Entry(file_row, textvariable=self.lot_id, width=22).pack(side="left", padx=6)
        ttk.Label(file_row, text="Wafer ID:").pack(side="left", padx=(12, 0))
        ttk.Entry(file_row, textvariable=self.wafer_id_var, width=22).pack(side="left", padx=6)

        path_row = ttk.Frame(export_frame)
        path_row.pack(fill="x", padx=10, pady=(4, 12))
        ttk.Label(path_row, text="Export Path:").pack(side="left")
        # Operator: the path can only be picked from the dropdown - no typing
        # or Browse. Electroglas has no dropdown, so its path is read-only.
        if self._system == "accretech":
            self._export_dir_choices = {
                "PROBE08 (network)": r"\\prober\NewData\ETL\RAWDATA\PROBE08",
                "Cenfire-DataDump": r"C:\Cenfire-DataDump",
                "C:\\data": r"C:\data",
                "Downloads": self._downloads_dir,
            }
            export_dir_var = tk.StringVar(value="PROBE08 (network)")
            export_dir_cb = ttk.Combobox(
                path_row, textvariable=export_dir_var, state="readonly",
                width=30, values=list(self._export_dir_choices.keys()))
            export_dir_cb.pack(side="left", padx=6)
            export_dir_cb.bind(
                "<<ComboboxSelected>>",
                lambda _e: self.export_path_var.set(
                    self._export_dir_choices[export_dir_var.get()]))
            self._operator_follow_export_path(export_dir_var)
        else:
            ttk.Entry(path_row, textvariable=self.export_path_var, width=40,
                      state="readonly").pack(side="left", padx=6)
        ttk.Button(
            path_row, text="Save to CSV", command=self.controller.cmd_save_csv
        ).pack(side="left", padx=10)
        ttk.Button(
            path_row, text="📂 Import CSV",
            command=self.controller.cmd_import_results_csv
        ).pack(side="left", padx=(0, 4))

        sql_row = ttk.Frame(export_frame)
        sql_row.pack(fill="x", padx=10, pady=(0, 12))
        ttk.Label(sql_row, text="Export Format:").pack(side="left")
        self.export_format_var = tk.StringVar()
        # Operator: the format is the ATA folder's default, shown read-only.
        # The engineer code still fills its (hidden) dropdown.
        self._export_format_cb = ttk.Combobox(
            self._operator_hidden(), textvariable=self.export_format_var,
            state="readonly", width=42)
        ttk.Entry(sql_row, textvariable=self.export_format_var, width=42,
                  state="readonly").pack(side="left", padx=6)
        ttk.Button(
            sql_row, text="💾 Export", command=self.controller.cmd_export_sql
        ).pack(side="left", padx=(4, 10))
        self._cenfire_transfer_btn = ttk.Button(
            sql_row, text="Transfer Cenfire", command=self._run_cenfire_transfer,
            state="disabled")
        self._cenfire_transfer_btn.pack(side="left", padx=(6, 0))
        self._lamp_push_btn = ttk.Button(
            sql_row, text="Push Lamp SQL", command=self._run_lamp_sql_push,
            state="disabled")
        self._lamp_push_btn.pack(side="left", padx=(6, 0))
        self._build_mdb_row(export_frame)

        self._export_formats: list = []

        def _apply_initial_results_sashes():
            h = split.winfo_height()
            if h <= 1:
                return
            split.sashpos(0, int(h * 0.4))
        def _set_initial_results_sashes(_event=None):
            if split.winfo_height() <= 1:
                return
            split.unbind("<Configure>", results_sash_bind_id[0])
            split.after_idle(_apply_initial_results_sashes)
        results_sash_bind_id = [split.bind("<Configure>", _set_initial_results_sashes)]

        results_area = ttk.Frame(export_frame)
        results_area.pack(fill="both", expand=True, padx=(4, 4), pady=(0, 6))
        results_area.rowconfigure(0, weight=1)
        results_area.columnconfigure(0, weight=1)

        cols = ("timestamp", "recipe", "die", "step", "type", "value", "unit")
        self._results_tree = ttk.Treeview(
            results_area, columns=cols, show="headings", height=8, selectmode="browse")
        heads = [("timestamp", "Time", 135), ("recipe", "Recipe", 110),
                 ("die", "Die", 90), ("step", "Step", 110), ("type", "Type", 75),
                 ("value", "Value", 90), ("unit", "Unit", 45)]
        for cid, text, width in heads:
            self._results_tree.heading(cid, text=text)
            self._results_tree.column(cid, width=width,
                                      anchor="center" if cid in ("type", "unit") else "w")
        self._results_tree.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        rsb = ttk.Scrollbar(results_area, orient="vertical",
                            command=self._results_tree.yview)
        rsb.grid(row=0, column=1, sticky="ns", pady=(0, 6))
        self._results_tree.configure(yscrollcommand=rsb.set)

        ttk.Button(results_area, text="Clear Results", command=self.clear_results).grid(
            row=1, column=0, columnspan=2, sticky="e")

    def _operator_follow_export_path(self, label_var):
        # With the path entry gone the dropdown is the only place the export
        # path shows, so keep it naming where exports really go - loading an
        # ATA folder applies that project's saved export path, which may not
        # be one of the dropdown's choices (then the path itself is shown).
        def sync(*_):
            path = self.export_path_var.get()
            key = os.path.normcase(os.path.normpath(path)) if path else ""
            for label, choice in self._export_dir_choices.items():
                if os.path.normcase(os.path.normpath(choice)) == key:
                    label_var.set(label)
                    return
            label_var.set(path)
        self.export_path_var.trace_add("write", sync)
        sync()

    def _build_mdb_row(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(row, text="Access DB (.mdb):").pack(side="left")
        self.mdb_path_var = tk.StringVar(
            value=app_settings.load_settings().get("mdb_path", ""))
        # Operator: the .mdb path is read-only (no Browse / Set Default).
        ttk.Entry(row, textvariable=self.mdb_path_var, width=38,
                  state="readonly").pack(side="left", padx=6)
        ttk.Button(row, text="Check", command=self._mdb_check).pack(
            side="left", padx=(8, 2))
        ttk.Button(row, text="Push to DB", command=self._mdb_push).pack(
            side="left", padx=2)
        self._mdb_status_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self._mdb_status_var, foreground="#6b7280",
                 font=("Segoe UI", 8), wraplength=620, justify="left").pack(
                 anchor="w", padx=10, pady=(0, 2))

    def _build_results_wafer_map(self, tab):
        map_frame = ttk.LabelFrame(tab, text="Wafer Map — Pass / Fail")
        map_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        map_frame.rowconfigure(1, weight=1)
        map_frame.columnconfigure(0, weight=2)
        map_frame.columnconfigure(1, weight=1)

        top_row = ttk.Frame(map_frame)
        top_row.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4))
        self.lbl_results_large = ttk.Label(
            top_row, text="Total Passed: 0     |     Total Failed: 0     |     Untested: 0",
            font=("Arial", 11, "bold"))
        self.lbl_results_large.pack(side="left")

        self._results_map_frame = map_frame
        self._new_results_wafer_map()

        detail_lf = ttk.LabelFrame(map_frame, text="Selected Die")
        detail_lf.grid(row=1, column=1, sticky="nsew", padx=(4, 8), pady=(0, 8))
        detail_lf.rowconfigure(1, weight=1)
        detail_lf.columnconfigure(0, weight=1)

        self._results_die_var = tk.StringVar(
            value="Click a die to see the measurements")
        ttk.Label(detail_lf, textvariable=self._results_die_var, wraplength=220,
                 justify="left").grid(row=0, column=0, sticky="w", padx=6, pady=6)

        dcols = ("step", "type", "value", "unit")
        self._results_die_tree = ttk.Treeview(detail_lf, columns=dcols, show="headings", height=10)
        for cid, text, width in (("step", "Step", 90), ("type", "Type", 60),
                                 ("value", "Value", 70), ("unit", "Unit", 40)):
            self._results_die_tree.heading(cid, text=text)
            self._results_die_tree.column(cid, width=width, anchor="w" if cid == "step" else "center")
        self._results_die_tree.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        ddsb = ttk.Scrollbar(detail_lf, orient="vertical", command=self._results_die_tree.yview)
        ddsb.grid(row=1, column=1, sticky="ns")
        self._results_die_tree.configure(yscrollcommand=ddsb.set)

        self._results_selected_rc = None
        self._sync_results_wafer_map()
