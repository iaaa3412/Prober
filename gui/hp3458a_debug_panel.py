"""Bench panel for the HP 3458A on its own — every function, one click each.

Two things about this meter that the panel cannot paper over:

FRONT/REAR IS A MECHANICAL SWITCH. The manual: "the 3458's input terminals
cannot be controlled from remote". TERM is accepted only for compatibility with
older meters and errors if you try to set it. So the panel READS the switch with
TERM? and shows it - it cannot move it. On probe03 the relay wiring goes to the
REAR terminals, so a reading taken with the switch on FRONT is measuring an
empty front panel, which looks exactly like a perfect open circuit. That is the
single easiest way to get a convincing wrong answer here, hence the readback
sitting at the top of the panel.

RESISTANCE IS A SOURCED-CURRENT MEASUREMENT. Every ohms range drives a known
current (10 mA on the 10 Ohm range down to 500 nA at 1 GOhm) and reads the
resulting voltage. Picking a range therefore picks how much current goes through
whatever is connected, which is worth knowing before probing a device - so the
range buttons show it.

4-wire (OHMF) only means anything with the sense pair actually run to the
device. With two pins per die on probe03 there is nowhere to sense from, so
OHMF there returns a meaningless number rather than a merely imprecise one.
"""

import threading
import tkinter as tk
from tkinter import ttk

from instruments.hp3458a import DCI_SHUNT, FUNCTIONS, OHMS_TEST_CURRENT


