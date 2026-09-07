import tkinter as tk
from tkinter import ttk
import time
import random

from wafer_map_view import WaferMapPanel


class ExecutionDashboard(ttk.Frame):

    STEPS = [
        ("ALIGN_WAFER",   "Find alignment marks and calculate transform"),
        ("MOVE_TO_DIE",   "Move UF200R stage to target die"),
        ("TOUCHDOWN",     "Lower probes onto pad set"),
        ("CONTACT_CHECK", "Verify safe contact state"),
        ("NANOZ_RUN",     "Start NanoZ EK-IV cycle over USB"),
        ("NANOZ_SAMPLE",  "Collect sensor / heater data frames"),
        ("BIN_DIE",       "Evaluate results and assign bin"),
        ("SEPARATE",      "Lift probes before XY move"),
    ]

    def __init__(self, parent, log_fn=None, on_stats_change=None):
        super().__init__(parent)
        self._log_fn          = log_fn
        self._on_stats_change = on_stats_change
        self._wafer_map       = None
        self._prev_die        = None

        self.running       = False
        self.in_contact    = False
        self.aborted       = False
        self.dies          = []
        self.current_index = 0
        self.current_die   = None

        self.wafer_id  = "—"
        self.recipe    = "NAUTILUS_EKIV_SENSOR_TEST"
        self.alignment = {
            "offset_x_um": 0.0, "offset_y_um": 0.0,
            "theta_deg": 0.0,   "confidence": 0.0,
        }
        self.stats = {"tested": 0, "pass": 0, "fail": 0, "skip": 0, "untested": 0}

        self.nanoz_connected       = True
        self.nanoz_cycle           = 3
        self.nanoz_frames          = 0
        self.nanoz_checksum_errors = 0

        self._configure_after_id = None

        self.rowconfigure(0, weight=0)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_control_bar()
        self._build_body()

        self.lbl_die   = _FakeLabel()
        self.lbl_route = _FakeLabel()

        self.log("GUI started.")

    def set_wafer_map(self, wafer_map_panel, wafer_id="—"):
        self._wafer_map = wafer_map_panel
        self.wafer_id   = wafer_id

        if wafer_map_panel._last_dies:
            self._exec_map._last_dies = wafer_map_panel._last_dies
            self._exec_map._draw_from_die_list(wafer_map_panel._last_dies)
            n = len(wafer_map_panel.dies)
            self._exec_map.config(text=f"Wafer Map — {n} dies")

        self.load_dies()

    def load_dies(self):
        src = self._exec_map._last_dies if self._exec_map._last_dies else (
            self._wafer_map._last_dies if self._wafer_map else None
        )
        if not src:
            return

        self.dies = []
        for d in src:
            row, col = d["row"], d["col"]
            x_raw = d["x_um"] if d["x_um"] is not None else 0.0
            y_raw = d["y_um"] if d["y_um"] is not None else 0.0
            self.dies.append({
                "die_id":            f"R{row:02d}C{col:02d}",
                "row":               row,
                "col":               col,
                "x_val":             x_raw,
                "y_val":             y_raw,
                "status":            "UNTESTED",
                "leakage_na":        None,
                "sensor_current_ma": None,
                "heater_current_ma": None,
                "bin":               "UNTESTED",
            })

        self.stats = {
            "tested": 0, "pass": 0, "fail": 0, "skip": 0,
            "untested": len(self.dies),
        }
        self.running = self.in_contact = self.aborted = False
        self.current_index = 0
        self.nanoz_frames = self.nanoz_checksum_errors = 0
        self._prev_die = None
        self._update_current_die()
        self._refresh()
        self._fire_stats()

    def load_recipe(self):
        pass

    def _build_control_bar(self):
        bar = tk.Frame(self, bg="#f1f5f9", bd=1, relief="solid")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        ttk.Button(
            bar, text="▶  Start / Pause", command=self.toggle_running
        ).pack(side="left", padx=(8, 4), pady=5)

        for label, cmd in [
            ("Align",       self.run_alignment),
            ("Touchdown",   self.toggle_touchdown),
            ("Run Test",    self.run_test),
            ("Next Die",    self.next_die),
            ("Abort",       self.abort),
        ]:
            ttk.Button(bar, text=label, command=cmd).pack(side="left", padx=3, pady=5)

        self._state_lbl = tk.Label(
            bar, text="IDLE", bg="#f1f5f9", fg="#6b7280",
            font=("Segoe UI", 11, "bold"),
        )
        self._state_lbl.pack(side="right", padx=12)

    def _build_body(self):
        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=3)
        body.columnconfigure(2, weight=2)

        self._build_left_map(body)
        self._build_center(body)
        self._build_right(body)

    def _build_left_map(self, body):
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        left.rowconfigure(0, weight=1)
        left.rowconfigure(1, weight=0)
        left.columnconfigure(0, weight=1)

        self._exec_map = WaferMapPanel(left)
        self._exec_map.grid(row=0, column=0, sticky="nsew")

        self._exec_map.canvas.bind("<Configure>", self._on_exec_map_configure)

        legend = tk.Frame(left, bg="#f9fafb")
        legend.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        for lbl, color in [
            ("Pass",     "#00d200"),
            ("Fail",     "#e53935"),
            ("Current",  "#dbeafe"),
            ("Contact",  "#ede9fe"),
            ("Untested", "#7aaec8"),
        ]:
            item = tk.Frame(legend, bg="#f9fafb")
            item.pack(side="left", padx=5, pady=2)
            tk.Label(item, text="■", fg=color, bg="#f9fafb",
                     font=("Segoe UI", 10, "bold")).pack(side="left")
            tk.Label(item, text=lbl, bg="#f9fafb", fg="#374151",
                     font=("Segoe UI", 8)).pack(side="left")

    def _on_exec_map_configure(self, _event):
        if self._configure_after_id is not None:
            self.after_cancel(self._configure_after_id)
        self._configure_after_id = self.after(150, self._redraw_exec_map)

    def _redraw_exec_map(self):
        self._configure_after_id = None
        if not self._exec_map._last_dies:
            return
        self._exec_map._draw_from_die_list(self._exec_map._last_dies)
        for d in self.dies:
            if d["status"] != "UNTESTED":
                self._exec_map.update_die(d["row"], d["col"], d["status"])
        if self.current_die:
            r, c = self.current_die["row"], self.current_die["col"]
            if self.current_die["status"] == "UNTESTED":
                self._exec_map.update_die(
                    r, c, "CONTACT" if self.in_contact else "CURRENT"
                )

    def _build_center(self, body):
        center = ttk.Frame(body)
        center.grid(row=0, column=1, sticky="nsew", padx=4)
        center.rowconfigure(1, weight=1)
        center.columnconfigure(0, weight=1)

        seq_lf = ttk.LabelFrame(center, text="Test Sequence")
        seq_lf.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        self._seq = ttk.Treeview(
            seq_lf,
            columns=("step", "description", "status"),
            show="headings",
            height=len(self.STEPS),
        )
        self._seq.heading("step",        text="Step")
        self._seq.heading("description", text="Description")
        self._seq.heading("status",      text="Status")
        self._seq.column("step",        width=120, anchor="w",      stretch=False)
        self._seq.column("description", width=300, anchor="w")
        self._seq.column("status",      width=80,  anchor="center", stretch=False)
        self._seq.pack(fill="x", padx=6, pady=6)

        for step, desc in self.STEPS:
            self._seq.insert("", "end", values=(step, desc, "Ready"))

        log_lf = ttk.LabelFrame(center, text="Execution Log")
        log_lf.grid(row=1, column=0, sticky="nsew")
        log_lf.rowconfigure(0, weight=1)
        log_lf.columnconfigure(0, weight=1)

        ttk.Button(log_lf, text="Clear", command=self._clear_log).grid(
            row=0, column=1, sticky="ne", padx=4, pady=4
        )

        self._log_box = tk.Text(
            log_lf,
            bg="#0f172a", fg="#dbeafe",
            font=("Consolas", 9), wrap="word",
            insertbackground="white", state="disabled",
        )
        self._log_box.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)

        sb = ttk.Scrollbar(log_lf, command=self._log_box.yview)
        sb.grid(row=0, column=1, sticky="nse", pady=6)
        self._log_box.configure(yscrollcommand=sb.set)

    def _build_right(self, body):
        outer = tk.Frame(body)
        outer.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        sc = tk.Canvas(outer, bg="#f9fafb", highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=sc.yview)
        sc.configure(yscrollcommand=vsb.set)
        sc.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        inner = tk.Frame(sc, bg="#f9fafb")
        win_id = sc.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>",
                   lambda e: sc.configure(scrollregion=sc.bbox("all")))
        sc.bind("<Configure>",
                lambda e: sc.itemconfig(win_id, width=e.width))

        def _wheel(e):
            sc.yview_scroll(-1 if e.delta > 0 else 1, "units")

        sc.bind("<MouseWheel>", _wheel)
        inner.bind("<MouseWheel>", _wheel)

        self._die_text   = self._make_card(inner, "Current Die",     lines=9)
        self._nanoz_text = self._make_card(inner, "NanoZ EK-IV USB", lines=10)
        self._build_recipe_card(inner)
        self._stats_text = self._make_card(inner, "Run Statistics",  lines=9)

    @staticmethod
    def _make_card(parent, title, lines):
        frame = tk.Frame(parent, bg="#ffffff", bd=1, relief="groove")
        frame.pack(fill="x", padx=4, pady=3)
        tk.Label(
            frame, text=title, bg="#f3f4f6", fg="#111827",
            font=("Segoe UI", 9, "bold"), anchor="w", padx=6, pady=3,
        ).pack(fill="x")
        txt = tk.Text(
            frame, height=lines, bg="#ffffff", fg="#111827",
            font=("Consolas", 9), bd=0, state="disabled",
        )
        txt.pack(fill="x", padx=6, pady=(2, 4))
        return txt

    def _build_recipe_card(self, parent):
        frame = tk.Frame(parent, bg="#ffffff", bd=1, relief="groove")
        frame.pack(fill="x", padx=4, pady=3)
        tk.Label(
            frame, text="Recipe", bg="#f3f4f6", fg="#111827",
            font=("Segoe UI", 9, "bold"), anchor="w", padx=6, pady=3,
        ).pack(fill="x")
        body = tk.Frame(frame, bg="#ffffff")
        body.pack(fill="x", padx=6, pady=(6, 8))
        self._recipe_name_lbl = tk.Label(
            body, text=f"Loaded:  {self.recipe}",
            bg="#ffffff", fg="#374151",
            font=("Consolas", 9), anchor="w", justify="left", wraplength=180,
        )
        self._recipe_name_lbl.pack(fill="x", pady=(0, 8))
        ttk.Button(
            body, text="⚙  Load Recipe…", command=self._cmd_load_recipe
        ).pack(fill="x")

    def _cmd_load_recipe(self):
        self.log("[RECIPE] Load recipe: not yet wired to a recipe file.")

    def log(self, message):
        ts = time.strftime("%H:%M:%S")
        self._log_box.configure(state="normal")
        self._log_box.insert("end", f"{ts}  {message}\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")
        if self._log_fn:
            self._log_fn(message)

    def _clear_log(self):
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")

    def _update_current_die(self):
        valid = [d for d in self.dies if d["status"] != "SKIP"]
        if not valid:
            self.current_die = None
            return
        self.current_index = max(0, min(self.current_index, len(valid) - 1))
        self.current_die = valid[self.current_index]

    def _map_update_die(self, row, col, status):
        if self._wafer_map:
            self._wafer_map.update_die(row, col, status)
        self._exec_map.update_die(row, col, status)

    def _refresh(self):
        self._refresh_cards()
        self._refresh_state()
        self._highlight_maps()

    def _highlight_maps(self):
        if self._prev_die:
            pr, pc = self._prev_die
            if not self.current_die or (pr, pc) != (self.current_die["row"], self.current_die["col"]):
                prev = next(
                    (d for d in self.dies if d["row"] == pr and d["col"] == pc), None
                )
                if prev:
                    self._map_update_die(pr, pc, prev["status"])

        if self.current_die:
            r, c = self.current_die["row"], self.current_die["col"]
            if self.current_die["status"] == "UNTESTED":
                highlight = "CONTACT" if self.in_contact else "CURRENT"
                self._map_update_die(r, c, highlight)
            else:
                self._map_update_die(r, c, self.current_die["status"])
            self._prev_die = (r, c)

    def _refresh_cards(self):
        d = self.current_die
        die_text = (
            f"Die ID:       {d['die_id']}\n"
            f"Row / Col:    R{d['row']:02d} / C{d['col']:02d}\n"
            f"Stage X:      {d['x_val']:.3f}\n"
            f"Stage Y:      {d['y_val']:.3f}\n"
            f"Status:       {d['status']}\n"
            f"Contact:      {'YES' if self.in_contact else 'NO'}\n"
            f"Leakage:      {self._fmt(d['leakage_na'], 'nA')}\n"
            f"NanoZ I:      {self._fmt(d['sensor_current_ma'], 'mA')}\n"
            f"Heater I:     {self._fmt(d['heater_current_ma'], 'mA')}\n"
        ) if d else "No die selected.\n"
        self._txt_set(self._die_text, die_text)

        self._txt_set(
            self._nanoz_text,
            f"Connection:   {'Connected' if self.nanoz_connected else 'Disconnected'}\n"
            f"Port:         COM7 / FTDI USB Serial\n"
            f"Identity:     Iam 5164\n"
            f"Firmware:     NANOZ EK gen IV  SW:V1.12.153\n"
            f"Cycle:        {self.nanoz_cycle}\n"
            f"Frames:       {self.nanoz_frames}\n"
            f"Checksum Err: {self.nanoz_checksum_errors}\n"
            f"Sensor Mask:  0x0F\n"
            f"Channels:     4 sensors + 2 heaters\n"
            f"Command Set:  ver, whoami, run, pause, #env?\n",
        )

        self._recipe_name_lbl.config(text=f"Loaded:  {self.recipe}")

        total     = len(self.dies)
        tested    = self.stats["tested"]
        yield_pct = 0.0 if tested == 0 else 100 * self.stats["pass"] / tested
        self._txt_set(
            self._stats_text,
            f"Wafer ID:     {self.wafer_id}\n"
            f"Recipe:       {self.recipe}\n"
            f"Total Dies:   {total}\n"
            f"Tested:       {tested}\n"
            f"Pass:         {self.stats['pass']}\n"
            f"Fail:         {self.stats['fail']}\n"
            f"Skip:         {self.stats['skip']}\n"
            f"Untested:     {self.stats['untested']}\n"
            f"Yield:        {yield_pct:.1f}%\n",
        )

    def _refresh_state(self):
        if self.aborted:
            state, color = "ABORTED", "#dc2626"
        elif self.running:
            state, color = "RUNNING", "#2563eb"
        elif self.in_contact:
            state, color = "CONTACT", "#7c3aed"
        else:
            state, color = "IDLE", "#6b7280"
        self._state_lbl.config(text=state, fg=color)

    @staticmethod
    def _txt_set(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    @staticmethod
    def _fmt(value, unit):
        return "--" if value is None else f"{value:.4f} {unit}"

    def _fire_stats(self):
        if self._on_stats_change:
            self._on_stats_change(
                self.stats["tested"],
                self.stats["pass"],
                self.stats["fail"],
                len(self.dies),
            )

    def toggle_running(self):
        if not self.dies:
            return
        self.running = not self.running
        self.aborted = False
        if self.running:
            self._auto_step()
        self._refresh()

    def start_run(self):
        if not self.running:
            self.toggle_running()

    def _auto_step(self):
        if not self.running or self.aborted:
            return
        if not self.current_die:
            self.running = False
            self._refresh()
            return
        if not self.in_contact:
            self.toggle_touchdown()
            self.after(900, self._auto_step)
            return
        self.run_test()
        self.after(900, self._auto_next)

    def _auto_next(self):
        if not self.running or self.aborted:
            return
        if self.in_contact:
            self.toggle_touchdown()
        self.next_die()
        total_testable = len([d for d in self.dies if d["status"] != "SKIP"])
        if self.stats["tested"] >= total_testable:
            self.running = False
            self._refresh()
            return
        self.after(700, self._auto_step)

    def run_alignment(self):
        self.alignment["offset_x_um"] = random.uniform(-3.0, 3.0)
        self.alignment["offset_y_um"] = random.uniform(-3.0, 3.0)
        self.alignment["theta_deg"]   = random.uniform(-0.025, 0.025)
        self.alignment["confidence"]  = random.uniform(98.2, 99.9)
        self._refresh()

    def toggle_touchdown(self):
        if not self.current_die:
            return
        if self.in_contact:
            self.in_contact = False
            self.log(f"[PROBER] Probes separated from {self.current_die['die_id']}.")
        else:
            self.in_contact = True
            self.log(f"[PROBER] Touchdown on {self.current_die['die_id']}.")
        self._refresh()

    def run_test(self):
        if not self.current_die:
            return
        if not self.in_contact:
            return
        d = self.current_die

        leakage        = abs(random.gauss(0.45, 0.25))
        sensor_current = abs(random.gauss(0.120, 0.018))
        heater_current = abs(random.gauss(4.8, 0.5))

        d["leakage_na"]         = leakage
        d["sensor_current_ma"]  = sensor_current
        d["heater_current_ma"]  = heater_current

        passed = (
            leakage < 1.0
            and 0.060 <= sensor_current <= 0.180
            and 3.5   <= heater_current <= 6.5
        )
        if random.random() < 0.07:
            passed = False

        old_status = d["status"]
        d["status"] = "PASS" if passed else "FAIL"
        d["bin"]    = d["status"]

        if old_status not in ("PASS", "FAIL", "CONTACT_FAIL"):
            self.stats["tested"]  += 1
            self.stats["untested"] = max(0, self.stats["untested"] - 1)

        if passed:
            self.stats["pass"] += 1
            self.log(
                f"[RESULTS] PASS {d['die_id']}: "
                f"leak={leakage:.3f} nA, "
                f"I_nanoz={sensor_current:.4f} mA, "
                f"I_heater={heater_current:.3f} mA."
            )
        else:
            self.stats["fail"] += 1
            self.log(
                f"[RESULTS] FAIL {d['die_id']}: "
                f"leak={leakage:.3f} nA, "
                f"I_nanoz={sensor_current:.4f} mA, "
                f"I_heater={heater_current:.3f} mA."
            )

        self._fire_stats()
        self._refresh()

    def next_die(self):
        if self.in_contact:
            self.log("[PROBER] Blocked: probes in contact. Separate first.")
            return
        valid = [d for d in self.dies if d["status"] != "SKIP"]
        if not valid:
            return
        start = self.current_index
        for i in range(1, len(valid) + 1):
            idx = (start + i) % len(valid)
            if valid[idx]["status"] == "UNTESTED":
                self.current_index = idx
                self.current_die   = valid[idx]
                d = self.current_die
                self.log(
                    f"[PROBER] Moved to {d['die_id']} "
                    f"at X={d['x_val']:.3f}, Y={d['y_val']:.3f}."
                )
                self._refresh()
                return
        self.current_index = (self.current_index + 1) % len(valid)
        self.current_die   = valid[self.current_index]
        self.log(f"[PROBER] No untested dies left. At {self.current_die['die_id']}.")
        self._refresh()

    def abort(self):
        self.running    = False
        self.in_contact = False
        self.aborted    = True
        self.log("[PROBER] Run Stopped.")
        self._refresh()

class _FakeLabel:
    def config(self, **_kwargs):
        pass
