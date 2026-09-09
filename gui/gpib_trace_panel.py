import datetime
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from instruments import gpib_trace

import workdir


class _FakeResource:
    """Stand-in for a pyvisa MessageBasedResource - swapped onto a REAL
    driver object's own .inst for the life of one dry-run measurement (see
    GpibTracePanel._log_commands_from_recipe), so every real command-
    building/response-parsing code path in that driver's actual class runs
    exactly as it would for a real measurement, but nothing it does ever
    reaches the real bus. Never opens anything, never closes anything,
    never talks to hardware - .write()/.query() just record the exact
    string that would have gone out and hand back an always-parseable
    placeholder reply, so a driver that reads its own response back (a
    reading, a CLOS? check, SYST:ERR?) does not raise and stop the trace
    partway through the recipe."""

    def __init__(self, label: str, log: list):
        self._label = label
        self._log = log
        self.timeout = 3000
        self.encoding = "latin-1"

    def write(self, message, *a, **kw):
        self._log.append(("TX", self._label, str(message)))
        return (len(str(message)), 1)

    def query(self, message, *a, **kw):
        self._log.append(("TX", self._label, str(message)))
        resp = self._fake_reply(message)
        self._log.append(("RX", self._label, resp))
        return resp

    def read(self, *a, **kw):
        resp = self._fake_reply("")
        self._log.append(("RX", self._label, resp))
        return resp

    def read_raw(self, *a, **kw):
        return self._fake_reply("").encode(self.encoding, errors="replace")

    def read_stb(self):
        self._log.append(("STB", self._label, 64))
        return 64

    def control_ren(self, *a, **kw):
        return None

    def clear(self):
        pass

    def close(self):
        pass

    def _fake_reply(self, message) -> str:
        # Good enough for anything downstream that expects a reading, a
        # boolean CLOS?-style 0/1 (reads as "not closed", harmless - this
        # trace is never used to judge PASS/FAIL, only which commands were
        # sent), or an error-queue check. Never used to fabricate a real
        # measurement value anywhere else in the app.
        m = (str(message) or "").upper()
        if "ERR" in m:
            return '+0,"No error"'
        return "+1.000000E-09"