def _eng(value: float, unit: str) -> str:
    """1.2e-9 -> '1.2 n'. Keeps the range buttons readable."""
    if value == 0:
        return f"0 {unit}"
    for scale, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""),
                          (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= scale:
            shown = value / scale
            text = f"{shown:.0f}" if abs(shown - round(shown)) < 1e-9 else f"{shown:g}"
            return f"{text} {prefix}{unit}"
    return f"{value:g} {unit}"


class HP3458ADebugPanel(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self._busy = False
        self._continuous = False
        self._func = "DCV"

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        self._build_top()
        self._build_functions()
        self._build_config()
        self._build_readout()

    # -- plumbing -----------------------------------------------------------

    def _log(self, msg: str):
        self.controller.log(msg)

    def _drv(self):
        drv = self.controller.drivers.get("dmm")
        return drv if (drv and drv.inst) else None

    def _ui(self, fn):
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _run(self, label: str, fn):
        """Run `fn` off the UI thread, one at a time."""
        if self._busy:
            self._log(f"[INSTRUMENT] Busy — {label} ignored")
            return
        drv = self._drv()
        if not drv:
            self._log("[INSTRUMENT] Not connected")
            return
        self._busy = True
        self._state_var.set(f"… {label}")

        def _work():
            try:
                done = fn(drv)
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:90]}"
                self._ui(lambda: self._log(f"[INSTRUMENT] {label} failed — {err}"))
                done = None
            finally:
                self._busy = False
                self._ui(lambda: self._state_var.set("idle"))
            if callable(done):
                self._ui(done)

        threading.Thread(target=_work, daemon=True).start()

    # -- layout -------------------------------------------------------------

    def _build_top(self):
        lf = ttk.LabelFrame(self, text="Meter", padding=6)
        lf.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))

        row = ttk.Frame(lf)
        row.pack(fill="x")
        for text, cmd in (("ID?", self._read_id), ("REV?", self._read_rev),
                          ("Errors", self._read_errors), ("Reset", self._reset),
                          ("Preset", self._preset), ("Self-test", self._self_test),
                          ("Beep", self._beep)):
            ttk.Button(row, text=text, width=9, command=cmd).pack(side="left", padx=2)

        term = ttk.Frame(lf)
        term.pack(fill="x", pady=(8, 0))
        ttk.Button(term, text="↻ TERM?  which terminals",
                   command=self._read_terminals).pack(side="left")
        self._term_var = tk.StringVar(value="terminals: unknown")
        self._term_lbl = tk.Label(term, textvariable=self._term_var, font=("Consolas", 10, "bold"),
                                  bg="#374151", fg="#e5e7eb", padx=8, pady=2)
        self._term_lbl.pack(side="left", padx=8)
        ttk.Label(term, text="mechanical switch — read only",
                  foreground="#888", font=("Arial", 8)).pack(side="left")

        cal = ttk.Frame(lf)
        cal.pack(fill="x", pady=(6, 0))
        ttk.Label(cal, text="ACAL:").pack(side="left")
        for mode, note in (("DCV", "~1 min"), ("OHMS", "~10 min"), ("ALL", "~11 min")):
            ttk.Button(cal, text=f"{mode} ({note})", width=14,
                       command=lambda m=mode: self._autocal(m)).pack(side="left", padx=2)
        ttk.Label(cal, text="disconnect the input first", foreground="#b45309",
                  font=("Arial", 8)).pack(side="left", padx=(8, 0))

    def _build_functions(self):
        lf = ttk.LabelFrame(self, text="Measurement function", padding=6)
        lf.grid(row=1, column=0, sticky="ew", padx=6, pady=2)

        self._func_var = tk.StringVar(value="DCV")
        grid = ttk.Frame(lf)
        grid.pack(fill="x")
        for i, (cmd, label, unit, _ranges) in enumerate(FUNCTIONS):
            ttk.Radiobutton(grid, text=f"{cmd} — {label}", value=cmd,
                            variable=self._func_var, command=self._on_func
                            ).grid(row=i % 5, column=i // 5, sticky="w", padx=(0, 18))

        rng = ttk.Frame(lf)
        rng.pack(fill="x", pady=(8, 0))
        ttk.Label(rng, text="Range:").pack(side="left")
        self._range_var = tk.StringVar(value="AUTO")
        self._range_cb = ttk.Combobox(rng, textvariable=self._range_var, width=22,
                                      state="readonly")
        self._range_cb.pack(side="left", padx=6)
        ttk.Button(rng, text="Autorange ON",
                   command=lambda: self._simple("ARANGE ON", lambda d: d.autorange(True))
                   ).pack(side="left", padx=2)
        ttk.Button(rng, text="OFF",
                   command=lambda: self._simple("ARANGE OFF", lambda d: d.autorange(False))
                   ).pack(side="left", padx=2)
        self._range_note = tk.StringVar(value="")
        ttk.Label(rng, textvariable=self._range_note, foreground="#0077cc",
                  font=("Consolas", 8)).pack(side="left", padx=(10, 0))
        self._range_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_range())
        self._on_func()

    def _build_config(self):
        lf = ttk.LabelFrame(self, text="Configuration", padding=6)
        lf.grid(row=2, column=0, sticky="ew", padx=6, pady=2)

        r1 = ttk.Frame(lf)
        r1.pack(fill="x")
        for label, on_cmd, off_cmd, meth in (
                ("AZERO", "AZERO ON", "AZERO OFF", "autozero"),
                ("OCOMP", "OCOMP ON", "OCOMP OFF", "offset_compensation"),
                ("FIXEDZ", "FIXEDZ ON", "FIXEDZ OFF", "fixed_input_z"),
                ("LFILTER", "LFILTER ON", "LFILTER OFF", "level_filter")):
            box = ttk.Frame(r1)
            box.pack(side="left", padx=(0, 14))
            ttk.Label(box, text=label).pack()
            btns = ttk.Frame(box)
            btns.pack()
            ttk.Button(btns, text="on", width=4,
                       command=lambda c=on_cmd, m=meth: self._simple(
                           c, lambda d, m=m: getattr(d, m)(True))).pack(side="left")
            ttk.Button(btns, text="off", width=4,
                       command=lambda c=off_cmd, m=meth: self._simple(
                           c, lambda d, m=m: getattr(d, m)(False))).pack(side="left")

        r2 = ttk.Frame(lf)
        r2.pack(fill="x", pady=(8, 0))
        ttk.Label(r2, text="NPLC:").pack(side="left")
        self._nplc_var = tk.StringVar(value="1")
        ttk.Combobox(r2, textvariable=self._nplc_var, width=7, state="readonly",
                     values=["0.0001", "0.001", "0.01", "0.1", "1", "10", "100", "1000"]
                     ).pack(side="left", padx=4)
        ttk.Button(r2, text="Set", command=self._set_nplc).pack(side="left")

        ttk.Label(r2, text="NDIG:").pack(side="left", padx=(14, 0))
        self._ndig_var = tk.StringVar(value="7")
        ttk.Combobox(r2, textvariable=self._ndig_var, width=4, state="readonly",
                     values=["3", "4", "5", "6", "7", "8"]).pack(side="left", padx=4)
        ttk.Button(r2, text="Set", command=self._set_ndig).pack(side="left")

        ttk.Label(r2, text="ACBAND:").pack(side="left", padx=(14, 0))
        self._acband_var = tk.StringVar(value="20,2E6")
        ttk.Entry(r2, textvariable=self._acband_var, width=12).pack(side="left", padx=4)
        ttk.Button(r2, text="Set", command=self._set_acband).pack(side="left")

    def _build_readout(self):
        lf = ttk.LabelFrame(self, text="Reading", padding=6)
        lf.grid(row=3, column=0, sticky="nsew", padx=6, pady=(2, 6))
        lf.columnconfigure(0, weight=1)

        btns = ttk.Frame(lf)
        btns.grid(row=0, column=0, sticky="ew")
        ttk.Button(btns, text="▶ Measure once", command=self._measure).pack(side="left")
        self._cont_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Continuous", variable=self._cont_var,
                        command=self._toggle_continuous).pack(side="left", padx=(10, 0))
        self._state_var = tk.StringVar(value="idle")
        ttk.Label(btns, textvariable=self._state_var, foreground="gray",
                  font=("Consolas", 8)).pack(side="right")

        self._reading_var = tk.StringVar(value="—")
        tk.Label(lf, textvariable=self._reading_var, font=("Consolas", 20, "bold"),
                 fg="#0077cc", anchor="w").grid(row=1, column=0, sticky="ew", pady=(6, 4))

        raw = ttk.Frame(lf)
        raw.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(raw, text="Raw:").pack(side="left")
        self._raw_var = tk.StringVar()
        entry = ttk.Entry(raw, textvariable=self._raw_var)
        entry.pack(side="left", fill="x", expand=True, padx=4)
        entry.bind("<Return>", lambda _e: self._send_raw())
        ttk.Button(raw, text="Send", command=self._send_raw).pack(side="left")

    # -- helpers ------------------------------------------------------------

    def _emit(self, text: str):
        self._log(f"[INSTRUMENT] {text}")

    def _on_func(self):
        self._func = self._func_var.get()
        entry = next(f for f in FUNCTIONS if f[0] == self._func)
        _cmd, _label, unit, ranges = entry
        values = ["AUTO"] + [_eng(r, unit) for r in ranges]
        self._range_cb.config(values=values)
        if self._range_var.get() not in values:
            self._range_var.set("AUTO")
        self._on_range()

    def _selected_range(self):
        """The numeric range for the current function, or None for AUTO."""
        text = self._range_var.get()
        if text == "AUTO":
            return None
        _cmd, _label, unit, ranges = next(f for f in FUNCTIONS if f[0] == self._func)
        for r in ranges:
            if _eng(r, unit) == text:
                return r
        return None

    def _on_range(self):
        rng = self._selected_range()
        note = ""
        if self._func in ("OHM", "OHMF") and rng in OHMS_TEST_CURRENT:
            note = f"sources {_eng(OHMS_TEST_CURRENT[rng], 'A')} through the device"
        elif self._func == "DCI" and rng in DCI_SHUNT:
            note = f"shunt {_eng(DCI_SHUNT[rng], 'Ohm')} (burden voltage)"
        elif self._func in ("OHM", "OHMF"):
            note = "autorange picks the sourced current"
        self._range_note.set(note)

    # -- actions ------------------------------------------------------------

    def _simple(self, label, fn):
        self._run(label, lambda d: (fn(d), (lambda: self._emit(f"{label} sent")))[1])

    def _read_id(self):
        self._run("ID?", lambda d: (lambda v=d.get_id(): self._emit(f"ID? {v}"))())

    def _read_rev(self):
        self._run("REV?", lambda d: (lambda v=d.revision(): self._emit(f"REV? {v}"))())

    def _read_errors(self):
        def _work(d):
            errs = d.drain_errors()
            return lambda: self._emit("errors: " + ("; ".join(errs) if errs else "none"))
        self._run("ERRSTR?", _work)

    def _read_terminals(self):
        def _work(d):
            raw = (d.terminals() or "").strip()
            text = {"1": "FRONT", "2": "REAR", "0": "OPEN"}.get(raw, raw or "?")
            return lambda: self._show_terminals(text, raw)
        self._run("TERM?", _work)

    def _show_terminals(self, text: str, raw: str):
        self._term_var.set(f"terminals: {text}")
        self._term_lbl.config(bg="#166534" if text == "REAR" else "#7f1d1d")
        self._emit(f"TERM? {raw} -> {text}"
                   + ("" if text == "REAR" else "   <-- probe03 wiring is on the REAR"))

    def _reset(self):
        self._simple("RESET", lambda d: d.reset())

    def _preset(self):
        self._simple("PRESET NORM", lambda d: d.preset("NORM"))

    def _beep(self):
        self._simple("TONE", lambda d: d.beep())

    def _self_test(self):
        def _work(d):
            result = d.self_test()
            return lambda: self._emit(f"TEST -> {result or 'no error reported'}")
        self._run("TEST", _work)

    def _autocal(self, mode: str):
        self._simple(f"ACAL {mode}", lambda d: d.autocal(mode))

    def _set_nplc(self):
        v = self._nplc_var.get()
        self._simple(f"NPLC {v}", lambda d: d.set_nplc(float(v)))

    def _set_ndig(self):
        v = self._ndig_var.get()
        self._simple(f"NDIG {v}", lambda d: d.set_ndig(int(v)))

    def _set_acband(self):
        raw = self._acband_var.get()
        low, _, high = raw.partition(",")
        self._simple(f"ACBAND {raw}",
                     lambda d: d.set_acband(float(low), float(high)))

    def _measure(self):
        func = self._func_var.get()
        rng = self._selected_range()
        unit = next(f[2] for f in FUNCTIONS if f[0] == func)

        def _work(d):
            value = d.measure(func, rng)
            shown = f"{value:.8g} {unit}"
            return lambda: (self._reading_var.set(shown),
                            self._emit(f"{func}"
                                       f"{'' if rng is None else ' ' + repr(rng)} -> {shown}"))

        self._run(f"measure {func}", _work)

    def _toggle_continuous(self):
        self._continuous = self._cont_var.get()
        if self._continuous:
            self._continuous_tick()

    def _continuous_tick(self):
        if not self._continuous:
            return
        if not self._busy:
            self._measure()
        self.after(600, self._continuous_tick)

    def _send_raw(self):
        cmd = self._raw_var.get().strip()
        if not cmd:
            return

        def _work(d):
            resp = d.raw(cmd)
            return lambda: self._emit(f"> {cmd}" + (f"\n< {resp}" if resp else ""))

        self._run(f"raw {cmd}", _work)
        self._raw_var.set("")
