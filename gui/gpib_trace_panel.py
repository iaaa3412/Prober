import datetime
import os
import tkinter as tk
from tkinter import ttk

from instruments import gpib_trace

import workdir


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
        # instrument_panel._exec2_safe_after uses elsewhere.
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

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        if gpib_trace.is_enabled():
            gpib_trace.stop()
        gpib_trace.remove_listener(self._on_line)