class GpibTracePanel(ttk.Frame):
    """Debug tab: live view of every GPIB/USB command THIS app sends (see
    instruments/gpib_trace.py for why it can't see LabVIEW's own commands -
    that needs NI I/O Trace, run alongside this). Start writes a timestamped
    log file next to GUI System and mirrors every line here as it happens;
    Stop just gates the tracer back off (the underlying pyvisa patch stays
    installed for the life of the process - see gpib_trace.py)."""

    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self._log_path_var = tk.StringVar(value=self._default_log_path())
        self._status_var = tk.StringVar(value="Not tracing")

        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_topbar()
        self._build_view()

        self.bind("<Destroy>", self._on_destroy)

    def _default_log_path(self) -> str:
        try:
            base = workdir.gui_system_dir()
        except Exception:
            base = os.getcwd()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(base, f"gpib_trace_{ts}.log")

    def _build_topbar(self):
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="Log file:").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(bar, textvariable=self._log_path_var)
        entry.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(bar, text="Browse…", command=self._browse).grid(row=0, column=2)

        btn_row = ttk.Frame(bar)
        btn_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._start_btn = ttk.Button(btn_row, text="Start Trace", command=self._start)
        self._start_btn.pack(side="left")
        self._stop_btn = ttk.Button(btn_row, text="Stop", command=self._stop,
                                    state="disabled")
        self._stop_btn.pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Clear view", command=self._clear_view).pack(
            side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Open log folder", command=self._open_folder).pack(
            side="left", padx=(6, 0))
        ttk.Label(btn_row, textvariable=self._status_var, foreground="#2563eb").pack(
            side="left", padx=(12, 0))

        ttk.Separator(bar, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(10, 8))

        recipe_row = ttk.Frame(bar)
        recipe_row.grid(row=3, column=0, columnspan=3, sticky="w")
        self._recipe_log_status_var = tk.StringVar(value="")
        ttk.Button(recipe_row, text="Log commands from recipe",
                  command=self._log_commands_from_recipe).pack(side="left")
        ttk.Label(recipe_row, text="  reads the currently loaded recipe and writes "
                                   "every raw GPIB command one measurement would "
                                   "send to Downloads - never touches real hardware.",
                  foreground="#6b7280").pack(side="left", padx=(6, 0))
        ttk.Label(bar, textvariable=self._recipe_log_status_var,
                  foreground="#2563eb").grid(row=4, column=0, columnspan=3,
                                             sticky="w", pady=(4, 0))

    def _build_view(self):
        frame = ttk.LabelFrame(self, text="Live trace")
        frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self._text = tk.Text(
            frame, bg="#1e1e1e", fg="#e5e7eb", font=("Consolas", 9),
            wrap="none", state="disabled")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._text.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self._text.xview)
        self._text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self._text.tag_configure("tx", foreground="#93c5fd")
        self._text.tag_configure("rx", foreground="#86efac")
        self._text.tag_configure("stb", foreground="#fcd34d")

    def _browse(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="GPIB trace log file", defaultextension=".log",
            initialfile=os.path.basename(self._log_path_var.get()),
            initialdir=os.path.dirname(self._log_path_var.get()) or None,
            filetypes=[("Log files", "*.log"), ("All files", "*.*")])
        if path:
            self._log_path_var.set(path)

    def _start(self):
        path = self._log_path_var.get().strip()
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        gpib_trace.add_listener(self._on_line)
        gpib_trace.start(path)
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._status_var.set(f"Tracing -> {path}")

    def _stop(self):
        gpib_trace.stop()
        gpib_trace.remove_listener(self._on_line)
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._status_var.set("Not tracing")

    def _on_line(self, line: str):
        # Fires from whichever thread made the instrument call - often the
        # background measurement run thread, never safe to touch a Tk
        # widget from directly. Hop to the main loop, same pattern
        # instrument_panel._exec_safe_after uses elsewhere.
        try:
            self.after(0, lambda l=line: self._append(l))
        except Exception:
            pass

    def _append(self, line: str):
        tag = "tx" if line.split()[1:2] == ["TX"] else (
              "rx" if line.split()[1:2] == ["RX"] else (
              "stb" if line.split()[1:2] == ["STB"] else None))
        self._text.configure(state="normal")
        if tag:
            self._text.insert("end", line + "\n", tag)
        else:
            self._text.insert("end", line + "\n")
        self._text.see("end")
        self._text.configure(state="disabled")

    def _clear_view(self):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def _open_folder(self):
        folder = os.path.dirname(self._log_path_var.get()) or "."
        try:
            os.startfile(folder)
        except Exception as e:
            self.controller.log(f"[SETUP] Could not open folder: {e}")

    # -- Log commands from recipe --------------------------------------------
    #
    # A dry run of ONE measurement iteration of whatever recipe is currently
    # loaded on the active system's Recipe tab, through the SAME engine a
    # real Measure press uses (MainLayout._exec_run_steps_once) - so every
    # command it would send (config, turn-on, reset, the works, to every
    # instrument the recipe touches including the switch matrix) is
    # authentic, not a hand-reconstructed guess. The only thing faked is the
    # TRANSPORT: each real driver object's own .inst (its live pyvisa/GPIB
    # session) is swapped for a _FakeResource for the duration, so nothing
    # this produces ever reaches the real bus, then swapped straight back -
    # the real drivers' actual connections are completely untouched
    # afterward. record_result is also stubbed out for the duration so no
    # fabricated reading is written into the real Results tab/exports.

    def _log_commands_from_recipe(self):
        controller = self.controller
        main_layout = getattr(controller, "ui", None)
        system = getattr(controller, "active_system", None)
        if main_layout is None or not hasattr(main_layout, "_exec_run_steps_once"):
            self._recipe_log_status_var.set("No active system found.")
            return
        recipe_panel = getattr(main_layout, "recipe_panel", None)
        if recipe_panel is None:
            self._recipe_log_status_var.set("The active system has no Recipe tab.")
            return
        steps = recipe_panel.get_steps()
        if not steps:
            self._recipe_log_status_var.set(
                "No recipe loaded (or it has no steps) on the active system's "
                "Recipe tab - load one first.")
            return
        recipe_name = recipe_panel.get_active_recipe() or "(unsaved)"

        eg_run = getattr(main_layout, "eg_pma_run", None)
        # Refuse rather than race a real run/measurement for the same
        # driver objects' .inst - both engines' own "already running" flags,
        # since Electroglas tracks its separately from the generic one.
        if getattr(main_layout, "_exec_running", False) or (
                eg_run is not None and getattr(eg_run, "_running", False)):
            self._recipe_log_status_var.set(
                "A run/measurement is already in progress — try again once "
                "it finishes.")
            return

        drivers = dict(controller.drivers)
        swapped = []  # (driver_obj, real_inst) - restored no matter what
        command_log = []
        for role, drv in drivers.items():
            if drv is None or not hasattr(drv, "inst"):
                continue
            swapped.append((drv, drv.inst))
            drv.inst = _FakeResource(role.upper(), command_log)

        real_record_result = getattr(main_layout, "record_result", None)
        if real_record_result is not None:
            main_layout.record_result = lambda *a, **kw: None

        main_layout._exec_running = True
        if eg_run is not None:
            eg_run._running = True
        try:
            main_layout._exec_set_running_buttons(True)
        except Exception:
            pass

        self._recipe_log_status_var.set(
            f"Tracing '{recipe_name}' ({len(steps)} step(s))…")
        threading.Thread(
            target=self._run_recipe_dry_run,
            args=(main_layout, eg_run, real_record_result, swapped,
                 command_log, system, recipe_name, steps),
            daemon=True).start()

    def _run_recipe_dry_run(self, main_layout, eg_run, real_record_result,
                            swapped, command_log, system, recipe_name, steps):
        error = None
        try:
            main_layout._exec_run_steps_once(steps)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        finally:
            # Real connections back first, before anything else - this is
            # the one part that actually matters for "don't touch normal
            # operation".
            for drv, real_inst in swapped:
                drv.inst = real_inst
            if real_record_result is not None:
                main_layout.record_result = real_record_result
            main_layout._exec_running = False
            if eg_run is not None:
                eg_run._running = False
            self.after(0, lambda: self._safe_set_running_buttons(main_layout, False))

        path, count = self._write_recipe_log(system, recipe_name, steps,
                                             command_log, error)
        self.after(0, lambda: self._recipe_log_done(path, count, error))

    def _safe_set_running_buttons(self, main_layout, running: bool):
        try:
            main_layout._exec_set_running_buttons(running)
        except Exception:
            pass

    def _write_recipe_log(self, system, recipe_name, steps, command_log, error):
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            os.makedirs(downloads, exist_ok=True)
        except OSError:
            downloads = os.getcwd()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in " ._-" else "_"
                            for c in (recipe_name or "recipe")).strip() or "recipe"
        path = os.path.join(downloads, f"recipe_commands_{safe_name}_{ts}.log")
        lines = [
            f"Commands one measurement of '{recipe_name}' ({system}) would send",
            f"Generated {datetime.datetime.now().isoformat(timespec='seconds')}",
            f"{len(steps)} recipe step(s) — every real driver's own command-"
            "building/response-parsing ran for real; only the transport "
            "(pyvisa .inst) was faked, so nothing here ever reached real "
            "hardware.",
            "",
        ]
        for kind, label, value in command_log:
            lines.append(f"{kind:<4} {label:<10} {value}")
        if error:
            lines.append("")
            lines.append(f"STOPPED EARLY: {error}")
        if not command_log:
            lines.append("(no commands captured - the recipe may not reference "
                         "any connected instrument role, or every step is "
                         "delay/passfail/move only)")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            return None, len(command_log)
        return path, len(command_log)

    def _recipe_log_done(self, path, count, error):
        if path is None:
            self._recipe_log_status_var.set(
                "Could not write the log file - see the console.")
            return
        note = f" — {error}" if error else ""
        self._recipe_log_status_var.set(
            f"{count} command(s) written to {path}{note}")
        try:
            os.startfile(os.path.dirname(path))
        except Exception:
            pass

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        if gpib_trace.is_enabled():
            gpib_trace.stop()
        gpib_trace.remove_listener(self._on_line)
