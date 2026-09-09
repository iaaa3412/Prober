"""Alternate whole-window GUI mode for NanoZ (Nautilus 1x20-shot) work."""

import tkinter as tk
from tkinter import ttk

from nanoz_panel import NanoZPanel


class NanozModeLayout(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build_header()

        self._body = ttk.Frame(self)
        self._body.grid(row=1, column=0, sticky="nsew")
        self._body.columnconfigure(0, weight=1)
        self._body.rowconfigure(0, weight=1)

        self._holders = {}
        self._current_system = None
        self.refresh_for_system()

    def _build_header(self):
        bar = tk.Frame(self, bg="#374558", height=36)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        tk.Label(bar, text="NanoZ Mode", bg="#374558", fg="#f0a020",
                 font=("Arial", 11, "bold")).pack(side="left", padx=(10, 4))
        tk.Button(bar, text="⬅ Switch to Normal", bd=1, relief="flat",
                  font=("Arial", 9, "bold"), padx=10, pady=2,
                  command=lambda: self.controller.cmd_set_gui_mode("normal")
                  ).pack(side="right", padx=10, pady=6)

    def refresh_for_system(self):
        system = self.controller.active_system
        holder = self._holders.get(system)
        if holder is None:
            holder = self._build_holder(system)
            self._holders[system] = holder
        if self._current_system is not None and self._current_system != system:
            self._holders[self._current_system].grid_remove()
        holder.grid(row=0, column=0, sticky="nsew")
        self._current_system = system

    def _build_holder(self, system):
        holder = ttk.Frame(self._body)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        main_layout = self.controller._by_system[system]["ui"]
        panel = NanoZPanel(holder, controller=self.controller, main_layout=main_layout,
                           system=system)
        panel.grid(row=0, column=0, sticky="nsew")
        holder.nanoz_panel = panel
        return holder

    @property
    def nanoz_panel(self):
        holder = self._holders.get(self._current_system)
        return getattr(holder, "nanoz_panel", None) if holder is not None else None

    def any_running(self) -> bool:
        for holder in self._holders.values():
            panel = getattr(holder, "nanoz_panel", None)
            if panel is not None and (
                    getattr(panel, "_running", False)
                    or getattr(panel, "_cst_armed", False)):
                return True
        return False

    def on_ata_folder_loaded(self, folder_path):
        for holder in self._holders.values():
            panel = getattr(holder, "nanoz_panel", None)
            if panel is None:
                continue
            try:
                if hasattr(panel, "_clear_overlay"):
                    panel._clear_overlay()
                    panel.wafer_map.clear_picks()
                    panel._on_sites_changed([])
                panel.on_ata_folder_loaded(folder_path)
            except Exception:
                pass
