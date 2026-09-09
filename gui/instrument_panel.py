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
from cassette_panel import CassettePanel, save_yield_threshold, load_yield_threshold
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
        # Shared with the Wafer Builder tab's Die Map "Label min width (px):"
        # control (recipe_gen_panel.py binds its Spinbox to this SAME
        # Variable, not a copy) - one knob for die-ID-label zoom thresholds
        # on both maps. See _exec_labels_fit for how the Run tab uses it.
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

        # Not packed - the separate "Prober: connected/not connected/ready/
        # not ready" line is gone; status_label above (PENDING/SYSTEM READY)
        # already covers whether the prober is usable. Left instantiated
        # (just not shown) so AtomicaDashboard._update_prober_status_label
        # doesn't need touching everywhere it's called from.
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
        """Show only these in the sidebar roster, in the declared order.

        An instrument that is neither fitted nor pinged has no status to
        report, so listing it just adds a permanently grey row. The bench
        profile decides which those are, and it changes when the prober
        selector changes - hence re-applied on every connect sweep rather
        than fixed at build time.

        A name never seen before gets a row created here, on the spot -
        the fixed roster (ACCRETECH_INSTRUMENT_NAMES/ELECTROGLAS_
        INSTRUMENT_NAMES) only ever covers instruments this project has a
        real driver class for; a custom instrument added on the Setup tab
        (Accretech "+ Add Instrument", no driver required - see
        accretech_profiles.GENERIC_MODEL) has no such fixed entry to match,
        so without this it would connect fine but never appear here at all.
        """
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
                # before= keeps the Refresh button at the bottom; a bare
                # pack() would re-append the label underneath it.
                lbl.pack(anchor="w", padx=4, pady=2,
                         before=self._refresh_conn_btn)

    def set_bench_label(self, bench: str = ""):
        """Name the bench this roster is reporting on, in the frame's title.

        The Electroglas benches carry different instruments, so a roster with
        no bench on it is genuinely ambiguous - "Keithley 2400" missing could
        mean broken or simply not fitted here.
        """
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

        # Run leads on both systems - it is what an operator opens the GUI to
        # do; the setup tabs behind it are visited far less often.
        self._tab_execution2(main_nb)
        if self._system == "accretech":
            # Cassette's export controls read self.export_format_var, which
            # _tab_results creates - built last so that already exists, then
            # both are moved to sit right after Run to match the intended
            # tab order (Run, Results, Cassette, Wafer Builder, Probe Card,
            # Recipe, Internal) without adding a second build-order
            # dependency.
            self._tab_pma_wafer(main_nb)
            self._tab_probe_card(main_nb)
            self._tab_recipe(main_nb)
            self._tab_wafer_map(main_nb)
            self._tab_results(main_nb)
            self._tab_cassette(main_nb)
            main_nb.insert(1, self.results_tab_frame)
            main_nb.insert(2, self.cassette_panel.master)
        else:
            # Electroglas has no cassette handling - order is Run, Results,
            # Recipe, Probe Card, PMA Process, Wafer Builder, Internal.
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

        # Scrollable - the SMU section (2636B card + the Keithley 2400
        # card below it) plus DMM/WGEN/addresses run taller than the
        # window on a lot of real screens, and this tab had no scrollbar
        # at all before, so anything past the visible area was simply
        # unreachable. Canvas + Scrollbar is the standard Tk pattern (ttk
        # has no built-in scrollable frame) - `inner` is NOT stretched to
        # the canvas's own height (only its width, via the <Configure>
        # binding below), so it - and the PanedWindow inside it - size
        # to their natural content height and the canvas scrolls the
        # difference, instead of the PanedWindow being squashed into
        # whatever the tab's own fixed height happened to be.
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

        tab = inner  # everything below this point was already written
                    # against `tab` - redirect it into the scrollable
                    # inner frame instead of the (now just a scrollbar
                    # mount) outer one, with no other lines to touch.

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

        # Ping/address section - a diagnostic, not the first thing an
        # operator needs - sits below the instrument control panels.
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
        """Single-channel manual-control card for a Keithley 2400 - was
        missing entirely; the only SMU section here was hardcoded to the
        2636B (dual-channel, TSP-scripted). Shares the same "smu" driver
        slot the 2636B card above targets (only one physical SMU is ever
        really plugged into a given bench at a time), reusing
        _build_smu_channel with its own state key so the two cards'
        widgets/dicts don't collide - see that method's own docstring."""
        card = ttk.LabelFrame(parent, text="Keithley 2400  (SMU)")
        card.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        card.columnconfigure(0, weight=1)

        # Front/Rear terminal select - only meaningful for a driver that
        # actually has set_terminals (Keithley2400 does; see that
        # driver's own comment). A switch-matrix-routed probe card is
        # wired to REAR; a hand-clipped DIRECT-wired one (e.g. Peanut's
        # probe08, 2026-09) is typically FRONT instead - this used to be
        # a fixed class constant with no way to flip it without editing
        # code, which is exactly backwards for a recipe set that's
        # switching between the two.
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
        """ch is this panel's own STATE key - what indexes
        _smu_output_lf/_smu_level_vars/_smu_cont_active/_smu_last, and
        what shows up in log lines. drv_channel is what actually gets
        passed to the driver's set_voltage()/measure_current()/etc, and
        defaults to ch when not given.

        These have to be allowed to differ: the Keithley 2400 card (see
        _build_smu2400_card) reuses this same builder with its own state
        key ("smu2400") so it doesn't collide with the 2636B card's
        "smua"/"smub" dict entries above it, but still has to pass a
        LITERAL "smua"/"smub" as drv_channel - a 2636B driver interpolates
        that string directly into a TSP command (channel.source.func =
        ...), so anything else sent to one would be a bad command,
        whereas a 2400 driver ignores the channel argument entirely and
        so accepts any string here safely.
        """
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
            # The Keithley 2400 card's own widgets - the hardware output
            # is already off from the "smua" pass above (it shares the
            # same physical channel, see _build_smu2400_card), this just
            # brings that card's displayed level/label back in sync with
            # it instead of leaving them showing stale ON/nonzero state.
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
        """Release every connected instrument's remote lock so its own
        front-panel keys work again - one GTL per instrument, not a single
        bus-wide REN deassert, so nothing else on the same GPIB line gets
        pulled out of remote along with it. Session stays open; the next
        write/query from this app re-asserts remote automatically."""
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
        """Which prober the GUI comes up on, across BOTH systems.

        Deliberately not per-system: the point is to decide whether the app
        starts on Accretech or Electroglas at all, so one list spans both and
        picking an entry sets the system as well as the bench.
        """
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
        """Same setting as the Cassette tab's own "Pass yield >= ...%"
        Entry (cassette_panel.CassettePanel) - a per-ATA-folder default,
        not per-machine (see save_yield_threshold's own comment: different
        projects have different real yield expectations). Surfaced here
        too, next to every other per-folder default this tab already
        manages (Set as Default ATA folder, default prober), since that's
        where an operator looking for "where do I set a default" would
        naturally check first - the Cassette tab's Entry already
        auto-saves on edit, this is a second, explicit place to see/set
        the same value, not a second source of truth for it. Accretech
        only - Electroglas has no Cassette tab at all.
        """
        lf = ttk.Frame(parent)
        lf.pack(fill="x", pady=(0, 4))

        ttk.Label(lf, text="Cassette pass-yield default — "
                          "Pass yield ≥").pack(side="left", padx=(0, 2))
        self._default_yield_var = tk.StringVar(value="0")
        ttk.Entry(lf, textvariable=self._default_yield_var, width=5).pack(
            side="left", padx=(0, 8))
        ttk.Button(lf, text="Set Default",
                  command=self._set_default_yield).pack(side="left", padx=(0, 10))

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
        # Keep the Cassette tab's own Entry in sync immediately, same
        # session - both are the same setting, not two independent ones.
        cassette = getattr(self, "cassette_panel", None)
        if cassette is not None:
            cassette._yield_folder = self._ata_folder
            cassette._yield_var.set(f"{pct:g}")
        self._update_default_yield_label()
        self.controller.log(
            f"[SYSTEM] Cassette pass-yield default set to {pct:g}% for "
            f"'{os.path.basename(self._ata_folder)}'.")

    def _prober_choices(self) -> list:
        """[(label, system, bench)] for every prober on both systems."""
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
        # Switch to it now too, so the setting is visibly what you just chose
        # rather than something that only takes effect next launch.
        self.controller.apply_prober(system, bench)

    def _clear_default_prober(self):
        app_settings.clear_default_prober()
        self._default_prober_var.set("")
        self._update_default_prober_label()
        self.controller.log("[SYSTEM] Default prober cleared.")

    def _build_working_dir_row(self, parent):
        """Where ATA folders are looked for/created - was previously the
        first thing in the toolbar row, now grouped with the other
        startup/default settings below it in one Settings section instead
        of living apart from them."""
        lf = ttk.Frame(parent)
        lf.pack(fill="x", pady=(0, 4))

        ttk.Label(lf, text="Working Directory:").pack(side="left", padx=(0, 4))
        # Preset picker (automationproject / proberautomation) - the Entry
        # next to it still shows/accepts the full path either way (a preset
        # just fills it in), so a one-off custom path via Browse still works.
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
        tab.rowconfigure(3, weight=1)          # the file list / map split
        tab.columnconfigure(0, weight=1)

        ctrl = ttk.Frame(tab)
        ctrl.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))

        ttk.Button(ctrl, text="📁 Load ATA Folder…",
                  command=self.controller.cmd_import_map).pack(side="left", padx=(0, 10))
        ttk.Button(ctrl, text="＋ New ATA Folder…",
                  command=self.controller.cmd_new_ata_folder).pack(side="left", padx=(0, 10))
        ttk.Button(ctrl, text="Set Default",
                  command=self._set_default_ata_folder).pack(side="left", padx=(0, 10))
        # Moved in from the top toolbar's "ATA Folder:" row.
        ttk.Button(ctrl, text="↻ Refresh",
                  command=self.controller.cmd_refresh_ata).pack(side="left", padx=(0, 10))

        self._ata_path_lbl = ttk.Label(ctrl, text="No folder selected", foreground="gray")
        self._ata_path_lbl.pack(side="left", padx=10)

        self._default_ata_lbl = ttk.Label(ctrl, text="", foreground="#374151",
                                          font=("Segoe UI", 8, "italic"))
        self._default_ata_lbl.pack(side="left", padx=(0, 10))
        self._update_default_ata_label()

        # One Settings section instead of three separate LabelFrames
        # (Default prober / Cassette pass-yield / Working Directory used
        # to each get their own row) - same settings, just grouped under
        # one heading.
        settings_lf = ttk.LabelFrame(tab, text="Settings", padding=6)
        settings_lf.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))
        self._build_working_dir_row(settings_lf)
        self._build_default_prober_row(settings_lf)
        if self._system == "accretech":
            self._build_default_yield_row(settings_lf)

        # Same fix as the Run tab's _exec_map_source_var: Electroglas has no
        # hardware-extracted map of its own - the legacy "Electroglas" source
        # (ata_wafer_map_electroglas.csv) is whatever the old PMA Process
        # extraction last wrote, often stale or a placeholder rectangle (see
        # MADDYATA). There is no picker for this var - it is set once here
        # and never touched again - so getting the default right matters.
        self._map_source_var = tk.StringVar(
            value="Accretech" if self._system == "accretech" else "Wafer Builder")

        # One tree, the whole tab - a flat file list plus a wafer map (which
        # every other tab already shows) told you less about the FOLDER
        # itself than a real directory/recipe/probe-card breakdown does.
        # Real Treeview nesting (not "── Section ──" fake header rows) for
        # the tree feel: folder -> key files / probe cards+recipes per
        # system / subfolders (each expandable) / other root files.
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

        # Still built, just never gridded - this tab no longer SHOWS a
        # wafer map (see _build_internal_tree), but self.wafer_map's data
        # (._last_dies/.dies from load_from_ata below) is what feeds the
        # Results tab's own map (app.py's set_wafer_map calls) and other
        # readers - removing the widget entirely would have broken those,
        # not just this tab's own display.
        self.wafer_map = WaferMapPanel(tab)

    # Recipe names for one card/system, or None if there's genuinely nothing
    # saved. WaferMapPanel.save_recipes (wafer_map_view.py) writes these two
    # DIFFERENT ways depending on system, and this has to match both:
    #   - Electroglas: a separate probe_cards/<base>.recipes.electroglas.csv
    #     side file next to the card's own <base>.csv.
    #   - Accretech: RECIPE/STEP rows written straight INTO <base>.csv
    #     itself, right after the card's own PIN rows - no side file at
    #     all. Checking only for the side file (as this used to) reported
    #     "no recipes file for this system" for every Accretech card even
    #     when one was saved and sitting in plain sight - confirmed against
    #     LAMP's real LaMP_HP_b.csv, which carries "lampaccr" this way.
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

    # Same as _card_recipe_names, but grouped by the RECIPE row's own
    # "bench" column instead of flattened - so a card shared between two
    # benches (e.g. probe08 vs probe08old, or probe02 vs probe03) shows
    # which recipes actually apply to which one instead of one undifferentiated
    # list. A recipe saved before "bench" existed - or one deliberately left
    # blank because it's not bench-specific - comes back under "" (any
    # bench), same as RecipePanel's own bench-tag filtering treats it.
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
        """Everything the ATA Folder tab used to split across a flat file
        list plus a wafer map (which every other tab already shows), as one
        real tree: key files, probe cards + each system's own recipe count
        side by side (so a default recipe pointing at a card with no
        recipes file for that system - a real incident, LAMP's "lampaccr"
        - is visible at a glance instead of a silent lookup failure),
        subfolders (expandable), and whatever else is sitting in the
        folder root."""
        tree = self._ata_tree
        for item in tree.get_children():
            tree.delete(item)
        # item id -> {"kind": "recipe"/"wafer_map"/"probe_card", ...} for
        # the right-click Copy menu (_ata_tree_on_right_click) - only rows
        # a copy actually makes sense for get an entry; anything else (Key
        # ATA Files, Subfolders, Other Files) is left out on purpose.
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

        # -- Probe cards + each system's own recipe file (default: open,
        # and listed above Key ATA Files - the more actionable of the two,
        # and the one an incident like LAMP's "lampaccr" gap shows up in)
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
                    # Blank bench ("any bench") sorts last - it's the
                    # catch-all, not a specific bench worth leading with.
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

        # -- Wafer Builder maps - one saved map per .json in
        # wafer_builder_maps/ (each is a self-contained Shot/Shot Map/Die
        # Map snapshot - see recipe_gen_panel's map save/load); _default.txt
        # names whichever one autoloads for this system, same convention
        # probe cards use their own _default_<system>.txt marker for.
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

        # -- Key ATA files - collapsed by default, less immediately
        # actionable than the probe cards/recipes above --------------
        key_id = tree.insert(root_id, "end", text="📄 Key ATA Files",
                             open=False, tags=("section",))
        for fname, (desc, owner) in ATA_KEY_FILES.items():
            if owner not in ("shared", self._system):
                continue
            found = fname in all_files
            tree.insert(key_id, "end", text=fname,
                       values=("✔" if found else "–", desc),
                       tags=("found" if found else "missing",))

        # -- Subfolders, one level of contents each --------------------
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

        # -- Everything else in the folder root -------------------------
        others = sorted(f for f in all_files if f not in ATA_KEY_FILES)
        if others:
            other_id = tree.insert(root_id, "end",
                                   text=f"📄 Other Files ({len(others)})",
                                   open=False, tags=("section",))
            for fname in others:
                tree.insert(other_id, "end", text=fname, tags=("other",))

    # -- Internal tab: right-click Copy -----------------------------------

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
                # Blank stays a real, first-class choice - copy_recipe's own
                # dst_bench=None (untagged, shows on every bench) is exactly
                # what an empty Prober field has always meant here.
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
        else:  # probe_card
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
        # Drop the recipe the Run tab adopted from the PREVIOUS folder. Its
        # touchdowns, anchor list and row/col index all belong to that wafer,
        # and nothing else clears them - so switching folder left the Run tab
        # offering the old wafer's dies over the new wafer's map, with the two
        # silently disagreeing about what a given square is.
        run = getattr(self, "eg_pma_run", None)
        reset = getattr(run, "forget_recipe", None)
        if callable(reset):
            try:
                reset()
            except Exception as exc:
                self._exec_log(f"[RUN] Could not reset the Run tab for the new "
                                f"ATA folder: {type(exc).__name__}: {exc}")
        # NOT here yet - _exec_autoload_default_recipe (moved below,
        # after the new wafer map is actually drawn) selects the recipe's
        # touchdowns via self._exec_wafer_map.set_picked(), which needs
        # self._exec_wafer_map.dies to already be this folder's dies. This
        # early in the method it is still the PREVIOUS folder's (or empty on
        # the very first load), so the picks it set matched nothing - and
        # either way clear_picks()/_on_sites_changed([]) below wiped them a
        # few lines later regardless. That is why the highlight sometimes
        # would not show up right after an autoload.

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
            # autoload_map_for_folder only restores the map into memory
            # (Wafer Builder's own Shot/Shot Map/Die Map canvases) - it does
            # NOT publish it to ata_wafer_map_builder.csv, the file the Run
            # tab's "Wafer Builder" source actually reads. Without this, a
            # map edited and saved (e.g. a new die pitch) looked correct on
            # the Die Map tab but the Run tab kept showing whatever was
            # published by the LAST manual Save/Sync - stale until the
            # operator happened to press Save again. _sync_views is the same
            # publish _save_wafer_map/LOAD ALL/Sync Run Map already do.
            try:
                recipe_gen._sync_views(folder_path)
            except Exception as exc:
                self._exec_log(f"[RUN] Could not publish the auto-loaded "
                                f"Wafer Builder map: {type(exc).__name__}: {exc}")
        self._exec_map_folder = folder_path
        # Electroglas has no hardware-extracted map of its own anymore - the
        # Wafer Builder tab (synced above via autoload_map_for_folder) IS the
        # wafer there. Defaulting back to the legacy "Electroglas" source
        # here would silently reintroduce whatever ata_wafer_map_electroglas
        # .csv happens to still be sitting in the folder from the old PMA
        # Process extraction (often stale or a placeholder rectangle) even
        # though _sync_views already pointed the Run tab at "Wafer Builder"
        # the last time a map was actually published from that tab.
        self._exec_map_source_var.set(
            "Accretech" if self._system == "accretech" else "Wafer Builder")
        # The drawn overlay (canvas items + die_ids) belongs to the PREVIOUS
        # folder's map and is stale the instant the map changes underneath
        # it - cleared here unconditionally. The alignment itself (row/col
        # offset + confirmed flag) is NOT touched by this: recipe_gen's
        # autoload_map_for_folder (above) already restored it from the new
        # folder's own saved Wafer Builder map, if that map ever had one
        # confirmed - see recipe_gen_panel._state_from_dict. It gets
        # re-drawn below, once the new folder's Accretech map is actually on
        # screen.
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

        # PMA Process is no longer auto-scanned/adopted here - a .PMA is a
        # one-time import onto the Wafer Builder tab now (see
        # pma_process_panel.load_all/load_path), never an ongoing folder-
        # load dependency, so nothing on this tab fires unless the operator
        # explicitly opens it and picks a file themselves.
        #
        # Now that the map actually holds this folder's dies (and the picks
        # from the previous folder are cleared), the default recipe's
        # touchdowns can be selected and will actually paint. This also has
        # to be after _exec_draw_wafer_map above, not just anywhere:
        # _exec_apply_recipe_sites expands each selected site into its
        # WHOLE shot via eg_pma_run._seq_at_rc/_cells, and those are only
        # populated once eg_pma_run has adopted the published map and built
        # its row/col index (_build_rc_index, via _exec_draw_wafer_map's own
        # _exec_seed_die_list_from_map -> adopt_from_wafer_builder).
        # Selecting sites before that index exists does not fail loudly -
        # _exec_touchdown_cells falls back to the raw (row, col) picks with
        # no shot expansion - so only the anchor die of each shot got
        # selected, not the whole quad. A manual reselect from the Recipe
        # dropdown later worked fine because by then the index was already
        # built, which made this look intermittent.
        self._exec_autoload_default_recipe(folder_path)
        self._exec_sync_wafer_map_on_folder_load()

        # NanoZ is no longer a tab nested in this MainLayout (see
        # gui/nanoz_mode.py) - forward the load to whichever NanoZPanel
        # exists via the controller instead of reaching for a local
        # self.nanoz_panel attribute that no longer exists.
        self.controller.notify_nanoz_ata_folder_loaded(folder_path)

        cassette = getattr(self, "cassette_panel", None)
        if cassette is not None and hasattr(cassette, "on_ata_folder_loaded"):
            cassette.on_ata_folder_loaded(folder_path)
        if hasattr(self, "_default_yield_var"):
            self._default_yield_var.set(f"{load_yield_threshold(folder_path):g}")
            self._update_default_yield_label()

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
            # Save All covers the hand-drawn Custom pad sketch too now, so
            # there is one save button for the whole tab instead of two.
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
        # Nothing to tell Wafer Builder here any more: the Shot tab used to
        # hold the card's die-to-pin table and had to follow a card change,
        # but pins are picked per measurement step on the Recipe tab now, so
        # a shot is the same shot whichever card is loaded.
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
            # get_card_names() (unfiltered), not get_card_names_for_system():
            # that filter drops any card whose PIN table isn't a subset of
            # this bench's own addressing scheme (Accretech's plain "1".."24"
            # switch_topology numbers, or Electroglas's per-bench
            # wired_pin_labels - empty for every bench but probe02). A card
            # wired with a different pin format (e.g. Peanut's "J1".."J48",
            # or LaMP_HP's original "A9"/"B32" labels) isn't invalid - it's
            # just a real card from a different project/bench - but the
            # filter silently emptied the whole Recipe tab picker for it
            # while the Probe Card tab (already unfiltered) kept working,
            # which is exactly the "dropdown is blank" bug this fixes.
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
        """Add/edit prober benches and their instrument fitment - separate
        implementations per system (see eg_setup_panel.py/
        accretech_setup_panel.py's module docstrings for why: Electroglas
        already has real per-bench profiles to edit, Accretech has one
        fixed bench with no such infrastructure yet)."""
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
        """Live view of every GPIB/USB command this app itself sends - see
        gpib_trace_panel.py/instruments/gpib_trace.py for what it can and
        can't see (LabVIEW's own traffic needs NI I/O Trace alongside it)."""
        tab = ttk.Frame(nb)
        nb.add(tab, text="GPIB Trace")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.gpib_trace_panel = GpibTracePanel(tab, controller=self.controller)
        self.gpib_trace_panel.grid(row=0, column=0, sticky="nsew")

    def _tab_nanoz_switch(self, nb):
        """Debug sub-tab that switches the WHOLE window into NanoZ mode
        (gui/nanoz_mode.py) rather than opening NanoZ as a tab here - see
        that module's docstring for why NanoZ moved out of MainLayout.
        Present on both systems (not just Accretech) since Electroglas
        needs the same switch once its own NanoZ main section exists, even
        though today it only shows a placeholder once switched to."""
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
        """Accretech's Wafer Builder - same RecipeGenPanel (Shot/Shot Map/
        Die Map) Electroglas uses, via _tab_recipe_gen, plus Accr Wafer
        (the hardware extraction) as its own first sub-tab rather than a
        separate top-level tab - it feeds the same wafer, so it lives where
        the rest of the wafer-building work does. Load PMA/Load Recipe Gen
        (.xls) autofill Shot/Shot Map/Die Map exactly like Import CSV does,
        just from an older file.

        PmaWaferPanel is still built - just not shown - and still assigned
        to self.pma_wafer: the Overlay sub-tab (see _exec_build_overlay_tab,
        added below as this notebook's last sub-tab) reads self.pma_wafer.
        workbook_data/_pma_shot_data/etc defensively via getattr for its
        PMA/xls/csv comparison sources, so keeping the object alive avoids
        breaking that even though there is no more UI here to feed it from.
        """
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

        # Overlay - reconciles this Die Map against the real Accretech
        # extraction (Accr Wafer above). Appended last (after Shot/Shot
        # Map/Die Map) since it needs the wafer already defined; unlike
        # those three it lives on MainLayout, not RecipeGenPanel, because
        # the process it replaced (the old "Overlay…" Run tab dialog) reads
        # and writes MainLayout's own _exec_* overlay state/Run+Results
        # maps directly - see _exec_build_overlay_tab's own comment.
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
        """The Wafer Builder tab on Electroglas - RecipeGenPanel owns three
        pages of its own (Shot / Shot Map / Die Map) that together replace
        the old Build/Edit + read-only Wafer View split entirely.

        PmaWaferPanel is still built - just not shown - and still assigned
        to self.pma_wafer: other code (Accretech's Overlay sub-tab,
        _exec_overlay_source_data, centroid matching against an Accretech
        map) reads self.pma_wafer.workbook_data/_pma_shot_data/etc
        defensively via getattr, so keeping the object alive avoids breaking
        those paths even though there is no more .PMA/.xls-driven UI to feed
        it from this tab.
        """
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
        # Reset every run start (_exec_reset_counts) - see
        # _exec_should_configure's own docstring.
        self._exec_step_config_cache = {}
        self._exec_avg_count_cache = {}
        # Set per touchdown by the Electroglas run so exports name the whole
        # shot; blank means fall back to the map/overlay per-cell die ID.
        self._exec_die_id_override = ""
        self._exec_total_dies = 0
        # Bumped on every start/abort. A run thread captures its own token and
        # re-checks it at every loop step/finish — if a new run (or an abort)
        # bumps the token out from under it, the stale thread stops touching
        # shared state/hardware instead of racing the new run and silently
        # "resuming" its own old loop.
        self._exec_run_token = 0
        self._exec_lot_thread: threading.Thread | None = None
        # Cassette automation hooks into this - set to a callable
        # fn(pass_n, fail_n, total_n, aborted) to be notified whenever a run
        # (Full Die today) finishes, instead of polling _exec_running.
        self._exec_on_run_finished = None
        # Index into controller.results_data where the most recently started
        # run began — export formats (unlike "Save as CSV") only export from
        # here onward, so re-running doesn't pile old runs' rows into a new
        # export.
        self._exec_last_run_start_idx = 0
        self._exec_steps    = []
        self._exec_current_rc = None
        # See _exec_start_site_list's own comment - the last real (row,
        # col) list a "test" mode run actually used, since get_picked() is
        # already empty again by the time that run finishes.
        self._exec_last_test_sites: list = []
        self._exec_overlay_row_offset = 0
        self._exec_overlay_col_offset = 0
        self._exec_overlay_offset_confirmed = False
        self._exec_overlay_items: list = []
        self._exec_overlay_result_items: list = []
        self._exec_overlay_die_ids: dict = {}
        # Accretech's equivalent of NanoZ's 1x20 window / Electroglas's 2x2
        # quad window - see _exec_update_shot_window.
        self._exec_shot_window_items: list = []
        # → Move to Selected's own arm/target state - see
        # _exec_move_selected_button. Deliberately separate from the
        # normal pick system (_exec_wafer_map._picked/get_picked()).
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

        # Wafer Map: a direct pick-and-load, same list the Recipe tab's own
        # Wafer Map: dropdown offers (recipe_gen.list_map_names()) but this
        # one acts immediately on selection rather than just recording a
        # preference - see _exec_on_wafer_map_picked/
        # _exec_load_and_publish_wafer_map (which also reapplies the
        # Overlay alignment saved as part of the picked map, Accretech
        # only - _sync_views alone does not touch it).
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

        # Kept but not packed on either system - Test Selected replaces it as
        # the sole "test some dies" entry point, but _exec_abort/
        # _exec_finish_run/_exec_start_test_die still toggle its state
        # alongside _exec_full_btn regardless of which system this is, so
        # the attribute stays around either way. _exec_full_btn/
        # _exec_test_selected_btn themselves are built below Recipe Steps
        # now, not here - see the Recipe Steps LabelFrame further down.
        self._exec_test_btn = ttk.Button(
            ctrl, text="▶  Test Die", command=self._exec_start_test_die)

        # The real, full-story entry point - recipe steps, the recipe's own
        # saved touchdown list, Minor Moves (Accretech) and all - unlike
        # Full Die/Test Selected to its left, which stay the plain
        # single-die case only (see _exec_start_run's docstring). To the
        # RIGHT of the separator, immediately next to Unload: it starts the
        # wafer, and sitting among Full Die/Test Selected/Unload (which
        # only ever touch one die) made it indistinguishable from them.
        #
        # Green border, default everything else. Drawn as a frame BEHIND
        # the button rather than a ttk style: Windows' native button themes
        # paint their own border and ignore a style's bordercolor entirely,
        # so the only way to get a coloured edge without switching the
        # whole app to 'clam' is to let a coloured frame show through
        # around it.
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
            # Pause is what ⏹ Stop Run used to do: finish what is in
            # progress and hold, keeping the position so Run resumes. Stop
            # is now a real stop - see _exec_abort.
            ("⏸  Pause",       self._exec_pause, "_exec_pause_btn"),
            ("⏹  Stop Run",       self._exec_abort, "_exec_stop_btn"),
            # Release To Local itself moved below Recipe Steps (see that
            # LabelFrame further down) - _exec_local_btn is built there now.
        ]:
            btn = ttk.Button(ctrl, text=label, command=cmd)
            btn.pack(side="left", padx=3, pady=5)
            if attr:
                setattr(self, attr, btn)
        # Nothing is running yet at startup - Stop Run/Pause have nothing to
        # stop or pause, and pressing Stop Run with no run active used to
        # still open every channel and drop Z for no reason. See
        # _exec_set_running_buttons, called at every real start/finish.
        self._exec_set_running_buttons(False)

        self._exec_state_lbl = tk.Label(
            ctrl, text="IDLE", bg="#f1f5f9", fg="#6b7280",
            font=("Segoe UI", 11, "bold"))
        self._exec_state_lbl.pack(side="right", padx=12)

        body = ttk.PanedWindow(tab, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 6))

        # Electroglas drives a .PMA as relative die steps anchored on a die the
        # operator names, which has nothing in common with the Accretech flow
        # above - so it gets its own pane rather than being woven into it.
        if self._system == "electroglas":
            self.eg_pma_run = EgPmaRunPanel(body, controller=self.controller,
                                            main_layout=self)
            # 25:20:55 (this pane : left_col : map_lf, added below) -
            # weight alone only governs how EXTRA space is distributed on
            # resize, not the initial split, so the real ratio is set once
            # via sashpos after the window is first drawn - see the
            # after_idle call below map_lf's own body.add.
            body.add(self.eg_pma_run, weight=25)

        left_col = ttk.Frame(body)
        body.add(left_col, weight=25 if self._system == "electroglas" else 1)
        left_col.rowconfigure(0, weight=0)
        left_col.rowconfigure(1, weight=1)
        left_col.columnconfigure(0, weight=1)

        # Chuck Position and Pass/Fail share one row, side by side, rather
        # than each owning a whole section of their own (Accretech used to
        # give Pass/Fail an entire extra pane to the right of the wafer
        # map; Electroglas stacked it under Chuck Position instead) - same
        # total footprint, just laid out as two boxes across instead of
        # stacked or off in their own pane.
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
            # Directly under the X/Y it qualifies: the die size is the unit
            # those counts are IN, so reading them apart from it means
            # nothing. It used to sit at the bottom of the box, below every
            # button, where the connection to the numbers was invisible.
            # There is no direct query for it - see
            # electroglas_2001x.infer_die_size and _exec_refresh_die_size.
            self._exec_die_size_var = tk.StringVar(value="Prober Die size: unknown")
            ttk.Label(pos_lf, textvariable=self._exec_die_size_var,
                     font=("Consolas", 8), foreground="#6b7280",
                     justify="center").grid(row=2, column=0, columnspan=2,
                                            pady=(0, 4))

        ttk.Separator(pos_lf, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=3)

        # 3x2 grid: Measure/First Die, Z Up/Z Down, Back/Next, then (Accretech
        # only) Move to Selected and ↻ Refresh XY. Reset Counts moved to the
        # Pass/Fail section, next to what it resets - not here anymore.
        # Measure itself moved below Recipe Steps (see that LabelFrame
        # further down) - _exec_measure_btn is built there now; First Die
        # widened to fill the row it used to share with it.
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
            # ▶▶ Next Die (an Accretech-shaped "advance one die" action) is
            # replaced by EgPmaRunPanel's own ▶ Next/◀ Back - moved in from
            # that pane's former Run section, since single-die-step
            # advancing through the touchdown list IS what Back/Next mean
            # for a .PMA step-through.
            self._exec_back_btn = ttk.Button(
                pos_lf, text="◀ Back", command=lambda: self.eg_pma_run._step_back())
            self._exec_back_btn.grid(
                       row=6, column=0, sticky="ew", padx=(0, 1), pady=1)
            self._exec_next_btn = ttk.Button(
                pos_lf, text="▶ Next", command=lambda: self.eg_pma_run._step_once())
            self._exec_next_btn.grid(
                       row=6, column=1, sticky="ew", padx=(1, 0), pady=1)
            # Same arm/target process as Accretech's own Move to Selected
            # (row 8 there) - see EgPmaRunPanel.toggle_move_armed. The
            # widget itself lives here (Chuck Position), same placement as
            # Accretech, but its state/text is owned by eg_pma_run.
            self.eg_pma_run._goto_btn = ttk.Button(
                pos_lf, text="→ Move to Selected",
                command=self.eg_pma_run.toggle_move_armed)
            self.eg_pma_run._goto_btn.grid(
                row=7, column=0, columnspan=2, sticky="ew", pady=1)
            # The "Prober die size" label it used to build here now sits at
            # row 2, directly under the X/Y counts it is the unit for. It is
            # read once whenever the prober connects
            # (app.py._connect_instruments_eg) and again whenever a Die Size
            # write goes out from Prober Debug
            # (eg_prober_debug_panel._send_setup) - there is no direct query
            # for it, so this is the only place an operator can see it
            # without opening Prober Debug and inferring it by hand.
        else:
            # Accretech has no native "previous die" GPIB command (only "J"
            # Next Die) - Back is a plain relative die-index step backward
            # instead (S command), the closest "die mode" equivalent to
            # Next's bare J. Neither touches the picked-sites list or shots
            # - see _exec_manual_prev_die/_exec_manual_next_die.
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
            # Its own separate arm/target system - see
            # _exec_move_selected_button's docstring - deliberately not
            # tied to the normal pick system (Test Selected's picks) at all.
            self._exec_move_selected_btn = ttk.Button(
                pos_lf, text="→ Move to Selected",
                command=self._exec_move_selected_button)
            self._exec_move_selected_btn.grid(
                row=8, column=0, columnspan=2, sticky="ew", pady=1)
            # Manual, fire-and-forget version of the same Q read
            # _exec_refresh_xy_blocking runs automatically (and blocking)
            # right before Full Die/Test Die/Test Selected/Minor Moves'
            # first move - see that method.
            self._exec_refresh_xy_btn = ttk.Button(
                pos_lf, text="↻ Refresh XY", command=self._exec_get_xy)
            self._exec_refresh_xy_btn.grid(
                row=9, column=0, columnspan=2, sticky="ew", pady=1)

        # Recipe Steps is the one that grows, so it takes the weighted row on
        # both systems - Chuck Position/Pass-Fail (row 0, above) is fixed
        # height.
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

        # Full Die/Test Selected/Measure/Release To Local, two per row,
        # right under the steps they'd actually run - these four used to
        # be split across the top control bar and the Chuck Position box,
        # nowhere near each other or the recipe they act on.
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
        # Same action as the Instruments tab's "Release All To Local",
        # repeated here because this is where an operator is standing
        # when they find the prober's own keys dead: connecting over
        # GPIB puts it in REMOTE and locks the panel out, and nothing
        # gives it back until this is pressed or the app exits.
        # Momentary, not a mode - the next command the GUI sends
        # re-asserts remote, exactly like any other instrument.
        self._exec_local_btn = ttk.Button(
            exec_btn_row2, text="↩  Release To Local", command=self._release_all_to_local)
        self._exec_local_btn.grid(row=0, column=1, sticky="ew", padx=(1, 0))

        map_lf = ttk.LabelFrame(body, text="Wafer Map")
        body.add(map_lf, weight=50 if self._system == "electroglas" else 2)
        map_lf.rowconfigure(1, weight=1)
        map_lf.columnconfigure(0, weight=1)

        if self._system == "electroglas":
            # PanedWindow has no percentage-based initial layout - the sash
            # positions have to be set explicitly, once the pane actually
            # has a real width. That is NOT true yet at construction time
            # (an after_idle here measured a too-small, not-yet-final width
            # whenever Run isn't the notebook's initially-selected tab, or
            # the window itself isn't mapped by the OS window manager yet -
            # both true during normal startup) - bound to <Configure>
            # instead, which fires with the REAL width whenever that
            # actually happens, and unbinds itself once it has, so this
            # only runs once and never fights the operator's own later
            # sash drags.
            def _apply_initial_sashes():
                w = body.winfo_width()
                if w <= 1:
                    return
                # 25 : 25 : 50 - Run column (eg_pma_run) ~25%, left_col
                # (Chuck Position + Pass/Fail, split 50/50 by pos_row's own
                # columnconfigure - ~12.5% each) ~25%, Wafer Map the rest.
                body.sashpos(0, int(w * 0.25))
                body.sashpos(1, int(w * 0.50))
            def _set_initial_sashes(_event=None):
                if body.winfo_width() <= 1:
                    return
                # Unbind BEFORE touching sashpos, and do the actual set on
                # the next idle pass, not inline - sashpos() itself can
                # generate another <Configure>, and handling that
                # re-entrantly (still inside THIS handler, against a width
                # that may be mid-layout-pass) is what produced a
                # noticeably-off ratio (e.g. 29/23/48 instead of 25/20/55)
                # during testing.
                body.unbind("<Configure>", sash_bind_id[0])
                body.after_idle(_apply_initial_sashes)
            sash_bind_id = [body.bind("<Configure>", _set_initial_sashes)]

        map_bar = ttk.Frame(map_lf)
        map_bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self._exec_map_folder = None
        # Electroglas has no hardware-extracted map of its own (unlike
        # Accretech's own "Accretech" source) - Wafer Builder IS the wafer
        # there, published straight to the Run tab by _sync_views whenever
        # it changes (see recipe_gen_panel.py). The old "Electroglas"
        # source (ata_wafer_map_electroglas.csv) predates Wafer Builder
        # entirely and is retired.
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
        # Overlay… moved to Wafer Builder > Overlay (see _tab_pma_wafer /
        # _exec_build_overlay_tab) - same process, embedded there instead
        # of a popup so the accretech map/offset controls live together.
        # 💾 Save Selected Map removed (both systems) - it duplicated the
        # Recipe tab's ⬅ Take from map selection (recipe_panel._sites_from_
        # map), which does the exact same thing (save the picked dies as
        # the loaded recipe's touchdown list) from the other tab; that one
        # now also does the Electroglas shot-collapsing this one used to.
        self._exec_select_all_btn = ttk.Button(
            map_bar, text="☑ Select All", command=self._exec_toggle_select_all)
        self._exec_select_all_btn.pack(side="left", padx=(6, 0))

        self._exec_wafer_map = WaferMapPanel(
            map_lf, show_title=False, show_axis_grid=True)
        self._exec_wafer_map.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self._exec_wafer_map.enable_picking(on_change=self._exec_on_sites_changed)
        self._exec_wafer_map.on_redraw = self._exec_redraw_overlay_on_run_map
        # Both halves are needed and they do different jobs: on_zoom REBUILDS
        # the labels (a zoom scales canvas items in place rather than
        # redrawing, so stale ones survive at the wrong size), while the
        # bindings decide whether they should be VISIBLE at this zoom level.
        # Keeping only the visibility half would leave wrongly-sized labels;
        # keeping only the rebuild would show them when too small to read.
        # Debounced (_exec_debounced) rather than called directly - a fast
        # scroll or a middle-drag pan (which reuses this same on_zoom, see
        # wafer_map_view._bind_zoom_only) fires this many times a second,
        # and each call rebuilds every die-ID label - so a burst of events
        # now collapses into one rebuild shortly after the burst ends,
        # instead of rebuilding on every single one of them. What gets
        # redrawn, and when the data itself changes, is unchanged.
        self._exec_wafer_map.on_zoom = self._exec_debounced(
            "_exec_zoom_debounce_id", self._exec_redraw_overlay_on_run_map)
        # Double-click "reset view" would otherwise redraw a second time on
        # this same long-lived canvas - the exact "packed with no gaps"
        # corruption _new_results_wafer_map's own comment documents. Route
        # it through a fresh-widget rebuild instead, same fix, same reason.
        self._exec_wafer_map.on_reset_request = self._exec_rebuild_run_map
        # Bound with add="+" so the map's own pan/zoom/reset bindings (set up
        # inside WaferMapPanel.__init__) still run first.
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
            # Called from a run thread on every step, so a queued Tcl call
            # occasionally losing the "main thread is not in main loop"
            # race (same class of bug fixed in pma_wafer_panel.py's
            # workbook loader) must not take the run down with it - the
            # run continues; this one line just goes to stdout instead.
            print(line)

    def _exec_minor_moves_active(self) -> bool:
        """Whether the CURRENTLY LOADED recipe wants shot-aware single-die
        stepping - see the Recipe tab's Minor Moves checkbox. Read fresh
        each time rather than cached, since it can change any time the
        operator picks a different recipe."""
        rp = getattr(self, "recipe_panel", None)
        return bool(rp and hasattr(rp, "is_minor_moves") and rp.is_minor_moves())

    def _exec_draw_wafer_map(self, quiet_if_missing: bool = False):
        folder = self._exec_map_folder
        # The Run tab map is always the real per-die Accretech/Wafer
        # Builder map, Minor Moves on or off - a shot is drawn as an
        # OUTLINE over that die map (see _exec_update_shot_window), never
        # by swapping the map itself to one square per shot.
        filename = WAFER_MAP_SOURCES[self._exec_map_source_var.get()]
        # The Accretech source file is just row/col die-step indices, no
        # real micron size - load_from_ata's fallback used to turn that
        # into a flat 1-unit square regardless of the real die shape. Wafer
        # Builder's own die_pitch_x/y (the operator's saved shot template,
        # e.g. LAMP's rectangular die) is the only place that real ratio
        # lives, so borrow it here too - a no-op for the Wafer Builder
        # source itself, which already carries real x_um/y_um and never
        # hits that fallback.
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
        # Both systems mirror the Run map onto the Results tab, so the
        # pass/fail map there is never a stale copy of a different wafer.
        self._sync_results_wafer_map()
        if self._system == "accretech":
            self._exec_load_selected_map(quiet_if_missing=True)
        else:
            self._exec_seed_die_list_from_map()

    def _exec_seed_die_list_from_map(self):
        """Build the Run tab's Die list straight from the map that just
        loaded. Always - there is no other source and no button for it.

        The Wafer Builder map IS the wafer, so every die on it is a
        position the operator can anchor to or drive to, and the Die list
        is meant to show exactly that set (see EgPmaRunPanel._build_table).
        It used to be populated only by loading a .PMA or by pressing
        "Build from Wafer Builder Map" by hand, so an operator who
        published a map and went to the Run tab found the list empty, with
        nothing saying a button press was what it wanted - and nothing
        persisted the result, so the same press was needed again next
        launch. Doing it here makes the map alone sufficient, every time,
        and removes anything to save.

        adopt_from_wafer_builder is a no-op when the map yields the same
        touchdowns it already holds, so this cannot silently drop an
        anchor on a redraw.
        """
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
        """Replace the Run tab wafer map with a fresh widget instead of
        redrawing on its existing canvas - wired as on_reset_request so a
        double-click "reset view" (which used to redraw straight on the
        same long-lived canvas) can no longer reproduce the "packed with
        no gaps" corruption. rebuild_wafer_map_panel already carries over
        pick selection and PASS/FAIL/CURRENT status; this just re-attaches
        the extra bindings that live outside WaferMapPanel itself."""
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
        """Use the die IDs the loaded map file carries as the overlay.

        The Electroglas map (ata_wafer_map_electroglas.csv) names every
        touchdown in a device_id column, so there is nothing to match up -
        unlike Accretech, where the Overlay dialog has to reconcile two
        different maps. Feeding them into the SAME _exec_overlay_die_ids
        dict means the labels inherit everything that already works there:
        redrawn from WaferMapPanel.on_redraw after any rebuild, moved with
        the dies by canvas.scale on zoom, and written out by Save Selected
        Map, so an export can never disagree with what is on screen.
        """
        wm = self._exec_wafer_map
        ids = {rc: text for rc, text in (wm.die_ids or {}).items() if text}
        if not ids:
            return
        # Never clobber an overlay the operator built by hand in the dialog.
        if self._exec_overlay_die_ids and self._system == "accretech":
            return
        self._exec_clear_overlay_labels(wm, self._exec_overlay_items)
        self._exec_overlay_die_ids = ids
        self._exec_redraw_overlay_on_run_map()

    def _new_results_wafer_map(self):
        """(Re)create the Results tab's own WaferMapPanel from scratch, on
        a brand new Canvas.

        Reusing the SAME long-lived canvas for every redraw (the previous
        approach - a plain rwm._draw_from_die_list(dies) call) is what
        actually produced the broken layout every "die packed with no
        gaps, overlay labels off their square" report traced back to -
        the very first draw on a canvas was always the correct one, every
        later one on that same canvas was not, for a Tk-geometry reason
        that resisted every attempt to pin down and fix in place. A fresh
        widget makes every draw the "first, always-good" one instead of
        chasing what state a long-lived canvas accumulates - see the
        session's git history for the abandoned attempts.
        """
        old = getattr(self, "_results_wafer_map", None)
        wm = WaferMapPanel(self._results_map_frame)
        wm.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=(0, 8))
        wm.canvas.bind("<Button-1>", self._on_results_map_click, add="+")
        wm.on_redraw = self._exec_redraw_overlay_on_results_map
        wm.on_zoom = self._exec_debounced(
            "_exec_results_zoom_debounce_id", self._exec_redraw_overlay_on_results_map)
        # A double-click "reset view" on THIS widget, later, must not redraw
        # a second time on this same canvas - see this method's own comment.
        # Route it through the lighter rebuild below instead, which keeps
        # PASS/FAIL/CURRENT status and the pick selection instead of
        # dropping them the way a brand new dataset load correctly does.
        wm.on_reset_request = self._exec_rebuild_results_map_view
        self._results_wafer_map = wm
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        return wm

    def _exec_rebuild_results_map_view(self):
        """Same fresh-widget fix as _new_results_wafer_map, but for a plain
        view reset (double-click) rather than a genuinely new dataset - so
        this one preserves PASS/FAIL/CURRENT status and the pick selection
        via rebuild_wafer_map_panel instead of dropping them."""
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
            rwm._draw_from_die_list(dies)  # triggers on_redraw -> overlay labels
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
        """Open every switch channel, whichever matrix this bench has.

        Called on a stop, so the bench is never left with a source still
        strapped to a pad after the operator has told it to stop.
        """
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
        """One place that decides which Run tab controls make sense right
        now - called at every real start (both engines) and every real
        finish/abort, never from a mid-run status change.

        running=True: Full Die/Test Die/Test Selected/Run AND every manual
        move/measure control (First Die, Z Up/Down, Back/Next, Prev/Next
        Shot, Move to Selected, Refresh XY, Unload) are disabled - none of
        them make sense (and several are actively dangerous) while a run
        already owns the chuck. Stop Run/Pause become usable. Also locks
        the Recipe tab (recipe_panel.set_locked - was called separately at
        every start/finish site before this absorbed it) and the top-level
        chrome that could otherwise switch hardware out from under an
        active run (system toggle, bench picker, ATA picker - see
        AtomicaDashboard.set_run_lock).

        running=False: the reverse - Stop Run/Pause disabled, since there
        is nothing to stop or pause; pressing Stop Run with no run active
        used to still open every switch channel and drop Z for no reason.
        """
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
        """Stop after the work in progress, keeping the position.

        What ⏹ Stop Run used to do. Nothing is reset and the bench is left
        as it is, so ▶ Run picks up from the next touchdown.
        """
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
        # The Accretech-style loops check _exec_running between dies, so
        # clearing it stops them the same graceful way - without the abort
        # flag, which is what triggers the emergency stop and the reset.
        self._exec_running = False
        self._exec_set_running_buttons(False)
        self._exec_set_state("PAUSED", "#b45309")
        self._exec_log("[RUN] Paused after the current die — position kept.")

    def _exec_abort(self):
        # One Stop Run button covers both run engines now - the normal Full
        # Die/Test Selected/Test Die loop below, AND (Electroglas only)
        # EgPmaRunPanel's own .PMA step-through, which used to need its own
        # separate ⏹ Stop button to halt.
        #
        # A real stop, not "wind down when convenient": the run thread bails
        # at the next step boundary rather than finishing the touchdown, the
        # switch is opened, the chuck is separated, and the position is
        # forgotten so ▶ Run starts the recipe over. ⏸ Pause is the gentle
        # one. What cannot be interrupted is a reading already in flight -
        # that is a single blocking GPIB call, and abandoning it mid-transfer
        # would leave the bus out of step for everything after it.
        #
        # Idempotency guard: this can genuinely be called twice for one
        # real stop - the operator's own Stop Run press racing
        # _exec_zup_measure_zdown's own automatic abort-on-failed-Z-down
        # safety check (a real run-thread call, not a UI click) - and with
        # no guard the emergency_stop/K/es commands below went out on the
        # wire twice per stop. self._exec_aborted is reset to False by
        # every run starter (_exec_start_full_die_walk/_start_site_list/
        # _start_minor_moves), so it is exactly "a stop was already
        # processed for the run in progress, nothing new has started
        # since" - not just "not currently running".
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
        # The .PMA run thread opens the channels and drops Z itself on the
        # way out (see EgPmaRunPanel._make_safe), so doing it here as well
        # would race it. Anything else, this is the only chance.
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
        # Accretech-only: send_es (buzzer clear) is a UF200R command with no
        # Electroglas equivalent - the EG driver stubs it as
        # _not_implemented, so this used to fire on every Electroglas Stop
        # Run too, always failing and logging an "es error" for a command
        # that was never going anywhere.
        #
        # emergency_stop (K) is NOT sent here anymore - Stop Run is a
        # graceful stop (open channels, separate the chuck via D, reset run
        # state) and K is a genuinely different, blunter hardware command.
        # It stays reserved for Prober Debug's own dedicated
        # "⏹ Emergency Stop (K)" button, not fired automatically on every
        # ordinary Stop Run press.
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
        """self.after(0, fn), swallowing the rare "main thread is not in
        main loop" RuntimeError a queued Tcl call can raise when called
        from a run thread (same class of bug fixed in
        pma_wafer_panel.py's workbook loader). Used in _exec_finish_run
        because that is the one cleanup path EVERY run thread's finally
        block depends on to unlock the UI - losing that race there left a
        run stuck showing RUNNING forever instead of just losing a log
        line, which is what happens everywhere else _exec_log is used."""
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _exec_finish_run(self, token: int, msg: str, color: str):
        if token != self._exec_run_token:
            # Superseded by a newer run (or an abort) while this thread was
            # blocked on a hardware call — it's no longer "the" run, so don't
            # stomp on whatever state that newer run/abort has already set.
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
            # pass_var/fail_var are Tk vars - read them inside the deferred
            # call (main thread) rather than here (this thread), same
            # reasoning as _exec_safe_after itself.
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
        """row/col: the touchdown's own real (row, col) - always needed,
        for the ordinary single-die fallback colouring. shot_geom (see
        _exec_prepare_shot_geometry): when this touchdown's shot has more
        than one die, published before the steps run and used after to
        colour each die in the shot on its own real square instead of
        just this one touched square - covers ANY shot shape (1x20, 3x9,
        2x2, ...) and any subset of dies the recipe actually measures
        (only the die #s a passfail step actually tagged get a verdict;
        the rest are simply never touched, still whatever colour they
        were) - see _exec_color_shot_squares."""
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
            # Captured before this gets wiped - _exec_tally_shot_result
            # (below) needs the real die ID per slot to know which slots
            # in _exec_slot_verdicts are actual dies (countable) versus
            # empty NA/TARGET corners (not), the same distinction
            # eg_pma_run_panel._measure_here already makes on the
            # Electroglas side. rc_by_slot is the matching real (row, col)
            # per slot, captured for the same reason - see
            # _exec_color_shot_squares's own docstring.
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
            # Stop Run already separated the chuck (its own D, sent the
            # moment it was pressed) - sending a second D here for the
            # in-flight measurement that just finished would be a real
            # duplicate command, not a safety margin.
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
        # The only persistent record of this verdict - the map widgets only
        # hold it as canvas item colour. cmd_save_csv reads this to write
        # per-die PASS/FAIL, and cmd_import_results_csv repaints from it.
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
        """False (and logged) if the loaded recipe names a wafer map that
        isn't the one actually active right now.

        Every run/measure/move entry point on both systems is gated on
        this - Full Die/Test Die/Test Selected/Run/Minor Moves (via
        _exec_can_start), Measure (_exec_touchdown_measure), and
        Electroglas's own Run/Next/Back/Move to Selected/Minor Moves (via
        EgPmaRunPanel._guard/_run_minor_moves) - because a mismatched map
        means every position and die ID any of them would compute is
        wrong: LaMP's own die pitch lives on the map, so probing against
        the wrong one doesn't just mislabel results, it can physically
        move the chuck to the wrong die entirely.

        A recipe with no saved wafer map preference (RecipePanel.
        get_wafer_map() returns "") has nothing to check against, so this
        is always True for it - same as _exec_autoload_recipe_wafer_map's
        own "blank preference means no opinion" rule.
        """
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
            # Only what THIS bench actually has fitted - a bench with no
            # wave gen wired (Setup tab's Fitted checkbox, e.g.
            # probe08new) must not block every run over an instrument it
            # was never supposed to need. See
            # AtomicaDashboard.accretech_required_drivers.
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
        # Full Die/Test Selected are the plain "walk the dies, measure"
        # entry points - Minor Moves (multi-die shots, touchdown list, the
        # whole story) is ▶ Run's job now, not theirs. Refuse rather than
        # silently doing a native G/J walk that doesn't mean anything on a
        # wafer where a map square is a multi-die shot.
        if self._system == "accretech" and self._exec_minor_moves_active():
            self._exec_log("[RUN] Full Die: this recipe has Minor Moves on — "
                            "use Run instead.")
            return
        self._exec_start_full_die_walk("Full Die")

    def _exec_start_full_die_walk(self, mode_label: str):
        """The actual native G/J whole-wafer walk - shared by Full Die and
        ▶ Run's own "no saved touchdowns, do the whole wafer" fallback."""
        if self._system == "electroglas":
            # This walk is built entirely on the Accretech STB protocol - G
            # (move_to_start_die, an admittedly SUSPECT/never-tested command
            # on the Electroglas driver), STB=81/90 end-of-wafer codes, J
            # (next_die) for stepping. None of that is how the Electroglas
            # works: it has no onboard wafer map, no STB-based handshake,
            # and its own datum moves every time the operator re-aligns -
            # see electroglas_2001x.py's module docstring. Silently falling
            # through to this path (e.g. via the ▶ Run "no saved
            # touchdowns" fallback) would send that untested command for
            # real. Use the PMA Run tab instead, which drives Electroglas
            # with verified MD/MM relative steps and a software-anchored
            # datum.
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
        # Resolved here, on the main thread - see _exec_prepare_shot_geometry.
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
            # move_to_start_die() raises if the prober answers with a GPIB
            # error (STB=76) instead of 67/70 — e.g. it wasn't sitting on
            # the probing menu when G was sent. Caught below so the GUI
            # reflects the real outcome instead of claiming a clean finish.
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
                # Pass/Fail counters are updated inside _exec_zup_measure_zdown
                # itself now (see _exec_tally_shot_result) - once per real
                # die in the shot, not once per touchdown.

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
        """Shared Full Die / Test Die startup for Minor Moves - `shots` are
        real absolute (row, col) die coordinates picked on the map, each
        naming a die that belongs to one shot (see goto_shot_die below for
        how the shot itself, and die #1's own cell within it, are found);
        from there only the die(s) the loaded recipe's steps reference by
        die # are visited.
        Same button/lock/state bookkeeping _exec_start_full_die and
        _exec_start_test_die already do for the native G/J path.

        Everything Tk-touching (recipe_gen's shot dims/cells, which read
        Tk StringVars) is resolved HERE, on the main thread, and handed to
        the worker as plain data - a background thread calling .get() on a
        Tk variable can raise "main thread is not in main loop" (the same
        class of bug fixed in pma_wafer_panel.py's workbook loader; see
        that file's load_workbook_path for the longer explanation).

        Origin: each (row, col) in `shots` is a real absolute die
        coordinate picked on the map (the map only ever shows real dies -
        see _exec_draw_wafer_map) - but it may be ANY die belonging to
        the target shot (whichever square the picker/shot-window
        highlighted), not necessarily die #1's own cell. Same as Next
        Shot/Previous Shot (_exec_go_to_shot/_exec_current_shot_index):
        the Overlay dialog's confirmed row/col offset is what tells us
        WHICH shot a real coordinate falls in (floor-divide by the shot
        dimensions), then shot_die_rc() gives any die #'s cell within
        that shot to land on."""
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

        # A saved touchdown list carries one SITE row per DIE the recipe
        # references (e.g. Cenfire's "first"/"second" pair), not one per
        # SHOT - two dies of the same physical shot resolve to the same
        # (shot_row, shot_col) below, and visiting a shot once per row
        # measured it twice, then a third time, etc. Collapse to one
        # representative pick per shot, first-seen order, before the
        # run thread ever starts.
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
        # Used to be a binary "Full Die" vs. everything-else-is-"test" check,
        # from when this function's only two callers were the Minor Moves
        # variants of Full Die and Test Die. ▶ Run (_exec_start_run) is the
        # real, only caller now, passing mode_label="Run" - which that old
        # check silently mislabeled as "test" too, since it wasn't "Full
        # Die". cassette_panel._start_next_run trusts this label to decide
        # how to replay the next wafer, and "test" means "look for a
        # remembered Test Selected pick list" - one that a Minor Moves ▶ Run
        # never populates (see _exec_start_site_list's own "test"-only
        # bookkeeping). That silently broke cassette automation for every
        # Minor Moves recipe run via ▶ Run - confirmed live on Cenfire,
        # which is Minor Moves' whole reason for existing, while LaMP (no
        # Minor Moves, ▶ Run takes the plain _exec_start_site_list path
        # instead of this function entirely) never touched this bug at all -
        # not a Cenfire-specific gap, a real generalization one.
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
        """Tell _exec_run_steps_once/_exec_slot_identity where each die #
        in shot (shot_row, shot_col) really sits, and its real die ID -
        used by the full Minor Moves run (_exec_minor_move_thread's own
        publish_die_slots), and by _exec_publish_die_slots_at's own
        Minor-Moves branch, both of which already know the correct
        theoretical shot bucket (Minor Moves always lands ON die #1's own
        computed cell first - see goto_shot_die - so this grid math can't
        disagree with reality there). Everything else (Minor Moves off)
        goes through _exec_publish_die_slots_anchored instead - see its
        own docstring for why."""
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
            # (reticle row, reticle col, row WITHIN the shot, col WITHIN
            # the shot) - generic reticle/shot-position bookkeeping any
            # export format can read (see export_formats.py's
            # shot_row/shot_col/intra_row/intra_col source fields), not
            # tied to any one project's naming.
            shotpos.append((shot_row, shot_col, r, c))
        self._exec_die_rc_by_slot = rcs
        self._exec_die_ids_by_slot = ids
        self._exec_die_shotpos_by_slot = shotpos

    def _exec_publish_die_slots_anchored(self, row: int, col: int, shot_rows, shot_cols,
                                          shot_cells, row_offset, col_offset):
        """Same job as _exec_publish_die_slots_for, but anchored at the
        real die that was just touched instead of a theoretical,
        Overlay-origin-quantized shot bucket - see _exec_publish_die_
        slots_at's own note on why this is the correct one whenever
        Minor Moves is off.

        (row, col) IS die #1 of this shot, by the same convention the
        Wafer Builder Shot tab and every quad recipe already follow (a
        LaMP-style SITE only ever records the top-left die of its shot -
        see recipe_gen_panel.py). Every other die # in the shot template
        is (row, col) plus that die's own offset from die #1's cell in
        the template, so a touchdown that landed a row or column off the
        theoretical grid still gets every real die in its shot attributed
        to the right square - the failure this replaces put a touchdown
        that landed "between" two theoretical shots into whichever one
        the floor-division happened to round to, mislabeling every die
        in it.

        shot_row/shot_col are still computed, floor-division against the
        Overlay origin same as before, purely as descriptive bookkeeping
        for export formats that read a shot_row/shot_col field - never
        used here to derive a die's real position."""
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
        """Main-thread-only: resolve the STATIC per-run geometry (shot
        template dims/cells, Overlay offset) needed to file each Die#-
        tagged step's reading against its own real square within whatever
        shot a touchdown lands on - independent of Minor Moves. Minor
        Moves only ever gates whether a "move" step actually repositions
        the chuck between dies (_exec_move_fn) - a recipe that reaches
        every die in a shot through switch routing at ONE physical
        touchdown (LAMP: 4 dies, 4 different HI/LO pin pairs, zero chuck
        movement) still wants its readings split across the shot's 4 real
        squares, with Minor Moves off the whole time.

        recipe_gen._shot_dims()/_shot_cells read live Tk StringVars, so
        this is resolved HERE and handed to a background run thread as
        plain data - calling those off the UI thread can raise "main
        thread is not in main loop" (same class of bug
        _exec_start_minor_moves's own docstring already covers).

        Returns (shot_rows, shot_cols, shot_cells, row_offset, col_offset)
        - the STATIC part, constant for the whole run - or None if there
        is nothing to split (a plain 1x1 shot template - the ordinary
        single-die case) or no confirmed Overlay alignment exists yet to
        resolve a real (row, col) into a shot."""
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
        """Per-touchdown: publish where every die # in this shot really
        sits and its real die ID. Pure arithmetic on plain data (shot_geom
        was already resolved on the main thread), safe to call from a
        background run thread. Returns (shot_row, shot_col) - descriptive
        bookkeeping only, see below.

        Minor Moves ON: unchanged from before - which theoretical shot
        (row, col) falls in (floor-divide by the shot dims, same as Next
        Shot/Previous Shot and Minor Moves' own shot_rc_for), then publish
        that shot's slots. Safe here specifically because Minor Moves
        always lands ON die #1's own computed cell first (goto_shot_die),
        so the touched die can never disagree with the theoretical grid.

        Minor Moves OFF: (row, col) is wherever the chuck actually just
        touched down - by the same "SITE records only the top-left die"
        convention every quad recipe follows (LaMP whole wafer, etc.),
        THAT is die #1 of this shot, not necessarily on an exact multiple
        of the shot dimensions from the Overlay origin. A touchdown that
        landed between two theoretical shots used to get every die in it
        mislabeled from whichever shot the floor-division rounded to -
        anchoring off the real touched position instead means the dies
        published (and later coloured/exported) always match what
        physically just happened, however the chuck actually landed."""
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
        """Colour every die in the shot on its OWN real square from
        _exec_slot_verdicts (each die passes/fails independently), same
        rule the Minor Moves run thread already applies - falling back to
        colouring just the touched square with the combined verdict when
        the recipe never tagged a passfail step with a Die # (or nothing
        was published at all - rc_by_slot is empty, the ordinary
        single-die case).

        rc_by_slot is the caller's own captured copy of
        self._exec_die_rc_by_slot from right before this touchdown's
        per-run bookkeeping was cleared - the SAME real (row, col) list
        _exec_run_steps_once used to look up each slot's die ID (via
        _exec_publish_die_slots_for/_anchored, whichever applied), so a
        die's colour on the map and the die ID its reading was filed
        under can never disagree - previously this recomputed real_row/
        real_col independently from shot_row/shot_col, which agreed with
        the ID lookup only when Minor Moves was on; off, a touchdown that
        landed between two theoretical shots got coloured on the wrong
        squares even after _exec_publish_die_slots_anchored started
        filing the READING under the right die."""
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
        """Add this touchdown's result(s) to the Pass/Fail counters - once
        per REAL die in the shot when the recipe tagged its passfail steps
        with a Die #, not once per touchdown.

        A LAMP-style quad is one physical touchdown covering up to 4 real
        devices, measured through 4 different switch-routed pin pairs -
        Minor Moves off the whole time, since the chuck never moves between
        them. Without this, a quad with two real dies (one PASS, one FAIL)
        added exactly one tally for the whole shot, so the counters showed
        results by SHOT (touchdown) instead of by DIE - the same "counts
        shots, not dies" complaint _exec_run_steps_once's passfail-step
        found-is-None gap partly caused, but this half of it lives here,
        not there: even with slot_verdicts fully and correctly populated,
        nothing before this ever expanded ONE _exec_add_pass/_exec_add_fail
        call per touchdown into one per die for the OVERALL counters (only
        _exec_color_shot_squares, just above, already did that for the map
        squares).

        Every slot the shot template has is counted, whatever its die is
        called. This used to skip a slot whose ID was "NA" or blank, on the
        reasoning that such a corner "is not a real die" - but that is a
        judgement about a NAME, and it is not ours to make: "NA" is simply
        what one project calls some of its dies, and plenty of maps carry
        no IDs at all. A die is excluded only where the operator marked it
        so on the Wafer Builder Shot tab, which is what present_slots
        already applies when the slots are built - by the time a verdict
        exists for a slot, that slot is one the operator asked to probe.

        ids_by_slot is 0-indexed by die # - 1 (die #1 is index 0), built by
        _exec_publish_die_slots_for/_at right before the steps ran.
        """
        slot_verdicts = dict(getattr(self, "_exec_slot_verdicts", None) or {})
        if shot_geom is not None and slot_verdicts:
            counted = 0
            for die_num, passed in sorted(slot_verdicts.items()):
                counted += 1
                self._exec_safe_after(
                    self._exec_add_pass if passed else self._exec_add_fail)
            if counted:
                return
            # Nothing to count at all - fall through to the single combined
            # tally below rather than silently adding nothing for a real
            # touchdown that WAS measured.
        self._exec_safe_after(
            self._exec_add_pass if fallback_ok else self._exec_add_fail)

    def _exec_minor_move_thread(self, shots: list, my_token: int, overlay_offset: tuple,
                                 shot_rows: int, shot_cols: int, shot_cells: dict):
        """One touchdown per shot, exactly like the native G/J path - the
        difference is what happens AT that touchdown. A shot lands on die
        #1 automatically (same as any single-die touchdown lands on *a*
        die before anything runs), then the loaded recipe's steps run flat,
        top to bottom, once: a "move" step (see recipe_panel._STEP_TYPES)
        is what repositions to any OTHER die # within that same shot -
        nothing moves the chuck between dies on its own any more. Nothing
        moves it between SHOTS either, beyond that automatic die-#1
        landing on the next square.
        """
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
            """Which (shot_row, shot_col) a real absolute die coordinate
            falls in - same floor-division Next Shot/Previous Shot use
            (_exec_go_to_shot/_exec_current_shot_index)."""
            wb_row = pick_row - row_offset
            wb_col = pick_col - col_offset
            return wb_row // shot_rows, wb_col // shot_cols

        def publish_die_slots(shot_row, shot_col):
            """Tell _exec_run_steps_once/_exec_slot_identity where each
            die # in THIS shot really sits, and what its real die ID is -
            same publish-before-run pattern
            eg_pma_run_panel._advance_touchdown already uses for
            Electroglas quads, so every measurement is filed against the
            die it actually measured (matched by real XY position + the
            step's own Die # field) instead of the shot's landing square
            for all of them. Cleared after the shot in the caller.

            Body lives in _exec_publish_die_slots_for so the standalone
            Measure button (_exec_touchdown_then_measure) can do exactly
            the same publication for whichever shot the chuck is
            currently on, not just a full Minor Moves run."""
            self._exec_publish_die_slots_for(
                shot_row, shot_col, shot_rows, shot_cols, shot_cells,
                row_offset, col_offset)

        def goto_shot_die(pick_row, pick_col, die_num):
            """Separate, jump to (shot, die #), contact. Used both for the
            automatic die-#1 landing and for every in-recipe "move" step -
            the chuck must never travel in X/Y while contacted.

            (pick_row, pick_col) is a real absolute die coordinate that
            belongs to the target shot - not necessarily die #1's own
            cell (it may be whatever square the picker/shot-window
            highlighted). shot_rc_for() finds WHICH shot that is, then
            shot_die_rc() gives any die #'s cell to land on within it."""
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

                # Each die in the shot passes or fails on its OWN square,
                # not the shot's landing square for all of them - a shot's
                # dies are independent devices. Falls back to the single
                # combined verdict on the landing square only when the
                # recipe never tagged a passfail step with a Die #.
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
        """Header over the map. One pick names what sits on that square.

        On Accretech a square is one prober die, which for a quad product is a
        whole touchdown carrying up to four devices, so naming them answers
        "what comes down with this". Same ID sources and priority the exports
        use, so the header can never disagree with the recorded die_id.
        Multi-pick falls back to the count: this is only legible for one.
        """
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
        """Collapse picked map cells to one cell per PROBER TOUCHDOWN.

        On Accretech a square already is a touchdown, so this is a no-op. On
        Electroglas a square is a die and a 2x2 shot owns four of them - the
        chuck lands once and the recipe switches the mux through the dies
        under it, so clicking all four dies of a shot must still produce ONE
        touchdown, not four visits to the same place.
        """
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
                # Not part of any touchdown this recipe knows - keep it as
                # itself rather than dropping the operator's selection.
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
        """The inverse: every cell belonging to the touchdowns in `picks`.

        Used for DISPLAY, so selecting a recipe lights up whole shots rather
        than one corner die of each.
        """
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
        """die_id (label string) -> (row, col), from the CURRENTLY LOADED
        wafer map - the ground truth for what is physically on each square.

        Electroglas only. A recipe's or PMA's own touchdown resolution can
        be wrong (its geometry math, not the map's) - so a site naming a
        die_id is matched against where the map itself says that die_id
        lives, rather than trusted against whatever (row, col) the recipe
        computed for it. A site whose die_id isn't on the map at all is
        left unmatched rather than guessing a square from bad coordinates.

        Only UNIQUE labels make it in (a die_id that appears at exactly one
        square on the loaded map) - a repeated label (e.g. every PCM
        structure literally named "PCM") cannot be told apart by the id
        alone. _exec_resolve_site_cells checks the site's own (row, col)
        against the map directly first, before ever consulting this table,
        so a repeated-label site with its correct position already recorded
        still resolves right without needing to be unique here.
        """
        counts, single = {}, {}
        for rc, label in (self._exec_wafer_map.die_ids or {}).items():
            if not label:
                continue
            counts[label] = counts.get(label, 0) + 1
            single[label] = rc
        return {label: rc for label, rc in single.items() if counts[label] == 1}

    def _exec_resolve_site_cells(self, sites) -> list:
        """(row, col) to select for each recipe/PMA touchdown `sites` entry
        (dicts with row/col and optionally die_id).

        Accretech: unchanged - the site's own (row, col) is used as-is (a
        Minor Moves recipe's per-slot row/col is itself authoritative there,
        see _exec_apply_recipe_sites's docstring).

        Electroglas, per site:
          1. If the site's own (row, col) is already where the map says
             that exact die_id lives, use it directly - confirmed correct,
             regardless of whether the label repeats elsewhere on the
             wafer (e.g. every PCM site is literally named "PCM").
          2. Otherwise, if die_id is unique on the map, look it up there.
          3. Otherwise fall back to the site's own (row, col) and log it -
             an unconfirmed, possibly-wrong touchdown resolution (e.g.
             21PCM's currently-unreliable one) shows up as a warning
             instead of silently landing on the wrong square.
        """
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
                    # A site saved when a touchdown WAS a whole shot names
                    # the quad ("93-01/83-71/93-02/83-72"), and no single
                    # die on the map is called that - the electrical gauge
                    # recipe's 13 sites are all of this form. A touchdown
                    # is one die now, so resolve it to the first real die
                    # the shot names, in slot order. That is the same die
                    # Pull Shots would pick for the shot today, and the run
                    # measures the whole shot from whichever of its dies
                    # the chuck lands on, so nothing else has to change.
                    # Whichever component the map can actually place. No
                    # label is special-cased: a name like "NA" or "TARGET"
                    # simply is not unique on the map, so it does not
                    # resolve, and the next component is tried.
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
        # This gets called more than once for the same recipe during a
        # single folder load/autoload pass (the Run tab's own recipe
        # dropdown load, plus whichever other path re-applies the saved
        # bench/CSV-import recipe) - logging every call printed the exact
        # same warning 2-3 times in a row. Only the outcome changing is
        # actually new information.
        outcome = (unmatched, len(sites))
        if unmatched and outcome != getattr(self, "_exec_resolve_mismatch_last", None):
            self._exec_resolve_mismatch_last = outcome
            self._exec_log(
                f"[RUN] {unmatched} of {len(sites)} touchdown(s) named a die ID "
                "that doesn't match the map at its own (row, col)")
        return resolved

    def _exec_preselect_align_die(self):
        """Re-sort the Run tab's "Chuck is on" list for the loaded recipe.

        The work is EgPmaRunPanel._fill_anchor_choices', which moves the
        recipe's align die to the front of that list; this just asks it to
        rebuild now that a different recipe is loaded. Rebuilding rather
        than setting the box directly means the hoist survives every later
        rebuild of the list (a map reload, a folder change) instead of
        being a one-shot assignment that the next rebuild undoes.
        """
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
        """Load a saved Wafer Builder map by name and make it the active
        one everywhere that matters, not just on the Wafer Builder tab's
        own Die Map view.

        _load_named_map only updates that tab's own Shot/Shot Map/Die Map
        state (same as picking it from that tab's own Map: dropdown by
        hand) - _sync_views is what actually publishes that onto the Run
        tab, the same publish Save Wafer Map/LOAD ALL already do. Unlike a
        raw .PMA import (deliberately left unpublished until the operator
        reviews and saves it - see pma_process_panel.load_all), a NAMED
        map is a previously-saved, already-reviewed artifact, so there is
        nothing here worth holding back for a manual Save Wafer Map press.

        Accretech only: _sync_views does NOT touch the Overlay alignment -
        that offset is saved as part of the map's own JSON (restored into
        self._exec_overlay_row_offset/_col_offset/_offset_confirmed by
        _load_named_map's own _state_from_dict call) but nothing redraws
        it from there automatically. _exec_reapply_overlay is what does,
        same call load_ata_folder itself makes right after drawing a
        folder's Accretech map - skipping it here would leave the newly
        loaded map's overlay saved-but-invisible until the next folder
        reload happened to trigger it.

        Shared by the Run tab's own Wafer Map: dropdown
        (_exec_on_wafer_map_picked) and _exec_autoload_recipe_wafer_map,
        so the two can never disagree about what "switch to this map"
        actually does.
        """
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
        """Run tab's own Wafer Map: dropdown, next to Probe Card - a
        direct pick-and-load, unlike the Recipe tab's own Wafer Map:
        field (which only records a preference, applied automatically by
        _exec_autoload_recipe_wafer_map whenever that recipe loads)."""
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
        """Switch to the loaded recipe's own saved wafer map, if it names
        one and it isn't already the active map - and either way, show
        that name in the Run tab's own Wafer Map: dropdown.

        recipe_panel.get_wafer_map() is the recipe's preference (set from
        its own Wafer Map: dropdown, next to Probe Card - see
        RecipePanel._on_wafer_map_pick); recipe_gen.map_name_var is
        whatever the Wafer Builder tab actually has loaded right now.
        Blank preference (a recipe saved before this existed, or one that
        was never assigned one) is left alone entirely - nothing to
        autoload, and nothing to warn about.

        The dropdown update used to happen only at the end, after an
        actual switch - so a recipe whose saved map ALREADY matched
        (e.g. a folder's default recipe on a fresh open, where Wafer
        Builder's own autoload already picked the same default map) hit
        the `wanted == active` no-op and returned before ever touching
        _exec_wafer_map_var, leaving the dropdown blank even though the
        right map genuinely was loaded - looked exactly like the feature
        did nothing at all.
        """
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
        # _load_named_map pops an error dialog for a name it can't find -
        # right for a deliberate pick from a Map: dropdown, wrong for an
        # automatic check that runs every time this recipe loads (a
        # renamed/deleted map would otherwise interrupt with the same
        # popup on every folder open). Checked quietly first instead.
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
        """The recipe the Run tab currently has loaded, if any."""
        if not getattr(self, "_exec_steps", None):
            return ""
        try:
            return self.recipe_panel.get_active_recipe() or ""
        except Exception:
            return ""

    def _exec_load_selected_map(self, quiet_if_missing: bool = False):
        # Only ever a recipe's own touchdown list - the standalone
        # "Load Selected Map" button is gone (picking a recipe from the
        # dropdown already does this, via _exec_apply_recipe_sites/here),
        # and the old
        # folder-level CSV fallback used to load a stale selection even with
        # no recipe loaded at all. With no recipe loaded there is nothing to
        # select, so this is now a no-op rather than resurrecting whatever
        # was last saved to that file.
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
        # Accretech-only: a Minor Moves recipe's own SITE table knows the
        # true per-slot die_id inside a shot square, which the map itself
        # cannot label (see _exec_apply_recipe_sites for the full reason).
        # On Electroglas the Wafer Builder map IS the ground truth for die
        # IDs - a recipe (especially one whose touchdown resolution isn't
        # trusted yet, e.g. a PMA-based recipe) must only ever select/
        # highlight squares here, never relabel them.
        #
        # MERGED into the existing overlay, not _exec_clear_overlay()+
        # replaced - a recipe selects touchdowns, it does not get to
        # redefine what the wafer map itself already knows about every
        # OTHER die. This used to wipe _exec_overlay_die_ids down to just
        # this recipe's own (often smaller) touchdown list on every recipe
        # switch, so a die ID shown under one recipe would visibly vanish
        # the moment a different recipe on the same folder - with fewer or
        # different touchdowns - was picked (confirmed: CENFIRE-INLINE_
        # probe08 vs. TEST on the same physical wafer). The redraw calls
        # below already clear and repaint their own canvas label items
        # every time regardless, so nothing here depends on the dict
        # itself having been emptied first.
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
        """The real, full-story entry point: the recipe's own saved
        touchdown list (Recipe tab's Touchdowns table - the same list
        Take from map selection/Take die IDs/Pull shots build), Minor
        Moves if the recipe has it on, all of it - unlike Full Die/
        Test Selected, which are deliberately the plain single-die case
        only. No saved touchdowns and Minor Moves off falls back to the
        same native whole-wafer G/J walk Full Die does; no saved
        touchdowns and Minor Moves on falls back to landing on EVERY real
        die on the map and treating each as its own shot's die #1 - not
        actually one touchdown per real shot (the map carries no shot-
        boundary info of its own to enumerate those from). Save a proper
        touchdown list (one entry per shot) rather than relying on this
        fallback for a real run.
        """
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
        """[{"row","col","die_ids","raw_text"}] from the Wafer Builder's Die
        Map, in die-pitch units - the same shape pma_shots_to_grid produces,
        so centroid_offset/merge_with_accretech work unchanged.

        This is the ONLY overlay source now. Accretech's Wafer Builder no
        longer keeps a PMA/Recipe Generator/CSV source loaded in memory the
        way the old three-map page did - it IS the wafer definition, so
        overlaying is always "the Wafer Builder map onto the Accretech map"
        rather than a choice of legacy sources.
        """
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
        """Every (row, col) Wafer Builder considers part of the wafer -
        present in a real shot, whether or not that die has been named yet
        - same row/col units as _exec_wafer_builder_grid() (which only
        keeps the NAMED subset, for labeling). This is what bounds
        Overlay's SELECTION to Wafer Builder's actual footprint - a wafer
        with fewer real shots than the Accretech extraction has die
        positions should select fewer squares, not the whole Accretech map.
        Empty if Wafer Builder has no map/shots defined at all, which
        _exec_overlay_all_accretech treats as "nothing to bound by" and
        falls back to selecting everything (unchanged from before)."""
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
        """One overlay entry per square the Accretech map actually has AND
        that falls within Wafer Builder's own footprint (see
        _exec_wafer_builder_footprint) once the offset is applied - unlike
        merge_with_accretech (which drops any square the Wafer Builder grid
        has no real ID for), this covers every REAL shot Wafer Builder
        defines, labeling whichever of them also got a real ID. `footprint`
        empty/None (no Wafer Builder map loaded at all) falls back to
        selecting the whole Accretech map, same as before - there is
        nothing to bound by."""
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

    _EXEC2_OVERLAY_MIN_DIE_PX = 22  # below this on-screen die width, overlay text is unreadable clutter

    def _exec_update_overlay_visibility(self):
        if not self._exec_overlay_items:
            return
        wm = self._exec_wafer_map
        # Measure a die that is actually ON the currently drawn map.
        #
        # This used to take next(iter(...)) - one arbitrary cell - and give
        # up if it could not be measured. _exec_overlay_die_ids is MERGED
        # rather than replaced (see _exec_overlay_apply and
        # _exec_load_recipe), so it can hold cells from a map that is no
        # longer drawn; when the arbitrary one happened to be such a cell,
        # bbox came back None and this returned with the labels left in
        # whatever state they already had. If that state was "hidden" from
        # an earlier zoom-out, every die ID stayed invisible until some
        # other event re-ran this with a luckier sample - which is exactly
        # what "the die IDs vanished, then came back on their own" looks
        # like, and why it was intermittent: which cell iteration yields
        # first changes as the dict is merged.
        bbox = None
        for rc in self._exec_overlay_die_ids:
            item = wm.dies.get(rc)
            if item is None:
                continue
            bbox = wm.canvas.bbox(item)
            if bbox:
                break
        if not bbox:
            # Nothing measurable at all. Show the labels rather than
            # leaving them hidden: a stuck-visible label is self-correcting
            # on the next redraw, a stuck-hidden one looks like data loss.
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
        """Wrap fn so a burst of calls (a fast mouse-wheel scroll, or a
        middle-drag pan - both fire many events a second) collapses into
        one call ~delay_ms after the last one in the burst, rather than
        running fn on every single event. Used for the overlay label
        rebuild, which is what made zoom/pan noticeably laggy on a wafer
        map with many die-ID labels showing - this changes how OFTEN that
        rebuild runs, not what it does or when the underlying data changes.

        pending_attr names a per-caller instance attribute that holds the
        pending after() id, so two different debounced callbacks (e.g. the
        Run tab map and the Results tab map) do not cancel each other.
        """
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
        # Explicitly clear first: a full redraw has already wiped the canvas,
        # but a ZOOM has not - it scales items in place - so without this the
        # old labels would survive alongside the new ones.
        self._exec_clear_overlay_labels(self._exec_wafer_map,
                                         self._exec_overlay_items)
        if self._exec_overlay_die_ids:
            self._exec_overlay_items = self._exec_draw_overlay_labels_on(
                self._exec_wafer_map, self._exec_overlay_die_ids)
        else:
            self._exec_overlay_items = []
        self._exec_update_overlay_visibility()
        # The PMA runner's "you are here" box is drawn on this same canvas and
        # is wiped by the same rebuild, so it re-draws off the one hook rather
        # than competing for on_redraw.
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
        """Writes the Overlay's current alignment into the active Wafer
        Builder map's saved JSON immediately, the moment it's confirmed (or
        cleared) - NOT deferred until some later save.

        Confirming Overlay is its own action; the Recipe tab's own ⬅ Take
        from map selection (recipe_panel._sites_from_map) saves the loaded
        RECIPE's touchdown list, not the map file, and pressing it is not
        guaranteed to happen right after Overlay at all. Without this, the
        alignment only ever lived in the self._exec_overlay_* instance
        attributes and was gone the moment the app closed - see
        recipe_gen_panel._state_to_dict/_state_from_dict for the fields this
        writes, and _exec_reapply_overlay for the restore side.
        """
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
        """Redraws the Overlay's saved alignment against whatever Accretech
        map/Wafer Builder grid this ATA folder just loaded.

        The row/col offset and confirmed flag are restored earlier, by
        recipe_gen_panel._state_from_dict (loaded together with the Wafer
        Builder map itself, since that offset is meaningless without knowing
        which map it was confirmed against) - this just re-draws from them,
        called from load_ata_folder AFTER the Accretech map is actually on
        screen (_exec_overlay_accretech_rc needs self._exec_wafer_map.dies
        populated, which is not true yet at state-restore time during a
        folder switch). A no-op if nothing was ever confirmed, or if the
        Accretech map turned out empty (e.g. Overlay was confirmed against a
        wafer map source that is no longer loaded).

        Accretech-only: the Overlay sub-tab itself only exists there (see
        _tab_pma_wafer/_exec_build_overlay_tab), reconciling the Accretech
        hardware-extracted map against the Wafer Builder grid.
        The confirmed flag/offsets live in the Wafer Builder map's own JSON
        though, which is shared and cross-synced between both systems (see
        recipe_gen_panel's "CROSS-SYSTEM SYNC") - so an Accretech-confirmed
        overlay was silently reapplied here on Electroglas too, selecting
        every matched die (2000+ on a real wafer) on a folder that was never
        overlaid on that bench at all.
        """
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
        # Cached: tkfont.Font is not free to build, and this runs per zoom step.
        if getattr(self, "_overlay_font_obj", None) is None:
            self._overlay_font_obj = tkfont.Font(family=self._OVERLAY_FONT[0],
                                                 size=self._OVERLAY_FONT[1])
        return self._overlay_font_obj

    def _exec_label_min_px(self) -> float:
        # See _exec_label_min_px_var's own comment (__init__) - shared with
        # the Wafer Builder Die Map tab's "Label min width (px):" Spinbox.
        try:
            return float(self._exec_label_min_px_var.get())
        except (tk.TclError, ValueError):
            return 22.0

    def _exec_labels_fit(self, wm, die_ids_by_rc: dict) -> bool:
        """Is a die currently drawn big enough to hold its ID?

        Zoomed out, a whole-wafer map draws dies a few pixels across and the
        IDs collapse into an unreadable smear, so they are not drawn at all
        until there is room. Measured with the real font rather than guessed,
        against the LONGEST label, so a quad ID like 'TARGET' does not
        overflow its neighbour.

        _exec_label_min_px() is an extra, operator-adjustable floor on top
        of that - raising it hides labels until zoomed in further even if
        they would already fit text-wise; it can never cause an overflow,
        since the text-fit check above still applies regardless of where
        the floor is set.
        """
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
        # Labels only for cells with a real ID - every cell in `matched`
        # still gets SELECTED below regardless, since Take from map
        # selection acts on the selection, not on which squares happened
        # to get a label. See _exec_overlay_all_accretech.
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

    # -- Overlay (moved from the Run tab's "Overlay…" popup onto its own
    # Wafer Builder sub-tab, inserted by _tab_pma_wafer - same underlying
    # process/state (_exec_overlay_row_offset/_col_offset/_offset_
    # confirmed, _exec_overlay_die_ids, _exec_draw_overlay, _exec_
    # persist_overlay_offset, _exec_clear_overlay, centroid_offset), just
    # embedded controls instead of a Toplevel, plus its own small preview
    # map so the alignment is visible without needing the Run tab open at
    # the same time (a modal dialog could float over it; a separate
    # top-level tab can't). -----------------------------------------------

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
        # Same reason the Run tab's own map wires these: die-ID labels are
        # only drawn once a die is big enough on screen to hold the text
        # (_exec_labels_fit) - without a hook here, zooming this preview
        # in (what an operator actually needs to do to read the IDs and
        # judge whether the overlay is centered correctly) never re-checked
        # that and the labels just never appeared, or stayed wherever they
        # were before the zoom. _reset_view (double-click) already routes
        # through on_zoom too (see WaferMapPanel), so one hook covers both.
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
        """Called whenever the Wafer Builder > Overlay sub-tab is selected -
        same "always freshly recomputed on arrival" approach Die Map already
        uses (see recipe_gen_panel._on_subtab_changed), so revisiting this
        tab never shows a stale preview from before the ATA folder/Die Map
        last changed."""
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
        # Full Die/Test Selected are the plain "walk the dies, measure"
        # entry points - Minor Moves is ▶ Run's job now, not theirs. See
        # _exec_start_full_die's matching refusal.
        if self._system == "accretech" and self._exec_minor_moves_active():
            self._exec_log("[RUN] Test Die: this recipe has Minor Moves on — "
                            "use Run instead.")
            return
        self._exec_start_site_list(sites, "Test Die", "test")

    def _exec_start_site_list(self, sites: list, mode_label: str, run_mode: str):
        """Shared starter for a fixed list of (row, col) touchdowns - Test
        Die/Test Selected's picks, or ▶ Run's saved touchdown list."""
        # enable_picking(0) just below clears the map's picks (so nothing
        # new can be clicked mid-run) - which for Test Selected specifically
        # means get_picked() comes back EMPTY the instant this run starts,
        # not just after it finishes. Cassette automation repeating "test"
        # mode for a later wafer used to read get_picked() at that point and
        # find nothing, silently falling back to 5 random sites instead of
        # the operator's real selection - remembered here, before it's
        # wiped, so cassette_panel._start_next_run can replay the exact same
        # sites instead of relying on live map state that no longer exists.
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
        # Resolved here, on the main thread - see _exec_prepare_shot_geometry.
        # Each site may be ANY die of its shot (not necessarily #1) - that's
        # fine, floor-dividing by the shot dims resolves to the same shot
        # regardless of which one was picked.
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
                # Pass/Fail counters are updated inside _exec_zup_measure_zdown
                # itself now (see _exec_tally_shot_result) - once per real
                # die in the shot, not once per touchdown.

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
        """If this ATA folder has a default recipe marked (Recipe tab's ⭐ Set
        as Default), switch to its probe card if needed and load it straight
        into the Run tab — same effect as manually picking it from the
        Recipe dropdown, just automatic on ATA folder open."""
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
        """Keep the Run tab's own Wafer Map: dropdown honest after an ATA
        folder switch, and make sure SOMETHING is actually active.

        _exec_autoload_recipe_wafer_map (called from _exec_load_recipe)
        already keeps the dropdown in sync whenever a default recipe
        exists and gets auto-loaded - but _exec_autoload_default_recipe
        returns immediately, before ever touching the Run tab, when this
        folder has no default recipe marked at all. That left both the
        dropdown AND the actual active map exactly whatever
        recipe_gen.autoload_map_for_folder's own fallback chain happened
        to land on (its saved default marker, else a map literally named
        "Autoload", else the single map if there is only one) - which is
        blank whenever a folder has more than one saved map and no
        marker naming either. This is the backstop: reflect whatever
        ended up active in the dropdown either way, and if nothing did,
        just load the first saved map rather than leaving the Run tab
        with no wafer map at all.
        """
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
        """Select the recipe's touchdowns on the map, if it defines any.

        This is what makes the touchdown list the recipe's property rather
        than the ATA folder's: loading a recipe re-picks its own dies, so
        switching recipes can no longer inherit the previous one's selection.
        A recipe with no list clears the selection too (the run then walks
        every die - see _exec_start_run) - it used to leave the map alone
        instead, which meant editing the Recipe tab's Touchdowns table down
        to zero (Remove Selected/Clear All) and saving left the PREVIOUS
        selection highlighted on screen, looking like the save had not
        taken effect.
        """
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
        # A Minor Moves recipe's own SITE table carries one row per DIE it
        # actually references (Cenfire's "first"/"second" pair), each with
        # its own real (row, col) AND its own real die_id - the loaded
        # wafer map file, by contrast, is shot-granularity (one label per
        # shot square, not per RuOx/Au sub-position), so publish_die_slots'
        # per-slot lookup missed the second die of every shot and silently
        # fell back to the shot-level id (the first die's). The recipe's
        # own site records are the authoritative source for exactly the
        # dies this recipe is about to measure - always adopt them here,
        # same as _exec_load_selected_map does, rather than depending on
        # that function happening to run again after a recipe is picked
        # (it does not - it only fires on initial map draw, before a
        # recipe is normally loaded yet, or as a Test Selected fallback
        # that never triggers once cells are already highlighted).
        #
        # Accretech-only (see the matching note in _exec_load_selected_map):
        # on Electroglas the Wafer Builder map is the ground truth for die
        # IDs - loading a recipe (or a PMA-derived one) must only select/
        # highlight squares, never relabel them with its own touchdown data.
        #
        # Minor Moves-only, too: that recipe type's own SITE table really
        # does carry one row per real die it measures, each with its own
        # true die_id (the comment above explains why that has to win over
        # the shot-level overlay). A basic recipe's touchdown list is NOT
        # that - e.g. Recipe tab's "Pull Shots" only ever fills in die #1 of
        # each shot, so treating IT as "the complete overlay" here wiped
        # every other die's label off the whole map (Run tab AND Results
        # tab) the moment the recipe was saved, even though Pull Shots/Push
        # to Map themselves never touch the map beyond highlighting.
        if self._system == "accretech" and self.recipe_panel.is_minor_moves():
            ids = {(s["row"], s["col"]): s["die_id"] for s in records if s.get("die_id")}
            if ids:
                # MERGED into the existing overlay, not cleared+replaced -
                # same fix and same reasoning as _exec_load_selected_map's
                # own version of this. Two Minor Moves recipes on the same
                # physical wafer can carry different-sized touchdown lists
                # (e.g. Cenfire's CENFIRE-INLINE_probe08, a filtered subset,
                # vs. TEST, the full original) - wiping the overlay down to
                # only the just-loaded recipe's own IDs made every OTHER
                # die's label vanish the moment a smaller-touchdown recipe
                # was picked, even though the two recipes describe the same
                # real wafer and a recipe is only supposed to select
                # touchdowns, never redefine what the map itself knows.
                self._exec_overlay_die_ids = {**(self._exec_overlay_die_ids or {}), **ids}
                self._exec_redraw_overlay_on_run_map()
                self._exec_redraw_overlay_on_results_map()

    def _exec_load_recipe_by_name(self, name: str):
        """Save button on the Recipe tab calls this too, so saving a recipe
        also loads it into the Run tab - redundant with picking it from the
        Run tab's own Recipe dropdown, on purpose."""
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
        # Before site resolution/map highlighting below, which both read
        # whatever map is CURRENTLY active - switching it after would
        # leave them working off the map this recipe just replaced.
        self._exec_autoload_recipe_wafer_map()

        self._exec_steps_tree.delete(*self._exec_steps_tree.get_children())
        for i, s in enumerate(self._exec_steps, 1):
            self._exec_steps_tree.insert("", "end", values=(
                i, s.get("name", ""), s.get("type", ""), s.get("conn", "")))
        self._exec_steps_var.set(f"{name} — {len(self._exec_steps)} step(s)")
        self._exec_apply_recipe_sites(name)
        # _exec_apply_recipe_sites only updates the map highlight - the
        # EG Run tab's own Die list marks which rows are "probed by this
        # recipe" from a SEPARATE read of the same recipe (_probe_seqs),
        # and nothing told it to re-read that on a recipe switch, so the
        # list kept showing the previous recipe's checkmarks even though
        # the map highlight (and everything else) had already moved on.
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
        # _exec_run_steps_once refuses to run at all while this is True
        # (see its own abort check) - every REAL run resets it at start,
        # but this manual one-shot path never went through a run starter,
        # so it kept whatever ⏹ Stop Run last left behind. A single Stop
        # Run press, at any point earlier in the session, permanently
        # broke every later Measure click with "stopped before step 1"
        # until the app was relaunched.
        self._exec_aborted = False
        if self._system == "electroglas":
            self._exec_measure_here_eg()
            return
        # Resolved here, on the main thread, not inside the background
        # thread below - see _exec_prepare_shot_geometry's own note.
        shot_geom = self._exec_prepare_shot_geometry()
        threading.Thread(target=self._exec_touchdown_then_measure,
                         args=(shot_geom,), daemon=True).start()

    def _exec_measure_here_eg(self):
        """Measure button, Electroglas: the same thing a run touchdown does.

        Straight through EgPmaRunPanel._measure_here, which is what the run
        itself calls, so one press produces exactly what one touchdown of a
        run produces: Z verified up (_ensure_contact raises it if the chuck
        is not in contact, rather than measuring open air), the whole shot's
        per-slot die IDs and map cells published for the engine, the recipe
        run once, then each slot's verdict recorded, painted on its own
        square, tallied, and written into controller.die_status so an export
        sees it.

        It used to go down the generic path, which resolves its shot
        geometry from the Wafer Builder tab's live entry boxes and a
        confirmed Accretech Overlay alignment. Electroglas has neither, so
        shot_geom came back None: no slots were published, no per-die
        results were recorded, and the only thing a Measure press produced
        was a log line.
        """
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
        # File each step's reading against the die it actually measured
        # (by the step's own Die # field), not always the shot's landing
        # square - same publish-before-run pattern every real run uses
        # (_exec_zup_measure_zdown). Works with or without Minor Moves -
        # see _exec_prepare_shot_geometry and _exec_publish_die_slots_at.
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

        # Colour the square(s) this measurement actually covered - Measure
        # used to never paint PASS/FAIL at all, only the log line showed
        # anything.
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
        # How long to wait AFTER the bias/output for THIS step is on but
        # BEFORE its first reading - separate from avg_delay (the gap
        # between averaged readings once already reading). Covers both a
        # plain "give the instrument a moment" delay and "I'm biasing while
        # measuring, wait for the bias to settle before recording" - see
        # recipe_panel._STEP_FIELDS' own comment on "settle_delay".
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
        """True if this step's SETUP calls (set_voltage/set_current_limit/
        set_nplc/set_current_range/set_source_delay/... - whatever actually
        configures the instrument, as opposed to triggering a reading)
        should be sent right now; False if they were already sent, with
        this EXACT signature, the last time this step ran, and can safely
        be skipped.

        Why: the recovered old LaMP exe's own SCPI trace configures the
        SMU ONCE per wafer, not once per die - this recipe engine instead
        resent the full setup (level, limit, NPLC, range, source delay) on
        EVERY touchdown of EVERY die, which is most of the ~4x GPIB
        traffic gap between this engine's runtime and the original exe's.
        Turning the output on/off and the actual read still happen every
        touchdown regardless (a relay closing onto a different die each
        time is real, physical work, not configuration) - only the value-
        setting calls are gated here.

        Generic, not hardcoded to any one recipe/step: `step` is keyed by
        Python object identity (id(step)) - the SAME step dict instance is
        what runs on every touchdown of a wafer walk (see
        _exec_run_steps_once's `steps` argument), so this cache only ever
        matches "the same step, run again" - never two different steps
        that merely look similar. `sig` is whatever tuple of the step's
        OWN resolved config values the caller cares about - if a step's
        fields genuinely differ between touchdowns (nothing in this
        codebase does that today, but nothing here assumes it can't), the
        signature changes and this returns True again, same as a brand
        new step would. Reset every run start - see _exec_reset_counts.

        Gated on the loaded recipe's own Shortcut checkbox (RecipePanel.
        is_shortcut()) - OFF (always return True, i.e. always configure)
        unless a recipe has explicitly opted in. Real-hardware testing
        (Maddy TL, Keithley 2400) found a case where skipping a resend
        left voltage compliance at the instrument's own default instead
        of the recipe's configured limit - not something to assume safe
        for every recipe/instrument combination without the operator
        deliberately verifying it on the bench first.
        """
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
        """Average a reading, on the instrument itself where it can do it.

        The 2400 averages internally (sens:aver:coun N with tcon rep) and
        returns the mean from ONE :READ?. Averaging in software instead meant N
        separate :READ?s - and with sour:clear:auto on, that is N source cycles
        per die rather than one. Slower than the original LaMP executable, and
        audible: each cycle re-applies the bias to a discharged path, which can
        trip the compliance beeper.

        Falls back to the software loop for anything without set_averages (the
        DMM path), and if the instrument refuses, so a recipe's Averages value
        is always honoured one way or the other.
        """
        # averaged_reading_ok lets a driver advertise set_averages while saying
        # its averaged read is not trusted yet (the 3458A). Anything that does
        # not define it is treated as fine, which is the verified default.
        trusted = getattr(smu, "averaged_reading_ok", True)
        can_hw = (avg_count > 1 and smu is not None
                  and getattr(smu, "inst", None) is not None
                  and hasattr(smu, "set_averages") and trusted)
        if can_hw:
            try:
                # Same "don't resend it if it's already set" principle as
                # _exec_should_configure, just keyed on (instrument,
                # channel) instead of step identity - this helper has no
                # step dict to key off of, and avg_count is the only thing
                # it ever configures.
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
        # Make sure the instrument is NOT also averaging, or the software loop
        # would average an already-averaged value.
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
        """recipe_panel's "Absolute Value" checkbox (step field abs_value) -
        applied right after the raw reading comes back, so the target
        calc, pass/fail compare, log line, and recorded/painted value all
        agree on the same (possibly rectified) number."""
        return abs(value) if (step.get("abs_value") or "").strip() else value

    def _exec_switch_driver(self):
        """The relay card a recipe's conn channels refer to on this system.

        Accretech has one matrix registered as "switch". Electroglas registers
        its three cards as relay1/relay2/relay3 and has no "switch" at all, so
        looking that key up returned None and every measurement step silently
        took the sim path - random.gauss() numbers recorded as if they were
        readings. relay1 is the wired card on both Electroglas benches
        (probe02's E1345A, probe03's E1364A), per hp_switchbox.BENCH_WIRING.
        """
        drivers = self.controller.drivers
        if self._system == "accretech":
            return drivers.get("switch")
        return drivers.get("relay1") or drivers.get("switch")

    def _exec_apply_target(self, s, raw_value: float, raw_unit: str, readings_by_name: dict):
        """Combine a measure step's own raw reading with its Target step's
        already-recorded value into a derived quantity - e.g. force current
        on an earlier step, measure voltage here, get resistance out (see
        recipe_panel.compute_target_derived). No Target, an unresolved
        Target, or a unit pairing with no known calculation all fall back to
        the raw reading unchanged - a passfail after this step must always
        get a real value, never a silent None.

        Returns (value, unit, note) - note is a ready-to-append log
        fragment ("" when there was no Target to apply).
        """
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
        """Which driver object a step's SMU/DMM branch should actually use.

        `s["instrument_key"]`, when set (see recipe_panel._refresh_
        instrument_dropdown - only ever non-blank when the operator picked
        a SPECIFIC slot from more than one fitted instrument of that
        family), names a controller.drivers key directly. Blank (every
        recipe saved before this field existed, and every bench with only
        one instrument of the family - the overwhelming common case)
        resolves to `family_default_key` instead, i.e. exactly the fixed
        "smu"/"dmm" key this method's callers already used before this
        existed - so a step with no instrument_key behaves byte-for-byte
        as it always has. `fallback_driver` is that same already-resolved
        default object, used if the key (whichever one applies) simply
        is not a currently-connected driver."""
        key = s.get("instrument_key") or family_default_key
        drv = self.controller.drivers.get(key)
        return drv if drv is not None else fallback_driver

    def _exec_apply_terminals(self, s: dict, drv):
        """s["terminals"] ("FRONT"/"REAR", set on the Recipe tab only when
        the step is direct-wired - see recipe_panel._apply_route_state)
        applied once at the top of this step, before it sources or
        measures anything. Blank (every step saved before this field
        existed, and every switch-routed step - the UI disables the
        control for those) does nothing, same as always. A driver with
        no set_terminals (anything but Keithley2400 today) just logs and
        moves on rather than raising - this is a convenience for
        instruments that support it, not a requirement every driver has
        to implement."""
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
        """Run the loaded recipe's steps once, top to bottom, against
        wherever the chuck currently sits.

        `steps` defaults to every loaded step (self._exec_steps) - the
        normal case; callers with their own already-resolved subset (e.g.
        a per-die replay) can still pass one explicitly. Minor Moves runs
        the full flat list unchanged - a "move" step is what repositions
        the chuck to a different die within the current shot mid-list (see
        self._exec_move_fn, set by _exec_minor_move_thread /
        eg_pma_run_panel._minor_move_thread before this is called; a "move"
        step with no such context set just logs and is skipped).
        """
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
        # The override first: _exec_die_num is an Accretech-only counter, so on
        # Electroglas it is always 0 and this fell through to the XY label -
        # which was unset, so every exported row read "X: — Y: —".
        die_label = (getattr(self, "_exec_die_id_override", "")
                     or (self._exec_die_var.get().replace("Die: ", "")
                         if self._exec_die_num else
                         self._exec_xy_var.get().replace("\n", " ")))

        cur_row, cur_col = self._exec_current_rc or (None, None)
        # Two possible die-ID sources, in priority order:
        #   1. The Overlay dialog's manual die IDs (self._exec_overlay_die_ids)
        #      — an explicit user action, so it wins if set.
        #   2. The currently-loaded wafer map's own ID column (e.g.
        #      Electroglas's "device_id"), captured by WaferMapPanel into
        #      .die_ids at load time — this is the map's real, authoritative
        #      ID and should be used automatically without any extra step.
        # Whichever wins, it's the same "die_id" every export format reads,
        # so the export always matches what the map/overlay actually shows.
        map_die_id = (self._exec_wafer_map.die_ids.get((cur_row, cur_col), "")
                      if cur_row is not None else "")
        overlay_die_id = (self._exec_overlay_die_ids.get((cur_row, cur_col), "")
                          if cur_row is not None else "")
        #   0. A shot-level override set by the Electroglas run, which knows the
        #      whole touchdown ("NA/92-74/NA/93-70") rather than the single die
        #      under one map cell. fldDieID names the SHOT in LaMP's schema -
        #      fldSwitch 1..4 is what picks the die within it - so exporting one
        #      corner's ID made the row claim the wrong device.
        die_id = (getattr(self, "_exec_die_id_override", "")
                  or overlay_die_id or map_die_id)
        # Whichever probe card is loaded right now, generic to any system/
        # project - "pin_wiring" is each system's own ProbeCardWiringFrame
        # instance, built the same way on both.
        probe_card = (self.pin_wiring.get_active_card()
                     if hasattr(self, "pin_wiring") else "")

        def _shotpos_kwargs(shotpos):
            sr, sc, ir, ic = shotpos or (None, None, None, None)
            return {"shot_row": sr, "shot_col": sc,
                    "intra_row": ir, "intra_col": ic, "probe_card": probe_card}
        last_set_voltage_by_ch = {}

        overall_ok = True
        # die number (from the step's own Die # field) -> verdict. Read by
        # the Electroglas .PMA-stepping pane so each die's own square is
        # coloured.
        self._exec_slot_verdicts = {}
        last_reading = None
        readings_by_name = {}

        self._exec_log(f"[MEASURE] One iteration — {len(steps)} step(s)")
        for i, s in enumerate(steps, 1):
            # Checked per STEP, not per touchdown: ⏹ Stop means stop, and a
            # shot's recipe is a dozen steps across four dies - finishing it
            # would keep sourcing into the wafer for seconds after the
            # button. The reading already in flight still completes (one
            # blocking GPIB call; abandoning it mid-transfer desyncs the bus
            # for everything after), but nothing new is started.
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
            # A step marked direct is cabled instrument-to-probe-card by
            # hand, so it closes nothing even if a stale conn string is still
            # sitting on it from before it was switched over.
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
                        # NOT a "skip this one step and keep going" case -
                        # every step after this one assumes the chuck is
                        # now sitting on die {die_no}, which never
                        # happened. Continuing used to run them anyway
                        # against wherever the chuck ACTUALLY was (die 1's
                        # spot) and record the result under die {die_no}
                        # as if it had moved - a real touchdown, a real
                        # reading, silently mislabeled as a different die
                        # (confirmed: CENFIRE-INLINE's own die-2 steps,
                        # run this way from the Measure button, which
                        # never sets _exec_move_fn the way a real Minor
                        # Moves run does). Stopping here, same as the
                        # off-wafer-die case just below, means Measure on
                        # a Minor Moves recipe correctly tests only the
                        # first die of the shot instead of quietly
                        # fabricating data for the rest of it.
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
                        # STB=74 ("target die outside probing area") from
                        # move_to_die_xy - this die # exists in the shot
                        # template but has no real die at that position on
                        # THIS wafer (e.g. a shot at the wafer's edge whose
                        # second member falls off the real map). Not a run-
                        # ending error: skip the rest of THIS shot's steps
                        # (the die #1 measurement already taken still
                        # counts) and let the caller move on to the next
                        # shot, same as an aborted run would stop early.
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
                        # A 707B addresses a crosspoint (row, column); a
                        # switchbox card addresses a plain channel number.
                        if hasattr(switch, "open_crosspoint"):
                            switch.open_crosspoint(ch[:2], ch[2:])
                        else:
                            switch.open_channel(ch)
                    self._exec_mark_open(chans)
                    continue

                if t == "passfail":
                    # Resolved before the reading lookup below, not after -
                    # a die whose OWN reading is missing/errored still needs
                    # its slot recorded (as a FAIL), or that die silently
                    # drops out of both the pass and the fail tally instead
                    # of counting as the fail it actually is. That is what
                    # under-counted a quad with a real failing die: its
                    # current-measure step came back with nothing to check
                    # against, this step "continue"d without ever touching
                    # _exec_slot_verdicts, and the die was never counted at
                    # all - not failed, not passed, just missing.
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
                    # Keep each die's verdict as well as the combined one. A
                    # shot's four dies pass or fail independently, so folding
                    # them into a single bool threw away three results.
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
                        # Manual mode (Recipe tab checkbox, off by default -
                        # see recipe_panel.is_manual_mode): only the
                        # Keithley 2400 has an AUTO/MANUAL ohms distinction
                        # (see instruments.keithley2400.measure_resistance's
                        # own comment) - hasattr(drv, "set_terminals") is
                        # the same 2400-only marker _exec_apply_terminals
                        # already uses, so a 2636B step's call shape is
                        # untouched.
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
                    # slot_die is the die _this step_ actually measured (Minor
                    # Moves published it per-die) - the shot-level die_id
                    # computed once above is only the SHOT's own overlay/map
                    # ID (its anchor cell), so used verbatim it tagged every
                    # die in the shot with the first die's identity. Prefer
                    # the resolved per-slot one whenever a publication
                    # actually happened (slot_sw is not None); fall back to
                    # the shot-level id otherwise (non-Minor-Moves runs).
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
                    # 4-wire (Kelvin) resistance - recipe_panel.py's
                    # FOUR_WIRE_TYPE. Restricted to DMM by the Recipe tab's
                    # own Instrument dropdown (_instrument_options) - no SMU
                    # driver in this codebase exposes a real 4-wire method,
                    # so an ohmf step with instrument=SMU is a malformed/
                    # hand-edited recipe, not something to guess at.
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
                    # Not every DMM driver shares one method name for this:
                    # the E1326B (Electroglas's stand-alone VXI meter) only
                    # ever has 4-wire at all and calls it measure_resistance_
                    # 4w(); the 3458A has the same method name too. A driver
                    # with neither (2-wire-only) genuinely cannot take this
                    # reading - errors instead of silently measuring 2-wire
                    # and mislabeling it as Kelvin.
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
                    # See the resistance-step case above for why slot_die (not
                    # the shot-level die_id) is preferred here.
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
                    # See the resistance-step case above for why slot_die (not
                    # the shot-level die_id) is preferred here.
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
                    # Not gated - this step's whole point is "output ON
                    # until an open step", so it has to be reasserted
                    # every time in case an earlier open step (or the
                    # previous touchdown's own close-out) left it off.
                    # Idempotent/cheap when it was already on.
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
                    # Force a known OFF state before reconfiguring - part
                    # of the exact per-die command sequence confirmed on
                    # the bench for this step (Maddy TL's "Force
                    # Current"). NOT gated by Shortcut - cheap, and this
                    # is the one call the bench trace showed has to
                    # happen every single die regardless.
                    smu.turn_output_off(smu_ch)
                    if do_cfg:
                        smu.set_current(smu_ch, float(lvl or 0))
                        if limit:
                            smu.set_voltage_limit(smu_ch, float(limit))
                    # Not gated - same "must stay/become ON regardless"
                    # reasoning as the plain "voltage" apply step above.
                    smu.turn_output_on(smu_ch)
                    # Skip auto-clear on Force Current (Recipe tab
                    # checkbox, off by default - see recipe_panel.
                    # is_fast_current_settle): the Keithley 2400's
                    # sour:clear:auto drops the output and re-applies the
                    # bias fresh on every :READ?, including the readback
                    # just below that exists purely to log the actual
                    # delivered current/voltage. Confirmed on the bench
                    # (Cenfire, a marginal contact) that transient alone
                    # was enough to collapse a dependent sense step's
                    # reading a moment later.
                    #
                    # Actively asserted BOTH ways, not just written when
                    # the checkbox is on - the SMU driver instance is
                    # built once at Connect Instruments and reused for
                    # every recipe run after that (gui/app.py), and
                    # _FIXED_SETUP only runs from __init__/reset(), never
                    # again between runs. Only ever writing "off" left
                    # clear:auto silently off for whatever ran NEXT in
                    # the same session (e.g. Cenfire with this box
                    # checked, then LaMP without reconnecting) - exactly
                    # the cross-project leak this checkbox exists to
                    # prevent. Asserting it every Force Current step,
                    # either direction, makes it self-correcting instead
                    # of dependent on what the previous recipe left
                    # behind. hasattr-gated - the 2636B has no such
                    # method and doesn't need one, this is a 2400-only
                    # transient.
                    if hasattr(smu, "set_source_clear_auto"):
                        fast_settle = bool(getattr(self, "recipe_panel", None)
                                          and self.recipe_panel.is_fast_current_settle())
                        smu.set_source_clear_auto(not fast_settle)
                    # One combined acquisition instead of two separate
                    # ones where the driver supports it (confirmed on
                    # the bench for the Keithley 2400: identical values,
                    # in the time of ONE call, not two - see
                    # Keithley2400.measure_current_and_voltage). Falls
                    # back to the original two-call sequence for any
                    # driver that doesn't have it yet.
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
                    # See the resistance-step case above for why slot_die (not
                    # the shot-level die_id) is preferred here.
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
                    # Populated by read_one() below when the driver supports
                    # a combined current+voltage acquisition, so the
                    # post-average voltage readback a few lines down can
                    # reuse it instead of taking a second, separate reading.
                    _combined_reading = {}
                    # Only an "apply" step (a different type/mode entirely)
                    # is meant to leave the instrument supplying power past
                    # its own step - see _exec_reset_output's mode=="apply"
                    # check, which is what an "open" step's turn_output_off
                    # is actually gated on. A "measure" step (this one) that
                    # forces a bias to take its own reading has to turn that
                    # bias back off itself once done, not leave it live
                    # through however many later steps until something else
                    # happens to target it.
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
                        # See _exec_should_configure's own docstring -
                        # this is the "resend setup once per wafer, not
                        # once per die" fix. lvl/limit/nplc/mrange/
                        # avg_delay together are everything below
                        # actually configures on the instrument; the
                        # bias/output-on/read/off sequence itself still
                        # runs every touchdown regardless.
                        do_cfg = self._exec_should_configure(
                            s, ("current_measure", smu_ch, lvl, limit,
                               s.get("nplc"), mrange, avg_delay))
                        if lvl:
                            if do_cfg:
                                drv.set_voltage(smu_ch, float(lvl))
                                if limit:
                                    drv.set_current_limit(smu_ch, float(limit))
                                    # NOT reading the limit back here anymore -
                                    # see references/HANDOFF_lampaccr_
                                    # compliance_investigation.md's "GPIB
                                    # response-desync" finding. This readback
                                    # query, immediately before the real
                                    # measure.i() query for the same step, is
                                    # exactly what a persistent one-query GPIB
                                    # lag (confirmed real on this bench, does
                                    # not self-correct) turns into silent data
                                    # corruption: once desynced, every
                                    # measure.i() call receives THIS query's
                                    # answer instead of its own - which is
                                    # always exactly limiti, explaining a real
                                    # run's results freezing at a constant
                                    # 1e-06 for every die for the rest of the
                                    # run. get_current_limit() is still on the
                                    # driver for manual/on-demand checks (the
                                    # SCPI/TSP row on the Instruments tab), just
                                    # not auto-called in this hot path anymore.
                            # Not gated - every touchdown closes a
                            # DIFFERENT relay path, so the output has to
                            # actually be (re)asserted on it every time
                            # even when the LEVEL it's set to hasn't
                            # changed since the last touchdown.
                            drv.turn_output_on(smu_ch)
                            last_set_voltage_by_ch[smu_ch] = float(lvl)
                            did_bias = True
                        if do_cfg:
                            if nplc is not None:
                                drv.set_nplc(smu_ch, nplc)
                            # Confirmed on the bench: the single biggest
                            # remaining per-touchdown cost after the
                            # combined-read fix (~1644ms -> ~931ms at
                            # NPLC=1/avg=20). LaMP-only opt-in - see
                            # Keithley2400.set_auto_zero's own docstring
                            # for the drift tradeoff this accepts; Maddy
                            # and Cenfire share this driver and have not
                            # been evaluated with auto-zero off, and a
                            # 2636B-backed recipe has no such method at
                            # all.
                            if hasattr(drv, "set_auto_zero"):
                                drv.set_auto_zero(False)
                            # LaMP's MeterRange, carried from the .PMA. Pinned
                            # rather than autoranged, so a different PMA
                            # reconfigures the meter on LOAD ALL instead of
                            # inheriting whatever the last recipe left set.
                            if mrange and hasattr(drv, "set_current_range"):
                                try:
                                    drv.set_current_range(smu_ch, float(mrange))
                                except (TypeError, ValueError) as e:
                                    self._exec_log(f"[MEASURE]    ignoring bad "
                                                    f"meter range {mrange!r}: {e}")
                            # sour:clear:auto on drops the output after every
                            # :READ?, so each of the averaged readings
                            # re-applies the bias to a discharged path. With no
                            # source delay the integration starts on the
                            # charging transient - a good die read ~90 nA where
                            # the original LaMP data shows sub-nanoamp. This is
                            # LaMP's MeterDelay, carried on the step as
                            # avg_delay (ms).
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
                            # read_one() above already captured this as part
                            # of the same acquisition that produced i_raw -
                            # _exec_measure_averaged/_exec_take_average
                            # always call read_one() at least once, so this
                            # is populated by the time we get here whenever
                            # the combined-read path was used.
                            actual_voltage = _combined_reading["v"]
                        else:
                            try:
                                actual_voltage = drv.measure_voltage(smu_ch)
                            except Exception:
                                actual_voltage = None
                    # Diagnostic only - read while the output is still on
                    # (compliance state means nothing once it's off), never
                    # touches pass/fail. A reading whose magnitude looks
                    # clamped at the step's own limit is a hint, not proof -
                    # this asks the instrument directly instead of guessing
                    # from the number.
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
                    # See the resistance-step case above for why slot_die (not
                    # the shot-level die_id) is preferred here.
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
        """(die label, row, col, switch, shotpos) for a step's own Die #
        field. shotpos is (shot_row, shot_col, intra_row, intra_col) or
        None when nothing published one (non-Minor-Moves, or a system/
        recipe with no shot concept at all).

        Replaces the old "... (Die N)" name-suffix convention - a step now
        carries its die number directly (recipe_panel._STEP_FIELDS "die"),
        so this no longer depends on how the step happens to be named.

        The Electroglas run publishes the shot's die IDs and map cells in
        QUAD_ORDER before each touchdown, and Accretech Minor Moves
        publishes the shot's real per-die coordinates/reticle position
        (_exec_minor_move_thread.publish_die_slots), before each
        touchdown, so a per-die step can be filed against the die it
        actually measured rather than against the shot's anchor cell.

        The test is whether that publication EXISTS, not whether die_no is
        greater than 1. Blank normalizes to "1", so "die 1" and "no die set"
        look identical here - short-circuiting on die_no <= 1 therefore filed
        die 1 of a quad under the whole shot's ID with a blank fldSwitch,
        while dies 2..4 got their own. One shot exported three individual
        dies and one shot-shaped row, and LaMP's fldSwitch 1..4 became
        0,2,3,4. With no publication (Accretech, or a single-die shot) there
        are no slots to file against and the shot-level fallback is right.
        """
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
               # Blank on any run that never resolved a shot for this die
               # (non-Minor-Moves, or a system with no shot concept at
               # all) - see _exec_slot_identity/_exec_minor_move_thread.
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
        """Results from the most recently started run only (Full Die/Test
        Die/Test Selected) — what export formats other than plain
        "Save as CSV" should write, so re-running doesn't accumulate old
        runs' rows into a new export."""
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
        """Back: no native "previous die" GPIB command exists on this
        hardware (only "J" Next Die - see _exec_manual_next_die), so this
        is the closest die-mode equivalent - a plain relative die-index
        step backward (S command, X-1), not a walk through any GUI-side
        site list. Bounded/verified the same way every other relative
        Accretech move in this file is (see move_xy_relative's own STB
        handling in instruments/accretech_uf200r.py)."""
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
        """Next: plain native J - the prober's own "next die" per its
        internal wafer map. Nothing to do with shots, the picked-sites
        list, or Minor Moves - just the bare hardware command, same as
        Electroglas's Next (eg_pma_run_panel._step_once) is the bare .PMA
        step, not a shot-aware move."""
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
        """Shared preflight for Next Shot/Previous Shot: the prober, Wafer
        Builder, confirmed Overlay alignment, shot size, and the sorted
        (row-major) shot list all need to exist before either can compute
        anything. Returns (prober, gen, shots, shot_rows, shot_cols,
        row_off, col_off) or None (already logged why) if not."""
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
        """Separate, jump to (shot_row, shot_col)'s die #1, same as Minor
        Moves' own landing (_exec_minor_move_thread's goto_shot_die)."""
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
        """Index into `shots` of whichever shot the current real die
        position falls in, or None if unknown/not on the list."""
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
        """Advance to die #1 of the NEXT shot (Wafer Builder Shot Map tab's
        shots, row-major order) - an absolute die-coordinate move, not a
        native command - Accretech has none that understands "shot"."""
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
        """Same as Next Shot, one shot back instead."""
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

    _EXEC2_MOVE_TARGET_COLOR = "#1e3a8a"  # dark blue - distinct from the pick color

    def _exec_move_selected_button(self):
        """→ Move to Selected is a self-contained arm/target toggle, NOT a
        reader of the normal pick system (_exec_wafer_map.get_picked(),
        which Test Selected/Take from map selection/Overlay all share and
        which this must never disturb):

          IDLE ("→ Move to Selected") --click--> ARMED, no target
              ("✕ Cancel Move") --click a die--> ARMED, one target,
              highlighted dark blue ("→ Move")

        While armed, clicking dies is intercepted via set_click_handler
        (see _exec_move_target_click) instead of going through picking -
        picking itself is suspended (not cleared) for the duration, so any
        real Test Selected picks are exactly as they were once this is
        done. Clicking the target again deselects it (back to "Cancel
        Move"); clicking a different die just moves the highlight - only
        one target at a time. Pressing the button with a target executes
        the move and returns to idle; with no target, it cancels.
        """
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
        """Move straight to (row, col) - Z down first (never travel in X/Y
        while contacted), then the absolute die-coordinate move.
        Deliberately does NOT Z up afterward - this is a positioning aid
        (e.g. lining up before a manual Z Up/Measure), not a touchdown of
        its own."""
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
        """The automatic, run-thread version of the ↻ Refresh XY button -
        called right before a run's first move (Full Die/Test Die/Test
        Selected/Minor Moves), so the displayed X/Y, the highlighted die,
        and self._exec_current_rc are read fresh rather than left over
        from whatever happened before Start was pressed (a manual jog, the
        previous run's last die, ...). Runs ON the calling thread (already
        off the main thread by the time any of those call this) - blocking
        here is the point, unlike the ↻ Refresh XY button's own fire-and-
        forget _exec_get_xy.
        """
        try:
            raw = prober.get_xy_position()
            x, y = _parse_q_response(raw)
            self._exec_safe_after(lambda: self._exec_xy_var.set(f"X: {x:.0f} die\nY: {y:.0f} die"))
            self._exec_safe_after(lambda: self._exec_log(f"[RUN] Q → die X={x:.0f}  Y={y:.0f}"))
            self._exec_safe_after(lambda: self._exec_highlight_current(int(y), int(x)))
        except Exception as e:
            self._exec_log(f"[RUN] Refresh XY before run failed: {e}")

    def _exec_refresh_die_size(self):
        """Electroglas only. Infers the prober's current SP1 die size (no
        direct query exists - see electroglas_2001x.infer_die_size) and
        shows it on the Run tab. Called once whenever the prober connects
        (app.py._connect_instruments_eg) and again whenever a Die Size
        write goes out from Prober Debug (eg_prober_debug_panel._send_setup),
        so the label never has to be trusted stale."""
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
                # The label is a one-line hint in a small box, and the
                # exception behind it is often a multi-line SCPI/VISA
                # complaint that wrapped the whole Chuck Position section
                # out of shape. It still goes to the log, in full, which is
                # where anyone diagnosing it would look.
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
        """Outline, on the Run tab's wafer map, the block of REAL dies the
        current shot spans - the Accretech equivalent of NanoZ's 1x20
        window and Electroglas's 2x2 quad window (see
        eg_pma_run_panel._draw_shot_window) - now that a shot can be more
        than one physical die (Minor Moves), a single highlighted square no
        longer shows the whole touchdown's footprint.

        Skipped (and cleared) when: the wafer's shots are 1 die each
        (Cenfire-style multi-die shots are exactly the case this is FOR -
        nothing to outline beyond the die itself otherwise), the live XY
        position isn't known yet, or the Wafer Builder<->Accretech
        alignment (Overlay) was never confirmed - the block's real
        die-coordinates can't be computed without that offset. Draws
        against whatever's actually on screen, so a shot corner that's
        genuinely absent from the real Accretech extraction (wafer edge)
        just narrows the box instead of guessing.

        Minor Moves ON: unchanged - the block is the theoretical shot the
        current die falls in (floor-divide by the shot dims against the
        Overlay origin), same as before. Correct there because Minor
        Moves always lands ON die #1's own computed cell first, so the
        touched die can never disagree with the theoretical grid.

        Minor Moves OFF: the window is anchored on the current die itself
        - treated as die #1 of the shot, same convention
        _exec_publish_die_slots_at uses for the actual measurement/colour
        data - rather than a theoretical grid bucket the real touchdown
        may not land exactly on. Without this, a touchdown that landed
        between two theoretical shots drew the outline around the WRONG
        neighboring shot instead of the one actually under the needles.
        """
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
        # The PMA runner keeps its own verdict-per-touchdown record and paints
        # the map from it; zeroing the counters without clearing that would
        # leave green/red squares that nothing counts any more.
        reset = getattr(getattr(self, "eg_pma_run", None), "reset_results", None)
        if reset:
            try:
                reset()
            except Exception:
                pass
        # Accretech has no equivalent per-touchdown record of its own - the
        # colour lives directly on the wafer map widgets (and
        # controller.die_status, cmd_save_csv's own persistent copy) - same
        # "counters reset but squares still show the old PASS/FAIL" gap.
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

        # Vertical PanedWindow instead of a plain scroll wrapper - drag the
        # sashes between sections to give more room to whichever one you
        # need (wafer map, export controls, results table, ...).
        split = ttk.PanedWindow(page, orient="vertical")
        split.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        # Both systems get the pass/fail wafer map at the top, above Data
        # Export. Electroglas used to get a donut of run statistics down at
        # the bottom instead, which said less than the map does and did not
        # let you click a die to read its measurements.
        wafer_pane = ttk.Frame(split)
        split.add(wafer_pane, weight=3)
        self._build_results_wafer_map(wafer_pane)

        export_frame = ttk.LabelFrame(split, text="Data Export")
        split.add(export_frame, weight=0)

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
        # Only meaningful for Cenfire - launches the SAME external tool
        # (references/AzTransfer, its own separate git repo/project) the
        # same way LabView invokes it after a run, so this is a convenience
        # shortcut, not a reimplementation - never touch AzTransfer.ps1
        # itself. Disabled unless a Cenfire ATA folder is loaded (see
        # _is_cenfire_folder/_refresh_cenfire_transfer_button), so it can't
        # be pressed against the wrong project's data by mistake.
        self._cenfire_transfer_btn = ttk.Button(
            sql_row, text="Transfer Cenfire", command=self._run_cenfire_transfer,
            state="disabled")
        self._cenfire_transfer_btn.pack(side="left", padx=(6, 0))
        # LaMP's analogue - there is no external tool like AzTransfer for
        # this one, so this actually does the database push itself (see
        # mdb_export.push_sql_dump_folder), rather than just launching
        # something else that already does it. Disabled unless a LaMP ATA
        # folder is loaded, same reasoning as the Cenfire button above.
        self._lamp_push_btn = ttk.Button(
            sql_row, text="Push LaMP SQL Dump", command=self._run_lamp_sql_push,
            state="disabled")
        self._lamp_push_btn.pack(side="left", padx=(6, 0))
        # Push straight into an Access database instead of writing a .sql
        # file for someone to run later. Built for both systems - the
        # target .mdb/table is whatever Export Format + path the operator
        # picks, not tied to LaMP/Electroglas specifically.
        self._build_mdb_row(export_frame)

        self._export_formats: list = []
        self._export_default_lbl_var = tk.StringVar(value="")
        ttk.Label(export_frame, textvariable=self._export_default_lbl_var,
                 foreground="#6b7280", font=("Segoe UI", 8)).pack(
                 anchor="w", padx=10, pady=(0, 8))

        results_lf = ttk.LabelFrame(split, text="Measurement Results")
        split.add(results_lf, weight=2)
        results_lf.rowconfigure(0, weight=1)
        results_lf.columnconfigure(0, weight=1)

        # ttk.PanedWindow's add(..., weight=) only steers how EXTRA space
        # beyond every pane's own natural/requested size gets divided - it
        # is not a percentage split, so the wafer map pane's actual height
        # is really just whatever its content's natural size happens to be
        # (observed on a real Cenfire folder: ~170-210px for 14631 dies,
        # which makes every individual die sub-pixel - not a redraw bug,
        # the canvas is just too short to show them distinctly, and the
        # exact pixel-alignment then decides whether that looks like a
        # solid blob or a stripe pattern, i.e. "looks different" is really
        # just aliasing luck at the same too-small size). Same fix already
        # used for the Electroglas Run tab's own PanedWindow (map_lf above)
        # - explicit sashpos(), set once the pane has a real height via a
        # one-shot <Configure> bind, never fighting a later manual drag.
        def _apply_initial_results_sashes():
            h = split.winfo_height()
            if h <= 1:
                return
            split.sashpos(0, int(h * 0.55))
            split.sashpos(1, int(h * 0.80))
        def _set_initial_results_sashes(_event=None):
            if split.winfo_height() <= 1:
                return
            split.unbind("<Configure>", results_sash_bind_id[0])
            split.after_idle(_apply_initial_results_sashes)
        results_sash_bind_id = [split.bind("<Configure>", _set_initial_results_sashes)]

        cols = ("timestamp", "recipe", "die", "step", "type", "value", "unit")
        self._results_tree = ttk.Treeview(
            results_lf, columns=cols, show="headings", height=8, selectmode="browse")
        heads = [("timestamp", "Time", 135), ("recipe", "Recipe", 110),
                 ("die", "Die", 90), ("step", "Step", 110), ("type", "Type", 75),
                 ("value", "Value", 90), ("unit", "Unit", 45)]
        for cid, text, width in heads:
            self._results_tree.heading(cid, text=text)
            self._results_tree.column(cid, width=width,
                                      anchor="center" if cid in ("type", "unit") else "w")
        self._results_tree.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        rsb = ttk.Scrollbar(results_lf, orient="vertical",
                            command=self._results_tree.yview)
        rsb.grid(row=0, column=1, sticky="ns", pady=6)
        self._results_tree.configure(yscrollcommand=rsb.set)

        ttk.Button(results_lf, text="Clear Results", command=self.clear_results).grid(
            row=1, column=0, columnspan=2, sticky="e", padx=6, pady=(0, 6))

    # ------------------------------------------------------------------
    # ACCESS DATABASE PUSH
    #
    # An .mdb is a FILE, not a server - pushing writes rows into that file
    # and nothing else. Point this at the shared copy on the network and
    # everyone reading it sees the rows; point it at a local copy and the
    # rows go nowhere but that copy. See mdb_export's module docstring.
    # ------------------------------------------------------------------
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
        self._mdb_default_lbl_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self._mdb_default_lbl_var, foreground="#6b7280",
                 font=("Segoe UI", 8), wraplength=620, justify="left").pack(
                 anchor="w", padx=10, pady=(0, 8))

    def _mdb_say(self, text: str):
        """Status line - guarded with getattr since _mdb_status_var is only
        ever set once _build_mdb_row actually runs."""
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
        # Remembered globally as a fallback (starting value before any ATA
        # folder has set its OWN default via Set Default below) - a fresh
        # PC/folder with no saved default yet still starts from whatever
        # was last picked anywhere, instead of blank.
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
        # The last run only, matching what "💾 Export" writes - so the file
        # and the database always describe the same run.
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
            # The recorded per-die name, NOT die_id - die_id is the whole shot
            # ("B26/B27/NA/B29/B30"), so falling back to it labelled an empty
            # corner with every device in the touchdown.
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
        # This project's own remembered export directory, if it has one -
        # falls back to whatever export_path_var already held (the fixed
        # system-wide default, or wherever the operator last pointed it)
        # rather than clearing the field when a project has never set one.
        #
        # Only applied if it actually exists ON THIS MACHINE: the saved
        # default is one plain string shared across every machine that
        # opens this ATA folder (GUI System is a network share), but the
        # Results tab's own export-directory quick-picker offers "Downloads"
        # (self._downloads_dir = os.path.expanduser('~')/Downloads) as a
        # choice, and THAT resolves to a different, per-user, per-machine
        # path on every PC. Someone pressing ⭐ Set Default while "Downloads"
        # was picked saved THEIR OWN machine's literal Downloads path (e.g.
        # C:\Users\aahmed\Downloads) into the shared default - which then
        # silently overwrote every OTHER machine's correctly-resolved
        # export_path_var (e.g. atomica's own C:\Users\atomica\Downloads,
        # or the fixed network default) the next time this folder loaded
        # there, even though nothing on that machine ever pointed it there.
        # A real folder path (the network default, or a real shared
        # location) is unaffected - this only ever protects against a
        # foreign machine's personal Downloads folder winning here.
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
        # The saved default is one string shared across every machine that
        # opens this ATA folder (GUI System is a network share) - a path
        # under THIS machine's own home directory (e.g. picking "Downloads"
        # from the quick-choice list) means something different on every
        # other PC, so saving it here would silently break/overwrite their
        # own export directory the next time they load this folder (see
        # _refresh_export_formats's own note - this is the save-side half
        # of that same bug).
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
        # Format and export directory are set as default together - the
        # two always travel together for a given project (a project's data
        # goes to its own place, in its own shape), so one button covers
        # both rather than needing two separate "set default" actions.
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
        # references/AzTransfer is a separate, already-working external
        # tool (its own git repo/remote) that LabView normally invokes at
        # the end of a Cenfire test session - this just fires the exact
        # same command in its own terminal window, nothing reimplemented
        # or reached into.
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
        # CSV-only, off by default (a format saved before this existed has
        # no such key, so it keeps its exact current one-row-per-die
        # behavior - see export_formats.build_csv_rows's own per_step
        # comment). Checked, a CSV format switches to the SAME raw,
        # uncollapsed rows an SQL format already sees (one row per
        # measurement) instead of group_results_by_die's one-row-per-die
        # merge - for a recipe like Peanut's FULL that runs several
        # differently-named tests per die, the merged version silently
        # keeps only the first current/first resistance reading and drops
        # the rest.
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
        # Not readonly: a "lookup" table's own column headers (e.g.
        # Cenfire's DIE_ID/ABS_ROW/COLUMN_RETICLE - see the "Lookup Table"
        # row below) are project-specific and cannot all be listed here,
        # so a source name can be typed directly as well as picked from
        # the documented list.
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
        # Independent of Multiply by - combine both (multiply first, then
        # round the result) or use Round alone on the plain source value.
        # Not offered alongside a constant/template column: both already
        # produce an exact, deliberately-chosen string.
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
        # A trusted per-die ID string (e.g. "2-7-7-1") already attached to
        # the row/group, joined against the lookup file's own ID column -
        # see export_formats.apply_lookup's own docstring for why this is
        # preferred over the (row, col) fields below whenever the run
        # itself already knows which die it measured: a position join only
        # works when this app's (row, col) grid and the reference file's
        # own coordinate columns agree on origin/sign/rotation, which nothing
        # here can verify - a silent mismatch there just looks like "no
        # match" (or, worse, a WRONG match) rather than an error. Editing
        # and re-saving a format through this dialog used to always rebuild
        # "lookup" from only the four (row, col) fields below, even for a
        # format that had key_field/lookup_key_col set instead - silently
        # discarding a working ID-based join and replacing it with a
        # position join that may not even use the same convention.
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
            # per_step csv reads the SAME raw, uncollapsed rows an sql
            # format does (see export_formats.build_csv_rows), so its
            # available fields are the sql field list (step/type/value/
            # unit/...), not the die-grouped csv one (resistance/current/
            # chip_id/...) - those don't exist on a raw row.
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
            # "Only include readings that have a die ID" filters the SAME
            # raw rows_for_format rows an sql format uses - meaningful for
            # sql, and for a per_step csv format (which reads that same
            # raw list), but not for the die-grouped csv path, which
            # doesn't consult it at all.
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
            # Trailing "~N" (round to N decimals) can ride along with any
            # of the other forms below, or stand alone on a plain source
            # column - stripped off first so what's left parses the same
            # as before.
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
            # A template's own braces are the marker - no prefix character
            # needed, since "={...}" would otherwise read as a literal
            # constant string containing braces instead.
            if "{" in txt and "}" in txt:
                result["template"] = txt
                return result
            return result

        def _col_from_editor():
            """(field, source, quote, transform_txt) from the current editor
            fields, or None if incomplete/invalid - shared by Add and Update
            Selected so the two can never build a column differently."""
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
            """Load the selected row into the edit fields, same as clicking
            a step on the Recipe tab - non-destructive. Update Selected
            writes the (possibly changed) fields back into this same row;
            the row itself is untouched until then."""
            sel = cols_tree.selection()
            if not sel:
                return
            f, src, q, tr = cols_tree.item(sel[0], "values")
            field_var.set(f)
            # Not just the documented list - a "lookup" table's own column
            # (see the Source combobox note above) is a perfectly valid
            # source that would otherwise silently vanish on re-edit.
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
                # ID-string join wins over row/col matching, matching
                # apply_lookup's own precedence - see this section's
                # comment above for why editing this dialog used to
                # silently destroy a working ID-based lookup.
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
