import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox
import csv
import os
import re
import threading
import time

from wafer_map_view import (WaferMapPanel, PadLayoutPanel, ProbeCardWiringFrame,
                            ATA_KEY_FILES, WAFER_MAP_SOURCES, _pz_bind,
                            recipe_file_path, copy_recipe, copy_probe_card, copy_wafer_map,
                            delete_recipe, delete_probe_card, delete_wafer_map,
                            rebuild_wafer_map_panel)
from gds_parser_panel import GdsParserPanel
from switch_settings_panel import SwitchSettingsPanel
from switchbox_test_panel import SwitchboxTestPanel
from instruments_eg_panel import InstrumentsEgPanel
from eg_setup_panel import EgSetupPanel
from accretech_setup_panel import AccretechSetupPanel
from instrument_connection_panel import build_address_panel
from probe_routing_panel import scrollable_routing
from prober_debug_panel import ProberDebugPanel
from eg_prober_debug_panel import EgProberDebugPanel
from gpib_trace_panel import GpibTracePanel
from eg_pma_run_panel import EgPmaRunPanel
from accr_wafer_panel import AccrWaferPanel
from cassette_panel import (CassettePanel, save_yield_threshold, load_yield_threshold,
                            save_autoexport_settings, load_autoexport_settings)
from recipe_panel import RecipePanel, load_default_recipe, compute_target_derived
from pma_wafer_panel import PmaWaferPanel, centroid_offset
from pma_process_panel import PmaProcessPanel
from recipe_gen_panel import RecipeGenPanel, shot_die_rc, present_slots
import export_formats as xfmt
import mdb_export
import app_settings
from engineering_units import parse_engineering, format_engineering
from instruments import accretech_profiles, eg_profiles


def _parse_q_response(raw: str):
    import re
    raw = (raw or "").strip()
    m = re.search(r'Y\s*([+-]?\d+)\s*X\s*([+-]?\d+)', raw)
    if m:
        return float(m.group(2)), float(m.group(1))
    parts = re.findall(r'[+-]?\d+\.?\d*', raw)
    if len(parts) >= 2:
        return float(parts[1]), float(parts[0])
    raise ValueError(f"Cannot parse Q response: {raw!r}")


