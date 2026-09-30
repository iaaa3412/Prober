import threading
import time
import tkinter as tk
from tkinter import ttk

from instrument_connection_panel import build_address_panel
from instruments import eg_profiles
from hp3458a_debug_panel import HP3458ADebugPanel


def _eg_instruments():
    return eg_profiles.roster()


class InstrumentsEgPanel(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self._addr_panel = None

        canvas = tk.Canvas(self, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._body = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=self._body, anchor="nw")
        self._body.bind("<Configure>",
                        lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))

        def _wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        canvas.bind("<MouseWheel>", _wheel)
        self._body.bind("<MouseWheel>", _wheel)

        self._body.columnconfigure(0, weight=1)
        self._body.columnconfigure(1, weight=1)

        self._build_bench_selector()
        self._build_addresses()
        self._build_smu()
        self._build_ps()
        self._build_dmm()

    def _log(self, msg: str):
        self.controller.log(msg)

    def _drv(self, key: str):
        drv = self.controller.drivers.get(key)
        return drv if (drv and drv.inst) else None

    def _build_bench_selector(self):
        lf = ttk.LabelFrame(self._body, text="Prober bench", padding=6)
        lf.grid(row=0, column=0, columnspan=2, sticky="new", padx=8, pady=(8, 0))

        row = ttk.Frame(lf)
        row.pack(fill="x")
        self._bench_var = tk.StringVar(value=eg_profiles.active_name())
        ttk.Label(row, textvariable=self._bench_var,
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(row, text="↻ Scan bus & match",
                   command=self._match_bench).pack(side="right")

        self._bench_lbl = tk.StringVar()
        ttk.Label(lf, textvariable=self._bench_lbl, font=("Consolas", 8),
                  justify="left", foreground="#0077cc").pack(anchor="w", pady=(5, 0))
        self._refresh_bench_label()

    def _refresh_bench_label(self):
        name = eg_profiles.active_name()
        fitted = eg_profiles.fitted_keys(name)
        self._bench_var.set(name)
        self._bench_lbl.set(f"{eg_profiles.label(name)}\n"
                            f"{len(fitted)} instrument(s) fitted: "
                            + ", ".join(k.replace('_eg', '') for k in fitted))

    def _match_bench(self):
        def _run():
            from instruments.gpib_base import discover_bus
            try:
                found = {d["address"]: (d["identity"], d["detail"])
                         for d in discover_bus(timeout_ms=700)}
            except Exception as e:
                self.after(0, lambda: self._log(f"[SYSTEM] Scan failed: {e}"))
                return
            lines = []
            for name in eg_profiles.profile_names():
                inst = eg_profiles.instruments(name)
                want = {k: v for k, v in inst.items() if v.get("fitted", True)}
                hit = sum(1 for v in want.values() if v["address"] in found)
                lines.append((hit / max(1, len(want)), hit, len(want), name))
            lines.sort(reverse=True)
            best = lines[0]
            msg = ["[SYSTEM] Bus scan matched:"]
            for score, hit, total, name in lines:
                mark = "  <-- best" if name == best[3] else ""
                msg.append(f"   {name}: {hit}/{total} fitted addresses present{mark}")
            for addr, (ident, _d) in sorted(found.items()):
                msg.append(f"      {addr:<22} {ident or '(no ID)'}")
            self.after(0, lambda: [self._log(m) for m in msg])
        threading.Thread(target=_run, daemon=True).start()

    def _rebuild_addresses(self):
        if getattr(self, "_addr_panel", None) is not None:
            self._addr_panel.destroy()
        self._build_addresses()

    def _build_addresses(self):
        self._addr_panel = build_address_panel(
            self._body, _eg_instruments(), self._log, self.controller.init_hardware_eg)
        self._addr_panel.grid(row=3, column=0, columnspan=2, sticky="new",
                              padx=8, pady=8)

    def _build_smu(self):
        lf = ttk.LabelFrame(self._body, text="SMU — Keithley 2400", padding=8)
        lf.grid(row=1, column=0, sticky="new", padx=8, pady=8)

        row = ttk.Frame(lf)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Source:").pack(side="left")
        self._smu_src_var = tk.StringVar(value="Voltage")
        ttk.Combobox(row, textvariable=self._smu_src_var, values=["Voltage", "Current"],
                    width=9, state="readonly").pack(side="left", padx=4)
        ttk.Label(row, text="Level:").pack(side="left", padx=(8, 0))
        self._smu_level_var = tk.StringVar(value="0")
        ttk.Entry(row, textvariable=self._smu_level_var, width=10).pack(side="left", padx=4)
        ttk.Label(row, text="Limit:").pack(side="left", padx=(8, 0))
        self._smu_limit_var = tk.StringVar(value="0.01")
        ttk.Entry(row, textvariable=self._smu_limit_var, width=10).pack(side="left", padx=4)

        btn_row = ttk.Frame(lf)
        btn_row.pack(fill="x", pady=(6, 2))
        ttk.Button(btn_row, text="Output ON", command=self._smu_output_on).pack(side="left")
        ttk.Button(btn_row, text="Output OFF", command=self._smu_output_off).pack(
            side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Measure", command=self._smu_measure).pack(
            side="left", padx=(6, 0))

        self._smu_reading_var = tk.StringVar(value="V: —    I: —    R: —")
        ttk.Label(lf, textvariable=self._smu_reading_var, font=("Consolas", 9)).pack(
            anchor="w", pady=(6, 0))

    def _smu_output_on(self):
        drv = self._drv("smu")
        if not drv:
            self._log("[INSTRUMENT] Not connected")
            return
        try:
            level = float(self._smu_level_var.get())
            limit = float(self._smu_limit_var.get())
            if self._smu_src_var.get() == "Voltage":
                drv.set_voltage("", level)
                drv.set_current_limit("", limit)
            else:
                drv.set_current("", level)
                drv.set_voltage_limit("", limit)
            drv.turn_output_on("")
            self._log(f"[INSTRUMENT] Output ON — {self._smu_src_var.get()}={level}, limit={limit}")
        except Exception as e:
            self._log(f"[INSTRUMENT] Error: {e}")

    def _smu_output_off(self):
        drv = self._drv("smu")
        if not drv:
            self._log("[INSTRUMENT] Not connected")
            return
        try:
            drv.turn_output_off("")
            self._log("[INSTRUMENT] Output OFF")
        except Exception as e:
            self._log(f"[INSTRUMENT] Error: {e}")

    def _smu_measure(self):
        drv = self._drv("smu")
        if not drv:
            self._log("[INSTRUMENT] Not connected")
            return

        def _run():
            try:
                v = drv.measure_voltage("")
                i = drv.measure_current("")
                r = drv.measure_resistance("")
                self.after(0, lambda: self._smu_reading_var.set(
                    f"V: {v:.6g} V    I: {i:.6g} A    R: {r:.6g} Ω"))
                self._log(f"[INSTRUMENT] V={v:.6g} V  I={i:.6g} A  R={r:.6g} Ω")
            except Exception as e:
                self._log(f"[INSTRUMENT] Measure error: {e}")
        threading.Thread(target=_run, daemon=True).start()

    def _build_dmm(self):
        lf = ttk.LabelFrame(self._body, text="DMM — HP 3458A", padding=8)
        lf.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=8, pady=(0, 8))
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        self.dmm_debug = HP3458ADebugPanel(lf, controller=self.controller)
        self.dmm_debug.grid(row=0, column=0, sticky="nsew")

    def _build_ps(self):
        lf = ttk.LabelFrame(self._body, text="Power Supply — Agilent 6634B", padding=8)
        lf.grid(row=1, column=1, sticky="new", padx=8, pady=8)

        row = ttk.Frame(lf)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Voltage:").pack(side="left")
        self._ps_v_var = tk.StringVar(value="0")
        ttk.Entry(row, textvariable=self._ps_v_var, width=10).pack(side="left", padx=4)
        ttk.Label(row, text="Current Limit:").pack(side="left", padx=(8, 0))
        self._ps_i_var = tk.StringVar(value="0.1")
        ttk.Entry(row, textvariable=self._ps_i_var, width=10).pack(side="left", padx=4)

        btn_row = ttk.Frame(lf)
        btn_row.pack(fill="x", pady=(6, 2))
        ttk.Button(btn_row, text="Output ON", command=self._ps_output_on).pack(side="left")
        ttk.Button(btn_row, text="Output OFF", command=self._ps_output_off).pack(
            side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Measure", command=self._ps_measure).pack(
            side="left", padx=(6, 0))

        self._ps_reading_var = tk.StringVar(value="V: —    I: —")
        ttk.Label(lf, textvariable=self._ps_reading_var, font=("Consolas", 9)).pack(
            anchor="w", pady=(6, 0))

    def _ps_output_on(self):
        drv = self._drv("power_supply")
        if not drv:
            self._log("[INSTRUMENT] Not connected")
            return
        try:
            drv.set_voltage(float(self._ps_v_var.get()))
            drv.set_current_limit(float(self._ps_i_var.get()))
            drv.turn_output_on()
            self._log(f"[INSTRUMENT] Output ON — V={self._ps_v_var.get()}, "
                     f"I limit={self._ps_i_var.get()}")
        except Exception as e:
            self._log(f"[INSTRUMENT] Error: {e}")

    def _ps_output_off(self):
        drv = self._drv("power_supply")
        if not drv:
            self._log("[INSTRUMENT] Not connected")
            return
        try:
            drv.turn_output_off()
            self._log("[INSTRUMENT] Output OFF")
        except Exception as e:
            self._log(f"[INSTRUMENT] Error: {e}")

    def _ps_measure(self):
        drv = self._drv("power_supply")
        if not drv:
            self._log("[INSTRUMENT] Not connected")
            return
        try:
            v = drv.measure_voltage()
            i = drv.measure_current()
            self._ps_reading_var.set(f"V: {v:.6g} V    I: {i:.6g} A")
            self._log(f"[INSTRUMENT] V={v:.6g} V  I={i:.6g} A")
        except Exception as e:
            self._log(f"[INSTRUMENT] Error: {e}")
