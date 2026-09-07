import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

_NONE = "none"
_INT1 = "int1"
_INT2 = "int2"
_FLOAT2 = "float2"

_ZERO_ARG_MOTION = [
    ("Z Up (ZU) — TOUCHDOWN", "z_up",
     "Send ZU?\n\n⚠ CONTACT: the chuck rises until the EDGE SENSOR detects "
     "the needles touching, then applies Z OVERTRAVEL (SP5Z) past that "
     "point.\n\n"
     "Measured on this machine: ZU lands at Z2987, matching a manual "
     "touchdown at Z2990.\n\n"
     "Overtravel is needle pressure — check SET PRMTR line 06 before "
     "sending. This machine's own value is 1.50 mils; a reference prober of "
     "the same family uses 1.00."),
    ("Z Down (ZD) — separate from card", "z_down",
     "Send ZD?\n\nThe chuck drops to the Z DOWN LIMIT — the wafer separates "
     "from the probe card. This is the safe direction."),
    ("Move to First Die (MF)", "move_to_start_die",
     "Send MF?\n\nPositions the first die of the current wafer map."),
    ("Move to Home (HO)", "move_to_home",
     "Send HO?\n\nReturns the chuck to its mechanical home position."),
]

# Z values are in 0.1-mil units (command = mils x 10). This machine's limits:
# 2000 = 200.0 mils = Z DOWN LIMIT, 4000 = 400.0 mils = Z UP LIMIT. ZM is the
# one that actually moves the axis - ZU/ZD are no-ops without a wafer profile.
_ONE_ARG_MOTION = [
    # ZM is open-loop: it goes to a commanded height with no contact sensing.
    # Fine for bench work with nothing fitted; NOT the way to approach a probe
    # card. Touchdown is found by PZ/auto-profile via the edge sensor.
    ("Z Absolute (ZM) — open loop, no contact sensing", "move_z_absolute", "Z"),
    ("Z Relative (ZR) — 0.1 mil steps", "move_z_relative", "dZ"),
    # Verified: MT rotates the chuck and ?T reports the angle, 1:1 with the
    # command value. The physical unit per count is NOT established.
    ("Theta Relative (MT) — ?T tracks 1:1, unit unknown", "move_theta_relative", "dθ"),
]

_TWO_ARG_MOTION = [
    ("Move Relative — M units (MM, default step)", "move_relative_m", "dX", "dY"),
    ("Move Absolute — M units (MA)", "move_absolute_m", "X", "Y"),
    ("Move Absolute — Die (MO)", "move_absolute_die", "Die X", "Die Y"),
    ("Move Relative — Die (MD)", "move_relative_die", "dDie X", "dDie Y"),
    ("Move Micro (FM)", "move_micro", "dX", "dY"),
]

_SETUP_COMMANDS = [
    ("Die Size (SP1, raw units)", "set_die_size", _INT2, ("X", "Y")),
    ("Die Size — mm  (×1000 → SP1)", "set_die_size_mm", _FLOAT2, ("X mm", "Y mm")),
    ("Die Size — mil  (×10 → SP1)", "set_die_size_mil", _FLOAT2, ("X mil", "Y mil")),
    ("Die Size — precise mm (SP29)", "set_die_size_precise_mm", _FLOAT2, ("X mm", "Y mm")),
    ("Reference Die Coordinate (SP2)", "set_reference_die_coordinate", _INT2, ("X", "Y")),
    ("Set First Die (FD)", "set_first_die", _NONE, ()),
    ("Wafer Diameter (SP4D)", "set_wafer_diameter", _INT1, ("Diameter",)),
    ("Starting Wafer Number (SM16N)", "set_starting_wafer_number", _INT1, ("Number",)),
    ("Current Cassette (SM70C)", "set_current_cassette", _INT1, ("Cassette",)),
    ("Flat Orientation (SM3F)", "set_flat_orientation", _INT1, ("Orientation",)),
    ("Coordinate Quadrant (SM11Q)", "set_coordinate_quadrant", _INT1, ("Quadrant",)),
    ("Probe Quadrant (SM2Q)", "set_probe_quadrant", _INT1, ("Quadrant",)),
    ("Units (SM1U)", "set_units", _INT1, ("Unit code",)),
]

_LIMIT_COMMANDS = [
    ("Z Autoalign Height (SP9Z)", "set_z_autoalign_height", _INT1, ("Z",)),
    ("Z Clearance (SP6Z)", "set_z_clearance", _INT1, ("Z",)),
    ("Z Down Limit (SP8Z)", "set_z_down_limit", _INT1, ("Z",)),
    ("Z Up Limit (SP7Z)", "set_z_up_limit", _INT1, ("Z",)),
    ("Z Overtravel (SP5Z)", "set_z_overtravel", _INT1, ("Z",)),
    ("Z Undertravel (SP10Z)", "set_z_undertravel", _INT1, ("Z",)),
    ("Zprofile Height (PH)", "set_zprofile_height", _NONE, ()),
    # LaMP sets 2 = Auto Profile, which is why ZU/ZD do nothing without a
    # profiled wafer. Other values are undocumented - read them off the
    # prober's SET MODE page rather than guessing.
    ("Z Travel Mode (SM5E)  2=auto profile", "set_z_travel_mode", _INT1, ("Mode",)),
]

