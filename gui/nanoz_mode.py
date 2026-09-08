"""Alternate whole-window GUI mode for NanoZ (Nautilus 1x20-shot) work.

Historically NanoZPanel lived as a third top-level tab inside MainLayout,
sitting next to Main/Debug on the Accretech side only. That worked while it
was purely an Accretech feature under active development, but Nautilus
needs to run against Electroglas eventually too, and NanoZ's own workflow
(one shared position window, its own wafer map/recipe handling) doesn't
actually fit as "one more tab" once it has to exist for both systems - it
wants to BE the main window, not share one with the normal Run/Recipe/
Results tab set.

So this module is a second top-level widget AtomicaDashboard can swap into
its _main_pane (see app.py's cmd_set_gui_mode), the same way it already
swaps one MainLayout for another when the operator toggles Accretech <->
Electroglas (cmd_set_active_system). NanozModeLayout itself is system-aware
internally: it shows the SAME NanoZPanel class for both systems, just
constructed with system="accretech" or system="electroglas" - NanoZPanel
itself branches internally wherever prober motion actually differs (the
Run tab's manual controls, and the wafer-plan/wafer-map data source), and
is completely unbranched everywhere else (board I/O, the shot list/named
recipes, Pass/Fail Limits, Charts, Results, NanoZ_EK), so both systems
get the exact same code there rather than two hand-kept copies that can
drift - see nanoz_panel.py's own __init__ docstring. Swaps between them
in place when the system toggle is used while in NanoZ mode, without the
outer widget object itself changing.

Both panels are built against the ALREADY-EXISTING MainLayout instance(s)
for whichever system they show (controller._by_system[system]["ui"]) -
those instances are always constructed at startup regardless of which mode
is displayed (see app.py __init__), so they keep reading/writing the same
ATA folder state, drivers, etc. those MainLayouts always have; only the
container packed around them is new.
"""

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

        # system -> the holder Frame built for it (built lazily, kept
        # around so switching systems back and forth doesn't rebuild the
        # NanoZPanel and lose its board connections/state).
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
        """Show whichever system is currently active, building its holder
        the first time it's needed. Called on first display and again
        whenever the operator toggles Accretech/Electroglas while already
        in NanoZ mode (see app.py cmd_set_active_system)."""
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
        """Whichever system's NanoZPanel is currently displayed - mirrors
        the attribute MainLayout.nanoz_panel used to expose, for external
        callers that only care about what's on screen right now."""
        holder = self._holders.get(self._current_system)
        return getattr(holder, "nanoz_panel", None) if holder is not None else None

    def any_running(self) -> bool:
        """True if ANY built holder's NanoZPanel has a run active, not just
        the currently-displayed one. Both Accretech and Electroglas now
        have a real, run-capable NanoZPanel (see nanoz_panel.py's __init__
        docstring), so a run started on one system keeps going on its own
        background thread even after the operator toggles the system
        switch or leaves NanoZ mode entirely - app.py._any_run_in_progress
        needs this, not the single-system `nanoz_panel` property above, or
        it misses a run left active on whichever system isn't on screen."""
        for holder in self._holders.values():
            panel = getattr(holder, "nanoz_panel", None)
            if panel is not None and (
                    getattr(panel, "_running", False)
                    or getattr(panel, "_cst_armed", False)):
                return True
        return False

    def on_ata_folder_loaded(self, folder_path):
        """Forwarded from MainLayout.load_ata_folder (via app.py) so a
        folder reload still reaches whichever NanoZPanel instance exists,
        even though neither is a child of the MainLayout that loaded it.
        Forwarded to EVERY built holder, not just whichever system is
        currently displayed - each NanoZPanel reads its own system's
        MainLayout's ATA folder regardless of whether NanoZ mode happens
        to be showing Accretech or Electroglas right now, so it needs to
        hear about a change either way.

        Mirrors what MainLayout used to do directly when NanoZPanel was one
        of its own tabs: clear whatever overlay/picks belonged to the
        previous folder before loading the new one's own saved map."""
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