class MainLayout(ttk.Frame):
    def __init__(self, parent, controller, instrument_names=None, init_hardware_fn=None,
                 system: str = "accretech"):
        super().__init__(parent)
        self.controller = controller
        self._system = system
        self._instrument_names = instrument_names or [
            "UF200R Prober", "SMU (2636B)", "DMM (34461A)", "SW_MATRIX", "Wave Gen (33512B)"]
        self._init_hardware_fn = init_hardware_fn or controller.init_hardware
        self._downloads_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
        default_export_dir = (r"\\prober\NewData\ETL\RAWDATA\PROBE08"
                              if self._system == "accretech" else self._downloads_dir)
        self.export_path_var = tk.StringVar(value=default_export_dir)
        self.working_dir_var = (getattr(controller, "working_dir_var", None)
                                or tk.StringVar(value="C:/automationproject"))
        self.lot_id = tk.StringVar()
        self.wafer_id_var = tk.StringVar()
        self.status_labels = {}
        self._ata_folder = None
        self._pad_custom_loaded = False
        self._smu_output_lf: dict = {}
        self._wg_output_lf: dict = {}
        self._smu_level_vars: dict = {}
        self._smu_cont_active: dict = {}
        self._dmm_cont_active: bool = False
        self._dmm_cont_thread = None
        self._dmm_status_var: tk.StringVar | None = None
        self._inst_status_vars: dict = {}
        self._exec_label_min_px_var = tk.IntVar(value=22)
        self._exec_label_min_px_var.trace_add(
            "write", self._on_exec_label_min_px_change)
        self._build_layout()

    def _on_exec_label_min_px_change(self, *_args):
        if hasattr(self, "_exec_wafer_map"):
            self._exec_redraw_overlay_on_run_map()
            self._exec_redraw_overlay_on_results_map()

    def _build_layout(self):
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill="both", expand=True)

        self._build_sidebar(paned)
        self._build_notebook(paned)

    def _build_sidebar(self, paned):
        sidebar = ttk.Frame(paned, width=230, relief="sunken", padding=5)
        paned.add(sidebar, weight=0)
        sidebar.pack_propagate(False)

        self.status_label = ttk.Label(
            sidebar, text="INITIALIZING", foreground="orange",
            font=("Arial", 11, "bold")
        )
        self.status_label.pack(anchor="w", pady=(0, 4))

        self.prober_status_label = ttk.Label(
            sidebar, text="Prober: —", foreground="orange",
            font=("Arial", 9)
        )

        inst_frame = ttk.LabelFrame(sidebar, text="Instruments")
        inst_frame.pack(fill="x", pady=4)
        self._inst_frame = inst_frame
        for inst in self._instrument_names:
            lbl = ttk.Label(inst_frame, text=f"⏳ {inst}", foreground="orange")
            lbl.pack(anchor="w", padx=4, pady=2)
            self.status_labels[inst] = lbl
        self._refresh_conn_btn = ttk.Button(
            inst_frame, text="↻ Refresh Connections",
            command=self._init_hardware_fn)
        self._refresh_conn_btn.pack(pady=(8, 4), padx=4, fill="x")

        self.lbl_progress = ttk.Label(sidebar, text="No wafer loaded")
        self.sidebar_canvas = tk.Canvas(
            sidebar, width=110, height=110, bg="#f0f0f0", highlightthickness=0
        )
        self.lbl_stats_text = ttk.Label(
            sidebar, text="Pass: 0  |  Fail: 0\nUntested: 0", justify="center"
        )

        log_frame = ttk.LabelFrame(sidebar, text="Execution Log")
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

    def set_visible_instruments(self, names):
        names = list(names or [])
        for name in names:
            if name not in self.status_labels:
                lbl = ttk.Label(self._inst_frame, text=f"⏳ {name}", foreground="orange")
                self.status_labels[name] = lbl
                self._instrument_names.append(name)
        wanted = set(names)
        for inst in self._instrument_names:
            lbl = self.status_labels.get(inst)
            if lbl is None:
                continue
            lbl.pack_forget()
            if inst in wanted:
                lbl.pack(anchor="w", padx=4, pady=2,
                         before=self._refresh_conn_btn)

    def set_bench_label(self, bench: str = ""):
        frame = getattr(self, "_inst_frame", None)
        if frame is None:
            return
        frame.config(text=f"Instruments — {bench}" if bench else "Instruments")

    @staticmethod
    def _enable_tab_drag(nb: ttk.Notebook):
        state = {}

        def on_press(event):
            try:
                state["src"] = nb.index(f"@{event.x},{event.y}")
            except tk.TclError:
                state["src"] = None

        def on_motion(event):
            if state.get("src") is None:
                return
            try:
                dst = nb.index(f"@{event.x},{event.y}")
            except tk.TclError:
                return
            if dst != state["src"]:
                nb.insert(dst, nb.tabs()[state["src"]])
                state["src"] = dst

        nb.bind("<ButtonPress-1>", on_press, add=True)
        nb.bind("<B1-Motion>",     on_motion, add=True)

    def _build_notebook(self, paned):
        top_nb = ttk.Notebook(paned)
        paned.add(top_nb, weight=1)

        main_frame = ttk.Frame(top_nb)
        top_nb.add(main_frame, text="  Main  ")
        main_nb = ttk.Notebook(main_frame)
        main_nb.pack(fill="both", expand=True)
        self._enable_tab_drag(main_nb)

        self._tab_execution2(main_nb)
        if self._system == "accretech":
            self._tab_pma_wafer(main_nb)
            self._tab_probe_card(main_nb)
            self._tab_recipe(main_nb)
            self._tab_wafer_map(main_nb)
            self._tab_results(main_nb)
            self._tab_cassette(main_nb)
            main_nb.insert(1, self.results_tab_frame)
            main_nb.insert(2, self.cassette_panel.master)
        else:
            self._tab_results(main_nb)
            self._tab_recipe(main_nb)
            self._tab_probe_card(main_nb)
            self._tab_pma_process(main_nb)
            self._tab_recipe_gen(main_nb)
            self._tab_wafer_map(main_nb)

        debug_frame = ttk.Frame(top_nb)
        top_nb.add(debug_frame, text="  Debug  ")
        debug_nb = ttk.Notebook(debug_frame)
        debug_nb.pack(fill="both", expand=True)
        self._enable_tab_drag(debug_nb)

        if self._system == "accretech":
            self._tab_instruments(debug_nb)
            self._tab_probe_routing(debug_nb)
        else:
            self._tab_instruments_eg(debug_nb)
        self._tab_setup(debug_nb)
        self._tab_gds_parser(debug_nb)
        self._tab_switch_settings(debug_nb)
        self._tab_prober_debug(debug_nb)
        self._tab_gpib_trace(debug_nb)
        self._tab_nanoz_switch(debug_nb)

    _ACCRETECH_INSTRUMENTS = [
        ("UF200R Prober", "prober"),
        ("Switch Matrix (Keithley 707B)", "switch_matrix"),
        ("SMU (Keithley 2636B)", "smu"),
        ("DMM (Keysight 34461A)", "dmm"),
        ("Wave Gen (Keysight 33512B)", "wave_gen"),
    ]

    def _build_addresses_accretech(self, parent, row: int):
        panel = build_address_panel(
            parent, self._ACCRETECH_INSTRUMENTS, self.controller.log, self._init_hardware_fn)
        panel.grid(row=row, column=0, sticky="ew", padx=8, pady=(6, 0))

    def _tab_instruments(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Instruments")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)

        canvas = tk.Canvas(tab, highlightthickness=0)
        vsb = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        inner = ttk.Frame(canvas)
        inner.columnconfigure(0, weight=1)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                   lambda e: canvas.itemconfig(inner_id, width=e.width))

        def _on_mousewheel(evt):
            canvas.yview_scroll(int(-1 * (evt.delta / 120)), "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        tab = inner

        rst = ttk.Frame(tab)
        rst.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            rst,
            text="Global Reset — All Outputs OFF + Open All Switches",
            command=self._global_reset,
        ).pack(side="left", padx=8, pady=4)
        ttk.Button(
            rst,
            text="↻ Query Status",
            command=lambda: threading.Thread(
                target=self._query_all_status, daemon=True).start(),
        ).pack(side="left", padx=4, pady=4)
        ttk.Button(
            rst,
            text="↩ Release All To Local",
            command=self._release_all_to_local,
        ).pack(side="left", padx=4, pady=4)

        sbar = ttk.Frame(tab)
        sbar.grid(row=1, column=0, sticky="ew")
        for key, lbl in [("smua", "SMU A"), ("smub", "SMU B"),
                          ("wg1", "WG CH1"), ("wg2", "WG CH2"),
                          ("dmm", "DMM"), ("prober", "Prober")]:
            v = tk.StringVar(value=f"{lbl}: ?")
            self._inst_status_vars[key] = v
            ttk.Label(sbar, textvariable=v,
                     font=("Consolas", 8), padding=(10, 2)).pack(side="left")

        pane = ttk.PanedWindow(tab, orient="horizontal")
        pane.grid(row=2, column=0, sticky="nsew")

        dmm_pane = ttk.Frame(pane)
        pane.add(dmm_pane, weight=1)

        smu_pane = ttk.Frame(pane)
        pane.add(smu_pane, weight=3)

        wg_pane = ttk.Frame(pane)
        pane.add(wg_pane, weight=1)

        self._build_dmm_card(dmm_pane)
        self._build_smu_card(smu_pane)
        self._build_smu2400_card(smu_pane)
        self._build_wavegen_card(wg_pane)

        self._build_addresses_accretech(tab, row=3)

    def _build_dmm_card(self, parent):
        card = ttk.LabelFrame(parent, text="Keysight 34461A  (DMM)")
        card.pack(fill="both", expand=True, padx=6, pady=6)

        ttk.Label(
            card, text="Addr: USB0::0x2A8D::0x1301::MY57216618::INSTR",
            foreground="gray", font=("Consolas", 8)
        ).pack(anchor="w", padx=6, pady=(4, 0))

        self._dmm_status_var = tk.StringVar(value="○ IDLE")
        ttk.Label(card, textvariable=self._dmm_status_var,
                  font=("Consolas", 8, "bold"), foreground="#6b7280",
                  ).pack(anchor="w", padx=6, pady=(0, 2))

        reading_var = tk.StringVar(value="──")
        ttk.Label(
            card, textvariable=reading_var,
            font=("Consolas", 18, "bold"), foreground="#0077cc"
        ).pack(pady=10)

        def measure(mode):
            drv = self.controller.drivers.get("dmm")
            if not drv or not drv.inst:
                reading_var.set("NOT CONNECTED")
                self.controller.log(f"[INSTRUMENT] {mode}: not connected")
                return
            try:
                if mode == "VDC":
                    val = drv.measure_voltage_dc();  reading_var.set(format_engineering(val, "V"))
                elif mode == "IDC":
                    val = drv.measure_current_dc();  reading_var.set(format_engineering(val, "A"))
                elif mode == "R2W":
                    val = drv.measure_resistance(2); reading_var.set(format_engineering(val, "Ω"))
                elif mode == "R4W":
                    val = drv.measure_resistance(4); reading_var.set(format_engineering(val, "Ω"))
                self.controller.log(f"[INSTRUMENT] {mode}: {reading_var.get()}")
            except Exception as e:
                reading_var.set("ERROR"); self.controller.log(f"[INSTRUMENT] {mode} error: {e}")

        btn_row = ttk.Frame(card)
        btn_row.pack(fill="x", padx=6, pady=2)
        for lbl, mode in [("VDC", "VDC"), ("IDC", "IDC"), ("Ω 2W", "R2W"), ("Ω 4W", "R4W")]:
            ttk.Button(btn_row, text=f"Meas {lbl}", command=lambda m=mode: measure(m)).pack(side="left", padx=2, pady=2)

        all_lf = ttk.LabelFrame(card, text="All Readings", padding=(6, 4))
        all_lf.pack(fill="x", padx=6, pady=(2, 0))
        all_lf.columnconfigure(1, weight=1)
        all_lf.columnconfigure(3, weight=1)

        _all_vars: dict[str, tk.StringVar] = {}
        _all_items = [("VDC:", "VDC", "V"), ("IDC:", "IDC", "A"), ("R 2W:", "R2W", "Ω"), ("R 4W:", "R4W", "Ω")]
        for i, (lbl, key, _) in enumerate(_all_items):
            r, c = divmod(i, 2)
            ttk.Label(all_lf, text=lbl, width=5, anchor="e").grid(row=r, column=c*2,   sticky="e",  padx=(4, 2), pady=2)
            v = tk.StringVar(value="——")
            ttk.Label(all_lf, textvariable=v,
                      font=("Consolas", 9, "bold"), foreground="#0077cc",
                      anchor="w").grid(row=r, column=c*2+1, sticky="ew", padx=(0, 8), pady=2)
            _all_vars[key] = v

        def _meas_all_dmm():
            drv = self.controller.drivers.get("dmm")
            if not drv or not drv.inst:
                self.controller.log("[INSTRUMENT] Meas All: not connected")
                return
            pairs = [
                ("VDC", drv.measure_voltage_dc,           lambda x: format_engineering(x, "V")),
                ("IDC", drv.measure_current_dc,           lambda x: format_engineering(x, "A")),
                ("R2W", lambda: drv.measure_resistance(2), lambda x: format_engineering(x, "Ω")),
                ("R4W", lambda: drv.measure_resistance(4), lambda x: format_engineering(x, "Ω")),
            ]
            for key, fn, fmt in pairs:
                try:
                    _all_vars[key].set(fmt(fn()))
                except Exception as e:
                    _all_vars[key].set("ERROR")
                    self.controller.log(f"[INSTRUMENT] Meas All {key} error: {e}")
            self.controller.log(
                f"[INSTRUMENT] All: VDC={_all_vars['VDC'].get()}  IDC={_all_vars['IDC'].get()}  "
                f"R2W={_all_vars['R2W'].get()}  R4W={_all_vars['R4W'].get()}"
            )

        ttk.Button(card, text="Meas All  (V · I · R2W · R4W)",
                   command=_meas_all_dmm).pack(fill="x", padx=6, pady=(4, 0))

        ttk.Separator(card, orient="horizontal").pack(fill="x", padx=6, pady=6)

        cfg_lf = ttk.LabelFrame(card, text="Configuration", padding=(8, 4))
        cfg_lf.pack(fill="x", padx=6, pady=(0, 4))

        dmm_func_var  = tk.StringVar(value="VDC")
        dmm_range_var = tk.StringVar(value="AUTO")
        dmm_nplc_var  = tk.StringVar(value="1")

        cfg_row1 = ttk.Frame(cfg_lf)
        cfg_row1.pack(fill="x", pady=2)
        ttk.Label(cfg_row1, text="Function:", width=9, anchor="e").pack(side="left")
        ttk.Combobox(cfg_row1, textvariable=dmm_func_var,
                     values=["VDC", "IDC", "R2W", "R4W"],
                     width=6, state="readonly").pack(side="left", padx=(4, 0))

        cfg_row2 = ttk.Frame(cfg_lf)
        cfg_row2.pack(fill="x", pady=2)
        ttk.Label(cfg_row2, text="Range:", width=9, anchor="e").pack(side="left")
        ttk.Entry(cfg_row2, textvariable=dmm_range_var, width=10).pack(side="left", padx=(4, 6))
        ttk.Label(cfg_row2, text="NPLC:", width=6, anchor="e").pack(side="left")
        ttk.Entry(cfg_row2, textvariable=dmm_nplc_var, width=5).pack(side="left", padx=(4, 0))

        def _dmm_configure():
            drv = self.controller.drivers.get("dmm")
            if not drv or not drv.inst:
                self.controller.log("[INSTRUMENT] Configure: not connected")
                return
            try:
                func  = dmm_func_var.get()
                rng   = dmm_range_var.get().strip()
                nplc  = float(dmm_nplc_var.get())
                drv.set_nplc(nplc)
                func_map = {
                    "VDC": ("VOLT:DC", "VOLT:DC:RANG"),
                    "IDC": ("CURR:DC", "CURR:DC:RANG"),
                    "R2W": ("RES",     "RES:RANG"),
                    "R4W": ("FRES",    "FRES:RANG"),
                }
                func_cmd, rang_cmd = func_map.get(func, ("VOLT:DC", "VOLT:DC:RANG"))
                drv.write(f"CONF:{func_cmd}")
                if rng.upper() != "AUTO":
                    try:
                        drv.write(f"{rang_cmd} {parse_engineering(rng)}")
                    except ValueError:
                        pass
                self.controller.log(f"[INSTRUMENT] Configured: func={func}, range={rng}, NPLC={nplc}")
            except Exception as e:
                self.controller.log(f"[INSTRUMENT] Configure error: {e}")

        ttk.Button(cfg_lf, text="Apply Configuration",
                   command=_dmm_configure).pack(fill="x", pady=(4, 2))

        cont_lf = ttk.LabelFrame(card, text="Continuous Read", padding=(6, 4))
        cont_lf.pack(fill="x", padx=6, pady=(4, 0))

        cont_r = ttk.Frame(cont_lf)
        cont_r.pack(fill="x", pady=2)
        ttk.Label(cont_r, text="Interval:", width=9, anchor="e").pack(side="left")
        _dmm_cont_iv = tk.StringVar(value="500")
        ttk.Entry(cont_r, textvariable=_dmm_cont_iv, width=6).pack(side="left", padx=2)
        ttk.Label(cont_r, text="ms", foreground="gray").pack(side="left")
        _dmm_cont_btn = ttk.Button(cont_r, text="▶ Continuous")
        _dmm_cont_btn.pack(side="right", padx=(4, 0))

        def _toggle_cont_dmm():
            if self._dmm_cont_active:
                self._dmm_cont_active = False
                _dmm_cont_btn.config(text="▶ Continuous")
                self._dmm_status_var.set("○ IDLE")
                self.controller.log("[INSTRUMENT] Continuous read stopped")
            else:
                self._dmm_cont_active = True
                _dmm_cont_btn.config(text="■ Stop")
                self.controller.log("[INSTRUMENT] Continuous read started")
                def _loop():
                    while self._dmm_cont_active:
                        try:
                            ms = max(100, int(_dmm_cont_iv.get()))
                        except ValueError:
                            ms = 500
                        drv = self.controller.drivers.get("dmm")
                        if drv and drv.inst:
                            try:
                                func_now = dmm_func_var.get()
                                if func_now == "VDC":
                                    val = drv.measure_voltage_dc()
                                    reading_var.set(format_engineering(val, "V"))
                                    self._dmm_status_var.set(f"● CONT  {format_engineering(val, 'V')}")
                                elif func_now == "IDC":
                                    val = drv.measure_current_dc()
                                    reading_var.set(format_engineering(val, "A"))
                                    self._dmm_status_var.set(f"● CONT  {format_engineering(val, 'A')}")
                                elif func_now in ("R2W", "R4W"):
                                    mode = 4 if func_now == "R4W" else 2
                                    val = drv.measure_resistance(mode)
                                    reading_var.set(format_engineering(val, "Ω"))
                                    self._dmm_status_var.set(f"● CONT  {format_engineering(val, 'Ω')}")
                            except Exception as e:
                                self._dmm_status_var.set(f"● ERR: {e}")
                        time.sleep(ms / 1000)
                self._dmm_cont_thread = threading.Thread(target=_loop, daemon=True)
                self._dmm_cont_thread.start()

        _dmm_cont_btn.config(command=_toggle_cont_dmm)

        ttk.Separator(card, orient="horizontal").pack(fill="x", padx=6, pady=6)
        self._scpi_row(card, "dmm")

    def _build_smu_card(self, parent):
        card = ttk.LabelFrame(parent, text="Keithley 2636B  (SMU)")
        card.pack(fill="both", expand=True, padx=6, pady=6)
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="Addr: GPIB0::10::INSTR",
                  foreground="gray", font=("Consolas", 8)).pack(
                  anchor="w", padx=8, pady=(4, 6))

        ch_frame = ttk.Frame(card)
        ch_frame.pack(fill="both", expand=True, padx=6)
        ch_frame.columnconfigure(0, weight=1)
        ch_frame.columnconfigure(1, weight=1)

        self._smu_last = {
            "smua": {"I": None, "V": None, "R": None},
            "smub": {"I": None, "V": None, "R": None},
        }

        for idx, ch in enumerate(("smua", "smub")):
            self._build_smu_channel(ch_frame, ch, col=idx)

        ttk.Separator(card, orient="horizontal").pack(fill="x", padx=6, pady=6)
        self._scpi_row(card, "smu")

    def _build_smu2400_card(self, parent):
        card = ttk.LabelFrame(parent, text="Keithley 2400  (SMU)")
        card.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        card.columnconfigure(0, weight=1)

        term_row = ttk.Frame(card)
        term_row.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(term_row, text="Terminals:").pack(side="left")
        term_status = tk.StringVar(value="—")
        ttk.Label(term_row, textvariable=term_status, font=("Consolas", 9, "bold"),
                 foreground="#374151", width=6).pack(side="left", padx=(4, 8))

        def _set_terminals(which):
            drv = self.controller.drivers.get("smu")
            if not drv or not drv.inst:
                self.controller.log("[INSTRUMENT] terminals: not connected")
                return
            if not hasattr(drv, "set_terminals"):
                self.controller.log(
                    "[INSTRUMENT] the instrument in the 'smu' slot right now has no "
                    "FRONT/REAR terminal switch")
                return
            try:
                drv.set_terminals(which)
                term_status.set(which)
                self.controller.log(f"[INSTRUMENT] terminals -> {which}")
            except Exception as e:
                self.controller.log(f"[INSTRUMENT] set_terminals error: {e}")

        ttk.Button(term_row, text="Front", width=7,
                  command=lambda: _set_terminals("FRONT")).pack(side="left", padx=2)
        ttk.Button(term_row, text="Rear", width=7,
                  command=lambda: _set_terminals("REAR")).pack(side="left", padx=2)

        ch_frame = ttk.Frame(card)
        ch_frame.pack(fill="both", expand=True, padx=6)
        ch_frame.columnconfigure(0, weight=1)
        self._smu_last["smu2400"] = {"I": None, "V": None, "R": None}
        self._build_smu_channel(ch_frame, "smu2400", col=0,
                                drv_channel="smua", title="Keithley 2400")

    def _build_smu_channel(self, parent, ch: str, col: int,
                           drv_channel: str = None, title: str = None):
        drv_channel = drv_channel or ch
        title = title or ch.upper()
        lf = ttk.LabelFrame(parent, text=f"{title}  ○ OFF", padding=(8, 6))
        lf.grid(row=0, column=col, sticky="nsew",
                padx=(0 if col == 0 else 6, 0), pady=0)
        lf.columnconfigure(1, weight=1)
        self._smu_output_lf[ch] = lf

        src_row = ttk.Frame(lf)
        src_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(src_row, text="Source:").pack(side="left")
        src_var = tk.StringVar(value="Voltage")
        src_cb  = ttk.Combobox(src_row, textvariable=src_var,
                                values=["Voltage", "Current"],
                                width=8, state="readonly")
        src_cb.pack(side="left", padx=(4, 0))

        level_row = ttk.Frame(lf)
        level_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(level_row, text="Level:", width=9, anchor="e").pack(side="left")
        level_var = tk.StringVar(value="0.0")
        self._smu_level_vars[ch] = level_var
        ttk.Entry(level_row, textvariable=level_var, width=7).pack(side="left", padx=2)
        level_unit = ttk.Label(level_row, text="V", foreground="gray")
        level_unit.pack(side="left")

        comp_row = ttk.Frame(lf)
        comp_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        comp_lbl = ttk.Label(comp_row, text="I Limit:", width=9, anchor="e")
        comp_lbl.pack(side="left")
        comp_var = tk.StringVar(value="100e-6")
        ttk.Entry(comp_row, textvariable=comp_var, width=7).pack(side="left", padx=2)
        comp_unit = ttk.Label(comp_row, text="A", foreground="gray")
        comp_unit.pack(side="left")

        def _on_src(*_):
            if src_var.get() == "Voltage":
                level_unit.config(text="V")
                comp_lbl.config(text="I Limit:")
                comp_unit.config(text="A")
            else:
                level_unit.config(text="A")
                comp_lbl.config(text="V Limit:")
                comp_unit.config(text="V")
        src_var.trace_add("write", _on_src)

        nplc_row = ttk.Frame(lf)
        nplc_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(nplc_row, text="NPLC:", width=9, anchor="e").pack(side="left")
        nplc_var = tk.StringVar(value="1")
        ttk.Entry(nplc_row, textvariable=nplc_var, width=7).pack(side="left", padx=2)
        ttk.Label(nplc_row, text="PLC", foreground="gray").pack(side="left")

        out_row = ttk.Frame(lf)
        out_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 2))
        ttk.Button(out_row, text="Set & On",
                   command=lambda: _smu_set_on()).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(out_row, text="Output Off",
                   command=lambda: _smu_off()).pack(side="left", expand=True, fill="x", padx=(2, 0))

        ttk.Separator(lf, orient="horizontal").grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=6)

        reading_vars = {}
        for r_idx, (meas, key) in enumerate([
            ("I",  "I"),
            ("V",  "V"),
            ("R", "R"),
        ]):
            ttk.Label(lf, text=meas + ":", anchor="e", width=6).grid(
                row=6 + r_idx, column=0, sticky="e", pady=2)
            var = tk.StringVar(value="——")
            ttk.Label(lf, textvariable=var,
                      font=("Consolas", 10, "bold"),
                      foreground="#cc5500", anchor="w").grid(
                      row=6 + r_idx, column=1, sticky="ew", padx=(4, 0))
            reading_vars[key] = var

        meas_row = ttk.Frame(lf)
        meas_row.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(4, 1))
        ttk.Button(meas_row, text="Meas I",
                   command=lambda: _measure("I")).pack(side="left", expand=True, fill="x", padx=(0, 1))
        ttk.Button(meas_row, text="Meas V",
                   command=lambda: _measure("V")).pack(side="left", expand=True, fill="x", padx=1)
        ttk.Button(meas_row, text="Meas R",
                   command=lambda: _measure("R")).pack(side="left", expand=True, fill="x", padx=(1, 0))

        def _meas_all():
            _measure("I"); _measure("V"); _measure("R")
        ttk.Button(lf, text="Meas All  (I · V · R)",
                   command=_meas_all).grid(
                   row=10, column=0, columnspan=2, sticky="ew", pady=(1, 0))

        cont_row = ttk.Frame(lf)
        cont_row.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        _cont_iv = tk.StringVar(value="500")
        ttk.Label(cont_row, text="Interval:", width=9, anchor="e").pack(side="left")
        ttk.Entry(cont_row, textvariable=_cont_iv, width=5).pack(side="left", padx=2)
        ttk.Label(cont_row, text="ms", foreground="gray").pack(side="left")
        _cont_btn = ttk.Button(cont_row, text="▶ Cont.")
        _cont_btn.pack(side="right", padx=(4, 0))

        def _drv():
            drv = self.controller.drivers.get("smu")
            if not drv or not drv.inst:
                self.controller.log(f"[INSTRUMENT] {ch}: not connected")
                return None
            return drv

        def _smu_set_on():
            drv = _drv()
            if not drv:
                return
            try:
                src  = src_var.get()
                lvl  = parse_engineering(level_var.get())
                comp = parse_engineering(comp_var.get())
                nplc = float(nplc_var.get())
                if src == "Voltage":
                    drv.set_voltage(drv_channel, lvl)
                    drv.set_current_limit(drv_channel, comp)
                else:
                    drv.set_current(drv_channel, lvl)
                    drv.set_voltage_limit(drv_channel, comp)
                try:
                    drv.set_nplc(drv_channel, nplc)
                except Exception:
                    pass
                drv.turn_output_on(drv_channel)
                self.controller.log(f"[INSTRUMENT] {ch} ON — {src}={lvl}, comp={comp}, NPLC={nplc}")
                lf.config(text=f"{title}  ● ON")
            except Exception as e:
                self.controller.log(f"[INSTRUMENT] {ch} set_on error: {e}")

        def _smu_off():
            drv = _drv()
            if not drv:
                return
            try:
                drv.turn_output_off(drv_channel)
                self.controller.log(f"[INSTRUMENT] {ch} output OFF")
                lf.config(text=f"{title}  ○ OFF")
            except Exception as e:
                self.controller.log(f"[INSTRUMENT] {ch} off error: {e}")

        def _measure(what: str):
            drv = _drv()
            if not drv:
                return
            try:
                if what == "I":
                    val = drv.measure_current(drv_channel)
                    reading_vars["I"].set(format_engineering(val, "A"))
                    self._smu_last[ch]["I"] = val
                    self.controller.log(f"[INSTRUMENT] {ch} I = {format_engineering(val, 'A')}")
                elif what == "V":
                    val = drv.measure_voltage(drv_channel)
                    reading_vars["V"].set(format_engineering(val, "V"))
                    self._smu_last[ch]["V"] = val
                    self.controller.log(f"[INSTRUMENT] {ch} V = {format_engineering(val, 'V')}")
                elif what == "R":
                    val = drv.measure_resistance(drv_channel)
                    reading_vars["R"].set(format_engineering(val, "Ω"))
                    self._smu_last[ch]["R"] = val
                    self.controller.log(f"[INSTRUMENT] {ch} R = {format_engineering(val, 'Ω')}")
            except Exception as e:
                reading_vars[what].set("ERROR")
                self.controller.log(f"[INSTRUMENT] {ch} meas_{what} error: {e}")

        self._smu_cont_active[ch] = False

        def _toggle_cont():
            if self._smu_cont_active.get(ch, False):
                self._smu_cont_active[ch] = False
                _cont_btn.config(text="▶ Cont.")
                self.controller.log(f"[INSTRUMENT] {ch} continuous stopped")
            else:
                self._smu_cont_active[ch] = True
                _cont_btn.config(text="■ Stop")
                self.controller.log(f"[INSTRUMENT] {ch} continuous started")
                def _loop():
                    while self._smu_cont_active.get(ch, False):
                        try:
                            ms = max(100, int(_cont_iv.get()))
                        except ValueError:
                            ms = 500
                        _meas_all()
                        time.sleep(ms / 1000)
                threading.Thread(target=_loop, daemon=True).start()
        _cont_btn.config(command=_toggle_cont)

    def _global_reset(self):
        log = self.controller.log

        for ch in list(self._smu_cont_active):
            self._smu_cont_active[ch] = False

        self._dmm_cont_active = False
        if self._dmm_status_var:
            self._dmm_status_var.set("○ IDLE")

        drv_smu = self.controller.drivers.get("smu")
        if drv_smu and drv_smu.inst:
            for ch in ("smua", "smub"):
                try:
                    drv_smu.turn_output_off(ch)
                    drv_smu.set_voltage(ch, 0)
                    log(f"[INSTRUMENT] SMU {ch} OFF, level → 0 V")
                except Exception as e:
                    log(f"[INSTRUMENT] SMU {ch} error: {e}")
                lv = self._smu_level_vars.get(ch)
                if lv:
                    lv.set("0.0")
                lf = self._smu_output_lf.get(ch)
                if lf:
                    try:
                        lf.config(text=f"{ch.upper()}  ○ OFF")
                    except Exception:
                        pass
            lv = self._smu_level_vars.get("smu2400")
            if lv:
                lv.set("0.0")
            lf = self._smu_output_lf.get("smu2400")
            if lf:
                try:
                    lf.config(text="Keithley 2400  ○ OFF")
                except Exception:
                    pass
                sv = self._inst_status_vars.get(ch)
                if sv:
                    sv.set(f"{ch.upper()}: ○ OFF  0 V")

        drv_wg = self.controller.drivers.get("wave_gen")
        if drv_wg and drv_wg.inst:
            for ch_num in (1, 2):
                try:
                    drv_wg.turn_output_off_ch(ch_num)
                    log(f"[INSTRUMENT] WaveGen CH{ch_num} OFF")
                except Exception as e:
                    log(f"[INSTRUMENT] WaveGen CH{ch_num} error: {e}")
                lf = self._wg_output_lf.get(ch_num)
                if lf:
                    try:
                        lf.config(text=f"CH {ch_num}  ○ OFF")
                    except Exception:
                        pass
                sv = self._inst_status_vars.get(f"wg{ch_num}")
                if sv:
                    sv.set(f"WG CH{ch_num}: ○ OFF")

        drv_sw = self.controller.drivers.get("switch")
        if drv_sw and drv_sw.inst:
            try:
                drv_sw.open_all()
                log("[INSTRUMENT] Switch matrix: all channels open")
            except Exception as e:
                log(f"[INSTRUMENT] Switch open_all error: {e}")

        log("[INSTRUMENT] Global reset complete")

    def _release_all_to_local(self):
        released, failed = [], []
        for key, drv in self.controller.drivers.items():
            if not drv or not getattr(drv, "inst", None):
                continue
            try:
                if drv.go_to_local():
                    released.append(key)
                else:
                    failed.append(key)
            except Exception as e:
                failed.append(key)
                self.controller.log(f"[INSTRUMENT] {key}: {e}")
        if released:
            self.controller.log(f"[INSTRUMENT] Released to local: {', '.join(released)}")
        if failed:
            self.controller.log(f"[INSTRUMENT] Could not release: {', '.join(failed)}")
        if not released and not failed:
            self.controller.log("[INSTRUMENT] No instruments connected.")

    def _query_all_status(self):
        def _sv(key, text):
            v = self._inst_status_vars.get(key)
            if v:
                v.set(text)

        drv_smu = self.controller.drivers.get("smu")
        if drv_smu and drv_smu.inst:
            for ch in ("smua", "smub"):
                key = ch
                try:
                    raw = drv_smu.query(f"print({ch}.source.output)")
                    is_on = str(raw).strip().startswith("1")
                    lf = self._smu_output_lf.get(ch)
                    if is_on:
                        lf and lf.config(text=f"{ch.upper()}  ● ON")
                        _sv(key, f"{ch.upper()}: ● ON")
                    else:
                        lf and lf.config(text=f"{ch.upper()}  ○ OFF")
                        _sv(key, f"{ch.upper()}: ○ OFF")
                except Exception as e:
                    _sv(key, f"{ch.upper()}: ERR")
                    self.controller.log(f"[INSTRUMENT] SMU {ch}: {e}")
        else:
            for ch in ("smua", "smub"):
                _sv(ch, f"{ch.upper()}: —")

        drv_wg = self.controller.drivers.get("wave_gen")
        if drv_wg and drv_wg.inst:
            for ch_num in (1, 2):
                key = f"wg{ch_num}"
                try:
                    raw = drv_wg.query(f"OUTPut{ch_num}?")
                    is_on = str(raw).strip() in ("1", "ON")
                    lf = self._wg_output_lf.get(ch_num)
                    if is_on:
                        lf and lf.config(text=f"CH {ch_num}  ● ON")
                        _sv(key, f"WG CH{ch_num}: ● ON")
                    else:
                        lf and lf.config(text=f"CH {ch_num}  ○ OFF")
                        _sv(key, f"WG CH{ch_num}: ○ OFF")
                except Exception as e:
                    _sv(key, f"WG CH{ch_num}: ERR")
                    self.controller.log(f"[INSTRUMENT] WaveGen CH{ch_num}: {e}")
        else:
            for ch_num in (1, 2):
                _sv(f"wg{ch_num}", f"WG CH{ch_num}: —")

        drv_dmm = self.controller.drivers.get("dmm")
        if drv_dmm and drv_dmm.inst:
            try:
                raw = drv_dmm.query(":FUNC?").strip().strip('"')
                _sv("dmm", f"DMM: {raw}")
            except Exception as e:
                _sv("dmm", "DMM: ERR")
                self.controller.log(f"[INSTRUMENT] DMM: {e}")
        else:
            _sv("dmm", "DMM: —")

        drv_prb = self.controller.drivers.get("prober")
        if drv_prb and drv_prb.inst:
            try:
                stb, desc = drv_prb.read_stb_decoded()
                Z_UP   = {67, 65, 75}
                Z_DOWN = {66, 68, 70}
                if stb in Z_UP:
                    z_str = "Z UP (contact)"
                elif stb in Z_DOWN:
                    z_str = "Z DOWN"
                else:
                    z_str = f"STB={stb}"
                _sv("prober", f"Prober: {z_str}")
            except Exception as e:
                _sv("prober", "Prober: ERR")
                self.controller.log(f"[INSTRUMENT] Prober: {e}")
        else:
            _sv("prober", "Prober: —")

    def _build_wavegen_card(self, parent):
        card = ttk.LabelFrame(parent, text="Keysight 33512B  (Wave Gen)")
        card.pack(fill="both", expand=True, padx=6, pady=6)

        ttk.Label(
            card, text="Addr: GPIB0::12::INSTR",
            foreground="gray", font=("Consolas", 8)
        ).pack(anchor="w", padx=6, pady=(4, 0))

        ch_frame = ttk.Frame(card)
        ch_frame.pack(fill="x", padx=6, pady=(4, 0))
        ch_frame.columnconfigure(0, weight=1)
        ch_frame.columnconfigure(1, weight=1)

        for idx, ch_num in enumerate((1, 2)):
            self._build_wavegen_channel(ch_frame, ch_num, col=idx)

        ttk.Separator(card, orient="horizontal").pack(fill="x", padx=6, pady=8)
        self._scpi_row(card, "wave_gen")

    def _build_wavegen_channel(self, parent, ch: int, col: int):
        lf = ttk.LabelFrame(parent, text=f"CH {ch}  ○ OFF", padding=(8, 6))
        lf.grid(row=0, column=col, sticky="nsew",
                padx=(0 if col == 0 else 6, 0), pady=0)
        self._wg_output_lf[ch] = lf

        shape_var  = tk.StringVar(value="SIN")
        freq_var   = tk.StringVar(value="1000")
        amp_var    = tk.StringVar(value="1.0")
        offset_var = tk.StringVar(value="0.0")

        sh_row = ttk.Frame(lf)
        sh_row.pack(fill="x", pady=(4, 3))
        ttk.Label(sh_row, text="Shape:", width=8, anchor="e").pack(side="left")
        ttk.Combobox(sh_row, textvariable=shape_var,
                     values=["SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"],
                     width=7, state="readonly").pack(side="left", padx=4)

        for lbl, var, unit in [("Freq:", freq_var, "Hz"),
                                ("Amp:", amp_var, "Vpp"),
                                ("Offset:", offset_var, "V")]:
            f = ttk.Frame(lf)
            f.pack(fill="x", pady=2)
            ttk.Label(f, text=lbl, width=8, anchor="e").pack(side="left")
            ttk.Entry(f, textvariable=var, width=9).pack(side="left", padx=4)
            ttk.Label(f, text=unit).pack(side="left")

        def _drv():
            drv = self.controller.drivers.get("wave_gen")
            if not drv or not drv.inst:
                self.controller.log(f"[INSTRUMENT] CH{ch}: not connected")
                return None
            return drv

        def _apply():
            drv = _drv()
            if not drv:
                return
            try:
                freq = parse_engineering(freq_var.get())
                amp = parse_engineering(amp_var.get())
                offset = parse_engineering(offset_var.get())
                drv.set_waveform_ch(ch, shape_var.get(), freq, amp, offset)
                self.controller.log(
                    f"[INSTRUMENT] CH{ch} {shape_var.get()} {format_engineering(freq, 'Hz')}  "
                    f"{format_engineering(amp, 'Vpp')}  offset={format_engineering(offset, 'V')}"
                )
            except Exception as e:
                self.controller.log(f"[INSTRUMENT] CH{ch} apply error: {e}")

        def _on():
            drv = _drv()
            if not drv:
                return
            try:
                drv.turn_output_on_ch(ch)
                self.controller.log(f"[INSTRUMENT] CH{ch} ON")
                lf.config(text=f"CH {ch}  ● ON")
            except Exception as e:
                self.controller.log(f"[INSTRUMENT] CH{ch} on error: {e}")

        def _off():
            drv = _drv()
            if not drv:
                return
            try:
                drv.turn_output_off_ch(ch)
                self.controller.log(f"[INSTRUMENT] CH{ch} OFF")
                lf.config(text=f"CH {ch}  ○ OFF")
            except Exception as e:
                self.controller.log(f"[INSTRUMENT] CH{ch} off error: {e}")

        ttk.Button(lf, text="Apply", command=_apply).pack(fill="x", pady=(8, 2))
        out_row = ttk.Frame(lf)
        out_row.pack(fill="x", pady=2)
        ttk.Button(out_row, text="Output ON",  command=_on).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(out_row, text="Output OFF", command=_off).pack(side="left", expand=True, fill="x", padx=(2, 0))

    def _scpi_row(self, parent, driver_key):
        cmd_var  = tk.StringVar()
        resp_var = tk.StringVar(value="")

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="SCPI:").pack(side="left")
        ttk.Entry(row, textvariable=cmd_var, width=22).pack(side="left", padx=4, fill="x", expand=True)

        def send():
            cmd = cmd_var.get().strip()
            if not cmd:
                return
            drv = self.controller.drivers.get(driver_key)
            if not drv or not drv.inst:
                resp_var.set("NOT CONNECTED"); return
            try:
                if cmd.strip().endswith("?"):
                    resp = drv.query(cmd); resp_var.set(resp or "")
                else:
                    drv.write(cmd); resp_var.set("OK")
                self.controller.log(f"[{driver_key.upper()}] {cmd}  →  {resp_var.get()}")
            except Exception as e:
                resp_var.set(f"ERR: {e}")

        ttk.Button(row, text="Send", command=send).pack(side="left")

        def go_local():
            drv = self.controller.drivers.get(driver_key)
            if not drv or not drv.inst:
                self.controller.log(f"[{driver_key.upper()}] Go To Local: not connected")
                return
            ok = drv.go_to_local()
            self.controller.log(f"[{driver_key.upper()}] Go To Local: "
                                f"{'released' if ok else 'failed - see console'}")

        ttk.Button(row, text="↩ Go To Local", command=go_local).pack(side="left", padx=(4, 0))

        resp_row = ttk.Frame(parent)
        resp_row.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(resp_row, text="Resp:").pack(side="left")
        ttk.Label(resp_row, textvariable=resp_var, foreground="#0055aa",
                  font=("Consolas", 9)).pack(side="left", padx=4)

    def _build_default_prober_row(self, parent):
        lf = ttk.Frame(parent)
        lf.pack(fill="x", pady=(0, 4))

        ttk.Label(lf, text="Default prober — Start the GUI on:").pack(
            side="left", padx=(0, 4))
        self._default_prober_var = tk.StringVar()
        self._default_prober_cb = ttk.Combobox(
            lf, textvariable=self._default_prober_var, state="readonly", width=32,
            postcommand=self._refresh_default_prober_choices)
        self._default_prober_cb.pack(side="left", padx=(0, 6))
        ttk.Button(lf, text="Set Default",
                   command=self._set_default_prober).pack(side="left", padx=(0, 4))

        self._default_prober_lbl = ttk.Label(lf, text="", foreground="#374151",
                                             font=("Segoe UI", 8, "italic"))
        self._default_prober_lbl.pack(side="left")
        self._refresh_default_prober_choices()
        self._update_default_prober_label()

    def _build_default_yield_row(self, parent):
        lf = ttk.Frame(parent)
        lf.pack(fill="x", pady=(0, 4))

        ttk.Label(lf, text="Cassette pass-yield default — "
                          "Pass yield ≥").pack(side="left", padx=(0, 2))
        self._default_yield_var = tk.StringVar(value="0")
        ttk.Entry(lf, textvariable=self._default_yield_var, width=5).pack(
            side="left", padx=(0, 8))
        ttk.Button(lf, text="Set Default",
                  command=self._set_default_yield).pack(side="left", padx=(0, 10))

        self._autoexport_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(lf, text="AutoExport",
                       variable=self._autoexport_var,
                       command=self._on_autoexport_toggle).pack(side="left", padx=(16, 6))
        self._autoexport_csv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(lf, text="Also Save CSV",
                       variable=self._autoexport_csv_var).pack(side="left", padx=(0, 6))
        ttk.Button(lf, text="Set Default",
                  command=self._set_default_autoexport).pack(side="left", padx=(0, 10))
        self._autoexport_default_lbl = ttk.Label(lf, text="", foreground="#374151",
                                                  font=("Segoe UI", 8, "italic"))
        self._autoexport_default_lbl.pack(side="left")
        self._update_autoexport_default_label()

    def _update_autoexport_default_label(self):
        lbl = getattr(self, "_autoexport_default_lbl", None)
        if lbl is None:
            return
        if self._ata_folder:
            auto_export, save_csv = load_autoexport_settings(self._ata_folder)
            lbl.config(text=f"saved for this folder: AutoExport={'on' if auto_export else 'off'}, "
                            f"Also Save CSV={'on' if save_csv else 'off'}",
                      foreground="#166534")
        else:
            lbl.config(text="load an ATA folder first", foreground="#6b7280")

    def _set_default_autoexport(self):
        if not self._ata_folder:
            messagebox.showerror("No ATA Folder", "Load an ATA folder first.")
            return
        save_autoexport_settings(self._ata_folder, self._autoexport_var.get(),
                                 self._autoexport_csv_var.get())
        self._update_autoexport_default_label()
        self.controller.log(
            f"[SYSTEM] AutoExport default set to AutoExport="
            f"{'on' if self._autoexport_var.get() else 'off'}, Also Save CSV="
            f"{'on' if self._autoexport_csv_var.get() else 'off'} for "
            f"'{os.path.basename(self._ata_folder)}'.")

    def _on_autoexport_toggle(self):
        if self._autoexport_var.get():
            self._autoexport_claim_hook()
        else:
            self._autoexport_release_hook()

    def _autoexport_claim_hook(self):
        if not getattr(self, "_autoexport_var", None) or not self._autoexport_var.get():
            return
        current = getattr(self, "_exec_on_run_finished", None)
        if current in (None, self._on_autoexport_run_finished):
            self._exec_on_run_finished = self._on_autoexport_run_finished

    def _autoexport_release_hook(self):
        if getattr(self, "_exec_on_run_finished", None) is getattr(
                self, "_on_autoexport_run_finished", None):
            self._exec_on_run_finished = None

    def _on_autoexport_run_finished(self, pass_n, fail_n, total, aborted, run_mode="full"):
        if aborted:
            return
        tested = pass_n + fail_n
        pct = (pass_n / tested * 100) if tested else 0.0
        lot_id = self.lot_id.get().strip()
        wafer_id = self.wafer_id_var.get().strip()
        if not lot_id or not wafer_id:
            self._show_autoexport_missing_ids_dialog(pass_n, fail_n, tested, pct)
            return
        self._autoexport_run(pass_n, fail_n, tested, pct)

    def _autoexport_run(self, pass_n, fail_n, tested, pct):
        path = self.controller.cmd_export_sql()
        self.controller.log(
            f"[SYSTEM] AutoExport -> {path}" if path else
            "[SYSTEM] AutoExport produced no file - see the log above for why.")
        csv_path = None
        if self._autoexport_csv_var.get():
            csv_path = self.controller.cmd_save_csv()
            self.controller.log(
                f"[SYSTEM] AutoExport CSV -> {csv_path}" if csv_path else
                "[SYSTEM] AutoExport CSV produced no file - see the log above for why.")
        self._show_autoexport_result_dialog(pass_n, fail_n, tested, pct, path, csv_path)

    def _show_autoexport_result_dialog(self, pass_n, fail_n, tested, pct, path, csv_path):
        dlg = tk.Toplevel(self)
        dlg.title("Run Finished — Exported")
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)

        body = (f"{pass_n}/{tested} pass ({pct:.1f}%)\n\n"
               f"Exported to: {self.export_path_var.get()}")
        if path:
            body += f"\nFile: {os.path.basename(path)}"
        if csv_path:
            body += f"\nCSV: {os.path.basename(csv_path)}"
        ttk.Label(frm, text=body, justify="left").pack(anchor="w")

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        if self._is_cenfire_folder():
            ttk.Button(btns, text="Transfer Cenfire",
                      command=self._run_cenfire_transfer).pack(side="left")
        if self._is_lamp_folder():
            ttk.Button(btns, text="Push LaMP SQL Dump",
                      command=self._run_lamp_sql_push).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Close", command=dlg.destroy).pack(side="right")

        dlg.update_idletasks()
        dlg.grab_set()

    def _show_autoexport_missing_ids_dialog(self, pass_n, fail_n, tested, pct):
        dlg = tk.Toplevel(self)
        dlg.title("Lot/Wafer ID Not Defined")
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Lot and wafer id not defined - Make sure to save data",
                 font=("Segoe UI", 9, "bold"), foreground="#b45309").pack(anchor="w")
        ttk.Label(frm, text=f"{pass_n}/{tested} pass ({pct:.1f}%)").pack(
            anchor="w", pady=(6, 0))
        ttk.Label(frm, text=f"Output directory: {self.export_path_var.get()}").pack(
            anchor="w", pady=(2, 10))

        idrow = ttk.Frame(frm)
        idrow.pack(fill="x", pady=(0, 10))
        ttk.Label(idrow, text="Lot ID:").grid(row=0, column=0, sticky="w")
        ttk.Entry(idrow, textvariable=self.lot_id, width=18).grid(
            row=0, column=1, padx=(4, 14))
        ttk.Label(idrow, text="Wafer ID:").grid(row=0, column=2, sticky="w")
        ttk.Entry(idrow, textvariable=self.wafer_id_var, width=18).grid(
            row=0, column=3, padx=(4, 0))

        status_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=status_var, foreground="#6b7280",
                 font=("Segoe UI", 8)).pack(anchor="w")

        def _save_csv():
            path = self.controller.cmd_save_csv()
            status_var.set(f"Saved CSV -> {path}" if path else
                           "CSV save failed - see the log.")

        def _export():
            path = self.controller.cmd_export_sql()
            status_var.set(f"Exported -> {path}" if path else
                           "Export failed - see the log.")

        def _unload():
            drv = self.controller.drivers.get("prober")
            if not (drv and drv.inst):
                status_var.set("Prober not connected.")
                return
            unload_btn.config(state="disabled")
            status_var.set("Unloading…")

            def _run():
                self._exec_log("[RUN] >> U  (Unload)")
                try:
                    stb = drv.unload_wafer()
                    msg = (f"Unloaded (STB={stb})." if stb == 71
                          else f"Unexpected STB={stb}.")
                except Exception as e:
                    msg = f"Unload error: {e}"
                self._exec_log(f"[RUN] << {msg}")
                self._exec_safe_after(lambda: status_var.set(msg))
                self._exec_safe_after(lambda: unload_btn.config(state="normal"))
            threading.Thread(target=_run, daemon=True).start()

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Save to CSV", command=_save_csv).pack(side="left")
        ttk.Button(btns, text="Export", command=_export).pack(side="left", padx=(6, 0))
        unload_btn = ttk.Button(btns, text="Unload", command=_unload)
        unload_btn.pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")

        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.update_idletasks()
        dlg.grab_set()

    def _update_default_yield_label(self):
        lbl = getattr(self, "_default_yield_lbl", None)
        if lbl is None:
            return
        if self._ata_folder:
            lbl.config(text=f"saved for this folder: {load_yield_threshold(self._ata_folder):g}%",
                      foreground="#166534")
        else:
            lbl.config(text="load an ATA folder first", foreground="#6b7280")

    def _set_default_yield(self):
        if not self._ata_folder:
            messagebox.showerror("No ATA Folder", "Load an ATA folder first.")
            return
        try:
            pct = float(self._default_yield_var.get())
        except ValueError:
            messagebox.showerror("Invalid Value", "Pass yield must be a number.")
            return
        save_yield_threshold(self._ata_folder, pct)
        cassette = getattr(self, "cassette_panel", None)
        if cassette is not None:
            cassette._yield_folder = self._ata_folder
            cassette._yield_var.set(f"{pct:g}")
        self._update_default_yield_label()
        self.controller.log(
            f"[SYSTEM] Cassette pass-yield default set to {pct:g}% for "
            f"'{os.path.basename(self._ata_folder)}'.")

    def _prober_choices(self) -> list:
        out = [(f"Accretech — {b}", "accretech", b)
               for b in self.controller.accretech_benches()]
        for b in self.controller.electroglas_benches():
            out.append((f"Electroglas — {b}", "electroglas", b))
        return out

    def _refresh_default_prober_choices(self):
        self._prober_choice_map = {lab: (s, b) for lab, s, b in self._prober_choices()}
        self._default_prober_cb.config(values=list(self._prober_choice_map))
        if not self._default_prober_var.get():
            system, bench = app_settings.get_default_prober()
            match = next((lab for lab, (s, b) in self._prober_choice_map.items()
                          if s == system and b == bench), "")
            self._default_prober_var.set(match)

    def _update_default_prober_label(self):
        system, bench = app_settings.get_default_prober()
        if not system:
            self._default_prober_lbl.config(
                text="no default — the GUI starts on Accretech", foreground="#6b7280")
        else:
            self._default_prober_lbl.config(
                text=f"default: {system} / {bench}", foreground="#166534")

    def _set_default_prober(self):
        from tkinter import messagebox
        label = self._default_prober_var.get()
        pick = getattr(self, "_prober_choice_map", {}).get(label)
        if not pick:
            messagebox.showinfo("Default prober", "Pick a prober from the list first.")
            return
        system, bench = pick
        app_settings.set_default_prober(system, bench)
        self._update_default_prober_label()
        self.controller.log(
            f"[SYSTEM] Default prober set to {system} / {bench} — the GUI will "
            f"start on {'Electroglas' if system == 'electroglas' else 'Accretech'}.")
        self.controller.apply_prober(system, bench)

    def _clear_default_prober(self):
        app_settings.clear_default_prober()
        self._default_prober_var.set("")
        self._update_default_prober_label()
        self.controller.log("[SYSTEM] Default prober cleared.")

    def _build_working_dir_row(self, parent):
        lf = ttk.Frame(parent)
        lf.pack(fill="x", pady=(0, 4))

        ttk.Label(lf, text="Working Directory:").pack(side="left", padx=(0, 4))
        import workdir as _workdir
        self._workdir_preset_var = tk.StringVar(value="")
        preset_box = ttk.Combobox(
            lf, textvariable=self._workdir_preset_var, state="readonly",
            width=16, values=list(_workdir.PRESETS.keys()))
        preset_box.pack(side="left", padx=(0, 4))
        preset_box.bind("<<ComboboxSelected>>",
                        lambda _e: self.controller.cmd_pick_working_dir_preset(
                            self._workdir_preset_var.get()))
        ttk.Entry(lf, textvariable=self.working_dir_var, width=26).pack(
            side="left", padx=(0, 4))
        ttk.Button(
            lf, text="Browse...", command=self.controller.cmd_browse_working_dir
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            lf, text="Set Default", command=self.controller.cmd_set_default_working_dir
        ).pack(side="left", padx=(0, 10))

    def _tab_wafer_map(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Internal")
        tab.rowconfigure(3, weight=1)
        tab.columnconfigure(0, weight=1)

        ctrl = ttk.Frame(tab)
        ctrl.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))

        ttk.Button(ctrl, text="📁 Load ATA Folder…",
                  command=self.controller.cmd_import_map).pack(side="left", padx=(0, 10))
        ttk.Button(ctrl, text="＋ New ATA Folder…",
                  command=self.controller.cmd_new_ata_folder).pack(side="left", padx=(0, 10))
        ttk.Button(ctrl, text="Set Default",
                  command=self._set_default_ata_folder).pack(side="left", padx=(0, 10))
        ttk.Button(ctrl, text="↻ Refresh",
                  command=self.controller.cmd_refresh_ata).pack(side="left", padx=(0, 10))

        self._ata_path_lbl = ttk.Label(ctrl, text="No folder selected", foreground="gray")
        self._ata_path_lbl.pack(side="left", padx=10)

        self._default_ata_lbl = ttk.Label(ctrl, text="", foreground="#374151",
                                          font=("Segoe UI", 8, "italic"))
        self._default_ata_lbl.pack(side="left", padx=(0, 10))
        self._update_default_ata_label()

        settings_lf = ttk.LabelFrame(tab, text="Settings", padding=6)
        settings_lf.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))
        self._build_working_dir_row(settings_lf)
        self._build_default_prober_row(settings_lf)
        if self._system == "accretech":
            self._build_default_yield_row(settings_lf)

        self._map_source_var = tk.StringVar(
            value="Accretech" if self._system == "accretech" else "Wafer Builder")

        tree_frame = ttk.Frame(tab)
        tree_frame.grid(row=3, column=0, sticky="nsew", padx=6, pady=(2, 6))
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        cols = ("status", "detail")
        self._ata_tree = ttk.Treeview(
            tree_frame, columns=cols, show="tree headings", height=24, selectmode="browse"
        )
        self._ata_tree.heading("#0",      text="ATA Folder")
        self._ata_tree.heading("status",  text="")
        self._ata_tree.heading("detail",  text="Detail")
        self._ata_tree.column("#0",      width=340, stretch=True)
        self._ata_tree.column("status",  width=28,  stretch=False, anchor="center")
        self._ata_tree.column("detail",  width=340, stretch=True)
        self._ata_tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._ata_tree.yview)
        self._ata_tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")

        self._ata_tree.tag_configure("found",   foreground="#006400")
        self._ata_tree.tag_configure("missing", foreground="#999999")
        self._ata_tree.tag_configure("other",   foreground="#333333")
        self._ata_tree.tag_configure("section", font=("Segoe UI", 9, "bold"))
        self._ata_tree_meta = {}
        self._ata_tree.bind("<Button-3>", self._ata_tree_on_right_click)

        self.wafer_map = WaferMapPanel(tab)

    @staticmethod
    def _card_recipe_names(card_dir: str, base: str, system: str):
        path = recipe_file_path(card_dir, base, system)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                names = {row.get("recipe", "").strip()
                        for row in csv.DictReader(f)
                        if (row.get("kind", "").strip().upper() == "RECIPE"
                            and row.get("recipe", "").strip())}
            return sorted(names) or None
        except (OSError, csv.Error):
            return None

    @staticmethod
    def _card_recipes_by_bench(card_dir: str, base: str, system: str):
        path = recipe_file_path(card_dir, base, system)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                by_bench: dict = {}
                for row in csv.DictReader(f):
                    if (row.get("kind", "").strip().upper() != "RECIPE"):
                        continue
                    name = row.get("recipe", "").strip()
                    if not name:
                        continue
                    bench = (row.get("bench") or "").strip()
                    by_bench.setdefault(bench, set()).add(name)
        except (OSError, csv.Error):
            return None
        if not by_bench:
            return None
        return {bench: sorted(names) for bench, names in by_bench.items()}

    def _build_internal_tree(self, folder_path: str):
        tree = self._ata_tree
        for item in tree.get_children():
            tree.delete(item)
        self._ata_tree_meta = {}
        if not folder_path or not os.path.isdir(folder_path):
            return

        all_entries = os.listdir(folder_path)
        all_files = {f for f in all_entries
                    if os.path.isfile(os.path.join(folder_path, f))}
        subfolders = sorted(f for f in all_entries
                            if os.path.isdir(os.path.join(folder_path, f)))

        root_id = tree.insert(
            "", "end", text=f"📁 {os.path.basename(folder_path)}",
            open=True, tags=("section",))

        cards_dir = os.path.join(folder_path, "probe_cards")
        card_bases = set()
        if os.path.isdir(cards_dir):
            for fname in os.listdir(cards_dir):
                if not fname.lower().endswith(".csv"):
                    continue
                base = fname[:-4]
                for marker in (".recipes.", ".movelist."):
                    idx = base.lower().find(marker)
                    if idx != -1:
                        base = base[:idx]
                        break
                card_bases.add(base)
        cards_id = tree.insert(root_id, "end",
                               text=f"🎫 Probe Cards & Recipes ({len(card_bases)})",
                               open=True, tags=("section",))
        if card_bases:
            active_card = self.pin_wiring.get_active_card() if hasattr(self, "pin_wiring") else ""
            for base in sorted(card_bases):
                mark = "  (active)" if base == active_card else ""
                card_id = tree.insert(cards_id, "end", text=base + mark,
                                     open=True, tags=("found",))
                self._ata_tree_meta[card_id] = {"kind": "probe_card", "base": base}
                for system in ("accretech", "electroglas"):
                    by_bench = self._card_recipes_by_bench(cards_dir, base, system)
                    label = system.capitalize()
                    if by_bench is None:
                        tree.insert(card_id, "end", text=label,
                                   values=("–", "no recipes file for this system"),
                                   tags=("missing",))
                        continue
                    total = sum(len(names) for names in by_bench.values())
                    sys_id = tree.insert(card_id, "end", text=label, open=True,
                                        values=("✔", f"{total} recipe(s)"),
                                        tags=("found",))
                    for bench in sorted(by_bench, key=lambda b: (b == "", b)):
                        names = by_bench[bench]
                        bench_label = bench or "(any bench)"
                        bench_id = tree.insert(
                            sys_id, "end", text=bench_label, open=True,
                            values=("✔", f"{len(names)} recipe(s)"), tags=("found",))
                        for name in names:
                            recipe_item = tree.insert(bench_id, "end", text=name,
                                                     tags=("found",))
                            self._ata_tree_meta[recipe_item] = {
                                "kind": "recipe", "card_base": base,
                                "system": system, "name": name}
        else:
            tree.insert(cards_id, "end", text="(none yet)",
                       values=("–", "create one on the Probe Card tab"),
                       tags=("missing",))

        maps_dir = os.path.join(folder_path, "wafer_builder_maps")
        map_names = []
        if os.path.isdir(maps_dir):
            map_names = sorted(f[:-5] for f in os.listdir(maps_dir)
                               if f.lower().endswith(".json"))
        default_map = ""
        default_marker = os.path.join(maps_dir, "_default.txt")
        if os.path.isfile(default_marker):
            try:
                with open(default_marker, encoding="utf-8") as f:
                    default_map = f.read().strip()
            except OSError:
                pass
        maps_id = tree.insert(root_id, "end",
                              text=f"🗺 Wafer Builder Maps ({len(map_names)})",
                              open=True, tags=("section",))
        if map_names:
            for name in map_names:
                mark = "  (default)" if name == default_map else ""
                map_item = tree.insert(maps_id, "end", text=name + mark, tags=("found",))
                self._ata_tree_meta[map_item] = {"kind": "wafer_map", "name": name}
        else:
            tree.insert(maps_id, "end", text="(none yet)",
                       values=("–", "create one on the Wafer Builder tab"),
                       tags=("missing",))

        key_id = tree.insert(root_id, "end", text="📄 Key ATA Files",
                             open=False, tags=("section",))
        for fname, (desc, owner) in ATA_KEY_FILES.items():
            if owner not in ("shared", self._system):
                continue
            found = fname in all_files
            tree.insert(key_id, "end", text=fname,
                       values=("✔" if found else "–", desc),
                       tags=("found" if found else "missing",))

        if subfolders:
            sub_root = tree.insert(root_id, "end",
                                   text=f"📁 Subfolders ({len(subfolders)})",
                                   open=False, tags=("section",))
            for sub in subfolders:
                sub_path = os.path.join(folder_path, sub)
                try:
                    sub_files = sorted(os.listdir(sub_path))
                except OSError:
                    sub_files = []
                sub_id = tree.insert(sub_root, "end",
                                    text=f"📁 {sub}/  ({len(sub_files)})",
                                    open=False, tags=("other",))
                for fname in sub_files:
                    is_dir = os.path.isdir(os.path.join(sub_path, fname))
                    tree.insert(sub_id, "end",
                               text=(f"📁 {fname}/" if is_dir else fname),
                               tags=("other",))

        others = sorted(f for f in all_files if f not in ATA_KEY_FILES)
        if others:
            other_id = tree.insert(root_id, "end",
                                   text=f"📄 Other Files ({len(others)})",
                                   open=False, tags=("section",))
            for fname in others:
                tree.insert(other_id, "end", text=fname, tags=("other",))


    def _ata_tree_on_right_click(self, event):
        tree = self._ata_tree
        item = tree.identify_row(event.y)
        if not item:
            return
        meta = self._ata_tree_meta.get(item)
        if not meta:
            return
        tree.selection_set(item)
        menu = tk.Menu(self, tearoff=0)
        copy_label = {"recipe": "📋 Copy Recipe…", "wafer_map": "📋 Copy Wafer Map…",
                     "probe_card": "📋 Copy Probe Card…"}[meta["kind"]]
        delete_label = {"recipe": "🗑 Delete Recipe…", "wafer_map": "🗑 Delete Wafer Map…",
                       "probe_card": "🗑 Delete Probe Card…"}[meta["kind"]]
        menu.add_command(label=copy_label, command=lambda: self._ata_copy_dialog(meta))
        menu.add_command(label=delete_label, command=lambda: self._ata_delete_item(meta))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _ata_delete_item(self, meta: dict):
        kind = meta["kind"]
        if kind == "recipe":
            desc = f"{meta['name']}  (card {meta['card_base']!r}, {meta['system'].capitalize()})"
        elif kind == "wafer_map":
            desc = meta["name"]
        else:
            desc = meta["base"]
        kind_title = {"recipe": "Recipe", "wafer_map": "Wafer Map",
                     "probe_card": "Probe Card"}[kind]
        if not messagebox.askyesno(
                f"Delete {kind_title}",
                f"Delete {kind_title.lower()} {desc!r}? This cannot be undone.",
                parent=self):
            return

        if kind == "recipe":
            cards_dir = os.path.join(self._ata_folder, "probe_cards")
            path = recipe_file_path(cards_dir, meta["card_base"], meta["system"])
            err = delete_recipe(path, meta["name"])
        elif kind == "wafer_map":
            maps_dir = os.path.join(self._ata_folder, "wafer_builder_maps")
            err = delete_wafer_map(maps_dir, meta["name"])
        else:
            cards_dir = os.path.join(self._ata_folder, "probe_cards")
            err = delete_probe_card(cards_dir, meta["base"])

        if err:
            messagebox.showerror("Delete Failed", err, parent=self)
            self.controller.log(f"[SETUP] Delete {kind} {desc!r} failed: {err}")
            return
        self.controller.log(f"[SETUP] Deleted {kind} {desc!r}")
        self._build_internal_tree(self._ata_folder)

    def _ata_copy_dialog(self, meta: dict):
        kind = meta["kind"]
        dlg = tk.Toplevel(self)
        dlg.title({"recipe": "Copy Recipe", "wafer_map": "Copy Wafer Map",
                  "probe_card": "Copy Probe Card"}[kind])
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)
        row = 0

        def _label(text):
            nonlocal row
            ttk.Label(frm, text=text).grid(row=row, column=0, sticky="e",
                                           padx=(0, 6), pady=3)

        if kind == "recipe":
            src_desc = (f"{meta['name']}  (card {meta['card_base']!r}, "
                       f"{meta['system'].capitalize()})")
        elif kind == "wafer_map":
            src_desc = meta["name"]
        else:
            src_desc = meta["base"]
        _label("Source:")
        ttk.Label(frm, text=src_desc, font=("Segoe UI", 9, "bold")).grid(
            row=row, column=1, columnspan=2, sticky="w", pady=3)
        row += 1

        dest_folder_var = tk.StringVar(value=self._ata_folder or "")
        _label("Destination ATA folder:")
        ttk.Entry(frm, textvariable=dest_folder_var, width=42).grid(
            row=row, column=1, sticky="w", pady=3)

        def _browse_dest_folder():
            picked = filedialog.askdirectory(
                initialdir=dest_folder_var.get() or self._ata_folder or "",
                title="Destination ATA Folder")
            if picked:
                dest_folder_var.set(picked)
                _refresh_dest_choices()
        ttk.Button(frm, text="Browse...", command=_browse_dest_folder).grid(
            row=row, column=2, sticky="w", padx=(4, 0))
        row += 1

        dest_system_var = tk.StringVar(
            value=meta["system"] if kind == "recipe" else "")
        dest_card_var = tk.StringVar(
            value=meta.get("card_base") or meta.get("base") or "")
        if kind == "recipe":
            _label("Destination system:")
            sys_cb = ttk.Combobox(frm, textvariable=dest_system_var, state="readonly",
                                  width=14, values=("accretech", "electroglas"))
            sys_cb.grid(row=row, column=1, sticky="w", pady=3)
            row += 1

            _label("Destination probe card:")
            card_cb = ttk.Combobox(frm, textvariable=dest_card_var, width=24)
            card_cb.grid(row=row, column=1, sticky="w", pady=3)
            row += 1

            def _refresh_dest_choices(*_a):
                cards_dir = os.path.join(dest_folder_var.get() or "", "probe_cards")
                names = []
                if os.path.isdir(cards_dir):
                    for fname in os.listdir(cards_dir):
                        if fname.lower().endswith(".csv"):
                            b = fname[:-4]
                            for marker in (".recipes.", ".movelist."):
                                idx = b.lower().find(marker)
                                if idx != -1:
                                    b = b[:idx]
                                    break
                            if b not in names:
                                names.append(b)
                card_cb.config(values=sorted(names))
            sys_cb.bind("<<ComboboxSelected>>", _refresh_dest_choices)
            _refresh_dest_choices()

            _label("New recipe name:")
            name_var = tk.StringVar(value=meta["name"])
            ttk.Entry(frm, textvariable=name_var, width=26).grid(
                row=row, column=1, sticky="w", pady=3)
            row += 1

            _label("Prober:")
            bench_var = tk.StringVar(value="")
            bench_cb = ttk.Combobox(frm, textvariable=bench_var, width=18)
            bench_cb.grid(row=row, column=1, sticky="w", pady=3)
            row += 1

            def _refresh_bench_choices(*_a):
                try:
                    if dest_system_var.get() == "electroglas":
                        names = eg_profiles.profile_names()
                    else:
                        names = accretech_profiles.profile_names()
                except Exception:
                    names = []
                bench_cb.config(values=[""] + list(names))
            sys_cb.bind("<<ComboboxSelected>>", _refresh_bench_choices, add="+")
            _refresh_bench_choices()

            note_var = tk.StringVar(value="")
            ttk.Label(frm, textvariable=note_var, foreground="#b45309",
                     font=("Segoe UI", 8), wraplength=360, justify="left").grid(
                     row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
            row += 1

            def _update_note(*_a):
                cards_dir = os.path.join(dest_folder_var.get() or "", "probe_cards")
                main_path = os.path.join(cards_dir, f"{dest_card_var.get()}.csv")
                if dest_card_var.get() and not os.path.isfile(main_path):
                    note_var.set("⚠ This probe card doesn't exist yet at the "
                                 "destination - it will be created with just this "
                                 "recipe, no pin table. Use Copy Probe Card first "
                                 "if you also need the wiring.")
                else:
                    note_var.set("")
            dest_card_var.trace_add("write", _update_note)
            dest_folder_var.trace_add("write", _update_note)
            _update_note()
        elif kind == "wafer_map":
            def _refresh_dest_choices(*_a):
                pass
            _label("New map name:")
            default_name = meta["name"]
            if os.path.normpath(dest_folder_var.get() or "") == os.path.normpath(
                    self._ata_folder or ""):
                default_name = meta["name"] + "_copy"
            name_var = tk.StringVar(value=default_name)
            ttk.Entry(frm, textvariable=name_var, width=26).grid(
                row=row, column=1, sticky="w", pady=3)
            row += 1
        else:
            def _refresh_dest_choices(*_a):
                pass
            _label("New probe card name:")
            default_name = meta["base"]
            if os.path.normpath(dest_folder_var.get() or "") == os.path.normpath(
                    self._ata_folder or ""):
                default_name = meta["base"] + "_copy"
            name_var = tk.StringVar(value=default_name)
            ttk.Entry(frm, textvariable=name_var, width=26).grid(
                row=row, column=1, sticky="w", pady=3)
            row += 1

        def _do_copy():
            dest_folder = (dest_folder_var.get() or "").strip()
            dest_name = (name_var.get() or "").strip()
            if not dest_folder or not os.path.isdir(dest_folder):
                messagebox.showerror("Invalid Destination",
                                     "Pick a real destination ATA folder first.",
                                     parent=dlg)
                return
            if not dest_name:
                messagebox.showerror("Missing Name", "Enter a destination name.",
                                     parent=dlg)
                return

            if kind == "recipe":
                dest_card = (dest_card_var.get() or "").strip()
                dest_system = dest_system_var.get()
                if not dest_card:
                    messagebox.showerror("Missing Probe Card",
                                         "Enter a destination probe card name.",
                                         parent=dlg)
                    return
                src_cards_dir = os.path.join(self._ata_folder, "probe_cards")
                src_path = recipe_file_path(src_cards_dir, meta["card_base"], meta["system"])
                dst_cards_dir = os.path.join(dest_folder, "probe_cards")
                dst_path = recipe_file_path(dst_cards_dir, dest_card, dest_system)
                bench = (bench_var.get() or "").strip() or None
                err = copy_recipe(src_path, meta["name"], dst_path, dest_name, dst_bench=bench)
            elif kind == "wafer_map":
                src_dir = os.path.join(self._ata_folder, "wafer_builder_maps")
                dst_dir = os.path.join(dest_folder, "wafer_builder_maps")
                err = copy_wafer_map(src_dir, meta["name"], dst_dir, dest_name)
            else:
                src_dir = os.path.join(self._ata_folder, "probe_cards")
                dst_dir = os.path.join(dest_folder, "probe_cards")
                err = copy_probe_card(src_dir, meta["base"], dst_dir, dest_name)

            if err:
                messagebox.showerror("Copy Failed", err, parent=dlg)
                self.controller.log(f"[SETUP] Copy {kind} {src_desc!r} failed: {err}")
                return
            self.controller.log(
                f"[SETUP] Copied {kind} {src_desc!r} -> "
                f"{dest_name!r} in {dest_folder}")
            if os.path.normpath(dest_folder) == os.path.normpath(self._ata_folder or ""):
                self._build_internal_tree(self._ata_folder)
            dlg.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)
        ttk.Button(btns, text="Copy", command=_do_copy).pack(side="left")

        dlg.update_idletasks()
        dlg.grab_set()

    def load_ata_folder(self, folder_path):
        self._ata_folder = folder_path
        self._ata_path_lbl.config(text=folder_path, foreground="black")
        self._pad_custom_loaded = False

        self.pin_wiring.load_from_ata(folder_path)
        run = getattr(self, "eg_pma_run", None)
        reset = getattr(run, "forget_recipe", None)
        if callable(reset):
            try:
                reset()
            except Exception as exc:
                self._exec_log(f"[RUN] Could not reset the Run tab for the new "
                                f"ATA folder: {type(exc).__name__}: {exc}")

        self._build_internal_tree(folder_path)

        gen = getattr(self, "recipe_gen", None)
        map_pitch = gen._die_pitch() if gen is not None and hasattr(gen, "_die_pitch") else (1.0, 1.0)
        n_dies = self.wafer_map.load_from_ata(
            folder_path, filename=WAFER_MAP_SOURCES[self._map_source_var.get()],
            pitch=map_pitch)

        self.load_pad_layout(folder_path)
        self._on_pad_source_change()

        accr_wafer = getattr(self, "accr_wafer", None)
        if accr_wafer is not None:
            accr_wafer.load_from_ata(folder_path)
        pma_wafer = getattr(self, "pma_wafer", None)
        if pma_wafer is not None:
            pma_wafer.load_from_ata(folder_path)
        recipe_gen = getattr(self, "recipe_gen", None)
        if recipe_gen is not None:
            recipe_gen.autoload_map_for_folder(folder_path)
            try:
                recipe_gen._sync_views(folder_path)
            except Exception as exc:
                self._exec_log(f"[RUN] Could not publish the auto-loaded "
                                f"Wafer Builder map: {type(exc).__name__}: {exc}")
        self._exec_map_folder = folder_path
        self._exec_map_source_var.set(
            "Accretech" if self._system == "accretech" else "Wafer Builder")
        self._exec_clear_overlay()
        self._exec_wafer_map.clear_picks()
        self._exec_on_sites_changed([])
        self._exec_draw_wafer_map(quiet_if_missing=True)
        self._exec_reapply_overlay()
        self._refresh_export_formats()
        self._refresh_cenfire_transfer_button()
        self._refresh_lamp_push_button()
        if hasattr(self, "mdb_path_var"):
            global_mdb = app_settings.load_settings().get("mdb_path", "")
            self.mdb_path_var.set(mdb_export.load_mdb_path(folder_path, default=global_mdb))
            self._update_mdb_default_label()

        self._exec_autoload_default_recipe(folder_path)
        self._exec_sync_wafer_map_on_folder_load()

        self.controller.notify_nanoz_ata_folder_loaded(folder_path)

        cassette = getattr(self, "cassette_panel", None)
        if cassette is not None and hasattr(cassette, "on_ata_folder_loaded"):
            cassette.on_ata_folder_loaded(folder_path)
        if hasattr(self, "_default_yield_var"):
            self._default_yield_var.set(f"{load_yield_threshold(folder_path):g}")
            self._update_default_yield_label()
        if hasattr(self, "_autoexport_var"):
            auto_export, save_csv = load_autoexport_settings(folder_path)
            self._autoexport_var.set(auto_export)
            self._autoexport_csv_var.set(save_csv)
            self._on_autoexport_toggle()
            self._update_autoexport_default_label()

        self._update_default_ata_label()
        return n_dies

    def _set_default_ata_folder(self):
        from tkinter import messagebox
        if not self._ata_folder:
            messagebox.showerror("No ATA Folder", "Load an ATA folder first.")
            return
        app_settings.set_default_ata_folder(self._ata_folder)
        self._update_default_ata_label()
        self.controller.log(
            f"[SYSTEM] '{os.path.basename(self._ata_folder)}' set as the default "
            "ATA folder for the project — auto-loads on startup and when "
            "switching systems or probers.")

    def _update_default_ata_label(self):
        default_folder = app_settings.get_default_ata_folder()
        if default_folder:
            is_current = (default_folder == self._ata_folder)
            self._default_ata_lbl.config(
                text=("⭐ default: this folder" if is_current
                      else f"⭐ default: {os.path.basename(default_folder)}"))
        else:
            self._default_ata_lbl.config(text="")

    def _tab_probe_card(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Probe Card")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)

        split = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        split.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        self.pin_wiring = ProbeCardWiringFrame(
            split,
            get_folder=lambda: self._ata_folder,
            log_fn=self.controller.log,
            on_card_change=self._on_probe_card_change,
            on_pins_change=lambda: self.pad_panel.refresh_pins(),
            system=self._system,
            on_save_all=lambda: self._save_custom_pads(quiet=True),
        )
        split.add(self.pin_wiring, weight=1)

        right_col = ttk.PanedWindow(split, orient=tk.VERTICAL, width=340)
        split.add(right_col, weight=0)

        list_frame = ttk.LabelFrame(right_col, text="Pads")
        right_col.add(list_frame, weight=1)

        cols = ("pad", "x", "y")
        self._pad_tree = ttk.Treeview(
            list_frame, columns=cols, show="headings", height=10, selectmode="browse"
        )
        self._pad_tree.heading("pad", text="Pad")
        self._pad_tree.heading("x",   text="X (µm)")
        self._pad_tree.heading("y",   text="Y (µm)")
        self._pad_tree.column("pad", width=80)
        self._pad_tree.column("x",   width=65, anchor="e")
        self._pad_tree.column("y",   width=65, anchor="e")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self._pad_tree.yview)
        self._pad_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._pad_tree.pack(fill="both", expand=True)

        pad_container = ttk.Frame(right_col)
        right_col.add(pad_container, weight=1)
        pad_container.rowconfigure(0, weight=1)
        pad_container.columnconfigure(0, weight=1)

        self.pad_panel = PadLayoutPanel(pad_container, on_custom_change=self._refresh_pad_tree_from_custom,
                                        get_pins=self.pin_wiring.get_wiring,
                                        rename_pad=self.pin_wiring.rename_pad)
        self.pad_panel.grid(row=0, column=0, sticky="nsew")

        pad_ctrl = ttk.Frame(pad_container)
        pad_ctrl.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        ttk.Label(pad_ctrl, text="Layout:").pack(side="left")
        self._pad_source_var = tk.StringVar(value="Custom")
        pad_source_cb = ttk.Combobox(pad_ctrl, textvariable=self._pad_source_var,
                                     values=["ATA", "Custom"], state="readonly", width=8)
        pad_source_cb.pack(side="left", padx=(4, 8))
        pad_source_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_pad_source_change())
        self._btn_pad_clear = ttk.Button(pad_ctrl, text="Clear", state="disabled",
                                         command=self._clear_custom_pads)
        self._btn_pad_clear.pack(side="left", padx=2)
        self._btn_pad_add_die = ttk.Button(pad_ctrl, text="Add Die", state="disabled",
                                           command=self._add_custom_die)
        self._btn_pad_add_die.pack(side="left", padx=2)

        self._on_pad_source_change()

    def _on_pad_source_change(self):
        source = self._pad_source_var.get()
        if source == "Custom":
            if not self._pad_custom_loaded and self._ata_folder:
                self.pad_panel.load_custom(self._ata_folder)
                self._pad_custom_loaded = True
            self.pad_panel.set_source("custom")
            self._btn_pad_clear.config(state="normal")
            self._btn_pad_add_die.config(state="normal")
            self._refresh_pad_tree_from_custom()
        else:
            self.pad_panel.set_source("ata")
            self._btn_pad_clear.config(state="disabled")
            self._btn_pad_add_die.config(state="disabled")
            self._populate_pad_tree_from_ata(self.pad_panel._last_pads or [])

    @staticmethod
    def _fmt_um(v):
        try:
            return str(round(float(v)))
        except (TypeError, ValueError):
            return v if v is not None else ""

    def _refresh_pad_tree_from_custom(self):
        for item in self._pad_tree.get_children():
            self._pad_tree.delete(item)
        for pad in self.pad_panel._custom_pads:
            self._pad_tree.insert("", "end", values=(
                pad["name"], self._fmt_um(pad["x"]), self._fmt_um(pad["y"])))

    def _populate_pad_tree_from_ata(self, pads: list):
        for item in self._pad_tree.get_children():
            self._pad_tree.delete(item)
        if not pads:
            return 0
        sample = pads[0]
        n_key = next((k for k in ("pad_name", "name", "label", "pad") if k in sample), None)
        x_key = next((k for k in ("x_um", "x_mm", "x", "center_x") if k in sample), None)
        y_key = next((k for k in ("y_um", "y_mm", "y", "center_y") if k in sample), None)
        for p in pads:
            self._pad_tree.insert("", "end", values=(
                p.get(n_key, "") if n_key else "",
                self._fmt_um(p.get(x_key)) if x_key else "",
                self._fmt_um(p.get(y_key)) if y_key else "",
            ))
        return len(pads)

    def _clear_custom_pads(self):
        from tkinter import messagebox
        if not messagebox.askyesno("Clear Custom Layout",
                                   "Delete every pad and die in the hand-drawn custom layout?"):
            return
        self.pad_panel.clear_custom()

    def _add_custom_die(self):
        self.pad_panel.add_die()

    def _save_custom_pads(self, quiet: bool = False):
        if not self._ata_folder:
            if not quiet:
                from tkinter import messagebox
                messagebox.showerror("No ATA Folder", "Load an ATA folder from the toolbar first.")
            return
        path = self.pad_panel.save_custom(self._ata_folder)
        self._pad_custom_loaded = True
        self.controller.log("[PROBE CARD] Custom layout saved")

    def _exec_on_card_picked(self):
        name = self._exec_card_var.get()
        if not hasattr(self, "pin_wiring"):
            return
        if name != self.pin_wiring.get_active_card():
            self.pin_wiring.switch_to_card(name)

    def _on_probe_card_change(self, card_name: str):
        if hasattr(self, "_exec_card_var"):
            self._exec_card_cb.config(values=[""] + sorted(self.pin_wiring.get_card_names()))
            self._exec_card_var.set(card_name)
        if not hasattr(self, "recipe_panel"):
            return
        self.recipe_panel.load_recipes(card_name, self.pin_wiring.get_recipes())
        self.recipe_panel.refresh_connections()
        if getattr(self, "_exec_steps", None):
            self._exec_steps = []
            self._exec_steps_tree.delete(*self._exec_steps_tree.get_children())
            self._exec_steps_var.set("No recipe loaded")
            self._exec_recipe_var.set("")
            self.controller.log(
                "[RUN] Probe card changed — refresh")
        if hasattr(self.controller, "check_system_ready"):
            self.controller.check_system_ready()

    def load_pad_layout(self, folder_path):
        pads = self.pad_panel.load_from_ata(folder_path)
        return self._populate_pad_tree_from_ata(pads)

    def _tab_gds_parser(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="GDS Parser")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.gds_panel = GdsParserPanel(tab, controller=self.controller)
        self.gds_panel.grid(row=0, column=0, sticky="nsew")

    def _tab_recipe(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Recipe")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.recipe_panel = RecipePanel(
            tab, controller=self.controller, system=self._system,
            get_pins=lambda: (self.pin_wiring.get_pin_choices()
                              if hasattr(self, "pin_wiring") else []),
            get_wiring=lambda: (self.pin_wiring.get_wiring()
                                if hasattr(self, "pin_wiring") else []),
            get_active_card=lambda: (self.pin_wiring.get_active_card()
                                     if hasattr(self, "pin_wiring") else ""),
            save_recipes=lambda card, recipes: (
                self.pin_wiring.save_recipes(card, recipes)
                if hasattr(self, "pin_wiring") else False),
            switch_card=lambda name: (self.pin_wiring.switch_to_card(name)
                                      if hasattr(self, "pin_wiring") else None),
            get_card_names=lambda: (self.pin_wiring.get_card_names()
                                    if hasattr(self, "pin_wiring") else []),
            get_ata_folder=lambda: self._ata_folder,
            get_die_pins=lambda: (self.pin_wiring.get_die_pins()
                                  if hasattr(self, "pin_wiring") else {}),
            get_wafer_map_names=lambda: (self.recipe_gen.list_map_names()
                                         if hasattr(self, "recipe_gen") else []),
            on_save=self._exec_load_recipe_by_name)
        self.recipe_panel.grid(row=0, column=0, sticky="nsew")

    def _tab_switch_settings(self, nb):
        tab = ttk.Frame(nb)
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        if self._system == "accretech":
            nb.add(tab, text="Switch Settings")
            self.switch_settings = SwitchSettingsPanel(tab, controller=self.controller)
            self.switch_settings.grid(row=0, column=0, sticky="nsew")
        else:
            nb.add(tab, text="Switch Settings")
            self.switch_debug = SwitchboxTestPanel(tab, controller=self.controller)
            self.switch_debug.grid(row=0, column=0, sticky="nsew")

    def _tab_instruments_eg(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Instruments")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.instruments_eg = InstrumentsEgPanel(tab, controller=self.controller)
        self.instruments_eg.grid(row=0, column=0, sticky="nsew")

    def _tab_setup(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Setup")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        if self._system == "accretech":
            self.setup_panel = AccretechSetupPanel(
                tab, controller=self.controller, main_layout=self)
        else:
            self.setup_panel = EgSetupPanel(
                tab, controller=self.controller, main_layout=self)
        self.setup_panel.grid(row=0, column=0, sticky="nsew")

    def _tab_probe_routing(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Switch Routing")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        holder, self.probe_routing = scrollable_routing(tab, self.controller)
        holder.grid(row=0, column=0, sticky="nsew")

    def _tab_prober_debug(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Prober Debug")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        if self._system == "accretech":
            self.prober_debug = ProberDebugPanel(tab, controller=self.controller)
        else:
            self.prober_debug = EgProberDebugPanel(tab, controller=self.controller)
        self.prober_debug.grid(row=0, column=0, sticky="nsew")

    def _tab_gpib_trace(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="GPIB Trace")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.gpib_trace_panel = GpibTracePanel(tab, controller=self.controller)
        self.gpib_trace_panel.grid(row=0, column=0, sticky="nsew")

    def _tab_nanoz_switch(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Nanoz Switch")
        tab.columnconfigure(0, weight=1)

        lf = ttk.LabelFrame(tab, text="GUI Mode", padding=10)
        lf.grid(row=0, column=0, sticky="new", padx=8, pady=8)
        lf.columnconfigure(0, weight=1)

        ttk.Label(
            lf, wraplength=460, justify="left",
            text=("NanoZ mode replaces this entire window's main section "
                  "with the NanoZ (Nautilus 1x20-shot) workspace, on "
                  "whichever system is active. Debug tabs are unaffected - "
                  "switching back is available from NanoZ mode's own "
                  "header, or by returning here.")
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        self._nanoz_switch_state_var = tk.StringVar()
        ttk.Label(lf, textvariable=self._nanoz_switch_state_var,
                  font=("Consolas", 9), foreground="#1d4ed8"
                  ).grid(row=1, column=0, sticky="w", pady=(0, 8))

        btns = ttk.Frame(lf)
        btns.grid(row=2, column=0, sticky="w")
        ttk.Button(btns, text="Switch to NanoZ",
                  command=lambda: self.controller.cmd_set_gui_mode("nanoz")
                  ).pack(side="left")
        ttk.Button(btns, text="Switch to Normal",
                  command=lambda: self.controller.cmd_set_gui_mode("normal")
                  ).pack(side="left", padx=(6, 0))

        default_row = ttk.Frame(lf)
        default_row.grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Label(default_row, text="Startup default:").pack(side="left")
        ttk.Button(default_row, text="Set NanoZ as Default",
                  command=lambda: self.controller.cmd_set_default_gui_mode("nanoz")
                  ).pack(side="left", padx=(6, 0))
        ttk.Button(default_row, text="Set Normal as Default",
                  command=lambda: self.controller.cmd_set_default_gui_mode("normal")
                  ).pack(side="left", padx=(6, 0))

        self._refresh_nanoz_switch_state()

    def _refresh_nanoz_switch_state(self):
        var = getattr(self, "_nanoz_switch_state_var", None)
        if var is None:
            return
        current = getattr(self.controller, "gui_mode", "normal")
        try:
            default = app_settings.get_default_gui_mode()
        except Exception:
            default = "normal"
        var.set(f"Current: {current}   |   Default at startup: {default}")

    def _tab_cassette(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Cassette")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.cassette_panel = CassettePanel(tab, controller=self.controller, ui=self)
        self.cassette_panel.grid(row=0, column=0, sticky="nsew")

    def _tab_pma_wafer(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Wafer Builder")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)

        self.recipe_gen = RecipeGenPanel(tab, controller=self.controller,
                                         main_layout=self, system="accretech")
        self.recipe_gen.grid(row=0, column=0, sticky="nsew")

        accr_tab = ttk.Frame(self.recipe_gen._sub_nb)
        accr_tab.rowconfigure(0, weight=1)
        accr_tab.columnconfigure(0, weight=1)
        self.accr_wafer = AccrWaferPanel(accr_tab, controller=self.controller,
                                         get_folder=lambda: self._ata_folder)
        self.accr_wafer.grid(row=0, column=0, sticky="nsew")
        self.recipe_gen._sub_nb.insert(0, accr_tab, text="Accr Wafer")

        overlay_tab = ttk.Frame(self.recipe_gen._sub_nb)
        self._exec_build_overlay_tab(overlay_tab)
        self.recipe_gen._sub_nb.add(overlay_tab, text="Overlay")
        self._exec_overlay_tab_widget = overlay_tab
        self.recipe_gen._sub_nb.bind(
            "<<NotebookTabChanged>>", self._exec_on_wafer_builder_subtab_changed, add="+")

        hidden = ttk.Frame(tab)
        self.pma_wafer = PmaWaferPanel(
            hidden, controller=self.controller, get_folder=lambda: self._ata_folder,
            main_layout=self)
        self.pma_wafer.grid(row=0, column=0, sticky="nsew")

    def _tab_pma_process(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="PMA Process")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.pma_process = PmaProcessPanel(tab, controller=self.controller, main_layout=self)
        self.pma_process.grid(row=0, column=0, sticky="nsew")

    def _tab_recipe_gen(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Wafer Builder")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)

        self.recipe_gen = RecipeGenPanel(tab, controller=self.controller,
                                         main_layout=self)
        self.recipe_gen.grid(row=0, column=0, sticky="nsew")

        hidden = ttk.Frame(tab)
        self.pma_wafer = PmaWaferPanel(
            hidden, controller=self.controller, get_folder=lambda: self._ata_folder,
            main_layout=self)
        self.pma_wafer.grid(row=0, column=0, sticky="nsew")

    def _tab_execution2(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="▶  Run")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)

        self._exec_running  = False
        self._exec_aborted  = False
        self._exec_run_mode = None
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
        self._exec_wafer_map_cb = ttk.Combobox(
            ctrl, textvariable=self._exec_wafer_map_var, width=14, state="readonly",
            postcommand=lambda: self._exec_wafer_map_cb.config(
                values=(self.recipe_gen.list_map_names()
                       if hasattr(self, "recipe_gen") else [])))
        self._exec_wafer_map_cb.pack(side="left", pady=6)
        self._exec_wafer_map_cb.bind(
            "<<ComboboxSelected>>", lambda _e: self._exec_on_wafer_map_picked())

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
        pos_row.rowconfigure(0, weight=1)

        pos_lf = ttk.LabelFrame(pos_row, text="Chuck Position", padding=6)
        pos_lf.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
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

        self._exec_first_die_btn = ttk.Button(
            pos_lf, text="◀ First Die", command=self._exec_manual_go_to_start)
        self._exec_first_die_btn.grid(
                   row=4, column=0, columnspan=2, sticky="ew", pady=1)
        self._exec_zup_btn = ttk.Button(
            pos_lf, text="↑ Z Up", command=self._exec_manual_z_up)
        self._exec_zup_btn.grid(
                   row=5, column=0, sticky="ew", padx=(0, 1), pady=1)
        self._exec_zdown_btn = ttk.Button(
            pos_lf, text="↓ Z Down", command=self._exec_manual_z_down)
        self._exec_zdown_btn.grid(
                   row=5, column=1, sticky="ew", padx=(1, 0), pady=1)
        if self._system == "electroglas":
            self._exec_back_btn = ttk.Button(
                pos_lf, text="◀ Back", command=lambda: self.eg_pma_run._step_back())
            self._exec_back_btn.grid(
                       row=6, column=0, sticky="ew", padx=(0, 1), pady=1)
            self._exec_next_btn = ttk.Button(
                pos_lf, text="▶ Next", command=lambda: self.eg_pma_run._step_once())
            self._exec_next_btn.grid(
                       row=6, column=1, sticky="ew", padx=(1, 0), pady=1)
            self.eg_pma_run._goto_btn = ttk.Button(
                pos_lf, text="→ Move to Selected",
                command=self.eg_pma_run.toggle_move_armed)
            self.eg_pma_run._goto_btn.grid(
                row=7, column=0, columnspan=2, sticky="ew", pady=1)
        else:
            self._exec_back_btn = ttk.Button(
                pos_lf, text="◀ Back", command=self._exec_manual_prev_die)
            self._exec_back_btn.grid(
                       row=6, column=0, sticky="ew", padx=(0, 1), pady=1)
            self._exec_next_btn = ttk.Button(
                pos_lf, text="▶ Next", command=self._exec_manual_next_die)
            self._exec_next_btn.grid(
                       row=6, column=1, sticky="ew", padx=(1, 0), pady=1)
            self._exec_prev_shot_btn = ttk.Button(
                pos_lf, text="◀◀ Previous Shot", command=self._exec_manual_prev_shot)
            self._exec_prev_shot_btn.grid(
                       row=7, column=0, sticky="ew", padx=(0, 1), pady=1)
            self._exec_next_shot_btn = ttk.Button(
                pos_lf, text="▶▶ Next Shot", command=self._exec_manual_next_shot)
            self._exec_next_shot_btn.grid(
                       row=7, column=1, sticky="ew", padx=(1, 0), pady=1)
            self._exec_move_selected_btn = ttk.Button(
                pos_lf, text="→ Move to Selected",
                command=self._exec_move_selected_button)
            self._exec_move_selected_btn.grid(
                row=8, column=0, columnspan=2, sticky="ew", pady=1)
            self._exec_refresh_xy_btn = ttk.Button(
                pos_lf, text="↻ Refresh XY", command=self._exec_get_xy)
            self._exec_refresh_xy_btn.grid(
                row=9, column=0, columnspan=2, sticky="ew", pady=1)

        steps_lf = ttk.LabelFrame(left_col, text="Recipe Steps", padding=(6, 4))
        steps_lf.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
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

        ttk.Separator(map_bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self._exec_select_all_btn = ttk.Button(
            map_bar, text="☑ Select All", command=self._exec_toggle_select_all)
        self._exec_select_all_btn.pack(side="left", padx=(6, 0))

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
        stat_lf.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
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

        ttk.Button(stat_lf, text="Reset Counts", command=self._exec_reset_counts).pack(
            fill="x", pady=(8, 0))


    def _exec_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"{ts}  {msg}"
        try:
            self.controller.log(line)
        except (RuntimeError, tk.TclError):
            print(line)

    def _exec_minor_moves_active(self) -> bool:
        rp = getattr(self, "recipe_panel", None)
        return bool(rp and hasattr(rp, "is_minor_moves") and rp.is_minor_moves())

    def _exec_draw_wafer_map(self, quiet_if_missing: bool = False):
        folder = self._exec_map_folder
        filename = WAFER_MAP_SOURCES[self._exec_map_source_var.get()]
        gen = getattr(self, "recipe_gen", None)
        pitch = gen._die_pitch() if gen is not None and hasattr(gen, "_die_pitch") else (1.0, 1.0)
        n = self._exec_wafer_map.load_from_ata(folder, filename=filename, pitch=pitch)
        run_dbg = self._exec_wafer_map.last_draw_debug or {}
        if run_dbg.get("warning"):
            self._exec_log(f"[ERROR] Run wafer map: {run_dbg['warning']}")
        self._exec_wafer_map.clear_picks()
        name = os.path.basename(folder)
        self._exec_map_path_var.set(
            f"{name}  ({n} dies)" if n else f"{name} — {filename} not found")
        if n or not quiet_if_missing:
            self._exec_log(f"[RUN] Wafer map loaded from '{name}/{filename}' — {n} dies")
        self._exec_adopt_map_die_ids()
        self._sync_results_wafer_map()
        if self._system == "accretech":
            self._exec_load_selected_map(quiet_if_missing=True)
        else:
            self._exec_seed_die_list_from_map()

    def _exec_seed_die_list_from_map(self):
        run = getattr(self, "eg_pma_run", None)
        adopt = getattr(run, "adopt_from_wafer_builder", None)
        if adopt is None:
            return
        try:
            adopt(quiet=True)
        except Exception as e:
            self._exec_log(f"[RUN] Could not build the Die list from the map — "
                           f"{type(e).__name__}: {e}")

    def _exec_rebuild_run_map(self):
        old = self._exec_wafer_map
        new = rebuild_wafer_map_panel(old)
        self._exec_wafer_map = new
        new.on_reset_request = self._exec_rebuild_run_map
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Double-Button-1>"):
            new.canvas.bind(
                seq, lambda _e: self._exec_update_overlay_visibility(), add="+")
        self._exec_redraw_overlay_on_run_map()
        dbg = new.last_draw_debug or {}
        self._exec_log("[RUN] Run map view reset")
        if dbg.get("warning"):
            self._exec_log(f"[ERROR] Run wafer map (reset): {dbg['warning']}")

    def _exec_adopt_map_die_ids(self):
        wm = self._exec_wafer_map
        ids = {rc: text for rc, text in (wm.die_ids or {}).items() if text}
        if not ids:
            return
        if self._exec_overlay_die_ids and self._system == "accretech":
            return
        self._exec_clear_overlay_labels(wm, self._exec_overlay_items)
        self._exec_overlay_die_ids = ids
        self._exec_redraw_overlay_on_run_map()

    def _new_results_wafer_map(self):
        old = getattr(self, "_results_wafer_map", None)
        wm = WaferMapPanel(self._results_map_frame)
        wm.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=(0, 8))
        wm.canvas.bind("<Button-1>", self._on_results_map_click, add="+")
        wm.on_redraw = self._exec_redraw_overlay_on_results_map
        wm.on_zoom = self._exec_debounced(
            "_exec_results_zoom_debounce_id", self._exec_redraw_overlay_on_results_map)
        wm.on_reset_request = self._exec_rebuild_results_map_view
        self._results_wafer_map = wm
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        return wm

    def _exec_rebuild_results_map_view(self):
        old = self._results_wafer_map
        if old is None:
            return
        new = rebuild_wafer_map_panel(old)
        self._results_wafer_map = new
        new.on_reset_request = self._exec_rebuild_results_map_view
        new.canvas.bind("<Button-1>", self._on_results_map_click, add="+")
        dbg = new.last_draw_debug or {}
        self._exec_log("[RUN] Results map view reset")
        if dbg.get("warning"):
            self._exec_log(f"[ERROR] Results wafer map (reset): {dbg['warning']}")

    def _sync_results_wafer_map(self):
        rwm = getattr(self, "_results_wafer_map", None)
        if rwm is None:
            return
        dies = self._exec_wafer_map._last_dies
        if dies:
            rwm = self._new_results_wafer_map()
            rwm._last_dies = dies
            rwm._draw_from_die_list(dies)
            dbg = rwm.last_draw_debug or {}
            if dbg.get("warning"):
                self._exec_log(f"[ERROR] Results wafer map: {dbg['warning']}")
        else:
            rwm.canvas.delete("all")
            rwm.dies.clear()
            rwm.canvas.create_text(150, 100, text="No wafer map loaded yet.", fill="gray")
            rwm._run_on_redraw()

    def _exec_set_state(self, text: str, color: str):
        self._exec_state_lbl.config(text=text, fg=color)

    def _exec_open_all_channels(self):
        switch = self.controller.drivers.get("switch")
        if switch is None or not getattr(switch, "inst", None):
            return
        try:
            if hasattr(switch, "open_all"):
                switch.open_all()
            elif hasattr(switch, "open_crosspoint"):
                switch.open_channel("allslots")
            else:
                switch.open_channel("allslots")
            self._exec_log("[RUN] All switch channels opened.")
        except Exception as e:
            self._exec_log(f"[RUN] Could not open all channels: "
                            f"{type(e).__name__}: {e}")

    def _exec_set_running_buttons(self, running: bool):
        state = "disabled" if running else "normal"
        for attr in ("_exec_full_btn", "_exec_test_btn",
                    "_exec_test_selected_btn", "_exec_run_btn",
                    "_exec_measure_btn", "_exec_first_die_btn",
                    "_exec_zup_btn", "_exec_zdown_btn",
                    "_exec_back_btn", "_exec_next_btn",
                    "_exec_prev_shot_btn", "_exec_next_shot_btn",
                    "_exec_move_selected_btn", "_exec_refresh_xy_btn",
                    "_exec_unload_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                try:
                    btn.config(state=state)
                except tk.TclError:
                    pass
        eg_run = getattr(self, "eg_pma_run", None)
        goto_btn = getattr(eg_run, "_goto_btn", None) if eg_run is not None else None
        if goto_btn is not None:
            try:
                goto_btn.config(state=state)
            except tk.TclError:
                pass
        stop_state = "normal" if running else "disabled"
        for attr in ("_exec_stop_btn", "_exec_pause_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                try:
                    btn.config(state=stop_state)
                except tk.TclError:
                    pass
        try:
            self.recipe_panel.set_locked(running)
        except Exception:
            pass
        try:
            self.controller.set_run_lock(running)
        except Exception:
            pass

    def _exec_pause(self):
        eg_run = getattr(self, "eg_pma_run", None)
        if eg_run is not None and getattr(eg_run, "_running", False):
            try:
                eg_run._pause()
                self._exec_set_running_buttons(False)
                self._exec_log("[RUN] Pause")
                return
            except Exception as e:
                self._exec_log(f"[RUN] Could not pause the .PMA run: {e}")
        if not self._exec_running:
            self._exec_log("[RUN] Nothing to pause.")
            return
        self._exec_running = False
        self._exec_set_running_buttons(False)
        self._exec_set_state("PAUSED", "#b45309")
        self._exec_log("[RUN] Paused after the current die — position kept.")

    def _exec_abort(self):
        if self._exec_aborted:
            self._exec_log("[RUN] Stop Run: already stopped — ignoring.")
            return
        eg_run = getattr(self, "eg_pma_run", None)
        eg_was_running = bool(getattr(eg_run, "_running", False))
        if eg_run is not None:
            try:
                eg_run._stop()
            except Exception as e:
                self._exec_log(f"[RUN] Could not stop the .PMA step-through: {e}")
        self._exec_running = False
        self._exec_aborted = True
        self._exec_set_running_buttons(False)
        if not eg_was_running:
            self._exec_open_all_channels()
            prober_z = self.controller.drivers.get("prober")
            if prober_z is not None and getattr(prober_z, "inst", None):
                try:
                    prober_z.z_down()
                    self._exec_log("[RUN] Chuck separated (Z down).")
                except Exception as e:
                    self._exec_log(f"[RUN] Could not separate the chuck: "
                                    f"{type(e).__name__}: {e}")
        self._exec_run_token += 1
        self.after(0, lambda: self._exec_wafer_map.enable_picking(
            on_change=self._exec_on_sites_changed))
        self.after(0, lambda: self._exec_set_state(
            "STOPPING…" if eg_was_running else "STOPPED", "#dc2626"))
        self._exec_log("[RUN] Stop and reset")
        prober = self.controller.drivers.get("prober")
        if prober and prober.inst and self._system == "accretech":
            def _clear_buzzer():
                try:
                    prober.send_es()
                    self._exec_log("[RUN] es sent (buzzer clear)")
                except Exception as e:
                    self._exec_log(f"[RUN] es error: {e}")
            threading.Thread(target=_clear_buzzer, daemon=True).start()

    def _exec_safe_after(self, fn):
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _exec_finish_run(self, token: int, msg: str, color: str):
        if token != self._exec_run_token:
            return
        finished_mode = self._exec_run_mode
        self._exec_running  = False
        self._exec_run_mode = None
        self._exec_safe_after(lambda: self._exec_set_running_buttons(False))
        self._exec_safe_after(lambda: self._exec_wafer_map.enable_picking(
            on_change=self._exec_on_sites_changed))
        if not self._exec_aborted:
            self._exec_safe_after(lambda: self._exec_set_state(msg, color))
        if self._exec_on_run_finished:
            total = self._exec_total_dies
            aborted = self._exec_aborted
            hook = self._exec_on_run_finished
            def _call_hook():
                hook(self._exec_pass_var.get(), self._exec_fail_var.get(),
                    total, aborted, finished_mode)
            self._exec_safe_after(_call_hook)

    def _exec_ensure_separated(self, prober, stb: int):
        if stb != 67:
            return
        self._exec_log("[RUN] finished chuck UP (STB=67 — contact) >> D  (Separate)")
        prober.z_down()

    def _exec_zup_measure_zdown(self, prober, die_label: str,
                                 steps: list = None, row: int = None, col: int = None,
                                 shot_geom=None) -> bool:
        try:
            self._exec_log("[RUN] >> Z  (Contact)")
            stb = prober.z_up()
            if stb == 67:
                self._exec_log("[RUN] << STB=67  (Z Up confirmed — CONTACT)")
            else:
                self._exec_log(f"[RUN] Z Up returned STB={stb} (expected 67)")
        except Exception as e:
            self._exec_log(f"[RUN] Touchdown error: {e} — measuring anyway")

        if shot_geom is not None and row is not None and col is not None:
            self._exec_publish_die_slots_at(shot_geom, row, col)

        try:
            ok = self._exec_run_steps_once(steps)
        finally:
            ids_by_slot = list(getattr(self, "_exec_die_ids_by_slot", None) or [])
            rc_by_slot = list(getattr(self, "_exec_die_rc_by_slot", None) or [])
            if shot_geom is not None:
                self._exec_die_rc_by_slot = []
                self._exec_die_ids_by_slot = []
                self._exec_die_shotpos_by_slot = []
        self._exec_safe_after(lambda p=ok, dl=die_label: self._exec_log(
            f"[RESULTS] {'PASS' if p else 'FAIL'}  {dl}"))

        if row is not None and col is not None:
            self._exec_color_shot_squares(rc_by_slot, row, col, ok)
        self._exec_tally_shot_result(shot_geom, ids_by_slot, ok)

        if self._exec_aborted:
            self._exec_log("[RUN] Skipping Separate (D) — already sent by Stop Run.")
            return ok

        z_down_confirmed = True
        try:
            self._exec_log("[RUN] >> D  (Separate)")
            stb = prober.z_down()
            if stb == 68:
                self._exec_log("[RUN] << STB=68  (Z Down confirmed — separated)")
            else:
                self._exec_log(f"[RUN] Z Down returned STB={stb} (expected 68)")
                z_down_confirmed = False
        except Exception as e:
            self._exec_log(f"[RUN] Separate error: {e}")
            z_down_confirmed = False

        if not z_down_confirmed:
            self._exec_log("[RUN] Aborting/rejected")
            self._exec_abort()
        else:
            self._exec_maybe_read_state()
        return ok

    def _exec_update_die_color(self, row: int, col: int, ok: bool):
        status = "PASS" if ok else "FAIL"
        try:
            self.controller.die_status[(row, col)] = status
        except Exception:
            pass
        try:
            if (row, col) in self._exec_wafer_map.dies:
                self._exec_wafer_map.update_die(row, col, status)
        except Exception:
            pass
        rwm = getattr(self, "_results_wafer_map", None)
        if rwm is not None:
            try:
                if (row, col) in rwm.dies:
                    rwm.update_die(row, col, status)
            except Exception:
                pass


    def _exec_switch_panels(self):
        panels = []
        probe_routing = getattr(self, "probe_routing", None)
        if probe_routing is not None:
            panels.append(probe_routing)
        switch_debug = getattr(self, "switch_debug", None)
        if switch_debug is not None and hasattr(switch_debug, "mark_closed"):
            panels.append(switch_debug)
        bottom = getattr(self.controller, "bottom_routing", None)
        if bottom is not None:
            panels.append(bottom)
        return panels

    def _exec_mark_closed(self, channels):
        for ch in channels:
            for p in self._exec_switch_panels():
                self.after(0, lambda p=p, ch=ch: p.mark_closed(ch))

    def _exec_mark_open(self, channels):
        for ch in channels:
            for p in self._exec_switch_panels():
                self.after(0, lambda p=p, ch=ch: p.mark_open(ch))

    def _exec_mark_all_open(self):
        for p in self._exec_switch_panels():
            self.after(0, p.mark_all_open)

    def _exec_maybe_read_state(self):
        if self._exec_die_num % 5:
            return
        for p in self._exec_switch_panels():
            self.after(0, p.read_state)


    def _exec_wafer_map_matches_recipe(self) -> bool:
        wanted = self.recipe_panel.get_wafer_map()
        if not wanted:
            return True
        gen = getattr(self, "recipe_gen", None)
        active = gen.map_name_var.get().strip() if gen is not None else ""
        if wanted == active:
            return True
        self._exec_log(
            f"[RUN] Blocked — this recipe wants wafer map '{wanted}', but "
            f"'{active or '(none)'}' is active. Pick '{wanted}' from the "
            "Wafer Map: dropdown first.")
        return False

    def _exec_can_start(self) -> bool:
        ok = True
        if not self._exec_wafer_map_matches_recipe():
            ok = False
        if self._exec_lot_thread and self._exec_lot_thread.is_alive():
            self._exec_log("[RUN] Cannot start — the previous run is still finishing")
            ok = False
        if not self._exec_steps:
            self._exec_log("[RUN] Cannot start — no recipe loaded "
                            "(pick one from the Recipe dropdown first).")
            ok = False
        if self._system == "accretech":
            if (self._exec_map_source_var.get() not in ("Accretech", "Wafer Builder")
                    or not self._exec_wafer_map._last_dies):
                self._exec_log("[RUN] Cannot start — no wafer map loaded")
                ok = False
        elif not self._exec_wafer_map._last_dies:
            self._exec_log("[RUN] Cannot start — no wafer map loaded")
            ok = False
        if self._system == "accretech":
            try:
                required_instruments = self.controller.accretech_required_drivers()
            except Exception:
                required_instruments = ("prober", "smu", "dmm", "switch", "wave_gen")
        else:
            required_instruments = ("prober", "smu", "relay1")
        missing_instruments = [k for k in required_instruments if k not in self.controller.drivers]
        if missing_instruments:
            self._exec_log("[RUN] Cannot start — instrument(s) not connected: "
                            f"{', '.join(missing_instruments)} (see the Instruments tab).")
            ok = False
        return ok

    def _exec_start_full_die(self):
        if self._exec_running:
            self._exec_log("[RUN] A run is already active.")
            return
        if not self._exec_can_start():
            return
        if self._system == "accretech" and self._exec_minor_moves_active():
            self._exec_log("[RUN] Full Die: this recipe has Minor Moves on — "
                            "use Run instead.")
            return
        self._exec_start_full_die_walk("Full Die")

    def _exec_start_full_die_walk(self, mode_label: str):
        if self._system == "electroglas":
            self._exec_log(
                f"[RUN] {mode_label}: the native whole-wafer walk (G/J) is "
                "Accretech-only - it assumes a status-byte protocol and "
                "onboard wafer map the Electroglas does not have. Use the "
                "PMA Run tab for Electroglas recipes instead.")
            return
        self._exec_reset_counts(total_dies=len(self._exec_wafer_map._last_dies or []))
        self._exec_running  = True
        self._exec_set_running_buttons(True)
        self._exec_aborted  = False
        self._exec_run_mode = "full"
        self._exec_run_token += 1
        my_token = self._exec_run_token
        self._exec_wafer_map.enable_picking(0)
        self.after(0, lambda: self._exec_set_state(f"RUNNING ({mode_label})", "#2563eb"))
        self._exec_log(f"[RUN] {mode_label} — walking the entire wafer (G/J), "
                        "measuring the loaded recipe at every die.")
        shot_geom = self._exec_prepare_shot_geometry()
        self._exec_lot_thread = threading.Thread(
            target=self._exec_full_die_thread, args=(my_token, shot_geom), daemon=True)
        self._exec_lot_thread.start()

    def _exec_full_die_thread(self, my_token: int, shot_geom=None):
        prober = self.controller.drivers.get("prober")
        if not (prober and prober.inst):
            self._exec_log("[RUN] ERROR: prober not connected")
            self._exec_finish_run(my_token, "ERROR: prober not connected", "#dc2626")
            return
        error_msg = None
        try:
            self._exec_refresh_xy_blocking(prober)
            self._exec_log("[RUN] >> D  (Separate)")
            prober.z_down()

            self._exec_log("[RUN] >> G  (Position start die)")
            stb = prober.move_to_start_die()
            self._exec_log(f"[RUN] << STB={stb}")
            self._exec_ensure_separated(prober, stb)

            while (self._exec_running and not self._exec_aborted
                   and self._exec_run_token == my_token):
                raw = prober.get_xy_position()
                x, y = _parse_q_response(raw)
                self._exec_die_num += 1
                die_label = f"Die #{self._exec_die_num}  (X{x:.0f} Y{y:.0f})"
                self.after(0, lambda d=die_label: self._exec_die_var.set(f"Die: {d}"))
                self.after(0, lambda x=x, y=y:
                           self._exec_xy_var.set(f"X: {x:.0f} die\nY: {y:.0f} die"))
                self._exec_highlight_current(int(y), int(x))
                self._exec_log(f"[RUN] << Q  die X={x:.0f} Y={y:.0f}")

                ok = self._exec_zup_measure_zdown(
                    prober, die_label, row=int(y), col=int(x), shot_geom=shot_geom)

                if (not self._exec_running or self._exec_aborted
                        or self._exec_run_token != my_token):
                    break

                self._exec_log("[RUN] >> J  (Next die)")
                stb = prober.next_die()
                if stb == 81:
                    self._exec_log("[RUN] << STB=81  (wafer end)")
                    break
                if stb == 90:
                    self._exec_log("[RUN] << STB=90  (probing stop — <STOP> pushed)")
                    break
                self._exec_log(f"[RUN] << STB={stb}")
                self._exec_ensure_separated(prober, stb)
        except Exception as e:
            error_msg = str(e)
            self._exec_log(f"[RUN] ERROR: {e}")
        finally:
            if error_msg:
                self._exec_finish_run(my_token, f"ERROR: {error_msg[:60]}", "#dc2626")
            else:
                self._exec_finish_run(my_token, "FINISHED (Full Die)", "#16a34a")

    def _exec_start_minor_moves(self, shots: list, mode_label: str):
        if not self._exec_overlay_offset_confirmed:
            self._exec_log("[RUN] Minor Moves: no Wafer Builder > Overlay")
            return
        overlay_offset = (self._exec_overlay_row_offset, self._exec_overlay_col_offset)
        gen = getattr(self, "recipe_gen", None)
        if gen is None:
            self._exec_log("[RUN] Minor Moves: the Wafer Builder tab is not available.")
            return
        shot_rows, shot_cols = gen._shot_dims()
        shot_cells = dict(gen._shot_cells)

        row_off, col_off = self._exec_overlay_row_offset, self._exec_overlay_col_offset
        seen_shots = {}
        deduped = []
        for row, col in shots:
            key = ((row - row_off) // shot_rows, (col - col_off) // shot_cols)
            if key in seen_shots:
                continue
            seen_shots[key] = (row, col)
            deduped.append((row, col))
        if len(deduped) != len(shots):
            self._exec_log(f"[RUN] Minor Moves: {len(shots)} touchdown(s) resolved to "
                            f"{len(deduped)} distinct shot(s)")
        shots = deduped

        self._exec_reset_counts(total_dies=len(shots))
        self._exec_running  = True
        self._exec_set_running_buttons(True)
        self._exec_aborted  = False
        self._exec_run_mode = {"Full Die": "full", "Test Die": "test",
                                "Run": "run"}.get(mode_label, "run")
        self._exec_run_token += 1
        my_token = self._exec_run_token
        self._exec_wafer_map.enable_picking(0)
        self.after(0, lambda: self._exec_set_state(
            f"RUNNING (Minor Moves — {mode_label})", "#2563eb"))
        self._exec_log(f"[RUN] {mode_label} (Minor Moves) — {len(shots)} shot(s), "
                        "visiting only the die(s) the recipe references.")
        self._exec_lot_thread = threading.Thread(
            target=self._exec_minor_move_thread,
            args=(shots, my_token, overlay_offset, shot_rows, shot_cols, shot_cells),
            daemon=True)
        self._exec_lot_thread.start()

    def _exec_publish_die_slots_for(self, shot_row, shot_col, shot_rows, shot_cols,
                                     shot_cells, row_offset, col_offset):
        present = present_slots(shot_cells, shot_rows, shot_cols)
        max_die = max(present.values()) if present else 1
        rcs, ids, shotpos = [], [], []
        wm = self._exec_wafer_map
        for die_num in range(1, max_die + 1):
            rc = shot_die_rc(shot_cells, shot_rows, shot_cols, die_num)
            if rc is None:
                rcs.append(None)
                ids.append("")
                shotpos.append(None)
                continue
            r, c = rc
            real_row = shot_row * shot_rows + r + row_offset
            real_col = shot_col * shot_cols + c + col_offset
            rcs.append((real_row, real_col))
            ids.append(self._exec_overlay_die_ids.get((real_row, real_col))
                      or wm.die_ids.get((real_row, real_col), ""))
            shotpos.append((shot_row, shot_col, r, c))
        self._exec_die_rc_by_slot = rcs
        self._exec_die_ids_by_slot = ids
        self._exec_die_shotpos_by_slot = shotpos

    def _exec_publish_die_slots_anchored(self, row: int, col: int, shot_rows, shot_cols,
                                          shot_cells, row_offset, col_offset):
        die1_rc = shot_die_rc(shot_cells, shot_rows, shot_cols, 1)
        if die1_rc is None:
            self._exec_die_rc_by_slot = []
            self._exec_die_ids_by_slot = []
            self._exec_die_shotpos_by_slot = []
            return
        r1, c1 = die1_rc
        shot_row = (row - row_offset) // shot_rows
        shot_col = (col - col_offset) // shot_cols
        present = present_slots(shot_cells, shot_rows, shot_cols)
        max_die = max(present.values()) if present else 1
        rcs, ids, shotpos = [], [], []
        wm = self._exec_wafer_map
        for die_num in range(1, max_die + 1):
            rc = shot_die_rc(shot_cells, shot_rows, shot_cols, die_num)
            if rc is None:
                rcs.append(None)
                ids.append("")
                shotpos.append(None)
                continue
            r, c = rc
            real_row = row + (r - r1)
            real_col = col + (c - c1)
            rcs.append((real_row, real_col))
            ids.append(self._exec_overlay_die_ids.get((real_row, real_col))
                      or wm.die_ids.get((real_row, real_col), ""))
            shotpos.append((shot_row, shot_col, r, c))
        self._exec_die_rc_by_slot = rcs
        self._exec_die_ids_by_slot = ids
        self._exec_die_shotpos_by_slot = shotpos

    def _exec_prepare_shot_geometry(self):
        gen = getattr(self, "recipe_gen", None)
        if gen is None:
            return None
        shot_rows, shot_cols = gen._shot_dims()
        if shot_rows * shot_cols <= 1:
            return None
        if not self._exec_overlay_offset_confirmed:
            self._exec_log("[RUN] This shot template has more than one die, but "
                            "there is no confirmed Overlay alignment")
            return None
        shot_cells = dict(gen._shot_cells)
        return (shot_rows, shot_cols, shot_cells,
                self._exec_overlay_row_offset, self._exec_overlay_col_offset)

    def _exec_publish_die_slots_at(self, shot_geom, row: int, col: int) -> tuple:
        shot_rows, shot_cols, shot_cells, row_offset, col_offset = shot_geom
        shot_row = (row - row_offset) // shot_rows
        shot_col = (col - col_offset) // shot_cols
        if self._exec_minor_moves_active():
            self._exec_publish_die_slots_for(
                shot_row, shot_col, shot_rows, shot_cols, shot_cells,
                row_offset, col_offset)
        else:
            self._exec_publish_die_slots_anchored(
                row, col, shot_rows, shot_cols, shot_cells,
                row_offset, col_offset)
        return shot_row, shot_col

    def _exec_color_shot_squares(self, rc_by_slot: list,
                                  fallback_row: int, fallback_col: int, fallback_ok: bool):
        slot_verdicts = dict(getattr(self, "_exec_slot_verdicts", None) or {})
        if rc_by_slot and slot_verdicts:
            for die_num, passed in sorted(slot_verdicts.items()):
                idx = die_num - 1
                if not (0 <= idx < len(rc_by_slot)) or rc_by_slot[idx] is None:
                    continue
                real_row, real_col = rc_by_slot[idx]
                self._exec_update_die_color(real_row, real_col, passed)
        else:
            self._exec_update_die_color(fallback_row, fallback_col, fallback_ok)

    def _exec_tally_shot_result(self, shot_geom, ids_by_slot: list, fallback_ok: bool):
        slot_verdicts = dict(getattr(self, "_exec_slot_verdicts", None) or {})
        if shot_geom is not None and slot_verdicts:
            counted = 0
            for die_num, passed in sorted(slot_verdicts.items()):
                counted += 1
                self._exec_safe_after(
                    self._exec_add_pass if passed else self._exec_add_fail)
            if counted:
                return
        self._exec_safe_after(
            self._exec_add_pass if fallback_ok else self._exec_add_fail)

    def _exec_minor_move_thread(self, shots: list, my_token: int, overlay_offset: tuple,
                                 shot_rows: int, shot_cols: int, shot_cells: dict):
        prober = self.controller.drivers.get("prober")
        if not (prober and prober.inst):
            self._exec_log("[RUN] ERROR: prober not connected")
            self._exec_finish_run(my_token, "ERROR: prober not connected", "#dc2626")
            return
        error_msg = None
        row_offset, col_offset = overlay_offset

        class _Stop(Exception):
            pass

        def shot_rc_for(pick_row, pick_col):
            wb_row = pick_row - row_offset
            wb_col = pick_col - col_offset
            return wb_row // shot_rows, wb_col // shot_cols

        def publish_die_slots(shot_row, shot_col):
            self._exec_publish_die_slots_for(
                shot_row, shot_col, shot_rows, shot_cols, shot_cells,
                row_offset, col_offset)

        def goto_shot_die(pick_row, pick_col, die_num):
            shot_row, shot_col = shot_rc_for(pick_row, pick_col)
            rc = shot_die_rc(shot_cells, shot_rows, shot_cols, die_num)
            if rc is None:
                raise RuntimeError(f"die #{die_num} is not on shot "
                                   f"R{shot_row}C{shot_col}")
            r, c = rc
            die_x = shot_col * shot_cols + c + col_offset
            die_y = shot_row * shot_rows + r + row_offset
            die_label = (f"shot R{shot_row}C{shot_col} die #{die_num} "
                        f"(X{die_x:.0f} Y{die_y:.0f})")
            self._exec_safe_after(lambda d=die_label: self._exec_die_var.set(f"Die: {d}"))
            self._exec_safe_after(
                lambda x=die_x, y=die_y:
                self._exec_xy_var.set(f"X: {x:.0f} die\nY: {y:.0f} die"))
            self._exec_die_num += 1

            self._exec_log(f"[RUN] >> D  (Separate before move)")
            prober.z_down()

            self._exec_log(f"[RUN] >> J  (Position die X={die_x:.0f} Y={die_y:.0f}, "
                            f"die #{die_num})")
            stb = prober.move_to_die_xy(die_x, die_y)
            self._exec_log(f"[RUN] << STB={stb}")
            if stb == 81:
                self._exec_log("[RUN] << (wafer end)")
                self._exec_running = False
                raise _Stop()
            if stb == 90:
                self._exec_log("[RUN] << (probing stop — <STOP> pushed)")
                self._exec_running = False
                raise _Stop()
            self._exec_ensure_separated(prober, stb)

            self._exec_log("[RUN] >> Z  (Contact)")
            stb = prober.z_up()
            if stb != 67:
                self._exec_log(f"[RUN] Z Up returned STB={stb} (expected 67)")

        try:
            self._exec_refresh_xy_blocking(prober)
            for land_row, land_col in shots:
                if (not self._exec_running or self._exec_aborted
                        or self._exec_run_token != my_token):
                    break
                self._exec_safe_after(
                    lambda r=land_row, c=land_col: self._exec_highlight_current(r, c))
                self._exec_log(f"[RUN] Shot at picked R{land_row}C{land_col}: landing on die #1")
                try:
                    goto_shot_die(land_row, land_col, 1)
                except _Stop:
                    break

                shot_row, shot_col = shot_rc_for(land_row, land_col)
                publish_die_slots(shot_row, shot_col)
                self._exec_move_fn = (
                    lambda die_num, lr=land_row, lc=land_col: goto_shot_die(lr, lc, die_num))
                try:
                    shot_ok = self._exec_run_steps_once()
                finally:
                    self._exec_move_fn = None
                    self._exec_die_rc_by_slot = []
                    self._exec_die_ids_by_slot = []
                    self._exec_die_shotpos_by_slot = []

                self._exec_log("[RUN] >> D  (Separate)")
                prober.z_down()

                slot_verdicts = dict(getattr(self, "_exec_slot_verdicts", None) or {})
                if slot_verdicts:
                    for die_num, passed in sorted(slot_verdicts.items()):
                        rc = shot_die_rc(shot_cells, shot_rows, shot_cols, die_num)
                        if rc is None:
                            continue
                        r, c = rc
                        real_row = shot_row * shot_rows + r + row_offset
                        real_col = shot_col * shot_cols + c + col_offset
                        self._exec_safe_after(
                            self._exec_add_pass if passed else self._exec_add_fail)
                        self._exec_update_die_color(real_row, real_col, passed)
                else:
                    self._exec_safe_after(
                        self._exec_add_pass if shot_ok else self._exec_add_fail)
                    self._exec_update_die_color(land_row, land_col, shot_ok)
        except Exception as e:
            error_msg = str(e)
            self._exec_log(f"[RUN] ERROR: {e}")
        finally:
            self._exec_move_fn = None
            if error_msg:
                self._exec_finish_run(my_token, f"ERROR: {error_msg[:60]}", "#dc2626")
            else:
                self._exec_finish_run(my_token, "FINISHED (Minor Moves)", "#16a34a")

    def _exec_on_sites_changed(self, picks):
        self._exec_sites_var.set(self._exec_sites_label(picks))
        btn = getattr(self, "_exec_select_all_btn", None)
        dies = self._exec_wafer_map._last_dies
        if btn and dies:
            all_rc = {(d["row"], d["col"]) for d in dies}
            is_all = bool(all_rc) and set(picks) == all_rc
            btn.config(text="☐ Deselect All" if is_all else "☑ Select All")

    def _exec_sites_label(self, picks) -> str:
        n = len(picks)
        if n != 1:
            return f"Test sites: {n} picked"
        rc = tuple(picks[0])
        ids = ((self._exec_overlay_die_ids or {}).get(rc, "")
               or self._exec_wafer_map.die_ids.get(rc, ""))
        where = f"Test site: 1 picked — R{rc[0]}C{rc[1]}"
        if not ids:
            return f"{where} (no die ID on this square)"
        devices = [d for d in ids.split("/") if d.strip()]
        if len(devices) < 2:
            return f"{where}:  {ids}"
        return f"{where}, touchdown of {len(devices)} devices:  {ids}"

    def _exec_randomize_sites(self):
        dies = self._exec_wafer_map._last_dies
        if not dies:
            self._exec_log("[RUN] No wafer map loaded.")
            return
        import random
        pool = [(d["row"], d["col"]) for d in dies]
        picks = random.sample(pool, min(5, len(pool)))
        self._exec_wafer_map.set_picked(picks)
        self._exec_on_sites_changed(picks)
        self._exec_log("[RUN] Randomized test sites: "
                        + ", ".join(f"R{r}C{c}" for r, c in picks))

    def _exec_toggle_select_all(self):
        dies = self._exec_wafer_map._last_dies
        if not dies:
            self._exec_log("[RUN] No wafer map loaded — load one before selecting dies.")
            return
        all_rc = [(d["row"], d["col"]) for d in dies]
        already_all = set(self._exec_wafer_map.get_picked()) == set(all_rc)
        if already_all:
            self._exec_wafer_map.set_picked([])
            self._exec_on_sites_changed([])
            self._exec_log("[RUN] Deselected all dies.")
        else:
            self._exec_wafer_map.set_picked(all_rc)
            self._exec_on_sites_changed(all_rc)
            self._exec_log(f"[RUN] Selected all {len(all_rc)} die(s).")

    def _exec_picks_as_touchdowns(self, picks) -> list:
        picks = [(int(r), int(c)) for r, c in picks]
        run = getattr(self, "eg_pma_run", None)
        seq_at_rc = getattr(run, "_seq_at_rc", None) or {}
        anchor_rc = getattr(run, "_anchor_rc", None) or {}
        if self._system != "electroglas" or not seq_at_rc:
            return picks
        out, seen = [], set()
        for rc in picks:
            seq = seq_at_rc.get(rc)
            if seq is None:
                if rc not in seen:
                    seen.add(rc)
                    out.append(rc)
                continue
            if seq in seen:
                continue
            seen.add(seq)
            out.append(anchor_rc.get(seq, rc))
        return out

    def _exec_touchdown_cells(self, picks) -> list:
        picks = [(int(r), int(c)) for r, c in picks]
        run = getattr(self, "eg_pma_run", None)
        seq_at_rc = getattr(run, "_seq_at_rc", None) or {}
        cells = getattr(run, "_cells", None) or {}
        if self._system != "electroglas" or not seq_at_rc:
            return picks
        out, seen = [], set()
        for rc in picks:
            seq = seq_at_rc.get(rc)
            for cell in (cells.get(seq) or [rc]):
                if cell not in seen:
                    seen.add(cell)
                    out.append(cell)
        return out

    def _exec_map_die_id_lookup(self) -> dict:
        counts, single = {}, {}
        for rc, label in (self._exec_wafer_map.die_ids or {}).items():
            if not label:
                continue
            counts[label] = counts.get(label, 0) + 1
            single[label] = rc
        return {label: rc for label, rc in single.items() if counts[label] == 1}

    def _exec_resolve_site_cells(self, sites) -> list:
        if self._system != "electroglas":
            return [(s["row"], s["col"]) for s in sites]
        wm_ids = self._exec_wafer_map.die_ids or {}
        unique_id_to_rc = self._exec_map_die_id_lookup()
        resolved, unmatched = [], 0
        for s in sites:
            die_id = (s.get("die_id") or "").strip()
            own_rc = (s["row"], s["col"])
            if die_id and wm_ids.get(own_rc) == die_id:
                rc = own_rc
            else:
                rc = unique_id_to_rc.get(die_id) if die_id else None
                if rc is None and "/" in die_id:
                    for part in (p.strip() for p in die_id.split("/")):
                        if not part:
                            continue
                        rc = unique_id_to_rc.get(part)
                        if rc is not None:
                            break
                if rc is None:
                    rc = own_rc
                    if die_id:
                        unmatched += 1
            resolved.append(rc)
        outcome = (unmatched, len(sites))
        if unmatched and outcome != getattr(self, "_exec_resolve_mismatch_last", None):
            self._exec_resolve_mismatch_last = outcome
            self._exec_log(
                f"[RUN] {unmatched} of {len(sites)} touchdown(s) named a die ID "
                "that doesn't match the map at its own (row, col)")
        return resolved

    def _exec_preselect_align_die(self):
        run = getattr(self, "eg_pma_run", None)
        refill = getattr(run, "_fill_anchor_choices", None)
        if refill is None or getattr(run, "_anchored", False):
            return
        try:
            refill()
        except Exception as e:
            self._exec_log(f"[RUN] Could not apply the recipe's align die — "
                           f"{type(e).__name__}: {e}")

    def _exec_load_and_publish_wafer_map(self, name: str) -> bool:
        gen = getattr(self, "recipe_gen", None)
        if gen is None or not hasattr(gen, "_load_named_map"):
            return False
        try:
            gen._load_named_map(name)
            gen._sync_views(self._ata_folder)
        except Exception as e:
            self._exec_log(f"[RUN] Could not load wafer map '{name}' — "
                           f"{type(e).__name__}: {e}")
            return False
        self._exec_reapply_overlay()
        return True

    def _exec_on_wafer_map_picked(self):
        name = self._exec_wafer_map_var.get().strip()
        if not name:
            return
        gen = getattr(self, "recipe_gen", None)
        active = gen.map_name_var.get().strip() if gen is not None else ""
        if name == active:
            self._exec_log(f"[RUN] '{name}' is already the active wafer map.")
            return
        if self._exec_load_and_publish_wafer_map(name):
            self._exec_log(f"[RUN] Switched to wafer map '{name}' "
                           f"(was '{active or '(none)'}').")

    def _exec_autoload_recipe_wafer_map(self):
        gen = getattr(self, "recipe_gen", None)
        if gen is None or not hasattr(gen, "_load_named_map"):
            return
        wanted = self.recipe_panel.get_wafer_map()
        if not wanted:
            return
        if hasattr(self, "_exec_wafer_map_var"):
            self._exec_wafer_map_var.set(wanted)
        active = gen.map_name_var.get().strip()
        if wanted == active:
            return
        if wanted not in gen.list_map_names():
            self._exec_log(f"[RUN] This recipe wants wafer map '{wanted}', "
                           "which no longer exists — pick a new one from "
                           "its Wafer Map: dropdown.")
            return
        if not self._exec_load_and_publish_wafer_map(wanted):
            return
        self._exec_log(f"[RUN] Switched to '{wanted}' — this recipe's own "
                       f"wafer map (was '{active or '(none)'}').")

    def _exec_loaded_recipe_name(self) -> str:
        if not getattr(self, "_exec_steps", None):
            return ""
        try:
            return self.recipe_panel.get_active_recipe() or ""
        except Exception:
            return ""

    def _exec_load_selected_map(self, quiet_if_missing: bool = False):
        recipe = self._exec_loaded_recipe_name()
        if not recipe:
            return []
        get_records = getattr(self.recipe_panel, "get_site_records", None)
        sites = list(get_records()) if get_records else []
        if not sites:
            if not quiet_if_missing:
                self._exec_log(
                    f"[RUN] Recipe '{recipe}' has no touchdown list yet — click "
                    "dies on the map, then Recipe tab's Take from map selection.")
            return []
        resolved = self._exec_resolve_site_cells(sites)
        picks = list(resolved)
        ids = {rc: s["die_id"] for rc, s in zip(resolved, sites) if s.get("die_id")}
        picks = [rc for rc in self._exec_touchdown_cells(picks)
                 if rc in self._exec_wafer_map.dies] or picks
        self._exec_wafer_map.set_picked(picks)
        self._exec_on_sites_changed(picks)
        if ids and self._system == "accretech":
            self._exec_overlay_die_ids = {**(self._exec_overlay_die_ids or {}), **ids}
            self._exec_redraw_overlay_on_run_map()
            self._exec_redraw_overlay_on_results_map()
        self._exec_log(f"[RUN] Loaded {len(picks)} touchdown(s) from "
                        f"recipe '{recipe}'.")
        return picks

    def _exec_start_test_selected(self):
        if self._exec_running:
            self._exec_log("[RUN] A run is already active.")
            return
        sites = self._exec_wafer_map.get_picked()
        if not sites:
            sites = self._exec_load_selected_map(quiet_if_missing=True)
        if not sites:
            self._exec_log("[RUN] Test Selected: no dies selected")
            return
        self._exec_log(f"[RUN] Test Selected — {len(sites)} selected die(s): "
                        + ", ".join(f"R{r}C{c}" for r, c in sites))
        self._exec_start_test_die()

    def _exec_start_run(self):
        if self._exec_running:
            self._exec_log("[RUN] A run is already active.")
            return
        if not self._exec_can_start():
            return
        sites = self.recipe_panel.get_sites()
        if self._system == "accretech" and self._exec_minor_moves_active():
            if not sites:
                sites = list(self._exec_wafer_map.dies.keys())
            if not sites:
                self._exec_log("[RUN] Run: Minor Moves is on but there is no "
                                "wafer map loaded")
                return
            self._exec_start_minor_moves(sites, "Run")
            return
        if sites:
            self._exec_start_site_list(sites, "Run", "run")
            return
        if not (self._exec_wafer_map._last_dies or []):
            self._exec_log("[RUN] Run: no saved touchdowns on this recipe and "
                            "no wafer map loaded.")
            return
        self._exec_log("[RUN] Run — no saved touchdowns on this recipe, "
                        "walking the whole wafer map instead.")
        self._exec_start_full_die_walk("Run")

    def _exec_wafer_builder_grid(self) -> list:
        gen = getattr(self, "recipe_gen", None)
        if gen is None:
            return []
        try:
            dpx, dpy = gen._die_pitch()
        except Exception:
            return []
        if not dpx or not dpy:
            return []
        out = []
        for d in gen._die_positions():
            if d["status"] != "normal" or not d["die_id"]:
                continue
            out.append({"row": round(d["y"] / dpy), "col": round(d["x"] / dpx),
                       "die_ids": [d["die_id"]], "raw_text": d["die_id"]})
        return out

    def _exec_overlay_accretech_rc(self):
        return set(self._exec_wafer_map.dies.keys())

    def _exec_wafer_builder_footprint(self) -> set:
        gen = getattr(self, "recipe_gen", None)
        if gen is None:
            return set()
        try:
            dpx, dpy = gen._die_pitch()
        except Exception:
            return set()
        if not dpx or not dpy:
            return set()
        out = set()
        for d in gen._die_positions():
            if d["status"] != "normal":
                continue
            out.add((round(d["y"] / dpy), round(d["x"] / dpx)))
        return out

    @staticmethod
    def _exec_overlay_all_accretech(grid: list, accretech_rc, row_offset: int,
                                     col_offset: int, footprint: "set | None" = None) -> list:
        by_rc: dict = {}
        for p in grid:
            rc = (p["row"] + row_offset, p["col"] + col_offset)
            by_rc.setdefault(rc, []).extend(p["die_ids"])
        if footprint:
            selected = sorted(rc for rc in accretech_rc
                              if (rc[0] - row_offset, rc[1] - col_offset) in footprint)
        else:
            selected = sorted(accretech_rc)
        return [{"row": r, "col": c, "die_ids": by_rc.get((r, c), []), "raw_text": ""}
               for r, c in selected]

    _EXEC2_OVERLAY_MIN_DIE_PX = 22

    def _exec_update_overlay_visibility(self):
        if not self._exec_overlay_items:
            return
        wm = self._exec_wafer_map
        bbox = None
        for rc in self._exec_overlay_die_ids:
            item = wm.dies.get(rc)
            if item is None:
                continue
            bbox = wm.canvas.bbox(item)
            if bbox:
                break
        if not bbox:
            for it in self._exec_overlay_items:
                try:
                    wm.canvas.itemconfigure(it, state="normal")
                except tk.TclError:
                    pass
            return
        width_px = bbox[2] - bbox[0]
        state = "normal" if width_px >= self._EXEC2_OVERLAY_MIN_DIE_PX else "hidden"
        for it in self._exec_overlay_items:
            try:
                wm.canvas.itemconfigure(it, state=state)
            except tk.TclError:
                pass

    def _exec_debounced(self, pending_attr: str, fn, delay_ms: int = 60):
        def _schedule():
            existing = getattr(self, pending_attr, None)
            if existing is not None:
                try:
                    self.after_cancel(existing)
                except Exception:
                    pass

            def _fire():
                setattr(self, pending_attr, None)
                fn()
            setattr(self, pending_attr, self.after(delay_ms, _fire))
        return _schedule

    def _exec_redraw_overlay_on_run_map(self):
        self._exec_clear_overlay_labels(self._exec_wafer_map,
                                         self._exec_overlay_items)
        if self._exec_overlay_die_ids:
            self._exec_overlay_items = self._exec_draw_overlay_labels_on(
                self._exec_wafer_map, self._exec_overlay_die_ids)
        else:
            self._exec_overlay_items = []
        self._exec_update_overlay_visibility()
        redraw_window = getattr(getattr(self, "eg_pma_run", None),
                                "update_shot_window", None)
        if redraw_window:
            try:
                redraw_window()
            except Exception:
                pass
        if self._system == "accretech":
            self._exec_update_shot_window()

    def _exec_redraw_overlay_on_results_map(self):
        rwm = getattr(self, "_results_wafer_map", None)
        if rwm is None:
            return
        self._exec_clear_overlay_labels(rwm, self._exec_overlay_result_items)
        if not self._exec_overlay_die_ids:
            self._exec_overlay_result_items = []
            return
        self._exec_overlay_result_items = self._exec_draw_overlay_labels_on(
            rwm, self._exec_overlay_die_ids)

    def _exec_clear_overlay_labels(self, wm, items: list):
        for item in items:
            try:
                wm.canvas.delete(item)
            except tk.TclError:
                pass
        items.clear()

    def _exec_clear_overlay(self):
        self._exec_clear_overlay_labels(self._exec_wafer_map, self._exec_overlay_items)
        rwm = getattr(self, "_results_wafer_map", None)
        if rwm is not None:
            self._exec_clear_overlay_labels(rwm, self._exec_overlay_result_items)
        self._exec_overlay_die_ids = {}

    def _exec_persist_overlay_offset(self):
        gen = getattr(self, "recipe_gen", None)
        folder = getattr(self, "_exec_map_folder", None) or getattr(self, "_ata_folder", None)
        if gen is None or not folder:
            return
        name_var = getattr(gen, "map_name_var", None)
        if name_var is None or not name_var.get().strip():
            return
        try:
            gen._autosave_named_map_quiet(folder)
        except Exception as e:
            self._exec_log(f"[RUN] Could not save Overlay alignment: "
                            f"{type(e).__name__}: {e}")

    def _exec_reapply_overlay(self):
        if self._system != "accretech":
            return
        if not self._exec_overlay_offset_confirmed:
            return
        accretech_rc = self._exec_overlay_accretech_rc()
        if not accretech_rc:
            return
        grid = self._exec_wafer_builder_grid()
        footprint = self._exec_wafer_builder_footprint()
        matched = self._exec_overlay_all_accretech(
            grid, accretech_rc, self._exec_overlay_row_offset,
            self._exec_overlay_col_offset, footprint)
        self._exec_draw_overlay(matched)
        self._exec_log(
            f"[RUN] Overlay restored from the saved map ({len(matched)} die(s), "
            f"row {self._exec_overlay_row_offset:+d}, col {self._exec_overlay_col_offset:+d}).")

    _OVERLAY_FONT = ("Consolas", 7)

    def _exec_overlay_font(self):
        if getattr(self, "_overlay_font_obj", None) is None:
            self._overlay_font_obj = tkfont.Font(family=self._OVERLAY_FONT[0],
                                                 size=self._OVERLAY_FONT[1])
        return self._overlay_font_obj

    def _exec_label_min_px(self) -> float:
        try:
            return float(self._exec_label_min_px_var.get())
        except (tk.TclError, ValueError):
            return 22.0

    def _exec_labels_fit(self, wm, die_ids_by_rc: dict) -> bool:
        box_w, box_h = wm.die_box_px()
        if box_w <= 0:
            return False
        longest = max(die_ids_by_rc.values(), key=len, default="")
        font = self._exec_overlay_font()
        return (box_w >= font.measure(longest) + 3
                and box_w >= self._exec_label_min_px()
                and box_h >= font.metrics("linespace"))

    def _exec_draw_overlay_labels_on(self, wm, die_ids_by_rc: dict) -> list:
        if not self._exec_labels_fit(wm, die_ids_by_rc):
            return []
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
                cx, cy, text=label_text, font=self._OVERLAY_FONT, fill="#1e293b"))
        return items

    def _exec_draw_overlay(self, matched: list):
        self._exec_clear_overlay()
        self._exec_overlay_die_ids = {(d["row"], d["col"]): "/".join(d["die_ids"])
                                       for d in matched if d["die_ids"]}
        self._exec_overlay_items = self._exec_draw_overlay_labels_on(
            self._exec_wafer_map, self._exec_overlay_die_ids)
        rwm = getattr(self, "_results_wafer_map", None)
        if rwm is not None:
            self._exec_overlay_result_items = self._exec_draw_overlay_labels_on(
                rwm, self._exec_overlay_die_ids)
        picks = [(d["row"], d["col"]) for d in matched]
        self._exec_wafer_map.set_picked(picks)
        self._exec_on_sites_changed(picks)
        self._exec_update_overlay_visibility()


    def _exec_build_overlay_tab(self, parent):
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        self._exec_overlay_preview_items: list = []
        self._exec_overlay_tab_matched: list = []

        bar = ttk.Frame(parent, padding=8)
        bar.grid(row=0, column=0, sticky="ew")

        self._exec_overlay_summary_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._exec_overlay_summary_var,
                 font=("Consolas", 9), justify="left").grid(
                 row=0, column=0, columnspan=6, sticky="w", pady=(0, 6))

        ttk.Label(bar, text="Row offset:").grid(row=1, column=0, sticky="e")
        self._exec_overlay_row_var = tk.IntVar(value=0)
        ttk.Spinbox(bar, from_=-9999, to=9999, width=6,
                   textvariable=self._exec_overlay_row_var).grid(
                   row=1, column=1, sticky="w", padx=(4, 16))
        ttk.Label(bar, text="Col offset:").grid(row=1, column=2, sticky="e")
        self._exec_overlay_col_var = tk.IntVar(value=0)
        ttk.Spinbox(bar, from_=-9999, to=9999, width=6,
                   textvariable=self._exec_overlay_col_var).grid(
                   row=1, column=3, sticky="w", padx=(4, 16))
        self._exec_overlay_row_var.trace_add("write", self._exec_overlay_tab_recompute)
        self._exec_overlay_col_var.trace_add("write", self._exec_overlay_tab_recompute)

        self._exec_overlay_status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._exec_overlay_status_var,
                 foreground="#6b7280", font=("Segoe UI", 8, "italic")).grid(
                 row=1, column=4, sticky="w")

        btns = ttk.Frame(parent, padding=(8, 0, 8, 8))
        btns.grid(row=2, column=0, sticky="ew")
        ttk.Button(btns, text="Auto-Center", command=self._exec_overlay_tab_center).pack(
            side="left")
        ttk.Button(btns, text="Overlay on Map",
                  command=self._exec_overlay_tab_confirm).pack(side="left", padx=6)
        ttk.Button(btns, text="Clear Overlay",
                  command=self._exec_overlay_tab_clear).pack(side="left")

        map_lf = ttk.LabelFrame(parent, text="Accretech map", padding=6)
        map_lf.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        map_lf.rowconfigure(0, weight=1)
        map_lf.columnconfigure(0, weight=1)
        self._exec_overlay_map = WaferMapPanel(map_lf, show_title=False, show_axis_grid=True)
        self._exec_overlay_map.grid(row=0, column=0, sticky="nsew")
        self._exec_overlay_map.on_zoom = self._exec_debounced(
            "_exec_overlay_zoom_debounce_id", self._exec_overlay_tab_recompute)
        self._exec_overlay_map.on_redraw = self._exec_overlay_tab_recompute

    def _exec_overlay_tab_recompute(self, *_a):
        accretech_rc = self._exec_overlay_accretech_rc()
        grid = self._exec_wafer_builder_grid()
        footprint = self._exec_wafer_builder_footprint()
        try:
            ro = self._exec_overlay_row_var.get()
            co = self._exec_overlay_col_var.get()
        except tk.TclError:
            return
        matched = self._exec_overlay_all_accretech(grid, accretech_rc, ro, co, footprint)
        self._exec_overlay_tab_matched = matched
        n_with_id = sum(1 for m in matched if m["die_ids"])
        self._exec_overlay_summary_var.set(
            f"Accretech dies on map:   {len(accretech_rc)}\n"
            f"Wafer Builder footprint: {len(footprint)}  (named: {len(grid)})\n"
            f"Will select: {len(matched)}  ({n_with_id} labeled with a real ID)"
            + ("" if footprint else
               "\n\nNo Wafer Builder map loaded - selecting the Accretech "
               "map's own squares with no ID labels."))
        wm = self._exec_overlay_map
        ids = {(d["row"], d["col"]): "/".join(d["die_ids"]) for d in matched if d["die_ids"]}
        self._exec_clear_overlay_labels(wm, self._exec_overlay_preview_items)
        self._exec_overlay_preview_items = self._exec_draw_overlay_labels_on(wm, ids)
        wm.set_picked([(d["row"], d["col"]) for d in matched])

    def _exec_overlay_tab_center(self):
        grid = self._exec_wafer_builder_grid()
        accretech_rc = self._exec_overlay_accretech_rc()
        if not grid or not accretech_rc:
            self._exec_overlay_tab_recompute()
            return
        ro, co = centroid_offset(grid, accretech_rc)
        self._exec_overlay_row_var.set(ro)
        self._exec_overlay_col_var.set(co)

    def _exec_overlay_tab_confirm(self):
        if not self._exec_overlay_accretech_rc():
            self._exec_log("[RUN] Overlay: no wafer map loaded")
            return
        self._exec_overlay_tab_recompute()
        matched = self._exec_overlay_tab_matched
        self._exec_overlay_row_offset = self._exec_overlay_row_var.get()
        self._exec_overlay_col_offset = self._exec_overlay_col_var.get()
        self._exec_overlay_offset_confirmed = True
        self._exec_draw_overlay(matched)
        self._exec_persist_overlay_offset()
        self._exec_overlay_tab_status_update()
        self._exec_log(f"[RUN] Overlaid {len(matched)} die(s) from the "
                        "Wafer Builder map onto the wafer map.")

    def _exec_overlay_tab_clear(self):
        self._exec_clear_overlay()
        self._exec_wafer_map.clear_picks()
        self._exec_on_sites_changed([])
        self._exec_overlay_offset_confirmed = False
        self._exec_persist_overlay_offset()
        wm = getattr(self, "_exec_overlay_map", None)
        if wm is not None:
            self._exec_clear_overlay_labels(wm, self._exec_overlay_preview_items)
            wm.clear_picks()
        self._exec_overlay_tab_status_update()
        self._exec_log("[RUN] Overlay cleared.")

    def _exec_overlay_tab_status_update(self):
        var = getattr(self, "_exec_overlay_status_var", None)
        if var is None:
            return
        if self._exec_overlay_offset_confirmed:
            var.set(f"✅ confirmed (row {self._exec_overlay_row_offset:+d}, "
                    f"col {self._exec_overlay_col_offset:+d})")
        else:
            var.set("⚠ not yet confirmed")

    def _exec_on_wafer_builder_subtab_changed(self, _event=None):
        widget = getattr(self, "_exec_overlay_tab_widget", None)
        if widget is None:
            return
        try:
            current = self.recipe_gen._sub_nb.select()
        except tk.TclError:
            return
        if current == str(widget):
            self._exec_overlay_tab_shown()

    def _exec_overlay_tab_shown(self):
        wm = getattr(self, "_exec_overlay_map", None)
        folder = getattr(self, "_exec_map_folder", None) or getattr(self, "_ata_folder", None)
        if wm is None or not folder:
            return
        gen = getattr(self, "recipe_gen", None)
        pitch = gen._die_pitch() if gen is not None and hasattr(gen, "_die_pitch") else (1.0, 1.0)
        wm.load_from_ata(folder, filename=WAFER_MAP_SOURCES["Accretech"], pitch=pitch)
        if self._exec_overlay_offset_confirmed:
            self._exec_overlay_row_var.set(self._exec_overlay_row_offset)
            self._exec_overlay_col_var.set(self._exec_overlay_col_offset)
            self._exec_overlay_tab_recompute()
        else:
            self._exec_overlay_tab_center()
        self._exec_overlay_tab_status_update()

    def _exec_start_test_die(self):
        if self._exec_running:
            self._exec_log("[RUN] A run is already active.")
            return
        if not self._exec_can_start():
            return
        sites = self._exec_wafer_map.get_picked()
        if not sites:
            self._exec_randomize_sites()
            sites = self._exec_wafer_map.get_picked()
        if not sites:
            self._exec_log("[RUN] No dies available to pick test sites from.")
            return
        if self._system == "accretech" and self._exec_minor_moves_active():
            self._exec_log("[RUN] Test Die: this recipe has Minor Moves on — "
                            "use Run instead.")
            return
        self._exec_start_site_list(sites, "Test Die", "test")

    def _exec_start_site_list(self, sites: list, mode_label: str, run_mode: str):
        if run_mode == "test":
            self._exec_last_test_sites = list(sites)
        self._exec_reset_counts(total_dies=len(sites))
        self._exec_running  = True
        self._exec_set_running_buttons(True)
        self._exec_aborted  = False
        self._exec_run_mode = run_mode
        self._exec_run_token += 1
        my_token = self._exec_run_token
        self._exec_wafer_map.enable_picking(0)
        self.after(0, lambda: self._exec_set_state(f"RUNNING ({mode_label})", "#2563eb"))
        self._exec_log(f"[RUN] {mode_label} — {len(sites)} site(s): "
                        + ", ".join(f"R{r}C{c}" for r, c in sites))
        shot_geom = self._exec_prepare_shot_geometry()
        self._exec_lot_thread = threading.Thread(
            target=self._exec_test_die_thread,
            args=(sites, my_token, shot_geom), daemon=True)
        self._exec_lot_thread.start()

    def _exec_test_die_thread(self, sites, my_token: int, shot_geom=None):
        prober = self.controller.drivers.get("prober")
        if not (prober and prober.inst):
            self._exec_log("[RUN] ERROR: prober not connected")
            self._exec_finish_run(my_token, "ERROR: prober not connected", "#dc2626")
            return
        error_msg = None
        try:
            self._exec_refresh_xy_blocking(prober)
            self._exec_log("[RUN] >> D  (Separate)")
            prober.z_down()

            row, col = sites[0]
            self._exec_log(f"[RUN] >> J  (Position die X={col} Y={row})")
            stb = prober.move_to_die_xy(col, row)
            if stb == 81:
                self._exec_log("[RUN] << STB=81  (wafer end)")
                return
            if stb == 90:
                self._exec_log("[RUN] << STB=90  (probing stop — <STOP> pushed)")
                return
            self._exec_log(f"[RUN] << STB={stb}")
            self._exec_ensure_separated(prober, stb)

            idx = 0
            while (self._exec_running and not self._exec_aborted
                   and self._exec_run_token == my_token and idx < len(sites)):
                row, col = sites[idx]
                die_label = f"R{row}C{col}  (X{col} Y{row})"
                self.after(0, lambda d=die_label: self._exec_die_var.set(f"Die: {d}"))
                self.after(0, lambda x=col, y=row:
                           self._exec_xy_var.set(f"X: {x} die\nY: {y} die"))
                self._exec_highlight_current(row, col)
                self._exec_die_num += 1

                ok = self._exec_zup_measure_zdown(
                    prober, die_label, row=row, col=col, shot_geom=shot_geom)

                idx += 1
                if (not self._exec_running or self._exec_aborted
                        or self._exec_run_token != my_token or idx >= len(sites)):
                    break

                row, col = sites[idx]
                self._exec_log(f"[RUN] >> J  (Position die X={col} Y={row})")
                stb = prober.move_to_die_xy(col, row)
                if stb == 81:
                    self._exec_log("[RUN] << STB=81  (wafer end)")
                    break
                if stb == 90:
                    self._exec_log("[RUN] << STB=90  (probing stop — <STOP> pushed)")
                    break
                self._exec_log(f"[RUN] << STB={stb}")
                self._exec_ensure_separated(prober, stb)
        except Exception as e:
            error_msg = str(e)
            self._exec_log(f"[RUN] ERROR: {e}")
        finally:
            if error_msg:
                self._exec_finish_run(my_token, f"ERROR: {error_msg[:60]}", "#dc2626")
            else:
                self._exec_finish_run(my_token, "FINISHED (Test Die)", "#16a34a")


    def _exec_autoload_default_recipe(self, folder_path):
        card, name = load_default_recipe(folder_path, system=self._system)
        if not card or not name:
            return
        if not hasattr(self, "recipe_panel") or not hasattr(self, "_exec_recipe_var"):
            return
        if self.pin_wiring.get_active_card() != card:
            valid_cards = self.pin_wiring.get_card_names_for_system()
            if card not in valid_cards:
                self._exec_log(f"[RUN] Default recipe '{name}' wants probe card "
                                f"'{card}', which doesn't exist or isn't wired for "
                                f"this bench — skipping autoload.")
                return
            self.pin_wiring.switch_to_card(card)
        if name not in self.recipe_panel.get_recipe_names():
            self._exec_log(f"[RUN] Default recipe '{name}' not found on probe card "
                            f"'{card}' — skipping autoload.")
            return
        self._exec_recipe_var.set(name)
        self._exec_load_recipe()
        self._exec_log(f"[RUN] Auto-loaded default recipe '{name}' (probe card '{card}').")

    def _exec_sync_wafer_map_on_folder_load(self):
        gen = getattr(self, "recipe_gen", None)
        if gen is None or not hasattr(gen, "list_map_names"):
            return
        active = gen.map_name_var.get().strip()
        if not active:
            names = gen.list_map_names()
            if not names:
                return
            first = names[0]
            if self._exec_load_and_publish_wafer_map(first):
                self._exec_log(f"[RUN] No default wafer map for this folder — "
                               f"loaded '{first}' (the first saved map).")
                active = first
        if hasattr(self, "_exec_wafer_map_var"):
            self._exec_wafer_map_var.set(active)

    def _exec_apply_recipe_sites(self, name: str):
        get_records = getattr(self.recipe_panel, "get_site_records", None)
        records = list(get_records()) if get_records else []
        sites = self._exec_resolve_site_cells(records)
        if not sites:
            self._exec_wafer_map.set_picked([])
            self._exec_on_sites_changed([])
            self._exec_log(f"[RUN] Recipe '{name}' has no touchdown list")
            return
        known = self._exec_wafer_map.dies
        on_map = [rc for rc in self._exec_touchdown_cells(sites) if rc in known]
        self._exec_wafer_map.set_picked(on_map)
        self._exec_on_sites_changed(on_map)
        missing = len(sites) - len(on_map)
        self._exec_log(
            f"[RUN] Recipe '{name}' defines {len(sites)} touchdown(s) — "
            f"selected {len(on_map)} on the map."
            + (f"  {missing} are not on this wafer map; check that the loaded "
               "map matches the recipe." if missing else ""))
        if self._system == "accretech" and self.recipe_panel.is_minor_moves():
            ids = {(s["row"], s["col"]): s["die_id"] for s in records if s.get("die_id")}
            if ids:
                self._exec_overlay_die_ids = {**(self._exec_overlay_die_ids or {}), **ids}
                self._exec_redraw_overlay_on_run_map()
                self._exec_redraw_overlay_on_results_map()

    def _exec_load_recipe_by_name(self, name: str):
        if not name or not hasattr(self, "_exec_recipe_var"):
            return
        self._exec_recipe_var.set(name)
        self._exec_load_recipe()

    def _exec_load_recipe(self):
        name = self._exec_recipe_var.get()
        if not name:
            self._exec_log("[RUN] Pick a recipe first.")
            return
        if not self.recipe_panel.select_recipe(name):
            self._exec_log(f"[RUN] Recipe '{name}' not found — reload the ATA folder.")
            return
        self._exec_steps = self.recipe_panel.get_steps()
        self._exec_autoload_recipe_wafer_map()

        self._exec_steps_tree.delete(*self._exec_steps_tree.get_children())
        for i, s in enumerate(self._exec_steps, 1):
            self._exec_steps_tree.insert("", "end", values=(
                i, s.get("name", ""), s.get("type", ""), s.get("conn", "")))
        self._exec_steps_var.set(f"{name} — {len(self._exec_steps)} step(s)")
        self._exec_apply_recipe_sites(name)
        run = getattr(self, "eg_pma_run", None)
        if run is not None and hasattr(run, "_fill_table"):
            run._fill_table()
        self._exec_preselect_align_die()

        self._exec_log(f"[RUN] Loaded recipe '{name}' with "
                        f"{len(self._exec_steps)} step(s):")
        for i, s in enumerate(self._exec_steps, 1):
            extra = (f" target={s['target']}" if s.get("target")
                     else f" {s.get('hi', '')}→{s.get('lo', '')}")
            self._exec_log(f"[RUN]   {i}. {s.get('name')} [{s.get('type')}"
                            f"{('/' + s['mode']) if s.get('mode') else ''}]"
                            f"{extra}  conn={s.get('conn') or '—'}")
        issues = self.recipe_panel.validate_recipe()
        for msg in issues:
            self._exec_log(f"[RUN] {msg}")
        if issues:
            self._exec_log(f"[RUN] {len(issues)} validation issue(s) — "
                            "review before Touchdown/Measure")
        if hasattr(self.controller, "check_system_ready"):
            self.controller.check_system_ready()


    def _exec_find_loaded_step(self, ref: str):
        ref = (ref or "").strip()
        if ref.isdigit():
            i = int(ref) - 1
            return self._exec_steps[i] if 0 <= i < len(self._exec_steps) else None
        for s in self._exec_steps:
            if s.get("name", "").strip().lower() == ref.lower():
                return s
        return None

    def _exec_reset_output(self, ref, smu, wgen):
        if ref is None:
            return ""
        if ref.get("type") == "wave":
            wch = 2 if ref.get("chan") == "CH2" else 1
            if wgen and wgen.inst:
                wgen.turn_output_off_ch(wch)
            return f"reset WGEN CH{wch}"
        if ref.get("mode") == "apply":
            smu_ch = "smub" if ref.get("chan") == "B" else "smua"
            if smu and smu.inst:
                smu.turn_output_off(smu_ch)
            return f"reset SMU {ref.get('chan') or 'A'}"
        return ""

    def _exec_touchdown_measure(self):
        if self._exec_running:
            self._exec_log("[MEASURE] A run is active — stop it first.")
            return
        if not self._exec_steps:
            self._exec_log("[MEASURE] No recipe loaded")
            return
        if not self._exec_wafer_map_matches_recipe():
            return
        self._exec_aborted = False
        if self._system == "electroglas":
            self._exec_measure_here_eg()
            return
        shot_geom = self._exec_prepare_shot_geometry()
        threading.Thread(target=self._exec_touchdown_then_measure,
                         args=(shot_geom,), daemon=True).start()

    def _exec_measure_here_eg(self):
        run = getattr(self, "eg_pma_run", None)
        if run is None:
            self._exec_log("[MEASURE] The Electroglas Run tab is not available.")
            return
        if getattr(run, "_running", False):
            self._exec_log("[MEASURE] A run is active — stop it first.")
            return
        if run._index is None or not run._touchdowns:
            self._exec_log("[MEASURE] Set where the chuck is first "
                           "(Chuck is on → Set), so the reading can be filed "
                           "against a die.")
            return
        drv = self.controller.drivers.get("prober")
        threading.Thread(target=run._measure_here, args=(drv,),
                         daemon=True).start()

    def _exec_touchdown_then_measure(self, shot_geom=None):
        prober = self.controller.drivers.get("prober")
        if prober and prober.inst:
            try:
                self._exec_log("[MEASURE] >> Z  (Touchdown)")
                prober.z_up()
                self._exec_log("[MEASURE] Touchdown complete")
            except Exception as e:
                self._exec_log(f"[MEASURE] Touchdown error: {e} — measuring anyway")
        else:
            self._exec_log("[MEASURE] Prober not connected")

        row = col = None
        if self._exec_current_rc is not None:
            row, col = self._exec_current_rc
        rc_by_slot = []
        if shot_geom is not None and row is not None:
            self._exec_publish_die_slots_at(shot_geom, row, col)
            rc_by_slot = list(getattr(self, "_exec_die_rc_by_slot", None) or [])
        try:
            overall_ok = self._exec_run_steps_once()
        finally:
            if shot_geom is not None:
                self._exec_die_rc_by_slot = []
                self._exec_die_ids_by_slot = []
                self._exec_die_shotpos_by_slot = []

        if row is not None:
            self._exec_color_shot_squares(rc_by_slot, row, col, overall_ok)

    def _exec_avg_spec(self, step: dict) -> tuple:
        try:
            count = max(1, int(step.get("avg_count") or 1))
        except ValueError:
            count = 1
        try:
            delay = max(0.0, float(step.get("avg_delay") or 0))
        except ValueError:
            delay = 0.0
        return count, delay

    def _exec_settle_ms(self, step: dict) -> float:
        try:
            return max(0.0, float(step.get("settle_delay") or 0))
        except ValueError:
            return 0.0

    def _exec_settle(self, step: dict, name: str, i: int):
        ms = self._exec_settle_ms(step)
        if ms > 0:
            self._exec_log(f"[MEASURE] {i}. {name}: settling {ms:.0f} ms")
            time.sleep(ms / 1000.0)

    def _exec_nplc_spec(self, step: dict):
        try:
            nplc = float(step.get("nplc") or 1)
        except ValueError:
            return None
        return nplc if nplc != 1 else None

    def _exec_should_configure(self, step: dict, sig: tuple) -> bool:
        recipe_panel = getattr(self, "recipe_panel", None)
        if recipe_panel is None or not recipe_panel.is_shortcut():
            return True
        key = id(step)
        cache = self._exec_step_config_cache
        if cache.get(key) == sig:
            return False
        cache[key] = sig
        return True

    def _exec_measure_averaged(self, smu, smu_ch, read_one, avg_count: int,
                                avg_delay_ms: float, unit: str) -> float:
        trusted = getattr(smu, "averaged_reading_ok", True)
        can_hw = (avg_count > 1 and smu is not None
                  and getattr(smu, "inst", None) is not None
                  and hasattr(smu, "set_averages") and trusted)
        if can_hw:
            try:
                avg_key = (id(smu), smu_ch)
                if self._exec_avg_count_cache.get(avg_key) != avg_count:
                    smu.set_averages(smu_ch, avg_count)
                    self._exec_avg_count_cache[avg_key] = avg_count
                value = (smu.read_average() if hasattr(smu, "read_average")
                         else read_one())
                self._exec_log(f"[MEASURE]      {avg_count} readings averaged "
                                f"inside the {type(smu).__name__} -> "
                                f"{value:.6g} {unit}")
                return value
            except Exception as e:
                self._exec_log(f"[MEASURE]      instrument averaging failed "
                                f"({type(e).__name__}: {e}) — averaging in software")
        elif avg_count > 1 and not trusted:
            self._exec_log("[MEASURE]      averaging in software")
        if smu is not None and hasattr(smu, "set_averages"):
            try:
                smu.set_averages(smu_ch, 1)
            except Exception:
                pass
        return self._exec_take_average(read_one, avg_count, avg_delay_ms, unit)

    def _exec_take_average(self, read_one, avg_count: int, avg_delay_ms: float, unit: str) -> float:
        readings = []
        for k in range(avg_count):
            readings.append(read_one())
            if avg_count > 1:
                self._exec_log(f"[MEASURE]      reading {k + 1}/{avg_count} = "
                                f"{readings[-1]:.6g} {unit}")
                if k < avg_count - 1 and avg_delay_ms > 0:
                    time.sleep(avg_delay_ms / 1000.0)
        return sum(readings) / len(readings)

    def _exec_maybe_abs(self, step: dict, value: float) -> float:
        return abs(value) if (step.get("abs_value") or "").strip() else value

    def _exec_switch_driver(self):
        drivers = self.controller.drivers
        if self._system == "accretech":
            return drivers.get("switch")
        return drivers.get("relay1") or drivers.get("switch")

    def _exec_apply_target(self, s, raw_value: float, raw_unit: str, readings_by_name: dict):
        tgt = (s.get("target") or "").strip()
        if not tgt:
            return raw_value, raw_unit, ""
        applied = readings_by_name.get(tgt)
        if applied is None:
            return raw_value, raw_unit, (f"  (target '{tgt}' has no recorded value yet "
                                         "— using the raw reading)")
        derived = compute_target_derived(raw_value, raw_unit, applied[0], applied[1])
        if derived is None:
            return raw_value, raw_unit, (f"  (no known calculation for {raw_unit}+"
                                         f"{applied[1]} — using the raw reading)")
        dv, du = derived
        du_symbol = {"ohm": "Ω", "V": "V", "A": "A"}.get(du, du)
        return dv, du, f"  -> {dv:.6g} {du_symbol}  (combined with '{tgt}')"

    def _exec_resolve_instrument(self, s: dict, family_default_key: str, fallback_driver):
        key = s.get("instrument_key") or family_default_key
        drv = self.controller.drivers.get(key)
        return drv if drv is not None else fallback_driver

    def _exec_apply_terminals(self, s: dict, drv):
        which = (s.get("terminals") or "").strip().upper()
        if not which or not drv or not drv.inst:
            return
        if not hasattr(drv, "set_terminals"):
            return
        try:
            drv.set_terminals(which)
        except Exception as e:
            self._exec_log(f"[MEASURE]    could not set terminals to {which}: {e}")

    def _exec_run_steps_once(self, steps: list = None) -> bool:
        if steps is None:
            steps = self._exec_steps
        import random
        import re
        switch = self._exec_switch_driver()
        smu    = self.controller.drivers.get("smu")
        dmm    = self.controller.drivers.get("dmm")
        wgen   = self.controller.drivers.get("wave_gen")
        sim = not (switch and switch.inst)

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        recipe_name = self.recipe_panel.get_active_recipe() if hasattr(self, "recipe_panel") else ""
        die_label = (getattr(self, "_exec_die_id_override", "")
                     or (self._exec_die_var.get().replace("Die: ", "")
                         if self._exec_die_num else
                         self._exec_xy_var.get().replace("\n", " ")))

        cur_row, cur_col = self._exec_current_rc or (None, None)
        map_die_id = (self._exec_wafer_map.die_ids.get((cur_row, cur_col), "")
                      if cur_row is not None else "")
        overlay_die_id = (self._exec_overlay_die_ids.get((cur_row, cur_col), "")
                          if cur_row is not None else "")
        die_id = (getattr(self, "_exec_die_id_override", "")
                  or overlay_die_id or map_die_id)
        probe_card = (self.pin_wiring.get_active_card()
                     if hasattr(self, "pin_wiring") else "")

        def _shotpos_kwargs(shotpos):
            sr, sc, ir, ic = shotpos or (None, None, None, None)
            return {"shot_row": sr, "shot_col": sc,
                    "intra_row": ir, "intra_col": ic, "probe_card": probe_card}
        last_set_voltage_by_ch = {}

        overall_ok = True
        self._exec_slot_verdicts = {}
        last_reading = None
        readings_by_name = {}

        self._exec_log(f"[MEASURE] One iteration — {len(steps)} step(s)")
        for i, s in enumerate(steps, 1):
            if self._exec_aborted:
                self._exec_log(f"[MEASURE] stopped before step {i} "
                                f"({s.get('name') or 'unnamed'}) — "
                                "remaining steps skipped.")
                self._exec_mark_all_open()
                return False
            t    = s.get("type")
            name = s.get("name") or f"step {i}"
            lvl  = s.get("level") or ""
            conn = (s.get("conn") or "").replace(" ", "")
            direct = s.get("route") == "direct"
            chans = ([] if direct else
                     [c for c in conn.split(",") if c and c.lower() != "all"])
            conn_str = "DIRECT" if direct else "_".join(chans)
            try:
                if t == "delay":
                    ms = float(lvl or 0)
                    self._exec_log(f"[MEASURE] {i}. {name}: wait {ms:.0f} ms")
                    time.sleep(ms / 1000.0)
                    continue

                if t == "move":
                    try:
                        die_no = int(float(s.get("die") or "1"))
                    except (TypeError, ValueError):
                        die_no = 1
                    move_fn = getattr(self, "_exec_move_fn", None)
                    if move_fn is None:
                        self._exec_log(f"[MEASURE] {i}. {name}: move to die {die_no} "
                                        "— no Minor Moves context active (Measure only "
                                        "tests the first die of a shot; run the recipe "
                                        "for real to walk the whole shot)")
                        self._exec_mark_all_open()
                        return False
                    self._exec_log(f"[MEASURE] {i}. {name}: moving to die {die_no}...")
                    try:
                        move_fn(die_no)
                    except RuntimeError as e:
                        self._exec_log(f"[MEASURE] {i}. {name}: die {die_no} is off the "
                                        f"real wafer map ({e}) — skipping the rest of this shot")
                        self._exec_mark_all_open()
                        return False
                    continue

                if t == "picture":
                    self._exec_log(f"[MEASURE] {i}. {name}: take picture "
                                    "(not yet implemented — skipped)")
                    continue

                if t == "open":
                    if conn.lower() == "all" or (s.get("target") or "").strip().lower() == "all":
                        if sim:
                            self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                            "switch matrix not connected")
                            return False
                        self._exec_log(f"[MEASURE] {i}. {name}: open ALL")
                        switch.open_all()
                        if smu and smu.inst:
                            smu.turn_output_off("smua")
                            smu.turn_output_off("smub")
                        if wgen and wgen.inst:
                            wgen.turn_output_off_ch(1)
                            wgen.turn_output_off_ch(2)
                        self._exec_mark_all_open()
                        continue
                    if not direct and chans and sim:
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                        "switch matrix not connected")
                        return False
                    ref = self._exec_find_loaded_step(s.get("target", ""))
                    note = self._exec_reset_output(ref, smu, wgen)
                    self._exec_log(f"[MEASURE] {i}. {name}: open {conn or '—'}"
                                    + (f"  ({note})" if note else ""))
                    for ch in chans:
                        if hasattr(switch, "open_crosspoint"):
                            switch.open_crosspoint(ch[:2], ch[2:])
                        else:
                            switch.open_channel(ch)
                    self._exec_mark_open(chans)
                    continue

                if t == "passfail":
                    try:
                        die_no = int(float(s.get("die") or "1"))
                    except (TypeError, ValueError):
                        die_no = 1
                    tgt = (s.get("target") or "").strip()
                    if tgt:
                        found = readings_by_name.get(tgt)
                        ref_name = tgt
                    else:
                        found = (last_reading[1], last_reading[2]) if last_reading else None
                        ref_name = last_reading[0] if last_reading else "(none)"
                    if found is None:
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR no reading found "
                                        f"for '{ref_name}' — FAIL")
                        overall_ok = False
                        self._exec_slot_verdicts[die_no] = False
                        continue
                    value, unit = found
                    mn, mx = s.get("min") or "", s.get("max") or ""
                    verdict = ((not mn or value >= float(mn)) and
                              (not mx or value <= float(mx)))
                    overall_ok = overall_ok and verdict
                    self._exec_slot_verdicts[die_no] = verdict
                    spec = f"[{mn or '-inf'}, {mx or '+inf'}]"
                    self._exec_log(f"[MEASURE] {i}. {name}: "
                                    f"{'PASS' if verdict else 'FAIL'}  "
                                    f"{ref_name} = {value:.6g} {unit}  spec {spec}")
                    continue

                mode       = s.get("mode") or ""
                instrument = s.get("instrument") or ""
                label = f"{i}. {name} [{t}{('/' + mode) if mode else ''} " \
                        f"via {instrument}]"
                if not direct and chans and sim:
                    self._exec_log(f"[MEASURE] {label}: ERROR — "
                                    "switch matrix not connected")
                    return False
                self._exec_log(f"[MEASURE] {label}: "
                                + ("direct wiring — no switchbox" if direct
                                   else f"close {conn or '—'}"))
                for ch in chans:
                    switch.close_channel(ch)
                self._exec_mark_closed(chans)
                smu_ch = "smub" if s.get("chan") == "B" else "smua"
                wch    = 2 if s.get("chan") == "CH2" else 1

                limit = s.get("limit") or ""
                avg_count, avg_delay = self._exec_avg_spec(s)
                avg_txt = f"  [avg of {avg_count}, {avg_delay:.0f} ms apart]" if avg_count > 1 else ""

                if t == "resistance":
                    nplc = self._exec_nplc_spec(s)
                    do_cfg = self._exec_should_configure(
                        s, ("resistance", instrument, smu_ch, nplc))
                    drv = self._exec_resolve_instrument(
                        s, "smu" if instrument == "SMU" else "dmm",
                        smu if instrument == "SMU" else dmm)
                    if not (drv and drv.inst):
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                        f"{instrument} not connected")
                        return False
                    self._exec_apply_terminals(s, drv)
                    if instrument == "SMU":
                        if do_cfg and nplc is not None:
                            drv.set_nplc(smu_ch, nplc)
                        manual_mode = bool(getattr(self, "recipe_panel", None)
                                           and self.recipe_panel.is_manual_mode())
                        def _read_smu_r(_drv=drv, _ch=smu_ch, _manual=manual_mode):
                            if hasattr(_drv, "set_terminals"):
                                return _drv.measure_resistance(_ch, manual=_manual)
                            return _drv.measure_resistance(_ch)
                        read_one = _read_smu_r
                    else:
                        read_one = lambda: drv.measure_resistance()
                    self._exec_settle(s, name, i)
                    r_raw = self._exec_maybe_abs(s, self._exec_measure_averaged(
                        drv, smu_ch,
                        read_one, avg_count, avg_delay, "Ω"))
                    r, r_unit, note = self._exec_apply_target(s, r_raw, "ohm", readings_by_name)
                    self._exec_log(f"[MEASURE]    R = {r_raw:.4g} Ω  (via {instrument})"
                                    f"{avg_txt}{note}")
                    slot_die, slot_row, slot_col, slot_sw, slot_shotpos = self._exec_slot_identity(
                        s.get("die"), die_label, (cur_row, cur_col))
                    slot_die_id = slot_die if slot_sw is not None else (die_id or None)
                    self.record_result(timestamp=ts, recipe=recipe_name, die=slot_die,
                                       step=name, type=t, mode=mode, value=f"{r:.6g}",
                                       unit=r_unit, die_id=slot_die_id, switch=slot_sw,
                                       connection=conn_str, instrument=instrument,
                                       die_row=slot_row, die_col=slot_col,
                                       **_shotpos_kwargs(slot_shotpos))
                    last_reading = (name, r, r_unit)
                    readings_by_name[name] = (r, r_unit)
                elif t == "ohmf":
                    if instrument != "DMM":
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                        "4-wire (ohmf) is DMM-only")
                        return False
                    drv = self._exec_resolve_instrument(s, "dmm", dmm)
                    if not (drv and drv.inst):
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                        "DMM not connected")
                        return False
                    self._exec_apply_terminals(s, drv)
                    if hasattr(drv, "measure_resistance_4w"):
                        read_one = lambda: drv.measure_resistance_4w()
                    elif hasattr(drv, "measure_resistance"):
                        read_one = lambda: drv.measure_resistance(wire_mode=4)
                    else:
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                        f"{type(drv).__name__} has no 4-wire "
                                        "resistance method")
                        return False
                    self._exec_settle(s, name, i)
                    r_raw = self._exec_maybe_abs(s, self._exec_measure_averaged(
                        drv, smu_ch,
                        read_one, avg_count, avg_delay, "Ω"))
                    r, r_unit, note = self._exec_apply_target(s, r_raw, "ohm", readings_by_name)
                    self._exec_log(f"[MEASURE]    R(4W) = {r_raw:.4g} Ω  (via DMM)"
                                    f"{avg_txt}{note}")
                    slot_die, slot_row, slot_col, slot_sw, slot_shotpos = self._exec_slot_identity(
                        s.get("die"), die_label, (cur_row, cur_col))
                    slot_die_id = slot_die if slot_sw is not None else (die_id or None)
                    self.record_result(timestamp=ts, recipe=recipe_name, die=slot_die,
                                       step=name, type=t, mode=mode, value=f"{r:.6g}",
                                       unit=r_unit, die_id=slot_die_id, switch=slot_sw,
                                       connection=conn_str, instrument=instrument,
                                       die_row=slot_row, die_col=slot_col,
                                       **_shotpos_kwargs(slot_shotpos))
                    last_reading = (name, r, r_unit)
                    readings_by_name[name] = (r, r_unit)
                elif t == "voltage" and mode == "measure":
                    nplc = self._exec_nplc_spec(s)
                    do_cfg = self._exec_should_configure(
                        s, ("voltage_measure", instrument, smu_ch, nplc))
                    drv = self._exec_resolve_instrument(
                        s, "smu" if instrument == "SMU" else "dmm",
                        smu if instrument == "SMU" else dmm)
                    if not (drv and drv.inst):
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                        f"{instrument} not connected")
                        return False
                    self._exec_apply_terminals(s, drv)
                    if instrument == "SMU":
                        if do_cfg and nplc is not None:
                            drv.set_nplc(smu_ch, nplc)
                        read_one = lambda: drv.measure_voltage(smu_ch)
                    else:
                        read_one = lambda: drv.measure_voltage_dc()
                    self._exec_settle(s, name, i)
                    v_raw = self._exec_maybe_abs(s, self._exec_measure_averaged(
                        drv, smu_ch,
                        read_one, avg_count, avg_delay, "V"))
                    v, v_unit, note = self._exec_apply_target(s, v_raw, "V", readings_by_name)
                    self._exec_log(f"[MEASURE]    V = {v_raw:.4g} V  (via {instrument})"
                                    f"{avg_txt}{note}")
                    slot_die, slot_row, slot_col, slot_sw, slot_shotpos = self._exec_slot_identity(
                        s.get("die"), die_label, (cur_row, cur_col))
                    slot_die_id = slot_die if slot_sw is not None else (die_id or None)
                    self.record_result(timestamp=ts, recipe=recipe_name, die=slot_die,
                                       step=name, type=t, mode=mode, value=f"{v:.6g}",
                                       unit=v_unit, die_id=slot_die_id, switch=slot_sw,
                                       connection=conn_str, instrument=instrument,
                                       die_row=slot_row, die_col=slot_col,
                                       **_shotpos_kwargs(slot_shotpos))
                    last_reading = (name, v, v_unit)
                    readings_by_name[name] = (v, v_unit)
                elif t == "voltage":
                    if not (smu and smu.inst):
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — SMU not connected")
                        return False
                    do_cfg = self._exec_should_configure(
                        s, ("voltage_apply", smu_ch, lvl, limit))
                    if do_cfg:
                        smu.set_voltage(smu_ch, float(lvl or 0))
                        if limit:
                            smu.set_current_limit(smu_ch, float(limit))
                    smu.turn_output_on(smu_ch)
                    last_set_voltage_by_ch[smu_ch] = float(lvl or 0)
                    lim_txt = f", current limit {limit} A" if limit else ""
                    self._exec_log(f"[MEASURE]    forcing {lvl or 0} V on SMU "
                                    f"{s.get('chan') or 'A'}{lim_txt}")
                    last_reading = (name, float(lvl or 0), "V")
                    readings_by_name[name] = (float(lvl or 0), "V")
                elif t == "current" and mode == "apply":
                    if not (smu and smu.inst):
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — SMU not connected")
                        return False
                    actual_current = None
                    actual_voltage = None
                    do_cfg = self._exec_should_configure(
                        s, ("current_apply", smu_ch, lvl, limit))
                    smu.turn_output_off(smu_ch)
                    if do_cfg:
                        smu.set_current(smu_ch, float(lvl or 0))
                        if limit:
                            smu.set_voltage_limit(smu_ch, float(limit))
                    smu.turn_output_on(smu_ch)
                    if hasattr(smu, "set_source_clear_auto"):
                        fast_settle = bool(getattr(self, "recipe_panel", None)
                                          and self.recipe_panel.is_fast_current_settle())
                        smu.set_source_clear_auto(not fast_settle)
                    if hasattr(smu, "measure_current_and_voltage"):
                        try:
                            actual_current, actual_voltage = \
                                smu.measure_current_and_voltage(smu_ch)
                        except Exception:
                            actual_current = actual_voltage = None
                    else:
                        try:
                            actual_current = smu.measure_current(smu_ch)
                        except Exception:
                            actual_current = None
                        try:
                            actual_voltage = smu.measure_voltage(smu_ch)
                        except Exception:
                            actual_voltage = None
                    if actual_current is None:
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                        "SMU readback failed")
                        return False
                    lim_txt = f", voltage limit {limit} V" if limit else ""
                    readback_txt = (f"  readback I={actual_current:.6g} A"
                                    + (f", V={actual_voltage:.6g} V"
                                       if actual_voltage is not None else ""))
                    self._exec_log(f"[MEASURE]    forcing {lvl or 0} A on SMU "
                                    f"{s.get('chan') or 'A'}{lim_txt}" + readback_txt)
                    slot_die, slot_row, slot_col, slot_sw, slot_shotpos = self._exec_slot_identity(
                        s.get("die"), die_label, (cur_row, cur_col))
                    slot_die_id = slot_die if slot_sw is not None else (die_id or None)
                    self.record_result(timestamp=ts, recipe=recipe_name, die=slot_die,
                                       step=name, type=t, mode=mode, value=f"{actual_current:.6g}",
                                       unit="A", voltage=actual_voltage, die_id=slot_die_id,
                                       switch=slot_sw, connection=conn_str, instrument=instrument,
                                       die_row=slot_row, die_col=slot_col,
                                       **_shotpos_kwargs(slot_shotpos))
                    last_reading = (name, actual_current, "A")
                    readings_by_name[name] = (actual_current, "A")
                elif t == "current":
                    set_voltage = None
                    actual_voltage = None
                    _combined_reading = {}
                    did_bias = False
                    drv = self._exec_resolve_instrument(
                        s, "smu" if instrument == "SMU" else "dmm",
                        smu if instrument == "SMU" else dmm)
                    if not (drv and drv.inst):
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                        f"{instrument} not connected")
                        return False
                    self._exec_apply_terminals(s, drv)
                    if instrument == "SMU":
                        nplc = self._exec_nplc_spec(s)
                        mrange = (s.get("mrange") or "").strip()
                        do_cfg = self._exec_should_configure(
                            s, ("current_measure", smu_ch, lvl, limit,
                               s.get("nplc"), mrange, avg_delay))
                        if lvl:
                            if do_cfg:
                                drv.set_voltage(smu_ch, float(lvl))
                                if limit:
                                    drv.set_current_limit(smu_ch, float(limit))
                            drv.turn_output_on(smu_ch)
                            last_set_voltage_by_ch[smu_ch] = float(lvl)
                            did_bias = True
                        if do_cfg:
                            if nplc is not None:
                                drv.set_nplc(smu_ch, nplc)
                            if hasattr(drv, "set_auto_zero"):
                                drv.set_auto_zero(False)
                            if mrange and hasattr(drv, "set_current_range"):
                                try:
                                    drv.set_current_range(smu_ch, float(mrange))
                                except (TypeError, ValueError) as e:
                                    self._exec_log(f"[MEASURE]    ignoring bad "
                                                    f"meter range {mrange!r}: {e}")
                            if avg_delay and hasattr(drv, "set_source_delay"):
                                drv.set_source_delay(avg_delay / 1000.0)
                        if hasattr(drv, "measure_current_and_voltage"):
                            def read_one():
                                i_val, v_val = drv.measure_current_and_voltage(smu_ch)
                                _combined_reading["v"] = v_val
                                return i_val
                        else:
                            read_one = lambda: drv.measure_current(smu_ch)
                        bias_txt = f"  (bias {lvl} V via SMU)" if lvl else "  (via SMU)"
                        set_voltage = last_set_voltage_by_ch.get(smu_ch)
                    else:
                        read_one = lambda: drv.measure_current_dc()
                        bias_txt = "  (via DMM)"
                    self._exec_settle(s, name, i)
                    i_raw = self._exec_maybe_abs(s, self._exec_measure_averaged(
                        drv, smu_ch,
                        read_one, avg_count, avg_delay, "A"))
                    if instrument == "SMU" and drv and drv.inst:
                        if "v" in _combined_reading:
                            actual_voltage = _combined_reading["v"]
                        else:
                            try:
                                actual_voltage = drv.measure_voltage(smu_ch)
                            except Exception:
                                actual_voltage = None
                    in_compliance = False
                    if instrument == "SMU" and drv and drv.inst \
                            and hasattr(drv, "in_compliance"):
                        try:
                            in_compliance = drv.in_compliance(smu_ch)
                        except Exception:
                            in_compliance = False
                    if did_bias:
                        try:
                            drv.turn_output_off(smu_ch)
                        except Exception as e:
                            self._exec_log(f"[MEASURE]    could not turn off SMU "
                                            f"{s.get('chan') or 'A'} after measuring: {e}")
                    i_a, i_unit, note = self._exec_apply_target(s, i_raw, "A", readings_by_name)
                    self._exec_log(f"[MEASURE]    I = {i_raw:.4g} A{bias_txt}{avg_txt}{note}"
                                    + ("  (bias off)" if did_bias else "")
                                    + ("  SMU REPORTS COMPLIANCE" if in_compliance else ""))
                    if actual_voltage is None:
                        actual_voltage = set_voltage
                    slot_die, slot_row, slot_col, slot_sw, slot_shotpos = self._exec_slot_identity(
                        s.get("die"), die_label, (cur_row, cur_col))
                    slot_die_id = slot_die if slot_sw is not None else (die_id or None)
                    self.record_result(
                        timestamp=ts, recipe=recipe_name, die=slot_die,
                        step=name, type=t, mode=mode, value=f"{i_a:.6g}", unit=i_unit,
                        die_id=slot_die_id,
                        switch=slot_sw,
                        die_row=slot_row, die_col=slot_col,
                        set_voltage=set_voltage, voltage=actual_voltage,
                        connection=conn_str, instrument=instrument,
                        **_shotpos_kwargs(slot_shotpos))
                    last_reading = (name, i_a, i_unit)
                    readings_by_name[name] = (i_a, i_unit)
                elif t == "wave":
                    if not (wgen and wgen.inst):
                        self._exec_log(f"[MEASURE] {i}. {name}: ERROR — "
                                        "wave generator not connected")
                        return False
                    shape = s.get("shape") or "SIN"
                    freq = float(s.get("freq") or 1000)
                    wgen.set_waveform_ch(wch, shape, freq, float(lvl or 1.0))
                    if limit:
                        wgen.set_voltage_limit_ch(wch, float(limit))
                    wgen.turn_output_on_ch(wch)
                    lim_txt = f", clamp ±{limit} V" if limit else ""
                    self._exec_log(f"[MEASURE]    WGEN CH{wch} ON — {shape} "
                                    f"{lvl or 1.0} Vpp @ {freq:.4g} Hz{lim_txt}")
            except Exception as e:
                self._exec_log(f"[MEASURE] {i}. {name}: ERROR {e} — iteration aborted")
                return False
        self._exec_log(f"[MEASURE] Iteration complete — "
                        f"{'PASS' if overall_ok else 'FAIL'}")
        return overall_ok


    def _exec_slot_identity(self, die_no, fallback_die, fallback_rc):
        try:
            switch = int(float(die_no))
        except (TypeError, ValueError):
            switch = 1
        ids = getattr(self, "_exec_die_ids_by_slot", None) or []
        rcs = getattr(self, "_exec_die_rc_by_slot", None) or []
        shotpos_list = getattr(self, "_exec_die_shotpos_by_slot", None) or []
        slot = switch - 1
        if switch < 1 or not (slot < len(ids) or slot < len(rcs)):
            return fallback_die, fallback_rc[0], fallback_rc[1], None, None
        die = ids[slot] if 0 <= slot < len(ids) and ids[slot] else fallback_die
        rc = rcs[slot] if 0 <= slot < len(rcs) and rcs[slot] else fallback_rc
        shotpos = shotpos_list[slot] if 0 <= slot < len(shotpos_list) else None
        return die, rc[0], rc[1], switch, shotpos

    def record_result(self, timestamp, recipe, die, step, type, mode, value, unit,
                      die_id=None, switch=None, set_voltage=None, voltage=None,
                      connection=None, instrument=None, die_row=None, die_col=None,
                      shot_row=None, shot_col=None, intra_row=None, intra_col=None,
                      probe_card=None):
        row = {"timestamp": timestamp, "recipe": recipe, "die": die, "step": step,
               "type": type, "mode": mode, "value": value, "unit": unit,
               "die_id": die_id or "", "switch": switch if switch is not None else "",
               "set_voltage": set_voltage if set_voltage is not None else "",
               "voltage": voltage if voltage is not None else "",
               "connection": connection or "", "instrument": instrument or "",
               "row": die_row, "col": die_col,
               "shot_row": shot_row if shot_row is not None else "",
               "shot_col": shot_col if shot_col is not None else "",
               "intra_row": intra_row if intra_row is not None else "",
               "intra_col": intra_col if intra_col is not None else "",
               "probe_card": probe_card or ""}
        self.controller.results_data.append(row)
        if hasattr(self, "_results_tree"):
            def _ui():
                self._results_tree.insert("", "end", values=(
                    row["timestamp"], row["recipe"], row["die"], row["step"],
                    row["type"], row["value"], row["unit"]))
                kids = self._results_tree.get_children()
                if kids:
                    self._results_tree.see(kids[-1])
            self._exec_safe_after(_ui)

    def clear_results(self):
        self.controller.results_data.clear()
        self._exec_last_run_start_idx = 0
        if hasattr(self, "_results_tree"):
            self._results_tree.delete(*self._results_tree.get_children())

    def get_last_run_results(self) -> list:
        return self.controller.results_data[self._exec_last_run_start_idx:]


    def _exec_manual_z_up(self):
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self._exec_log("[RUN] Z Up: prober not connected.")
            return
        def _run():
            try:
                self.after(0, lambda: self._exec_log("[RUN] >> Z  (Contact)"))
                prober.z_up()
                self.after(0, lambda: self._exec_log("[RUN] Z Up complete."))
            except Exception as e:
                self.after(0, lambda e=e: self._exec_log(f"[RUN] Z Up error: {e}"))
        threading.Thread(target=_run, daemon=True).start()

    def _exec_manual_z_down(self):
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self._exec_log("[RUN] Z Down: prober not connected.")
            return
        def _run():
            try:
                self.after(0, lambda: self._exec_log("[RUN] >> D  (Separate)"))
                prober.z_down()
                self.after(0, lambda: self._exec_log("[RUN] Z Down complete."))
            except Exception as e:
                self.after(0, lambda e=e: self._exec_log(f"[RUN] Z Down error: {e}"))
        threading.Thread(target=_run, daemon=True).start()

    def _exec_manual_go_to_start(self):
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self._exec_log("[RUN] First Die: prober not connected.")
            return
        threading.Thread(target=self._exec_go_to_start_thread, args=(prober,),
                         daemon=True).start()

    def _exec_go_to_start_thread(self, prober):
        try:
            self._exec_log("[RUN] >> G  (Position start die)")
            stb = prober.move_to_start_die()
            self._exec_log(f"[RUN] << STB={stb}  (start die positioned, chuck "
                            f"{'UP — CONTACT' if stb == 67 else 'DOWN'})")
            self._exec_get_xy()
        except Exception as e:
            self._exec_log(f"[RUN] First Die error: {e}")

    def _exec_manual_unload(self):
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self._exec_log("[RUN] Unload: prober not connected.")
            return
        threading.Thread(target=self._exec_unload_thread, args=(prober,),
                         daemon=True).start()

    def _exec_unload_thread(self, prober):
        try:
            self._exec_log("[RUN] >> U  (Unload wafer)")
            stb = prober.unload_wafer()
            self._exec_log(f"[RUN] << STB={stb}  (wafer unloaded)")
        except Exception as e:
            self._exec_log(f"[RUN] Unload error: {e}")

    def _exec_manual_prev_die(self):
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self._exec_log("[RUN] Back: prober not connected.")
            return
        def _run():
            try:
                self.after(0, lambda: self._exec_log("[RUN] >> S  (X-1, previous die)"))
                stb = prober.move_xy_relative(-1, 0)
                self.after(0, lambda: self._exec_log(f"[RUN] << STB={stb}"))
                self.after(0, self._exec_get_xy)
            except Exception as e:
                self.after(0, lambda e=e: self._exec_log(f"[RUN] Back error: {e}"))
        threading.Thread(target=_run, daemon=True).start()

    def _exec_manual_next_die(self):
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self._exec_log("[RUN] Next: prober not connected.")
            return
        def _run():
            try:
                self.after(0, lambda: self._exec_log("[RUN] >> J  (next die)"))
                stb = prober.next_die()
                self.after(0, lambda: self._exec_log(f"[RUN] << STB={stb}"))
                self.after(0, self._exec_get_xy)
            except Exception as e:
                self.after(0, lambda e=e: self._exec_log(f"[RUN] Next error: {e}"))
        threading.Thread(target=_run, daemon=True).start()

    def _exec_shot_step_setup(self, label: str):
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self._exec_log(f"[RUN] {label}: prober not connected.")
            return None
        gen = getattr(self, "recipe_gen", None)
        if gen is None:
            self._exec_log(f"[RUN] {label}: the Wafer Builder tab is not available.")
            return None
        if not self._exec_overlay_offset_confirmed:
            self._exec_log(f"[RUN] {label}: no confirmed Overlay alignment")
            return None
        try:
            shot_rows, shot_cols = gen._shot_dims()
        except Exception:
            self._exec_log(f"[RUN] {label}: could not read the Wafer Builder shot size.")
            return None
        shots = sorted((sr, sc) for (sr, sc), present in gen._shotmap_cells.items() if present)
        if not shots:
            self._exec_log(f"[RUN] {label}: no shots on the Wafer Builder Shot Map tab.")
            return None
        return (prober, gen, shots, shot_rows, shot_cols,
               self._exec_overlay_row_offset, self._exec_overlay_col_offset)

    def _exec_go_to_shot(self, prober, gen, shot_row: int, shot_col: int,
                          shot_rows: int, shot_cols: int, row_off: int, col_off: int,
                          label: str):
        r, c = shot_die_rc(dict(gen._shot_cells), shot_rows, shot_cols, 1) or (0, 0)
        die_x = shot_col * shot_cols + c + col_off
        die_y = shot_row * shot_rows + r + row_off
        def _run():
            try:
                self.after(0, lambda: self._exec_log("[RUN] >> D  (Separate)"))
                prober.z_down()
                self.after(0, lambda: self._exec_log(
                    f"[RUN] >> J  ({label} -> shot R{shot_row}C{shot_col}, "
                    f"die #1, X={die_x} Y={die_y})"))
                stb = prober.move_to_die_xy(die_x, die_y)
                self.after(0, lambda: self._exec_log(f"[RUN] << STB={stb}"))
                self.after(0, self._exec_get_xy)
                self.after(0, lambda: self._exec_highlight_current(die_y, die_x))
            except Exception as e:
                self.after(0, lambda e=e: self._exec_log(f"[RUN] {label} error: {e}"))
        threading.Thread(target=_run, daemon=True).start()

    def _exec_current_shot_index(self, shots: list, shot_rows: int, shot_cols: int,
                                  row_off: int, col_off: int) -> "int | None":
        if self._exec_current_rc is None:
            return None
        wb_row = self._exec_current_rc[0] - row_off
        wb_col = self._exec_current_rc[1] - col_off
        cur_shot = (wb_row // shot_rows, wb_col // shot_cols)
        try:
            return shots.index(cur_shot)
        except ValueError:
            return None

    def _exec_manual_next_shot(self):
        setup = self._exec_shot_step_setup("Next Shot")
        if setup is None:
            return
        prober, gen, shots, shot_rows, shot_cols, row_off, col_off = setup
        cur_idx = self._exec_current_shot_index(shots, shot_rows, shot_cols, row_off, col_off)
        idx = 0 if cur_idx is None else cur_idx + 1
        if idx >= len(shots):
            self._exec_log("[RUN] Next Shot: already at the last shot.")
            return
        shot_row, shot_col = shots[idx]
        self._exec_go_to_shot(prober, gen, shot_row, shot_col, shot_rows, shot_cols,
                               row_off, col_off, "Next Shot")

    def _exec_manual_prev_shot(self):
        setup = self._exec_shot_step_setup("Previous Shot")
        if setup is None:
            return
        prober, gen, shots, shot_rows, shot_cols, row_off, col_off = setup
        cur_idx = self._exec_current_shot_index(shots, shot_rows, shot_cols, row_off, col_off)
        idx = (len(shots) - 1) if cur_idx is None else cur_idx - 1
        if idx < 0:
            self._exec_log("[RUN] Previous Shot: already at the first shot.")
            return
        shot_row, shot_col = shots[idx]
        self._exec_go_to_shot(prober, gen, shot_row, shot_col, shot_rows, shot_cols,
                               row_off, col_off, "Previous Shot")

    _EXEC2_MOVE_TARGET_COLOR = "#1e3a8a"

    def _exec_move_selected_button(self):
        wm = self._exec_wafer_map
        if not self._exec_move_armed:
            self._exec_move_armed = True
            self._exec_move_target_rc = None
            self._exec_move_prev_click_handler = wm._click_handler
            self._exec_move_prev_picking_enabled = wm._picking_enabled
            wm._picking_enabled = False
            wm.set_click_handler(self._exec_move_target_click)
            self._exec_move_selected_btn.config(text="✕ Cancel Move")
            return
        target = self._exec_move_target_rc
        self._exec_disarm_move_selected()
        if target is None:
            self._exec_log("[RUN] Move to Selected: cancelled.")
            return
        self._exec_do_move_to(*target)

    def _exec_move_target_click(self, row: int, col: int):
        if not self._exec_move_armed:
            return
        wm = self._exec_wafer_map
        rc = (row, col)
        if rc not in wm.dies:
            return
        if rc == self._exec_move_target_rc:
            self._exec_restore_move_target_color()
            self._exec_move_target_rc = None
            self._exec_move_selected_btn.config(text="✕ Cancel Move")
            return
        self._exec_restore_move_target_color()
        item = wm.dies[rc]
        self._exec_move_target_prev_fill = wm.canvas.itemcget(item, "fill")
        wm.canvas.itemconfig(item, fill=self._EXEC2_MOVE_TARGET_COLOR)
        self._exec_move_target_rc = rc
        self._exec_move_selected_btn.config(text="→ Move")

    def _exec_restore_move_target_color(self):
        wm = self._exec_wafer_map
        rc = self._exec_move_target_rc
        if rc is not None and rc in wm.dies and self._exec_move_target_prev_fill is not None:
            try:
                wm.canvas.itemconfig(wm.dies[rc], fill=self._exec_move_target_prev_fill)
            except tk.TclError:
                pass

    def _exec_disarm_move_selected(self):
        self._exec_restore_move_target_color()
        self._exec_move_target_rc = None
        self._exec_move_target_prev_fill = None
        wm = self._exec_wafer_map
        wm.set_click_handler(self._exec_move_prev_click_handler)
        wm._picking_enabled = self._exec_move_prev_picking_enabled
        self._exec_move_armed = False
        self._exec_move_selected_btn.config(text="→ Move to Selected")

    def _exec_do_move_to(self, row: int, col: int):
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self._exec_log("[RUN] Move to Selected: prober not connected.")
            return
        def _run():
            try:
                self.after(0, lambda: self._exec_log("[RUN] >> D  (Separate)"))
                prober.z_down()
                self.after(0, lambda: self._exec_log(
                    f"[RUN] >> J  (X={col} Y={row})"))
                stb = prober.move_to_die_xy(col, row)
                self.after(0, lambda: self._exec_log(f"[RUN] << STB={stb}"))
                self.after(0, self._exec_get_xy)
                self.after(0, lambda: self._exec_highlight_current(row, col))
            except Exception as e:
                self.after(0, lambda e=e: self._exec_log(
                    f"[RUN] Move to Selected error: {e}"))
        threading.Thread(target=_run, daemon=True).start()

    def _exec_refresh_xy_blocking(self, prober):
        try:
            raw = prober.get_xy_position()
            x, y = _parse_q_response(raw)
            self._exec_safe_after(lambda: self._exec_xy_var.set(f"X: {x:.0f} die\nY: {y:.0f} die"))
            self._exec_safe_after(lambda: self._exec_log(f"[RUN] Q → die X={x:.0f}  Y={y:.0f}"))
            self._exec_safe_after(lambda: self._exec_highlight_current(int(y), int(x)))
        except Exception as e:
            self._exec_log(f"[RUN] Refresh XY before run failed: {e}")

    def _exec_refresh_die_size(self):
        var = getattr(self, "_exec_die_size_var", None)
        if var is None:
            return
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst or not hasattr(prober, "infer_die_size"):
            return
        def _run():
            try:
                x, y = prober.infer_die_size()
                self.after(0, lambda: var.set(f"Prober Die size: X{x} Y{y} um"))
            except Exception as e:
                self._exec_log(f"[RUN] Could not infer the prober die size — "
                               f"{type(e).__name__}: {e}")
                self.after(0, lambda: var.set("Prober Die size: Could not infer."))
        threading.Thread(target=_run, daemon=True).start()

    def _exec_get_xy(self):
        prober = self.controller.drivers.get("prober")
        if not prober or not prober.inst:
            self._exec_xy_var.set("X: —\nY: —")
            self._exec_log("[RUN] XY: prober not connected.")
            return
        def _run():
            try:
                raw = prober.get_xy_position()
                x, y = _parse_q_response(raw)
                self.after(0, lambda: self._exec_xy_var.set(f"X: {x:.0f} die\nY: {y:.0f} die"))
                self.after(0, lambda: self._exec_log(f"[RUN] Q → die X={x:.0f}  Y={y:.0f}"))
                self.after(0, lambda: self._exec_highlight_current(int(y), int(x)))
            except Exception as e:
                self.after(0, lambda e=e: self._exec_log(f"[RUN] XY error: {e}"))
                self.after(0, lambda: self._exec_xy_var.set("X: ERROR\nY: ERROR"))
        threading.Thread(target=_run, daemon=True).start()

    def _exec_highlight_current(self, row: int, col: int):
        wm = self._exec_wafer_map
        prev = self._exec_current_rc
        if prev is not None and prev != (row, col) and prev in wm.dies:
            try:
                if wm.canvas.itemcget(wm.dies[prev], "fill") == "#dbeafe":
                    wm.update_die(prev[0], prev[1], "UNTESTED")
            except Exception:
                pass
        self._exec_current_rc = (row, col)
        if (row, col) in wm.dies:
            wm.update_die(row, col, "CURRENT")
        if self._system == "accretech":
            self._exec_update_shot_window()

    def _exec_clear_shot_window(self):
        wm = getattr(self, "_exec_wafer_map", None)
        if wm is not None:
            for item in self._exec_shot_window_items:
                try:
                    wm.canvas.delete(item)
                except Exception:
                    pass
        self._exec_shot_window_items = []

    def _exec_update_shot_window(self):
        self._exec_clear_shot_window()
        wm = getattr(self, "_exec_wafer_map", None)
        gen = getattr(self, "recipe_gen", None)
        if (wm is None or gen is None or self._exec_current_rc is None
                or not self._exec_overlay_offset_confirmed):
            return
        try:
            shot_rows, shot_cols = gen._shot_dims()
        except Exception:
            return
        if shot_rows <= 1 and shot_cols <= 1:
            return
        cur_row, cur_col = self._exec_current_rc
        row_off = self._exec_overlay_row_offset
        col_off = self._exec_overlay_col_offset
        if self._exec_minor_moves_active():
            wb_row, wb_col = cur_row - row_off, cur_col - col_off
            shot_r0 = (wb_row // shot_rows) * shot_rows
            shot_c0 = (wb_col // shot_cols) * shot_cols
            cells = [(shot_r0 + r + row_off, shot_c0 + c + col_off)
                    for r in range(shot_rows) for c in range(shot_cols)]
        else:
            shot_cells = dict(gen._shot_cells)
            die1_rc = shot_die_rc(shot_cells, shot_rows, shot_cols, 1)
            if die1_rc is None:
                return
            r1, c1 = die1_rc
            cells = [(cur_row + r - r1, cur_col + c - c1)
                    for r in range(shot_rows) for c in range(shot_cols)]
        boxes = [wm.canvas.coords(wm.dies[rc]) for rc in cells if rc in wm.dies]
        boxes = [b for b in boxes if len(b) >= 4]
        if not boxes:
            return
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
              max(b[2] for b in boxes), max(b[3] for b in boxes))
        rect = wm.canvas.create_rectangle(*box, outline="#7c3aed", width=2, dash=(4, 3))
        wm.canvas.tag_raise(rect)
        self._exec_shot_window_items = [rect]

    def _exec_add_pass(self):
        self._exec_pass_var.set(self._exec_pass_var.get() + 1)
        self._exec_update_yield()
        self._exec_push_stats()

    def _exec_add_fail(self):
        self._exec_fail_var.set(self._exec_fail_var.get() + 1)
        self._exec_update_yield()
        self._exec_push_stats()

    def _exec_reset_counts(self, total_dies=None):
        self._exec_last_run_start_idx = len(self.controller.results_data)
        self._exec_pass_var.set(0)
        self._exec_fail_var.set(0)
        reset = getattr(getattr(self, "eg_pma_run", None), "reset_results", None)
        if reset:
            try:
                reset()
            except Exception:
                pass
        try:
            self.controller.die_status.clear()
        except Exception:
            pass
        for wm in (getattr(self, "_exec_wafer_map", None),
                  getattr(self, "_results_wafer_map", None)):
            if wm is not None and hasattr(wm, "reset_all_statuses"):
                try:
                    wm.reset_all_statuses()
                except Exception:
                    pass
        self._exec_die_num = 0
        self._exec_step_config_cache = {}
        self._exec_avg_count_cache = {}
        if total_dies is not None:
            self._exec_total_dies = total_dies
        self._exec_pct_var.set("Yield:  —")
        self._exec_die_var.set("Die: —")
        self._exec_push_stats()

    def _exec_push_stats(self):
        if not hasattr(self.controller, "on_exec_stats_change"):
            return
        p = self._exec_pass_var.get()
        f = self._exec_fail_var.get()
        self.controller.on_exec_stats_change(p + f, p, f, self._exec_total_dies)

    def _exec_update_yield(self):
        p = self._exec_pass_var.get()
        f = self._exec_fail_var.get()
        total = p + f
        pct = (p / total * 100) if total else 0.0
        self._exec_pct_var.set(f"Yield:  {pct:.1f}%  ({p}/{total})")


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
        ttk.Entry(path_row, textvariable=self.export_path_var, width=40).pack(side="left", padx=6)
        ttk.Button(
            path_row, text="Browse...", command=self.controller.cmd_browse_export
        ).pack(side="left", padx=4)
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
                width=16, values=list(self._export_dir_choices.keys()))
            export_dir_cb.pack(side="left", padx=(4, 0))
            export_dir_cb.bind(
                "<<ComboboxSelected>>",
                lambda _e: self.export_path_var.set(
                    self._export_dir_choices[export_dir_var.get()]))
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
        self._export_format_cb = ttk.Combobox(
            sql_row, textvariable=self.export_format_var, state="readonly", width=42)
        self._export_format_cb.pack(side="left", padx=6)
        ttk.Button(
            sql_row, text="💾 Export", command=self.controller.cmd_export_sql
        ).pack(side="left", padx=(4, 10))
        ttk.Button(
            sql_row, text="➕ New Format…", command=lambda: self._open_new_format_dialog()
        ).pack(side="left")
        ttk.Button(
            sql_row, text="✏ Edit Selected…", command=self._open_edit_format_dialog
        ).pack(side="left", padx=(6, 0))
        ttk.Button(
            sql_row, text="Set Default", command=self._set_default_export_format
        ).pack(side="left", padx=(6, 0))
        self._cenfire_transfer_btn = ttk.Button(
            sql_row, text="Transfer Cenfire", command=self._run_cenfire_transfer,
            state="disabled")
        self._cenfire_transfer_btn.pack(side="left", padx=(6, 0))
        self._lamp_push_btn = ttk.Button(
            sql_row, text="Push LaMP SQL Dump", command=self._run_lamp_sql_push,
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

    def _build_mdb_row(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(row, text="Access DB (.mdb):").pack(side="left")
        self.mdb_path_var = tk.StringVar(
            value=app_settings.load_settings().get("mdb_path", ""))
        ttk.Entry(row, textvariable=self.mdb_path_var, width=38).pack(
            side="left", padx=6)
        ttk.Button(row, text="Browse…", command=self._mdb_browse).pack(
            side="left", padx=2)
        ttk.Button(row, text="Check", command=self._mdb_check).pack(
            side="left", padx=(8, 2))
        ttk.Button(row, text="Push to DB", command=self._mdb_push).pack(
            side="left", padx=2)
        ttk.Button(row, text="Set Default", command=self._set_default_mdb_path).pack(
            side="left", padx=(8, 2))
        self._mdb_status_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self._mdb_status_var, foreground="#6b7280",
                 font=("Segoe UI", 8), wraplength=620, justify="left").pack(
                 anchor="w", padx=10, pady=(0, 2))
    def _mdb_say(self, text: str):
        var = getattr(self, "_mdb_status_var", None)
        if var is not None:
            var.set(text)

    def _mdb_browse(self):
        path = filedialog.askopenfilename(
            title="Select the Access database to push results into",
            filetypes=[("Access database", "*.mdb *.accdb"), ("All files", "*.*")])
        if not path:
            return
        self.mdb_path_var.set(path)
        settings = app_settings.load_settings()
        settings["mdb_path"] = path
        app_settings.save_settings(settings)
        self._mdb_check()

    def _set_default_mdb_path(self):
        if not self._ata_folder:
            messagebox.showerror("No ATA Folder",
                                 "Load an ATA folder first — the default is "
                                 "saved there (ata_mdb_path.json).")
            return
        path = self.mdb_path_var.get().strip()
        if not path:
            messagebox.showerror("No Path", "Enter or Browse to an .mdb path first.")
            return
        mdb_export.save_mdb_path(self._ata_folder, path)
        self._update_mdb_default_label()
        self.controller.log("[RESULTS] Set default Access DB path for this ATA folder")

    def _update_mdb_default_label(self):
        var = getattr(self, "_mdb_default_lbl_var", None)
        if var is None:
            return
        folder = getattr(self, "_ata_folder", "") or ""
        saved = mdb_export.load_mdb_path(folder, default="") if folder else ""
        var.set(f"⭐ Default for this ATA folder: {saved}" if saved else
                "No per-folder default set yet — using the global last-picked path.")

    def _mdb_format(self):
        fmt = self.get_selected_export_format()
        if not fmt:
            self._mdb_say(
                "Pick an Export Format first — it says which table and columns "
                "to write.")
            return None
        if fmt.get("type") == "csv":
            self._mdb_say(
                f"'{fmt['name']}' is a CSV format — a database push needs a SQL "
                "format (one with a table and columns), such as the LaMP one.")
            return None
        return fmt

    def _mdb_check(self):
        fmt = self._mdb_format()
        if not fmt:
            return None
        info = mdb_export.preflight(getattr(self, "mdb_path_var", tk.StringVar()).get().strip(), fmt["table"])
        if not info["ok"]:
            self._mdb_say("✖  " + "  ".join(info["problems"]))
            self.controller.log("[RESULTS] Check failed — " + "; ".join(info["problems"]))
            return None
        missing = [c["field"] for c in fmt["columns"]
                   if c["field"].lower() not in {x.lower() for x in info["columns"]}]
        if missing:
            msg = (f"'{fmt['table']}' exists but has no column(s): "
                  f"{', '.join(missing)} — the format and the table disagree.")
            self._mdb_say(msg)
            self.controller.log("[RESULTS] " + msg)
            return None
        n = info["row_count"]
        self._mdb_say(
            f"✔  {os.path.basename(getattr(self, "mdb_path_var", tk.StringVar()).get())} — table "
            f"'{fmt['table']}' found"
            + (f", {n} row(s) already in it" if n is not None else "")
            + f".  Driver: {info['driver']}.")
        return info

    def _mdb_push(self):
        fmt = self._mdb_format()
        if not fmt:
            return
        if not self._mdb_check():
            return
        lot = self.lot_id.get().strip()
        if not lot:
            self._mdb_say("✖  Enter a Lot ID first — it is what "
                                     "fldTestSerial is computed from.")
            return
        wafer = self.wafer_id_var.get().strip()
        results = self.get_last_run_results()
        ata_folder = getattr(self, "_ata_folder", "") or ""
        fields, rows = mdb_export.build_rows(fmt, results, lot, wafer, ata_folder)
        if not rows:
            self._mdb_say(
                "✖  No rows from the last run match this format "
                "(it needs readings that carry a device ID).")
            return
        path = getattr(self, "mdb_path_var", tk.StringVar()).get().strip()
        if not messagebox.askokcancel(
                "Push to Database",
                f"Insert {len(rows)} row(s) into [{fmt['table']}]\nin "
                f"{path}?\n\nThis writes directly into that file. If it is the "
                "shared copy on the network, everyone reading it sees these "
                "rows straight away — there is no undo."):
            return
        res = mdb_export.push(path, fmt, results, lot, wafer, folder=ata_folder)
        if res["ok"]:
            msg = (f"Pushed {res['inserted']} row(s) into "
                  f"[{res['table']}] — lot {lot}"
                  + (f", wafer {wafer}" if wafer else "") + ".")
            self._mdb_say(msg)
            self.controller.log("[RESULTS] " + msg)
        else:
            self._mdb_say("✖  " + res["error"])
            self.controller.log("[RESULTS] Push failed — " + res["error"])

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

    def _on_results_map_click(self, event):
        wm = self._results_wafer_map
        cx, cy = wm.canvas.canvasx(event.x), wm.canvas.canvasy(event.y)
        rc = wm._hit_die(cx, cy)
        if rc is None:
            return
        self._results_show_die(rc)

    def _results_show_die(self, rc):
        wm = getattr(self, "_results_wafer_map", None)
        if wm is None:
            return
        prev = self._results_selected_rc
        if prev is not None and prev in wm.dies:
            try:
                wm.canvas.itemconfig(wm.dies[prev], width=1)
            except Exception:
                pass
        self._results_selected_rc = rc
        if rc in wm.dies:
            try:
                wm.canvas.itemconfig(wm.dies[rc], width=3)
            except Exception:
                pass
        row, col = rc
        matches = [r for r in self.controller.results_data
                  if r.get("row") == row and r.get("col") == col]
        die_id = (self._exec_overlay_die_ids.get(rc, "")
                 or wm.die_ids.get(rc, ""))
        if not die_id:
            die_id = next((r.get("die") for r in matches if r.get("die")), "")
        die_desc = f"{die_id} (R{row}C{col})" if die_id else f"R{row}C{col}"
        self._results_die_var.set(
            f"Die {die_desc} — {len(matches)} reading(s)" if matches
            else f"Die {die_desc} — no measurements recorded yet.")
        for iid in self._results_die_tree.get_children():
            self._results_die_tree.delete(iid)
        for r in matches:
            self._results_die_tree.insert("", "end", values=(
                r.get("step"), r.get("type"), r.get("value"), r.get("unit")))


    def _refresh_export_formats(self, select_name: str = None):
        if not self._ata_folder:
            self._export_formats = []
            self._export_format_cb.config(values=[])
            self.export_format_var.set("")
            self._update_default_format_label(None)
            return
        self._export_formats = xfmt.load_formats(self._ata_folder, system=self._system)
        names = [f["name"] for f in self._export_formats]
        self._export_format_cb.config(values=names)
        default_name = xfmt.get_default_format_name(self._ata_folder, system=self._system)
        default_export_path = xfmt.get_default_export_path(self._ata_folder, system=self._system)
        if default_export_path and os.path.isdir(default_export_path):
            self.export_path_var.set(default_export_path)
        elif default_export_path:
            self.controller.log(
                "[RESULTS] This project's saved export directory "
                "doesn't exist on this machine")
        if select_name in names:
            self.export_format_var.set(select_name)
        elif self.export_format_var.get() not in names:
            self.export_format_var.set(default_name if default_name in names
                                       else (names[0] if names else ""))
        self._update_default_format_label(default_name)

    def _update_default_format_label(self, default_name):
        var = getattr(self, "_export_default_lbl_var", None)
        if var is None:
            return
        var.set(f"Default: {default_name}" if default_name else "No default format set.")

    def _set_default_export_format(self):
        from tkinter import messagebox
        if not self._ata_folder:
            messagebox.showerror("No ATA Folder", "Load an ATA folder first.")
            return
        fmt = self.get_selected_export_format()
        if not fmt:
            messagebox.showerror("No Format Selected",
                                 "Pick a format from the Export Format dropdown first.")
            return
        current_path = self.export_path_var.get()
        home = os.path.expanduser("~")
        if current_path and os.path.normcase(current_path).startswith(os.path.normcase(home)):
            if not messagebox.askyesno(
                "Personal Folder as Shared Default",
                f"{current_path!r} is inside THIS computer's own user "
                "folder - saving it as this project's default export "
                "directory will point every other computer that opens "
                "this ATA folder at a path that doesn't exist for them "
                "(their own account, not this one).\n\n"
                "Set it as the default anyway?"):
                return
        xfmt.set_default_format_name(self._ata_folder, fmt["name"], system=self._system)
        xfmt.set_default_export_path(self._ata_folder, self.export_path_var.get(),
                                     system=self._system)
        self._update_default_format_label(fmt["name"])
        self.controller.log(f"[RESULTS] '{fmt['name']}' and export path "
                            "set as default for this project.")

    def _is_cenfire_folder(self) -> bool:
        return bool(self._ata_folder) and os.path.basename(
            self._ata_folder).lower().startswith("cenfire")

    def _refresh_cenfire_transfer_button(self):
        btn = getattr(self, "_cenfire_transfer_btn", None)
        if btn is None:
            return
        btn.config(state="normal" if self._is_cenfire_folder() else "disabled")

    def _run_cenfire_transfer(self):
        import subprocess
        from tkinter import messagebox
        try:
            subprocess.Popen(
                'start "AzTransfer" powershell -ExecutionPolicy Bypass '
                '-file "C:/AzTransfer/AzTransfer.ps1"', shell=True)
            self.controller.log(
                "[RESULTS] Launched AzTransfer (Cenfire data transfer) in "
                "its own terminal window.")
        except Exception as exc:
            messagebox.showerror("Transfer Cenfire",
                                 f"Could not launch AzTransfer:\n{exc}")

    def _is_lamp_folder(self) -> bool:
        return bool(self._ata_folder) and os.path.basename(
            self._ata_folder).lower().startswith("lamp")

    def _refresh_lamp_push_button(self):
        btn = getattr(self, "_lamp_push_btn", None)
        if btn is None:
            return
        btn.config(state="normal" if self._is_lamp_folder() else "disabled")

    def _run_lamp_sql_push(self):
        from tkinter import messagebox
        dump_dir = mdb_export.LAMP_SQL_DUMP_DIR
        mdb_path = mdb_export.LAMP_MDB_PATH
        if not os.path.isdir(dump_dir):
            messagebox.showerror(
                "Push LaMP SQL Dump", f"Dump folder not found:\n{dump_dir}")
            return
        sql_files = [f for f in os.listdir(dump_dir) if f.lower().endswith(".sql")]
        if not sql_files:
            messagebox.showinfo(
                "Push LaMP SQL Dump", f"No .sql files waiting in:\n{dump_dir}")
            return
        if not messagebox.askokcancel(
                "Push LaMP SQL Dump",
                f"Push {len(sql_files)} .sql file(s) from\n{dump_dir}\n"
                f"into the database at\n{mdb_path}?\n\n"
                "Each file's rows are inserted all-or-nothing, then the "
                "file is moved into a 'Pushed' subfolder so it never gets "
                "pushed twice. This writes directly into the shared "
                "database — there is no undo."):
            return
        res = mdb_export.push_sql_dump_folder(mdb_path, dump_dir)
        if res.get("error") and not res["files"]:
            messagebox.showerror("Push LaMP SQL Dump", res["error"])
            self.controller.log(f"[RESULTS] Push failed — {res['error']}")
            return
        ok_files = [f for f in res["files"] if f["ok"]]
        bad_files = [f for f in res["files"] if not f["ok"]]
        msg = f"Pushed {res['total_rows']} row(s) from {len(ok_files)} file(s)."
        if bad_files:
            msg += ("\n" + f"{len(bad_files)} file(s) FAILED and were left "
                    "in place:\n" +
                    "\n".join(f"  {b['file']}: {b['error']}" for b in bad_files))
        self.controller.log("[RESULTS] " + msg.replace("\n", "  "))
        if bad_files:
            messagebox.showwarning("Push LaMP SQL Dump", msg)
        else:
            messagebox.showinfo("Push LaMP SQL Dump", msg)

    def get_selected_export_format(self):
        name = self.export_format_var.get()
        return next((f for f in self._export_formats if f["name"] == name), None)

    def _open_edit_format_dialog(self):
        from tkinter import messagebox
        fmt = self.get_selected_export_format()
        if not fmt:
            messagebox.showerror("No Format Selected",
                                 "Pick a format from the Export Format dropdown first.")
            return
        self._open_new_format_dialog(existing_fmt=fmt)

    def _open_new_format_dialog(self, existing_fmt=None):
        from tkinter import messagebox
        if not self._ata_folder:
            messagebox.showerror(
                "No ATA Folder",
                "Load an ATA folder first — export formats are saved there "
                "(ata_export_formats.json).")
            return

        dlg = tk.Toplevel(self)
        dlg.title("Edit Export Format" if existing_fmt else "New Export Format")
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(True, True)

        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Format Name:").grid(row=0, column=0, sticky="e", pady=2)
        name_var = tk.StringVar(value=(existing_fmt or {}).get("name", ""))
        ttk.Entry(frm, textvariable=name_var, width=46).grid(
            row=0, column=1, columnspan=3, sticky="w", pady=2)

        ttk.Label(frm, text="Append Name:").grid(row=1, column=0, sticky="e", pady=2)
        table_var = tk.StringVar(value=(existing_fmt or {}).get("table", ""))
        ttk.Entry(frm, textvariable=table_var, width=46).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=2)
        ttk.Label(frm, text="Lot+Wafer join:").grid(row=1, column=4, sticky="e", pady=2)
        wafer_join_var = tk.StringVar(value=(existing_fmt or {}).get("wafer_join", "_"))
        ttk.Entry(frm, textvariable=wafer_join_var, width=6).grid(
            row=1, column=5, sticky="w", pady=2)

        ttk.Label(frm, text="Format Type:").grid(row=2, column=0, sticky="e", pady=2)
        type_var = tk.StringVar(value=(existing_fmt or {}).get("type", "sql"))
        type_row = ttk.Frame(frm)
        type_row.grid(row=2, column=1, columnspan=3, sticky="w", pady=2)
        ttk.Radiobutton(type_row, text="SQL INSERT",
                       variable=type_var, value="sql",
                       command=lambda: _on_type_change()).pack(side="left")
        ttk.Radiobutton(type_row, text="CSV",
                       variable=type_var, value="csv",
                       command=lambda: _on_type_change()).pack(side="left", padx=(12, 0))
        per_step_var = tk.BooleanVar(value=(existing_fmt or {}).get("per_step", False))
        per_step_chk = ttk.Checkbutton(
            type_row, text="One row per test (Peanut)",
            variable=per_step_var, command=lambda: _on_type_change())
        per_step_chk.pack(side="left", padx=(12, 0))
        append_date_var = tk.BooleanVar(value=(existing_fmt or {}).get("append_date", False))
        ttk.Checkbutton(type_row, text="+ date",
                       variable=append_date_var).pack(side="left", padx=(20, 0))
        append_time_var = tk.BooleanVar(value=(existing_fmt or {}).get("append_time", False))
        ttk.Checkbutton(type_row, text="+ time",
                       variable=append_time_var).pack(side="left", padx=(8, 0))
        append_recipe_var = tk.BooleanVar(value=(existing_fmt or {}).get("append_recipe", False))
        ttk.Checkbutton(type_row, text="+ recipe name",
                       variable=append_recipe_var).pack(side="left", padx=(8, 0))

        only_pma_var = tk.BooleanVar(value=(existing_fmt or {}).get("requires_die_id", True))
        only_pma_chk = ttk.Checkbutton(
            frm, text="Only include readings that have a die ID",
            variable=only_pma_var)
        only_pma_chk.grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 8))

        detect_hint = tk.StringVar()
        ttk.Label(frm, text="Available fields:").grid(
            row=4, column=0, columnspan=4, sticky="w")
        avail_row = ttk.Frame(frm)
        avail_row.grid(row=6, column=0, columnspan=4, sticky="nsew", pady=(2, 6))
        avail_list = tk.Listbox(avail_row, height=6, width=58, exportselection=False)
        avail_list.pack(side="left", fill="both", expand=True)
        ttk.Button(avail_row, text="Add Selected →",
                  command=lambda: _add_from_available()).pack(side="left", padx=(6, 0), anchor="n")
        avail_sources: list = []

        ttk.Label(frm, text="Columns:").grid(
            row=7, column=0, columnspan=4, sticky="w")
        cols_tree = ttk.Treeview(
            frm, columns=("field", "source", "quote", "transform"),
            show="headings", height=7)
        for cid, text, width in [("field", "Field Name", 130), ("source", "Source", 130),
                                 ("quote", "Quote", 55), ("transform", "Transform", 110)]:
            cols_tree.heading(cid, text=text)
            cols_tree.column(cid, width=width, anchor="w" if cid == "field" else "center")
        cols_tree.grid(row=8, column=0, columnspan=4, sticky="nsew", pady=(2, 6))

        order_row = ttk.Frame(frm)
        order_row.grid(row=9, column=0, columnspan=4, sticky="w")
        ttk.Button(order_row, text="▲", command=lambda: move_col(-1)).pack(side="left")
        ttk.Button(order_row, text="▼", command=lambda: move_col(1)).pack(
            side="left", padx=(6, 0))
        ttk.Button(order_row, text="🗑 Remove", command=lambda: remove_col()).pack(
            side="left", padx=(6, 0))
        ttk.Button(order_row, text="✎ Update Selected",
                  command=lambda: update_col()).pack(side="left", padx=(6, 0))

        add_row = ttk.Frame(frm)
        add_row.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(add_row, text="Field:").pack(side="left")
        field_var = tk.StringVar()
        ttk.Entry(add_row, textvariable=field_var, width=14).pack(side="left", padx=(2, 8))
        ttk.Label(add_row, text="Source:").pack(side="left")
        source_var = tk.StringVar()
        source_cb = ttk.Combobox(add_row, textvariable=source_var, width=14)
        source_cb.pack(side="left", padx=(2, 8))
        quote_var = tk.BooleanVar(value=False)
        quote_chk = ttk.Checkbutton(add_row, text="Quote", variable=quote_var)
        quote_chk.pack(side="left")

        add_row2 = ttk.Frame(frm)
        add_row2.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        ttk.Label(add_row2, text="Multiply by:").pack(side="left")
        multiply_var = tk.StringVar()
        ttk.Entry(add_row2, textvariable=multiply_var, width=8).pack(side="left", padx=(2, 12))
        ttk.Label(add_row2, text="Or always use constant:").pack(side="left")
        constant_var = tk.StringVar()
        ttk.Entry(add_row2, textvariable=constant_var, width=14).pack(side="left", padx=(2, 8))
        ttk.Label(add_row2, text="Round to decimals:").pack(side="left", padx=(12, 0))
        round_var = tk.StringVar()
        ttk.Entry(add_row2, textvariable=round_var, width=4).pack(side="left", padx=(2, 8))
        ttk.Button(add_row2, text="＋ Add Column", command=lambda: add_col()).pack(
            side="left", padx=(8, 0))

        add_row3 = ttk.Frame(frm)
        add_row3.grid(row=12, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        ttk.Label(add_row3, text="Or template (combine fields, "
                                 "e.g. {intra_col}-{intra_row}-{shot_col}-{shot_row}):"
                 ).pack(side="left")
        template_var = tk.StringVar()
        ttk.Entry(add_row3, textvariable=template_var, width=44).pack(
            side="left", padx=(4, 0))

        lookup_lf = ttk.LabelFrame(frm, text="Lookup Table")
        lookup_lf.grid(row=13, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        _lu = (existing_fmt or {}).get("lookup") or {}
        lu_file_var = tk.StringVar(value=_lu.get("file", ""))
        lu_row_col_var = tk.StringVar(value=_lu.get("lookup_row_col", ""))
        lu_col_col_var = tk.StringVar(value=_lu.get("lookup_col_col", ""))
        lu_our_row_var = tk.StringVar(value=_lu.get("our_row_field", "abs_row"))
        lu_our_col_var = tk.StringVar(value=_lu.get("our_col_field", "abs_col"))
        lu_key_field_var = tk.StringVar(value=_lu.get("key_field", ""))
        lu_key_col_var = tk.StringVar(value=_lu.get("lookup_key_col", ""))
        lu_row0 = ttk.Frame(lookup_lf)
        lu_row0.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(lu_row0, text="Or match by a trusted ID string instead — "
                                "this format's own field:").pack(side="left")
        ttk.Entry(lu_row0, textvariable=lu_key_field_var, width=14).pack(
            side="left", padx=(2, 12))
        ttk.Label(lu_row0, text="against the CSV's own ID column:").pack(side="left")
        ttk.Entry(lu_row0, textvariable=lu_key_col_var, width=14).pack(
            side="left", padx=(2, 0))
        lu_row1 = ttk.Frame(lookup_lf)
        lu_row1.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(lu_row1, text="CSV filename:").pack(side="left")
        ttk.Entry(lu_row1, textvariable=lu_file_var, width=28).pack(
            side="left", padx=(2, 12))
        ttk.Label(lu_row1, text="Its row/col columns:").pack(side="left")
        ttk.Entry(lu_row1, textvariable=lu_row_col_var, width=14).pack(
            side="left", padx=(2, 4))
        ttk.Entry(lu_row1, textvariable=lu_col_col_var, width=14).pack(
            side="left", padx=(2, 0))
        lu_row2 = ttk.Frame(lookup_lf)
        lu_row2.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(lu_row2, text="Matched against this format's own:").pack(side="left")
        ttk.Entry(lu_row2, textvariable=lu_our_row_var, width=14).pack(
            side="left", padx=(2, 4))
        ttk.Entry(lu_row2, textvariable=lu_our_col_var, width=14).pack(
            side="left", padx=(2, 0))
        _NICE = {"dmm": "DMM", "id": "ID", "num": "Num"}

        def _default_field_name(source):
            return "_".join(_NICE.get(p, p.capitalize()) for p in source.split("_"))

        def _fields_for_type():
            if type_var.get() == "csv" and per_step_var.get():
                return xfmt.SQL_SOURCE_FIELDS
            return xfmt.SOURCE_FIELDS_BY_TYPE.get(type_var.get(), {})

        def _populate_available():
            avail_list.delete(0, "end")
            avail_sources.clear()
            fields = _fields_for_type()
            source_cb.config(values=list(fields))
            if source_var.get() not in fields:
                source_var.set(next(iter(fields), ""))
            results = self.controller.results_data
            if type_var.get() == "csv" and not per_step_var.get():
                populated = {"lot_id", "wafer_id", "test_serial"}
                for g in xfmt.group_results_by_die(results):
                    for k, v in g.items():
                        if v not in (None, ""):
                            populated.add(k)
                for source, desc in fields.items():
                    mark = "✓" if source in populated else " "
                    avail_list.insert("end", f"[{mark}] {source}  —  {desc}")
                    avail_sources.append(source)
                detect_hint.set("✓ = this field has data in the current Results tab right now.")
            else:
                kinds = xfmt.detect_reading_kinds(results)
                for source, desc in fields.items():
                    avail_list.insert("end", f"{source}  —  {desc}")
                    avail_sources.append(source)
                if kinds:
                    row_desc = ("row" if type_var.get() == "sql"
                               else "CSV row (one row per test, not merged by die)")
                    detect_hint.set(
                        "Reading kinds detected in current Results: " +
                        ", ".join(k["label"] for k in kinds) +
                        f".  Each {row_desc} is ONE reading — turn off \"One row per "
                        "test\" to merge them into one row per die instead.")
                else:
                    detect_hint.set(
                        "No results captured yet — run a recipe first, or pick "
                        "sources manually below.")

        def _on_type_change():
            is_csv = type_var.get() == "csv"
            if is_csv:
                per_step_chk.pack(side="left", padx=(12, 0))
                quote_chk.pack_forget()
            else:
                per_step_chk.pack_forget()
                quote_chk.pack(side="left")
            if is_csv and not per_step_var.get():
                only_pma_chk.grid_remove()
            else:
                only_pma_chk.grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 8))
            _populate_available()

        def _add_from_available(_evt=None):
            sel = avail_list.curselection()
            if not sel:
                return
            source = avail_sources[sel[0]]
            field_var.set(_default_field_name(source))
            source_var.set(source)
            add_col()
        avail_list.bind("<Double-Button-1>", _add_from_available)

        def _parse_transform(txt):
            txt = (txt or "").strip()
            result = {}
            m = re.search(r"~(\d+)\s*$", txt)
            if m:
                result["round"] = int(m.group(1))
                txt = txt[:m.start()].strip()
            if txt.startswith("="):
                result["constant"] = txt[1:].strip()
                return result
            if txt[:1] in ("×", "x", "X"):
                try:
                    result["multiply"] = float(txt[1:].strip())
                except ValueError:
                    pass
                return result
            if "{" in txt and "}" in txt:
                result["template"] = txt
                return result
            return result

        def _col_from_editor():
            field = field_var.get().strip()
            source = source_var.get().strip()
            constant = constant_var.get().strip()
            mult = multiply_var.get().strip()
            template = template_var.get().strip()
            rnd = round_var.get().strip()
            if not field or not (source or constant or template):
                return None
            transform_txt = (template if template else
                            (f"={constant}" if constant else
                             (f"×{mult}" if mult else "")))
            if rnd:
                try:
                    int(rnd)
                    transform_txt = (transform_txt + f" ~{rnd}").strip()
                except ValueError:
                    messagebox.showerror("Invalid", "Round to decimals must be a whole number.")
                    return None
            return (field, source, "yes" if quote_var.get() else "no", transform_txt)

        def _clear_col_editor():
            field_var.set("")
            multiply_var.set("")
            constant_var.set("")
            template_var.set("")
            round_var.set("")

        def add_col():
            row = _col_from_editor()
            if row is None:
                return
            cols_tree.insert("", "end", values=row)
            _clear_col_editor()

        def update_col():
            sel = cols_tree.selection()
            if not sel:
                messagebox.showinfo("No Selection", "Select a column to update.")
                return
            row = _col_from_editor()
            if row is None:
                return
            cols_tree.item(sel[0], values=row)

        def remove_col():
            sel = cols_tree.selection()
            if sel:
                cols_tree.delete(sel[0])

        def move_col(delta):
            sel = cols_tree.selection()
            if not sel:
                return
            iid = sel[0]
            idx = cols_tree.index(iid)
            cols_tree.move(iid, "", idx + delta)

        def _col_to_editor(_evt=None):
            sel = cols_tree.selection()
            if not sel:
                return
            f, src, q, tr = cols_tree.item(sel[0], "values")
            field_var.set(f)
            source_var.set(src)
            quote_var.set(q == "yes")
            parsed = _parse_transform(tr)
            multiply_var.set(str(parsed["multiply"]) if "multiply" in parsed else "")
            constant_var.set(parsed.get("constant", ""))
            template_var.set(parsed.get("template", ""))
            round_var.set(str(parsed["round"]) if "round" in parsed else "")
        cols_tree.bind("<<TreeviewSelect>>", _col_to_editor)

        if existing_fmt:
            for c in existing_fmt.get("columns", []):
                tr = ""
                if c.get("constant") not in (None, ""):
                    tr = f"={c['constant']}"
                elif c.get("template"):
                    tr = c["template"]
                elif c.get("multiply") not in (None, "", 1, 1.0):
                    tr = f"×{c['multiply']}"
                if c.get("round") not in (None, ""):
                    tr = (tr + f" ~{c['round']}").strip()
                cols_tree.insert("", "end", values=(
                    c.get("field", ""), c.get("source", ""),
                    "yes" if c.get("quote") else "no", tr))

        def save():
            name = name_var.get().strip()
            table = table_var.get().strip()
            if not name or not table:
                messagebox.showerror("Incomplete", "Format Name and Table Name are required.")
                return
            columns = []
            for iid in cols_tree.get_children():
                f, src, q, tr = cols_tree.item(iid, "values")
                col = {"field": f, "source": src, "quote": q == "yes"}
                col.update(_parse_transform(tr))
                columns.append(col)
            if not columns:
                messagebox.showerror("Incomplete", "Add at least one column.")
                return
            fmt = {"name": name, "table": table, "type": type_var.get(),
                  "requires_die_id": only_pma_var.get(), "append_date": append_date_var.get(),
                  "append_time": append_time_var.get(), "append_recipe": append_recipe_var.get(),
                  "per_step": per_step_var.get() if type_var.get() == "csv" else False,
                  "columns": columns}
            wafer_join = wafer_join_var.get()
            if wafer_join and wafer_join != "_":
                fmt["wafer_join"] = wafer_join
            lu_file = lu_file_var.get().strip()
            lu_key_field = lu_key_field_var.get().strip()
            lu_key_col = lu_key_col_var.get().strip()
            if lu_file and lu_key_field:
                fmt["lookup"] = {
                    "file": lu_file, "key_field": lu_key_field,
                    "lookup_key_col": lu_key_col or lu_key_field,
                }
            elif lu_file:
                fmt["lookup"] = {
                    "file": lu_file,
                    "lookup_row_col": lu_row_col_var.get().strip() or "row",
                    "lookup_col_col": lu_col_col_var.get().strip() or "col",
                    "our_row_field": lu_our_row_var.get().strip() or "abs_row",
                    "our_col_field": lu_our_col_var.get().strip() or "abs_col",
                }
            xfmt.add_format(self._ata_folder, fmt, system=self._system)
            self._refresh_export_formats(select_name=name)
            self.controller.log(f"[RESULTS] Saved export format '{name}' ({table}, "
                                f"{type_var.get()}) to ATA folder.")
            dlg.destroy()

        def delete():
            if not messagebox.askyesno(
                "Delete Export Format",
                f"Delete export format '{existing_fmt['name']}'? This cannot be undone."
            ):
                return
            xfmt.delete_format(self._ata_folder, existing_fmt["name"], system=self._system)
            self.controller.log(f"[RESULTS] Deleted export format '{existing_fmt['name']}'.")
            self._refresh_export_formats()
            dlg.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=14, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Save Format", command=save).pack(side="left")
        if existing_fmt is not None:
            ttk.Button(btns, text="🗑 Delete", command=delete).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")

        _on_type_change()
        dlg.update_idletasks()
        dlg.grab_set()

    def draw_donut(self, canvas, size, passed, failed, untested):
        canvas.delete("all")
        cx, cy = size / 2, size / 2
        r_outer, r_inner = size * 0.45, size * 0.25
        total = passed + failed + untested or 1
        start = 90
        for count, color in [(passed, "#00d200"), (failed, "red"), (untested, "#d0d0d0")]:
            if count > 0:
                extent = (count / total) * 360
                canvas.create_arc(
                    cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer,
                    start=start, extent=-extent, fill=color, outline=""
                )
                start -= extent
        canvas.create_oval(
            cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner,
            fill="#f0f0f0", outline=""
        )
        pct = int(((passed + failed) / total) * 100) if total > 1 else 0
        font_size = 11 if size < 150 else 24
        canvas.create_text(cx, cy, text=f"{pct}%", font=("Arial", font_size, "bold"), fill="#333333")