_COUNTER_COMMANDS = [
    ("Reprobe Count (SP14R)", "set_reprobe_count", _INT1, ("Count",)),
    ("Touchdown Counter (SP19C)", "set_touchdown_counter", _INT1, ("Count",)),
    ("Yield to Pass Wafer (SP33Y)", "set_yield_to_pass_wafer", _INT1, ("Yield %",)),
    ("Count Pulse Width (SM32P)", "set_count_pulse_width", _INT1, ("Width",)),
    ("Probe Clean Count (SM12C)", "set_probe_clean_count", _INT2, ("Count", "W")),
    ("Profiler Retry Count (SM42R)", "set_profiler_retry_count", _INT1, ("Retries",)),
]

_MISC_COMMANDS = [
    ("Wafer X Expansion (SX4C)", "set_wafer_x_expansion", _INT1, ("Coefficient",)),
    ("Wafer Y Expansion (SX5C)", "set_wafer_y_expansion", _INT1, ("Coefficient",)),
    ("Sync Date/Time to Now (TI)", "set_date_time", _NONE, ()),
]

_MOTION_PREFIXES = ("ZU", "ZD", "ZM", "ZR", "MT", "MM", "MO", "MA", "MD",
                    "FM", "MF", "HO", "J", "U", "L", "I")

# Jog direction -> (dX, dY) in die coordinates.
#
# MEASURED ONLY FOR THE BOTTOM-RIGHT (LOAD-POSITION) DATUM. With 0,0 there,
# the only reachable quadrant is up and to the LEFT, so increasing X moves
# the chuck LEFT and increasing Y moves it UP - the X axis reads inverted
# against screen intuition, which is exactly the trap these constants exist
# to remove.
#
# THAT REASONING DOES NOT HOLD ONCE FIRST (FD) HAS BEEN PRESSED WITH A
# CENTRE DATUM - see electroglas_2001x.py's module docstring ("THE DIE GRID
# DEPENDS ENTIRELY ON WHERE THE DATUM WAS SET"). Which physical direction is
# +X was never directly confirmed against a centre datum, only inferred from
# the load-position case. Do not trust the arrow labels blindly once the
# operator has re-zeroed at wafer centre - verify by eye (or against ?P)
# before jogging for real. Flip the pair here only once the centre-datum
# direction has actually been confirmed on the bench; do not guess.
_JOG_LEFT = (1, 0)      # +X
_JOG_RIGHT = (-1, 0)    # -X  (refused at the 0 edge)
_JOG_UP = (0, 1)        # +Y
_JOG_DOWN = (0, -1)     # -Y  (refused at the 0 edge)


class EgProberDebugPanel(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        # ONE GPIB conversation at a time, panel-wide.
        #
        # Every background action here used to get its own thread, and some
        # chain another (a motion finishes, then fires a status read). Two
        # threads sharing one VISA session interleave their drain/write/read
        # steps, so each collects the other's acknowledgement - which shows up
        # as moves that execute without confirmation, mismatched replies, and
        # eventually timeouts that persist.
        #
        # A scripted run doing the identical commands strictly one at a time
        # never reproduced any of it: 12/12 acknowledged, 0.3-0.4s each, and
        # deliberately provoking MF, an axis limit and a device clear changed
        # nothing. The difference was concurrency, not the prober.
        self._gpib_lock = threading.Lock()

        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_topbar()
        self._build_main()

    def _drv(self, silent: bool = False):
        drv = self.controller.drivers.get("prober")
        ok = drv is not None and drv.inst is not None
        if not ok and not silent:
            self._set_status("Not connected", "red")
        return drv if ok else None

    def _log(self, msg: str):
        self.controller.log(msg)

    def _set_status(self, text: str, color: str = "black"):
        self._status_lbl.config(text=text, foreground=color)

    def _show_response(self, label: str, resp: str):
        self._resp_var.set(f"[{label}]  {resp}")

    def _run_bg(self, fn, *args):
        """Run driver work off the UI thread, serialised against every other
        such call in this panel. See _gpib_lock in __init__ for why."""
        def _serialised():
            with self._gpib_lock:
                fn(*args)
        threading.Thread(target=_serialised, daemon=True).start()

    def _build_topbar(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.grid(row=0, column=0, sticky="ew")

        self._status_lbl = ttk.Label(bar, text="Status: —",
                                      font=("Consolas", 10, "bold"),
                                      foreground="gray", width=60, anchor="w")
        self._status_lbl.pack(side="left")
        ttk.Button(bar, text="Read Status (?S)",
                   command=self._cmd_read_status).pack(side="right", padx=2)
        ttk.Button(bar, text="Refresh Telemetry",
                   command=self._cmd_read_telemetry).pack(side="right", padx=2)
        ttk.Button(bar, text="Send LaMP Init",
                   command=self._cmd_send_init).pack(side="right", padx=2)
        ttk.Button(bar, text="Resync Link",
                   command=self._cmd_recover).pack(side="right", padx=2)

    def _build_main(self):
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
        self._build_left(pane)
        self._build_right(pane)

    def _cmd_recover(self):
        """Unwedge the link after a VI_ERROR_TMO.

        A timeout leaves the prober refusing every subsequent write, so without
        this the only way out was restarting the app. Drains, then device
        clear, then reopens the session - none of which move the machine.
        """
        drv = self._drv()
        if not drv:
            return
        self._set_status("resyncing link…", "#0077cc")

        def _run():
            try:
                result = drv.recover()
            except Exception as e:
                result = f"recovery failed: {e}"
            ok = result.startswith("recovered")
            self._log(f"[INSTRUMENT] {result}")
            self.after(0, lambda: self._set_status(result,
                                                   "#22bb55" if ok else "red"))
            if ok:
                self.after(0, self._cmd_read_status)
        self._run_bg(_run)

    def _cmd_read_telemetry(self):
        """Read every verified '?' query and show the decoded result.

        Read-only - no motion, no configuration change.
        """
        drv = self._drv()
        if not drv:
            return

        def _run():
            try:
                data = drv.read_telemetry()
            except Exception as e:
                self.after(0, lambda: self._set_status(f"Telemetry failed: {e}", "red"))
                return

            decoded = [
                ("Z state", data.get("z_state", "")),
                ("Position", data.get("position", "")),
                ("Z", data.get("z", "")),
                ("Wafer #", data.get("wafer_number", "")),
                ("Wafer diameter", (data.get("wafer_diameter_mm", "") and
                                    f"{data['wafer_diameter_mm']} mm")),
                ("Die tally", data.get("die_tally", "")),
                ("Error", data.get("error", "")),
            ]
            lines = [f"  {k:<16} {v}" for k, v in decoded if v]
            raw = [f"  {k:<16} {v}" for k, v in sorted(data.items())
                   if k in ("status", "wafer_info", "die_counts", "cassette", "run_state")]

            def _apply():
                self._log("[INSTRUMENT] decoded:")
                for line in lines:
                    self._log(line)
                self._log("[INSTRUMENT] raw replies:")
                for line in raw:
                    self._log(line)
                err = data.get("error", "")
                ok = err in ("E0", "")
                self._set_status(
                    f"Z {data.get('z_state', '?')}  |  pos {data.get('position', '?')}"
                    f"  |  {data.get('die_tally', '')}  |  {err}",
                    "green" if ok else "red")
            self.after(0, _apply)
        self._run_bg(_run)

    def _cmd_send_init(self):
        """Send the 20 configuration commands LaMP applied at startup.

        Configuration only - SP/SM/SO/SX/WM 'set' commands. Nothing here moves
        the chuck, stage or handler. It does overwrite the prober's current
        setup, so it is confirmed first.
        """
        drv = self._drv()
        if not drv:
            return
        if not messagebox.askokcancel(
                "Send LaMP init sequence — CHECK Z LIMITS FIRST",
                "Send the 20 configuration commands recovered from LaMP?\n\n"
                "Nothing moves — these are SP/SM/SO/SX/WM setup commands only.\n"
                "But they OVERWRITE the prober's Z setup, and LaMP's values do "
                "not match what this machine was last set to:\n\n"
                "    Z overtravel   ->  3.7 mils   (SP5Z37)\n"
                "    Z clearance    ->  15 mils    (SP6Z150)\n"
                "    Z UP LIMIT     ->  420 mils   (SP7Z4200)\n"
                "    Z align height ->  216 mils   (SP9Z2160)\n"
                "    Align scan vel ->  2000       (SP16V2000)\n\n"
                "Overtravel is needle pressure and the up limit is how far the "
                "chuck can climb toward the probe card. Compare these against "
                "the prober's SET PRMTR page before continuing — if the current "
                "values were chosen for the installed probe card, LaMP's are "
                "for a different setup.\n\n"
                "Z parameters are in 0.1-mil units (command = mils x 10)."):
            return

        def _run():
            try:
                count = drv.send_init_sequence(
                    log=lambda line: self.after(0, lambda t=line: self._log(f"[SYSTEM] {t}")))
            except Exception as e:
                self.after(0, lambda: self._set_status(f"Init failed: {e}", "red"))
                return
            self.after(0, lambda: self._set_status(
                f"Init sequence sent ({count} commands)", "green"))
            self.after(0, lambda: self._log(
                "[SYSTEM] complete — press Refresh Telemetry to read back the result"))
        self._run_bg(_run)

    def _build_jog(self, parent):
        """Arrow-pad jog, the software equivalent of the prober's joystick.

        XY defaults to MD (relative die), the only XY motion verified to
        work and which ?P tracks exactly - a Die/Distance toggle switches
        the same pad over to MM (relative microns, via move_relative_um -
        see that method and the MM_UNIT_UM measurement note in
        electroglas_2001x.py for the 2.5 um/count conversion this rests on).
        Z uses ZR (relative), because ZU/ZD are no-ops while Z TRAVEL MODE
        is auto profile.

        Step sizes are read on the main thread before the worker starts -
        Tkinter is not thread-safe.
        """
        lf = ttk.LabelFrame(parent, text="Jog", padding=6)
        lf.pack(fill="x", padx=4, pady=(4, 6))

        mode_row = ttk.Frame(lf)
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="XY mode:").pack(side="left")
        self._jog_xy_mode = tk.StringVar(value="die")
        ttk.Radiobutton(mode_row, text="Die (MD)", value="die",
                        variable=self._jog_xy_mode,
                        command=self._jog_xy_mode_changed).pack(side="left", padx=(4, 8))
        ttk.Radiobutton(mode_row, text="Distance (MM)", value="um",
                        variable=self._jog_xy_mode,
                        command=self._jog_xy_mode_changed).pack(side="left")

        row = ttk.Frame(lf)
        row.pack(fill="x", pady=(2, 0))
        self._jog_xy_step_lbl = tk.StringVar(value="XY step (dies):")
        ttk.Label(row, textvariable=self._jog_xy_step_lbl).pack(side="left")
        self._jog_xy_step = tk.StringVar(value="1")
        ttk.Entry(row, textvariable=self._jog_xy_step, width=4).pack(side="left", padx=(2, 8))
        ttk.Label(row, text="Z step (0.1 mil):").pack(side="left")
        self._jog_z_step = tk.StringVar(value="100")
        ttk.Entry(row, textvariable=self._jog_z_step, width=6).pack(side="left", padx=2)

        # One jog at a time. The prober QUEUES commands it has not finished, so
        # repeated presses stack up and execute long afterwards - cumulative
        # travel nobody asked for. Every button is disabled until the MC
        # acknowledgement for the move in flight comes back. It also keeps two
        # GPIB conversations from overlapping, which is what wedges the link.
        self._jog_busy = False
        self._jog_buttons = []

        pad = ttk.Frame(lf)
        pad.pack(pady=(8, 4))

        def mk(text, r, c, cmd, **kw):
            b = ttk.Button(pad, text=text, width=8, command=cmd)
            b.grid(row=r, column=c, padx=2, pady=2, **kw)
            self._jog_buttons.append(b)
            return b

        # Labelled for the BOTTOM-RIGHT (load-position) datum: die 0,0 there
        # puts the reachable quadrant up and to the LEFT, so increasing X
        # moves the chuck LEFT and increasing Y moves it UP - pressing
        # "right" when 0,0 is already the right-hand edge is what the prober
        # refuses with MF. See the _JOG_* comment above: this labelling is
        # NOT confirmed once FIRST has been pressed against a centre datum.
        mk("↑ up (+Y)", 0, 1, lambda: self._jog_xy(*_JOG_UP))
        mk("← left (+X)", 1, 0, lambda: self._jog_xy(*_JOG_LEFT))
        mk("⌂ 0,0", 1, 1, self._jog_goto_origin)
        mk("→ right (−X)", 1, 2, lambda: self._jog_xy(*_JOG_RIGHT))
        mk("↓ down (−Y)", 2, 1, lambda: self._jog_xy(*_JOG_DOWN))
        mk("where?", 0, 0, self._jog_show_position)
        ttk.Separator(pad, orient="vertical").grid(row=0, column=3, rowspan=3,
                                                   sticky="ns", padx=8)
        mk("Z ▲ up", 0, 4, lambda: self._jog_z(1))
        mk("Z ▼ down", 2, 4, lambda: self._jog_z(-1))

        self._jog_pos = tk.StringVar(value="press a direction to read position")
        ttk.Label(lf, textvariable=self._jog_pos, font=("Consolas", 9),
                  wraplength=380, justify="left").pack(anchor="w", pady=(4, 0))

        # Arrow keys follow the physical direction, same mapping as the
        # buttons - but ONLY while explicitly enabled below. This used to
        # be an unconditional self.bind_all(), which grabs arrow keys for
        # the WHOLE APPLICATION the moment this panel is built, not just
        # while this tab is visible or focused (bind_all's own semantics -
        # it isn't scoped by widget at all) - a real report: arrow keys
        # pressed anywhere else in the app (e.g. Accretech's own tabs) sent
        # a live jog command to whatever prober driver happened to be
        # active, which is exactly how 'AccretechUF200R' object has no
        # attribute 'move_relative_die' surfaced - this Electroglas-only
        # jog reaching an Accretech session. Locked behind an explicit,
        # default-OFF toggle instead of any implicit scoping (hover/focus),
        # so a stray arrow key press can never move real hardware unless
        # the operator deliberately turned this on for this tab, this
        # session.
        self._jog_keys_enabled_var = tk.BooleanVar(value=False)

        def _jog_keys_toggled():
            if self._jog_keys_enabled_var.get():
                for key, (dx, dy) in (("<Up>", _JOG_UP), ("<Down>", _JOG_DOWN),
                                      ("<Left>", _JOG_LEFT), ("<Right>", _JOG_RIGHT)):
                    self.bind_all(key, lambda e, x=dx, y=dy: self._jog_xy(x, y))
                self._log("[JOG] Arrow-key jog ENABLED.")
            else:
                for key in ("<Up>", "<Down>", "<Left>", "<Right>"):
                    self.unbind_all(key)
                self._log("[JOG] Arrow-key jog disabled.")

        self._jog_keys_toggled = _jog_keys_toggled
        ttk.Checkbutton(lf, text="⚠ Enable arrow-key jog (moves real hardware from "
                                 "anywhere in the app while checked - off by default)",
                        variable=self._jog_keys_enabled_var,
                        command=_jog_keys_toggled).pack(anchor="w", pady=(6, 0))

        self._build_theta(parent)

    def _build_theta(self, parent):
        """MT (relative rotation) - see electroglas_2001x.py's module
        docstring for what MT is actually verified to do: the command and
        ?T's one-for-one tracking are real, but the UNIT is not established
        in degrees, and normal operation drives rotation through AA (Auto
        Align), not by hand. CW/CCW here just means "increases ?T" /
        "decreases ?T" - which physical direction that is has not been
        checked against the machine.
        """
        lf = ttk.LabelFrame(parent, text="Theta (rotation)", padding=6)
        lf.pack(fill="x", padx=4, pady=(0, 6))

        row = ttk.Frame(lf)
        row.pack(fill="x")
        ttk.Label(row, text="Theta step (MT units):").pack(side="left")
        self._jog_theta_step = tk.StringVar(value="100")
        ttk.Entry(row, textvariable=self._jog_theta_step, width=6).pack(
            side="left", padx=(2, 0))

        btn_row = ttk.Frame(lf)
        btn_row.pack(pady=(6, 0))

        def mk(text, cmd):
            b = ttk.Button(btn_row, text=text, width=10, command=cmd)
            b.pack(side="left", padx=3)
            self._jog_buttons.append(b)
            return b

        mk("↻ CW", lambda: self._jog_theta(1))
        mk("↺ CCW", lambda: self._jog_theta(-1))

    def _jog_theta(self, direction):
        try:
            step = abs(int(self._jog_theta_step.get()))
        except ValueError:
            messagebox.showerror("Jog", "Theta step must be a whole number (MT units).")
            return
        dtheta = direction * step

        def work(drv):
            ack = drv.move_theta_relative(dtheta)
            t = drv.query("?T")
            self._log(f"[JOG] MT{dtheta:+d} -> {t}")
            return f"?T = {t}   [{ack}]"
        self._jog_start(f"MT{dtheta:+d}", work)

    def _jog_set_busy(self, busy: bool):
        """Lock the jog pad while a move is in flight. Main thread only."""
        self._jog_busy = busy
        state = "disabled" if busy else "normal"
        for button in self._jog_buttons:
            button.config(state=state)

    def _jog_start(self, label, work):
        """Run one jog at a time, unlocking only when the prober has replied.

        Presses made while busy are DROPPED, not queued: the prober already
        queues what it has not finished, so buffering on this side too would
        just stack up travel that arrives long after the operator stopped
        asking for it.
        """
        if self._jog_busy:
            return
        drv = self._drv()
        if not drv:
            return
        self._jog_set_busy(True)

        # Tick a visible elapsed counter while waiting. The buttons are locked
        # until the prober acknowledges, which can take many seconds when it is
        # working through queued moves - without this the pad just sits greyed
        # out and looks like the GUI has hung, which is exactly how it was
        # being read.
        self._jog_done = False
        started = time.monotonic()

        def _tick():
            if self._jog_done:
                return
            waited = time.monotonic() - started
            self._jog_pos.set(f"{label} — waiting for prober… {waited:0.1f}s "
                              f"(it executes queued moves in order)")
            self.after(200, _tick)
        _tick()

        def _run():
            try:
                text = work(drv)
            except Exception as e:
                hint = ("  —  press Resync Link"
                        if "VI_ERROR_TMO" in str(e) or "Timeout" in str(e) else "")
                text = f"blocked: {e}{hint}"
                self._log(f"[JOG] {label} refused: {e}")
            elapsed = time.monotonic() - started

            def _finish(t=text, secs=elapsed):
                self._jog_done = True
                self._jog_pos.set(f"{t}   ({secs:0.1f}s)")
                self._jog_set_busy(False)
            self.after(0, _finish)
        self._run_bg(_run)

    def _jog_xy_mode_changed(self):
        die_mode = self._jog_xy_mode.get() == "die"
        self._jog_xy_step_lbl.set(
            "XY step (dies):" if die_mode else "XY step (um):")

    def _jog_xy(self, sx, sy):
        die_mode = self._jog_xy_mode.get() == "die"
        try:
            step = (abs(int(self._jog_xy_step.get())) if die_mode
                   else abs(float(self._jog_xy_step.get())))
        except ValueError:
            messagebox.showerror(
                "Jog", "XY step must be a whole number of dies."
                if die_mode else "XY step must be a number of microns.")
            return
        dx, dy = sx * step, sy * step

        if die_mode:
            def work(drv):
                ack = drv.move_relative_die(dx, dy)
                pos = drv.get_xy_position()
                self._log(f"[JOG] MD {dx:+d},{dy:+d} -> {pos}")
                return f"?P = {pos}   [{ack}]"
            self._jog_start(f"MD {dx:+d},{dy:+d}", work)
        else:
            def work(drv):
                ack = drv.move_relative_um(dx, dy)
                pos = drv.get_xy_position()
                self._log(f"[JOG] MM {dx:+.0f},{dy:+.0f} um -> {pos}")
                return f"?P = {pos}   [{ack}]"
            self._jog_start(f"MM {dx:+.0f},{dy:+.0f} um", work)

    def _jog_z(self, direction):
        try:
            step = abs(int(self._jog_z_step.get()))
        except ValueError:
            messagebox.showerror("Jog", "Z step must be a whole number (0.1 mil units).")
            return
        dz = direction * step

        def work(drv):
            low, high = drv.z_limits
            here = drv._parse_z(drv.query("?Z"))
            # Z parks at Z0, outside the limits, and a relative move from there
            # is refused because the target is still outside. Step into range
            # first rather than reporting a failure the operator can do nothing
            # obvious about.
            if here is not None and not low <= here <= high:
                entry = low if dz > 0 else high
                ack = drv.move_z_absolute(entry)
                z = drv.query("?Z")
                self._log(f"[JOG] Z was parked at {here}, outside "
                          f"[{low}..{high}] — moved to {z}")
                return (f"?Z = {z}   [{ack}]   "
                        f"(was parked outside the Z limits, moved into range)")
            ack = drv.move_z_relative(dz)
            z = drv.query("?Z")
            self._log(f"[JOG] ZR {dz:+d} -> {z}")
            return f"?Z = {z}   [{ack}]"
        self._jog_start(f"ZR {dz:+d}", work)

    def _jog_goto_origin(self):
        """Walk the chuck back to die 0,0.

        Steps there with MD rather than a single absolute move - see
        goto_die(). Whether 0,0 is wafer centre or the load corner depends
        entirely on where FIRST/FD last set the datum, so this goes to the
        prober's current origin, not to a fixed physical place.
        """
        def work(drv):
            here = drv.get_xy_position()
            if drv._parse_die_position(here) == (0, 0):
                return f"already at {here}"
            result = drv.goto_die(0, 0)
            self._log(f"[JOG] goto 0,0: {here} -> {result}")
            return f"?P = {drv.get_xy_position()}   (walked from {here})"
        self._jog_start("goto 0,0", work)

    def _jog_show_position(self):
        def work(drv):
            return (f"?P = {drv.get_xy_position()}   "
                    f"?Z = {drv.query('?Z')}   "
                    f"{drv.decode_status(drv.get_prober_status())}")
        self._jog_start("reading position", work)

    def _build_left(self, pane):
        outer = ttk.Frame(pane)
        pane.add(outer, weight=1)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        sc = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=sc.yview)
        sc.configure(yscrollcommand=vsb.set)
        sc.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        left = tk.Frame(sc)
        win_id = sc.create_window((0, 0), window=left, anchor="nw")

        left.bind("<Configure>", lambda e: sc.configure(scrollregion=sc.bbox("all")))
        sc.bind("<Configure>", lambda e: sc.itemconfig(win_id, width=e.width))

        def _wheel(e):
            sc.yview_scroll(-1 if e.delta > 0 else 1, "units")
        sc.bind("<MouseWheel>", _wheel)
        left.bind("<MouseWheel>", _wheel)

        self._build_jog(left)

        mf = ttk.LabelFrame(left, text="Chuck / Die Motion", padding=6)
        mf.pack(fill="x", padx=4, pady=(4, 6))
        for label, method, confirm in _ZERO_ARG_MOTION:
            ttk.Button(mf, text=label,
                       command=lambda m=method, l=label, c=confirm:
                       self._send_motion(m, l, [], c)).pack(fill="x", pady=1)
        ttk.Separator(mf, orient="horizontal").pack(fill="x", pady=4)
        for label, method, f1 in _ONE_ARG_MOTION:
            self._motion_row(mf, label, method, (f1,))
        for label, method, f1, f2 in _TWO_ARG_MOTION:
            self._motion_row(mf, label, method, (f1, f2))

        self._setup_section(left, "Wafer / Die Setup", _SETUP_COMMANDS)
        infer_row = ttk.Frame(left)
        infer_row.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Button(infer_row, text="🔍 Infer Current Die Size",
                  command=self._cmd_infer_die_size).pack(fill="x")
        self._setup_section(left, "Z Limits & Profile", _LIMIT_COMMANDS)
        self._setup_section(left, "Counters & Yield", _COUNTER_COMMANDS)
        self._setup_section(left, "Wafer Expansion & Time", _MISC_COMMANDS)

    def _motion_row(self, parent, label, method, field_labels):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=32, anchor="w").pack(side="left")
        vars_ = []
        for fl in field_labels:
            ttk.Label(row, text=f"{fl}:").pack(side="left")
            v = tk.StringVar(value="0")
            ttk.Entry(row, textvariable=v, width=7).pack(side="left", padx=(2, 6))
            vars_.append(v)
        mnemonic = label.rsplit("(", 1)[-1].rstrip(")")
        confirm = f"Send {mnemonic}?\n\nThis causes physical prober motion."
        ttk.Button(row, text="Send", width=6,
                   command=lambda m=method, l=label, vs=vars_, c=confirm:
                   self._send_motion(m, l, vs, c)).pack(side="left")

    def _setup_section(self, parent, title, specs):
        lf = ttk.LabelFrame(parent, text=title, padding=6)
        lf.pack(fill="x", padx=4, pady=(0, 6))
        for label, method, kind, field_labels in specs:
            row = ttk.Frame(lf)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=32, anchor="w").pack(side="left")
            vars_ = []
            for fl in field_labels:
                ttk.Label(row, text=f"{fl}:").pack(side="left")
                v = tk.StringVar(value="0")
                ttk.Entry(row, textvariable=v, width=8).pack(side="left", padx=(2, 6))
                vars_.append(v)
            ttk.Button(row, text="Send", width=6,
                       command=lambda m=method, l=label, k=kind, vs=vars_:
                       self._send_setup(m, l, k, vs)).pack(side="left")

    def _build_right(self, pane):
        right = ttk.Frame(pane, padding=4)
        pane.add(right, weight=1)
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)

        rf = ttk.LabelFrame(right, text="Last Response", padding=6)
        rf.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._resp_var = tk.StringVar(value="—")
        ttk.Label(rf, textvariable=self._resp_var, font=("Consolas", 9),
                  foreground="#0077cc", wraplength=380, justify="left").pack(anchor="w")

        tf = ttk.LabelFrame(right, text="Raw GPIB Terminal", padding=6)
        tf.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        term = ttk.Frame(tf)
        term.pack(fill="x")
        self._cmd_var = tk.StringVar()
        entry = ttk.Entry(term, textvariable=self._cmd_var, font=("Consolas", 10), width=24)
        entry.pack(side="left", padx=(0, 4))
        entry.bind("<Return>", lambda _e: self._send_raw())
        ttk.Button(term, text="Send", command=self._send_raw).pack(side="left", padx=2)

        nf = ttk.LabelFrame(right, text="About Status Reporting", padding=6)
        nf.grid(row=2, column=0, sticky="new")

    def _cmd_read_status(self):
        def _run():
            drv = self._drv()
            if not drv:
                return
            try:
                status = drv.get_prober_status()
                plain = drv.decode_status(status)
                self._log(f"[PROBER] ?S -> {status}    = {plain}")
                bad = "unrecognised" in plain or "CONTACTING" in plain
                self.after(0, lambda: self._set_status(plain,
                                                       "#cc7700" if bad else "#22bb55"))
            except Exception as e:
                self._log(f"[PROBER] Status error: {e}")
                self.after(0, lambda: self._set_status(f"Status error: {e}", "red"))
        self._run_bg(_run)

    def _cmd_infer_die_size(self):
        if not messagebox.askyesno(
                "Infer Die Size",
                "This briefly writes a KNOWN die size (SP1), reads ?P to see "
                "how the die count rescaled, then writes the INFERRED real "
                "size back - no physical motion, but it does send two SP1 "
                "writes.\n\nSend it?"):
            return
        def _run():
            drv = self._drv()
            if not drv:
                return
            try:
                size = drv.infer_die_size()
                self._log(f"[PROBER] Inferred die size: X{size[0]} Y{size[1]} um "
                          "(read via ?P before/after a probe SP1 write, then "
                          "restored)")
                self.after(0, lambda: self._show_response(
                    "Infer Die Size", f"X{size[0]} Y{size[1]} um"))
            except Exception as e:
                self._log(f"[PROBER] Infer Die Size error: {e}")
                self.after(0, lambda: self._show_response("Infer Die Size", f"Error: {e}"))
        self._run_bg(_run)

    def _send_motion(self, method, label, vars_, confirm_msg):
        try:
            args = [int(round(float(v.get()))) for v in vars_]
        except ValueError:
            messagebox.showerror("Invalid Input", f"{label}: enter numeric value(s).")
            return
        if not messagebox.askyesno("Confirm Motion", confirm_msg):
            return
        def _run():
            drv = self._drv()
            if not drv:
                return
            try:
                self._log(f"[PROBER] >> {label}  args={args}")
                result = getattr(drv, method)(*args)
                verdict = ("move complete"
                           if str(result).strip().lower().startswith("mc")
                           else str(result))
                self._log(f"[PROBER] << {result}    = {verdict}")
                self.after(0, lambda: self._show_response(label, verdict))
                self.after(0, self._cmd_read_status)
            except Exception as e:
                self._log(f"[PROBER] ERROR ({label}): {e}")
                self.after(0, lambda: self._resp_var.set(f"Error: {e}"))
        self._run_bg(_run)

    def _send_setup(self, method, label, kind, vars_):
        try:
            if kind == _NONE:
                args = []
            elif kind == _INT1:
                args = [int(round(float(vars_[0].get())))]
            elif kind == _INT2:
                args = [int(round(float(vars_[0].get()))),
                        int(round(float(vars_[1].get())))]
            else:
                args = [float(vars_[0].get()), float(vars_[1].get())]
        except ValueError:
            messagebox.showerror("Invalid Input", f"{label}: enter numeric value(s).")
            return
        def _run():
            drv = self._drv()
            if not drv:
                return
            try:
                self._log(f"[PROBER] >> {label}  args={args}")
                getattr(drv, method)(*args)
                # These SET PRMTR/SET MODE commands are plain writes with no
                # MC/MF acknowledgement of their own (unlike a motion
                # command) - so "sent" alone does not mean the prober
                # actually accepted it. ?E is READ-AND-CLEAR (see the
                # driver's own docstring) and reports the most recent
                # latched error, so reading it right after is the verified
                # way to check whether that write actually landed, without
                # inventing a query outside the confirmed command set.
                code = ""
                try:
                    code = (drv.get_error_code() or "").strip()
                except Exception as e:
                    code = f"?E failed: {e}"
                self._log(f"[PROBER] << ?E -> {code!r}  (checking {label} landed)")
                ok = code.upper() in ("E0", "")
                verdict = "sent — no error (E0)" if ok else f"sent — ERROR: {code}"
                self.after(0, lambda: self._show_response(label, verdict))
                # Any of the Die Size rows just changed SP1 - re-infer it
                # (rather than trust the raw/mm/mil input math here too) and
                # push the result to the Run tab's own display, same as a
                # fresh connect does. See instrument_panel._exec_refresh_die_size.
                if method.startswith("set_die_size"):
                    self._refresh_run_tab_die_size()
            except Exception as e:
                self._log(f"[PROBER] ERROR ({label}): {e}")
                self.after(0, lambda: self._resp_var.set(f"Error: {e}"))
        self._run_bg(_run)

    def _refresh_run_tab_die_size(self):
        ui = getattr(self.controller, "instrument_panel_eg", None)
        refresh = getattr(ui, "_exec_refresh_die_size", None)
        if refresh:
            self.after(0, refresh)

    def _send_raw(self):
        raw = self._cmd_var.get().strip()
        if not raw:
            return
        is_query = raw.startswith("?")
        is_motion = any(raw.upper().startswith(p) for p in _MOTION_PREFIXES)
        if is_motion:
            if not messagebox.askyesno("Motion Command",
                                       f"'{raw}' looks like a motion command.\n\n"
                                       "This may cause physical prober motion.\n"
                                       "Send anyway?"):
                return
        def _run(cmd=raw):
            drv = self._drv()
            if not drv:
                return
            try:
                self._log(f"[PROBER] >> {cmd!r}")
                if is_query:
                    resp = drv.inst.query(cmd)
                    self._log(f"[PROBER] << {resp!r}")
                    self.after(0, lambda: self._resp_var.set(f"[{cmd}] {resp}"))
                else:
                    drv.inst.write(cmd)
                    self.after(0, lambda: self._resp_var.set(
                        "Write sent — no response expected"))
                    if is_motion:
                        self.after(0, self._cmd_read_status)
            except Exception as e:
                self._log(f"[PROBER] ERROR: {e}")
                self.after(0, lambda: self._resp_var.set(f"Error: {e}"))
        self._run_bg(_run)
