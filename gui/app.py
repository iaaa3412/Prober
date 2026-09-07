import tkinter as tk
from tkinter import ttk
from tkinter import filedialog, simpledialog, messagebox
import os
import csv
import sys
import datetime as dt
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import workdir
from instrument_panel import MainLayout
from probe_routing_panel import scrollable_routing
from instruments.accretech_uf200r import AccretechUF200R
from instruments.keysight_34461a import Keysight34461A
from instruments.keithley_2636b import Keithley2636B
from instruments.keithley_707b import Keithley707B
from instruments.keysight_33512b import Keysight33512B
from instruments.electroglas_2001x import Electroglas2001X
from instruments.keithley2400 import Keithley2400
from instruments.hp3458a import HP3458A
from instruments.hp6634b import Agilent6634B
from instruments.hp_switchbox import HPSwitchbox
from instruments.hp_e1326b import HPE1326B
from instruments import eg_profiles
from instruments import accretech_profiles
from instruments.gpib_base import GPIBInstrument
import export_formats as xfmt
import app_settings

# Every display name _EG_DRIVERS can produce, in the order eg_profiles.EG_KEYS
# connects them, so the sidebar reads top-to-bottom as the sweep progresses.
# A name missing from here has no status label, and the connect loop cannot
# report on it - keep the two in step.
ELECTROGLAS_INSTRUMENT_NAMES = ["Electroglas 2001X", "Keithley 2400", "HP 3458A",
                                "HP E1326B (VXI)", "HP Switchbox 1", "HP Switchbox 2",
                                "HP Switchbox 3", "Agilent 6634B"]

# Accretech is one machine for now. Electroglas benches come from
# GUI System/eg_probers.yaml instead, because they genuinely differ.
ACCRETECH_BENCHES = ("probe08",)

# Driver classes per (slot, model) - resolved against the active bench's
# accretech_profiles.py profile in init_hardware(), the Accretech analogue
# of _EG_DRIVERS below. instruments/accretech_profiles.py's MODEL_CHOICES
# holds the same model NAMES as plain strings (no driver imports there,
# same separation eg_profiles.py keeps from this file's _EG_DRIVERS).
#
# Every factory takes the SLOT KEY it's being built for and passes it on as
# config_key - each driver defaults that to its own original hardcoded slot
# (e.g. Keithley2636B() still means 'smu' with no argument), so nothing
# existing changes, but a SECOND one of the same model added as a custom
# Setup-tab instrument (Accretech's "+ Add Instrument", picking an already-
# coded driver instead of the driverless Generic fallback) reads/writes ITS
# OWN slot instead of colliding with the original's. See each driver's own
# file for why (same reasoning Keithley2400 already needed for Electroglas).
_ACCRETECH_MODELS = {
    "prober":        {"AccretechUF200R": lambda key: AccretechUF200R(config_key=key)},
    "smu":           {"Keithley2636B": lambda key: Keithley2636B(config_key=key),
                      "Keithley2400":  lambda key: Keithley2400(config_key=key)},
    "dmm":           {"Keysight34461A": lambda key: Keysight34461A(config_key=key)},
    "switch_matrix": {"Keithley707B": lambda key: Keithley707B(config_key=key)},
    "wave_gen":      {"Keysight33512B": lambda key: Keysight33512B(config_key=key)},
}

# Flat model-name -> factory, derived from the table above - lets a CUSTOM
# slot (not one of the five core keys, so not itself a key in
# _ACCRETECH_MODELS) still resolve to a real driver if its chosen model
# matches one already coded for some OTHER slot (e.g. a second 707B added
# as a spare switch matrix), instead of always falling back to the
# driverless Generic wrapper. See init_hardware().
_ALL_ACCRETECH_MODEL_FACTORIES = {}
for _slot_models in _ACCRETECH_MODELS.values():
    _ALL_ACCRETECH_MODEL_FACTORIES.update(_slot_models)

# A recipe step's "instrument" field ("SMU"/"DMM"/"WGEN" - see
# recipe_panel._INSTRUMENTS) -> the _ACCRETECH_MODELS family key it maps
# to. Used by AtomicaDashboard.slots_for_family, not by init_hardware
# itself (which already has its own per-slot model lookup).
_ACCRETECH_FAMILY_KEYS = {"SMU": "smu", "DMM": "dmm", "WGEN": "wave_gen"}

# Same idea for Electroglas, in AtomicaDashboard._EG_DRIVERS profile-key
# terms - a DMM step can already be satisfied by either the 3458A or the
# E1326B VXI on a bench that has both fitted.
_EG_FAMILY_KEYS = {"SMU": ("smu_eg",), "DMM": ("dmm_eg", "dmm_vxi_eg")}
# (display label, controller.drivers key) per slot - the drivers-dict key is
# an app-internal name unrelated to instruments.yaml's own key naming
# (switch_matrix has always been "switch" here, everywhere else in the GUI
# looks it up that way - kept exactly as before).
_ACCRETECH_SLOT_INFO = {
    "prober":        ("UF200R Prober",      "prober"),
    "smu":           ("SMU",                "smu"),
    "dmm":           ("DMM",                "dmm"),
    "switch_matrix": ("SW_MATRIX",          "switch"),
    "wave_gen":      ("Wave Gen",           "wave_gen"),
}

# Every display name init_hardware()'s connections list can produce -
# "{display} ({model})" for every model a slot can ever hold, generated from
# the two dicts above rather than hand-listed, so a status row exists for
# every model a bench might be switched to (see Setup tab's Model dropdown),
# not just whichever one happened to be active when this list was last
# edited. This IS the sidebar's actual row set (status_labels is built from
# it once at startup) - a name produced by init_hardware() that isn't in
# here has no row to update and silently never appears, which is exactly
# what happened before this was generated: the sidebar was still built from
# a hand-written list ("SMU (2636B)") that predated the "{display} ({model})"
# naming init_hardware() switched to ("SMU (Keithley2636B)") - nothing
# matched, so the whole Instruments panel showed no rows at all.
ACCRETECH_INSTRUMENT_NAMES = [
    f"{display} ({model})"
    for key, (display, _drv_key) in _ACCRETECH_SLOT_INFO.items()
    for model in _ACCRETECH_MODELS.get(key, {})
]

ACCRETECH_REQUIRED_DRIVERS = ("prober", "smu", "dmm", "switch", "wave_gen")
# Fallback only - used when the active bench's own profile can't be read
# (see _accretech_required_drv_keys). Now that a bench can freely drop
# smu/dmm/wave_gen or carry several of one kind (drivers/flexible-setup
# work), the REAL required set is computed per bench from whichever slots
# it actually has fitted right now, not this fixed five.
# No "smu"/"power_supply" for the same reason - requiring them would hold the
# Electroglas tab at PENDING forever.
ELECTROGLAS_REQUIRED_DRIVERS = ("prober", "dmm", "relay1", "relay2", "relay3")

# Just-for-fun splash screen shown while the main window builds and the
# startup instrument sweep runs. Flip to False to go straight back to the
# old plain-launch behavior - nothing else needs to change; every splash
# call below already no-ops harmlessly when this is off (see
# _build_splash_screen/_dismiss_splash_screen).
SHOW_SPLASH_SCREEN = True


class AtomicaDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Electrical Prober")
        self.geometry("1400x800")
        # Taskbar/title-bar icon - a live Tk window property, separate
        # from the exe's own embedded icon (see AtomicaATA.spec's EXE
        # icon=). Without this call the window (and so the taskbar
        # button/preview, which some Windows builds source from the
        # window itself rather than the exe resource) falls back to Tk's
        # own default feather icon, no matter what the exe file's icon
        # is set to.
        #
        # iconphoto(), not iconbitmap() - iconbitmap() on Windows hands
        # Tk a single .ico and Tk picks (or Windows scales) one baked-in
        # bitmap from it, which came out blurry at taskbar/preview size
        # on the bench. iconphoto() instead gets several real, freshly-
        # resampled PhotoImages and Windows chooses the sharpest one for
        # whatever context it's rendering (small taskbar button vs large
        # Alt-Tab preview) - True applies it to every Toplevel this root
        # spawns too, not just the root window.
        try:
            icon_path = os.path.join(os.path.dirname(__file__), "app_icon.png")
            if os.path.exists(icon_path):
                from PIL import Image, ImageTk
                master = Image.open(icon_path).convert("RGBA")
                self._app_icon_images = [
                    ImageTk.PhotoImage(master.resize((sz, sz), Image.LANCZOS))
                    for sz in (16, 24, 32, 48, 64, 128, 256)
                ]
                self.iconphoto(True, *self._app_icon_images)
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self._check_machine_config_folder()
        # Self-healing, independent of the dialog above: accretech_probers.yaml
        # is new (this machine's GUI System folder predates it), and
        # ensure_default_file() never guesses or overwrites - it either
        # migrates real addresses already sitting in instruments.yaml or
        # writes a blank probe08 shell. Without this, declining/missing that
        # one-time dialog silently left profile_names() empty forever, which
        # is why the Setup tab's bench picker had no probe08 to show.
        try:
            accretech_profiles.ensure_default_file()
        except Exception:
            pass
        self._splash = None
        self._switch_splash = None
        self._switch_splash_depth = 0
        self._build_splash_screen()
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        self.simulation_running = False
        self.test_queue = []
        self.active_system = "accretech"
        self._by_system = {
            # die_status: (row, col) -> "PASS"/"FAIL", set by
            # instrument_panel._exec2_update_die_color as a run paints the
            # wafer map - the only record of per-die verdicts outside the
            # map widgets themselves, so cmd_save_csv can write them out
            # and cmd_import_results_csv can repaint them on import.
            "accretech":   {"drivers": {}, "results": [], "ui": None,
                            "total": 0, "tested": 0, "passed": 0, "failed": 0,
                            "die_status": {}},
            "electroglas": {"drivers": {}, "results": [], "ui": None,
                            "total": 0, "tested": 0, "passed": 0, "failed": 0,
                            "die_status": {}},
        }
        # False until the startup sweep has run, so a bench selected during
        # construction does not connect twice.
        self._startup_done = False
        # Which systems have had a connect sweep run at least once - a
        # system is only pinged when it is actually selected (at startup,
        # or the first time the operator switches to it), never both, so
        # switching to Accretech does not also probe an Electroglas rig
        # that may not even be powered on, and vice versa.
        self._connected_systems = set()
        self._sys_ready_prev = None
        self._prober_ready = None
        self._prober_stb = None
        # workdir.get_current_working_dir() decides the actual starting
        # value (temporarily forced to proberautomation - see workdir.py);
        # this is just the Tk variable the UI reads/writes.
        self.working_dir_var = tk.StringVar(value=workdir.get_current_working_dir())
        # Keep workdir's own notion of "current" in sync with the UI,
        # whatever changes it (preset dropdown, Browse, or code) - every
        # module that resolves "GUI System" (app_settings, switch_topology,
        # gpib_base) reads workdir.get_current_working_dir(), not this Tk
        # variable directly, since they may run before any GUI exists.
        self.working_dir_var.trace_add(
            "write", lambda *_: workdir.set_current_working_dir(self.working_dir_var.get()))
        self._build_brand_header()
        self.create_toolbar()
        self._main_pane = ttk.PanedWindow(self, orient=tk.VERTICAL)
        self._main_pane.grid(row=2, column=0, sticky="nsew")

        self.instrument_panel = MainLayout(
            parent=self._main_pane, controller=self,
            instrument_names=ACCRETECH_INSTRUMENT_NAMES,
            init_hardware_fn=self.init_hardware, system="accretech")
        self._by_system["accretech"]["ui"] = self.instrument_panel
        self._main_pane.add(self.instrument_panel, weight=1)
        # Whichever top-level widget is actually attached to _main_pane
        # right now - self.ui always points at the active SYSTEM's
        # MainLayout regardless of display (NanoZ mode still needs it as a
        # data holder, see nanoz_mode.py), so this tracks the DISPLAYED
        # widget separately. cmd_set_active_system/cmd_set_gui_mode both
        # forget/add against this, not self.ui, so a mode swap and a
        # system swap never fight over which widget is actually attached.
        self._displayed_widget = self.instrument_panel

        self.instrument_panel_eg = MainLayout(
            parent=self._main_pane, controller=self,
            instrument_names=ELECTROGLAS_INSTRUMENT_NAMES,
            init_hardware_fn=self.init_hardware_eg, system="electroglas")
        self._by_system["electroglas"]["ui"] = self.instrument_panel_eg

        # "normal" (the regular tabbed workspace) or "nanoz" (see
        # gui/nanoz_mode.py) - built lazily by cmd_set_gui_mode the first
        # time it's actually needed, not here, since most stations never
        # use it.
        self.gui_mode = "normal"
        self._nanoz_mode_ui = None

        self._build_bottom_routing()
        if getattr(self, "_pending_setup_log", None):
            self.log(self._pending_setup_log)
            self._pending_setup_log = None
        self._autoload_default_ata_folders()
        # After the folders, so switching system finds its folder already
        # loaded; before init_hardware, so the first connect sweep runs
        # against the bench that was actually chosen.
        self._apply_default_prober()
        self._apply_default_gui_mode()
        # Only the active system (Accretech unless a default prober says
        # otherwise, applied just above) sweeps at startup - pinging the
        # other rig's instruments when nobody selected it just produces
        # "not connected" log noise for hardware that may not even be
        # powered on. The other system connects itself the first time the
        # operator actually switches to it - see cmd_set_active_system.
        self.after(500, self._startup_sweep)
        self.update_statistics_visuals()
        self.check_system_ready()
        self.after(2000, self._system_ready_loop)
        self.after(1500, self._poll_prober_ready)

    def _check_machine_config_folder(self):
        """First thing on startup: does this machine actually have a GUI
        System folder, and does it have all four setup files in it? A fresh
        machine (or one where GUI System was declined/deleted) has neither -
        warn about it up front and offer to either browse to a working
        directory that already has one (e.g. a network share or a cloned
        ProberFolders-style checkout), or scaffold blank versions, rather
        than let the app silently run with nothing connected and no obvious
        reason why, or crash reaching for a config file that was never
        written."""
        status = app_settings.machine_config_status()
        missing = [name for name, present in status.items()
                  if name != "folder" and not present]
        if not missing:
            return
        while True:
            if not status["folder"]:
                prompt = (f"This machine has no GUI System folder at "
                          f"{workdir.gui_system_dir()} - that's where the "
                          "GUI keeps this machine's real setup (instrument "
                          "addresses, Electroglas bench profiles, switch "
                          "wiring, default ATA folder/prober). None of that "
                          "exists yet.")
            else:
                prompt = ("This machine's GUI System folder is missing some "
                          "setup files: " + ", ".join(missing) + ".")
            choice = self._ask_missing_config_action(prompt)
            if choice == "browse":
                selected = filedialog.askdirectory(
                    title="Select Working Directory (contains GUI System)",
                    initialdir=workdir.get_current_working_dir(), parent=self)
                if not selected:
                    continue
                workdir.set_current_working_dir(selected)
                status = app_settings.machine_config_status()
                missing = [name for name, present in status.items()
                          if name != "folder" and not present]
                if not missing:
                    self._pending_setup_log = (
                        f"[SYSTEM] Working directory set to '{selected}'.")
                    return
                # Still missing something at the newly-picked location -
                # loop back and show the (now updated) prompt again rather
                # than silently falling through to blank-scaffold it.
                continue
            elif choice == "create":
                created = app_settings.create_basic_machine_config()
                self._pending_setup_log = (
                    f"[SYSTEM] GUI System folder: created {', '.join(created)} "
                    "with a blank starter setup - fill in real "
                    "addresses/benches on the Setup tab.") if created else None
                return
            else:
                messagebox.showwarning(
                    "No Machine Setup",
                    "Continuing without it - instrument connections and "
                    "per-bench profiles won't work until GUI System exists. "
                    "Nothing will crash, but nothing will connect either.",
                    parent=self)
                return

    def _ask_missing_config_action(self, prompt: str) -> str:
        """Modal choice for _check_machine_config_folder: "browse" (point at
        an existing working directory that already has a GUI System folder),
        "create" (scaffold a blank one here), or "skip" (continue without).
        A plain custom dialog rather than messagebox.askyesno since there
        are three distinct outcomes, not two."""
        dlg = tk.Toplevel(self)
        dlg.title("GUI System Folder")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        result = {"choice": "skip"}

        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(
            frm, wraplength=420, justify="left",
            text=("GUI System and ATA Folders not found. Expected working "
                  "directory is \\\\prober\\M\\ETL\\proberautomation or "
                  "default defined in json file.")
        ).pack(anchor="w", pady=(0, 12))

        def pick(choice):
            result["choice"] = choice
            dlg.destroy()

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Browse for Working Directory...",
                  command=lambda: pick("browse")).pack(side="left")
        ttk.Button(btns, text="Create Blank Setup Locally",
                  command=lambda: pick("create")).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Continue Without",
                  command=lambda: pick("skip")).pack(side="right")

        dlg.protocol("WM_DELETE_WINDOW", lambda: pick("skip"))
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(x,0)}+{max(y,0)}")
        dlg.wait_window()
        return result["choice"]

    def _autoload_default_ata_folders(self):
        """One default ATA folder for the whole project, set via the ⭐ Set
        as Default button on the ATA Folder tab — load it into both systems
        now so switching system doesn't need a manual load."""
        folder = app_settings.get_default_ata_folder()
        if not (folder and os.path.isdir(folder)):
            return
        for system in ("accretech", "electroglas"):
            ui = self._by_system[system]["ui"]
            n_dies = ui.load_ata_folder(folder)
            self._by_system[system]["total"] = n_dies
            self._by_system[system]["tested"] = 0
            self._by_system[system]["passed"] = 0
            self._by_system[system]["failed"] = 0
            self._by_system[system]["results"].clear()
            folder_name = os.path.basename(folder)
            if system == self.active_system:
                self._ata_lbl.config(text=f"ATA: {folder_name}  ({n_dies} dies)",
                                     foreground="#1d4ed8")
                self._refresh_ata_picker()
                self._ata_picker_var.set(self._ata_display_name(folder_name))
                ui.exec_panel.set_wafer_map(ui.wafer_map, wafer_id=folder_name)
                ui.wafer_id_var.set(folder_name)
            ui.exec_panel.log(
                f"[SYSTEM] Default ATA folder '{folder_name}'.")

    @property
    def drivers(self):
        return self._by_system[self.active_system]["drivers"]

    @property
    def results_data(self):
        return self._by_system[self.active_system]["results"]

    @property
    def die_status(self):
        return self._by_system[self.active_system]["die_status"]

    @property
    def ui(self):
        return self._by_system[self.active_system]["ui"]

    @property
    def total_dies(self):
        return self._by_system[self.active_system]["total"]

    @total_dies.setter
    def total_dies(self, value):
        self._by_system[self.active_system]["total"] = value

    @property
    def dies_tested(self):
        return self._by_system[self.active_system]["tested"]

    @dies_tested.setter
    def dies_tested(self, value):
        self._by_system[self.active_system]["tested"] = value

    @property
    def dies_passed(self):
        return self._by_system[self.active_system]["passed"]

    @dies_passed.setter
    def dies_passed(self, value):
        self._by_system[self.active_system]["passed"] = value

    @property
    def dies_failed(self):
        return self._by_system[self.active_system]["failed"]

    @dies_failed.setter
    def dies_failed(self, value):
        self._by_system[self.active_system]["failed"] = value

    def set_run_lock(self, locked: bool):
        """Locks/unlocks the chrome a run doesn't own directly but could
        still pull hardware out from under it - the Accretech/Electroglas
        toggle (switching system tears down and rebuilds self.ui mid-run),
        the ATA folder picker, and the prober bench picker (both reconnect
        instruments on selection). Called from
        instrument_panel._exec2_set_running_buttons (Accretech/EG runs) and
        cassette_panel's own lock (cassette automation) - one place so a
        run started from either doesn't leave the other's entry points
        live. Real per-run controls (Recipe tab, Run tab's own buttons)
        lock themselves; this is only the app-level chrome those panels
        can't reach."""
        for btn in getattr(self, "_system_buttons", {}).values():
            try:
                btn.config(state="disabled" if locked else "normal")
            except tk.TclError:
                pass
        for attr in ("_ata_picker", "_bench_picker"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.config(state="disabled" if locked else "readonly")
                except tk.TclError:
                    pass

    def cmd_set_active_system(self, system):
        if system == self.active_system or system not in self._by_system:
            return
        if self._any_run_in_progress():
            messagebox.showwarning(
                "Run In Progress",
                "A run (or armed cassette automation) is still going on the "
                "current system. Stop or finish it before switching to "
                f"{system.capitalize()} - switching now would pull the "
                "drivers/ATA folder out from under it mid-run.")
            return
        carry_over_folder = self.ui._ata_folder
        old_widget = self._displayed_widget
        self.active_system = system
        self.title("Electrical Prober")
        if self.gui_mode == "nanoz" and self._nanoz_mode_ui is not None:
            # Same widget object stays attached to _main_pane - NanoZ mode
            # is one container that shows whichever system is active
            # internally (see nanoz_mode.NanozModeLayout.refresh_for_system),
            # so there is nothing to forget/re-add here.
            self._nanoz_mode_ui.refresh_for_system()
        else:
            self._main_pane.forget(old_widget)
            if self._main_pane.panes():
                self._main_pane.insert(0, self.ui, weight=1)
            else:
                self._main_pane.add(self.ui, weight=1)
            self._displayed_widget = self.ui
        self._style_system_toggle()
        # Only auto-load anything here when this system has NEVER had a
        # folder loaded (self.ui._ata_folder is still empty) - each
        # system's MainLayout already remembers its own _ata_folder
        # independently, so a system that already has one loaded (whatever
        # it is) must be left exactly as it is on every later toggle. This
        # used to re-check the global default folder (or the OTHER
        # system's folder) on EVERY switch and force-reload it whenever it
        # differed from what was currently showing - so switching
        # Accretech (folder A) -> Electroglas (folder B, picked by hand) ->
        # back to Accretech silently reloaded Electroglas back onto
        # whatever the default/other-system's folder was, wiping the
        # operator's own pick and resetting that system's results/stats -
        # same "switching X should only SELECT, never silently change
        # state" class of bug already fixed for recipe-vs-wafer-map.
        if not self.ui._ata_folder:
            default_folder = app_settings.get_default_ata_folder()
            if default_folder and os.path.isdir(default_folder):
                self._do_load_ata_folder(default_folder)
            elif carry_over_folder:
                self._do_load_ata_folder(carry_over_folder)
        # _do_load_ata_folder (above) already syncs the label/picker when it
        # runs - but if this system's folder was already correctly loaded
        # (e.g. pre-loaded at startup), neither branch above fires, so do it
        # unconditionally here too.
        if self.ui._ata_folder:
            folder_name = os.path.basename(self.ui._ata_folder)
            self._ata_lbl.config(text=f"ATA: {folder_name}", foreground="#1d4ed8")
            self._refresh_ata_picker()
            self._ata_picker_var.set(self._ata_display_name(folder_name))
        else:
            self._ata_lbl.config(text="No ATA loaded", foreground="gray")
            self._ata_picker_var.set("")
        # The prober list is per-system, so it has to follow the toggle.
        self._refresh_bench_picker()
        self._refresh_routing_button()
        self._refresh_buzzer_clear_button()
        self.update_statistics_visuals()
        self.check_system_ready()
        self.log(f"[SYSTEM] Switched active system to {system.capitalize()} "
                 f"— prober {self._active_bench()}.")
        # init_hardware() (below, for a first-time connect) already does its
        # own Refresh XY once the prober answers - this covers switching
        # BACK to an Accretech that was already connected, where
        # init_hardware never runs again.
        if system == "accretech" and system in self._connected_systems \
                and hasattr(self.ui, "_exec2_get_xy"):
            self.ui._exec2_get_xy()
        # First time this system is actually selected, connect its own
        # instruments - not before, and never the other system's. Deferred
        # so the tab swap above finishes redrawing first.
        if system not in self._connected_systems:
            fn = self.init_hardware_eg if system == "electroglas" else self.init_hardware
            self._show_switch_splash(f"Connecting to {system.capitalize()}…")

            def _run_and_dismiss():
                try:
                    fn()
                finally:
                    self._dismiss_switch_splash()

            self.after(100, _run_and_dismiss)

    def cmd_set_gui_mode(self, mode: str):
        """Swap the WHOLE window's main section between "normal" (the
        regular MainLayout tab set) and "nanoz" (gui/nanoz_mode.py's
        NanozModeLayout) - same forget/add-on-_main_pane technique
        cmd_set_active_system already uses to swap MainLayout instances,
        just one level up: here the two things being swapped are entire
        modes rather than the two systems within one mode.
        """
        if mode not in ("normal", "nanoz") or mode == self.gui_mode:
            return
        old_widget = self._displayed_widget
        self.gui_mode = mode
        if mode == "nanoz":
            if self._nanoz_mode_ui is None:
                from nanoz_mode import NanozModeLayout
                self._nanoz_mode_ui = NanozModeLayout(self._main_pane, controller=self)
            else:
                self._nanoz_mode_ui.refresh_for_system()
            new_widget = self._nanoz_mode_ui
            # Same "about to look at NanoZ" moment the removed NanoZ tab's
            # click handler used to catch - load NAUTATA if it isn't
            # already the active ATA folder. Targets self.active_system's
            # own MainLayout, NOT hardcoded to Accretech: load_nautata_
            # folder's own early-return guard checks THAT instance's
            # _ata_folder, and the actual load
            # (controller._do_load_ata_folder) always lands on self.ui
            # (the active system) regardless of which MainLayout this is
            # called on - calling it on Accretech while Electroglas was
            # the active system used to check Accretech's folder state
            # (often already "nautata" from earlier use) and skip loading
            # anything, leaving Electroglas showing whatever unrelated ATA
            # folder it last had rather than the Nautilus wafer builder
            # map.
            try:
                self._by_system[self.active_system]["ui"].load_nautata_folder()
            except Exception:
                pass
        else:
            new_widget = self.ui

        self._main_pane.forget(old_widget)
        if self._main_pane.panes():
            self._main_pane.insert(0, new_widget, weight=1)
        else:
            self._main_pane.add(new_widget, weight=1)
        self._displayed_widget = new_widget
        self.log(f"[SYSTEM] GUI mode switched to {mode}.")
        for ui in (self._by_system["accretech"]["ui"], self._by_system["electroglas"]["ui"]):
            try:
                ui._refresh_nanoz_switch_state()
            except Exception:
                pass

    def cmd_set_default_gui_mode(self, mode: str):
        if mode not in ("normal", "nanoz"):
            return
        app_settings.set_default_gui_mode(mode)
        self.log(f"[SYSTEM] Default GUI mode set to {mode} for this machine.")
        for ui in (self._by_system["accretech"]["ui"], self._by_system["electroglas"]["ui"]):
            try:
                ui._refresh_nanoz_switch_state()
            except Exception:
                pass

    def _apply_default_gui_mode(self):
        """Startup only, mirrors _apply_default_prober. Silent when nothing
        is set - "normal" stays the fallback."""
        mode = app_settings.get_default_gui_mode()
        if mode and mode != self.gui_mode:
            self.cmd_set_gui_mode(mode)

    def notify_nanoz_ata_folder_loaded(self, folder_path: str):
        """Called by MainLayout.load_ata_folder (whichever system's) after
        a folder finishes loading, so NanoZPanel - no longer nested inside
        MainLayout, see gui/nanoz_mode.py - still hears about it. A no-op
        until NanoZ mode has actually been entered at least once (nothing
        to forward to before then)."""
        if self._nanoz_mode_ui is not None:
            try:
                self._nanoz_mode_ui.on_ata_folder_loaded(folder_path)
            except Exception:
                pass

    def _system_ready_loop(self):
        self.check_system_ready()
        self.after(2000, self._system_ready_loop)

    def _poll_prober_ready(self):
        prober = self.drivers.get("prober")
        if not (prober and prober.inst) or self._any_run_in_progress():
            self.after(3000, self._poll_prober_ready)
            return

        def _run():
            try:
                stb, _desc = prober.read_stb_decoded()
                if stb == 76 and prober.confirm_and_clear_alarm():
                    self.log("[SYSTEM] Alarm detected.")
            except Exception:
                stb = None
            self.after(0, lambda: self._set_prober_ready(stb))

        import threading
        threading.Thread(target=_run, daemon=True).start()
        self.after(3000, self._poll_prober_ready)

    def _any_run_in_progress(self) -> bool:
        ui = self.ui
        if getattr(ui, "_exec2_running", False):
            return True
        # CassettePanel never sets a "_running" attribute of its own - the
        # actual per-wafer run is one of the _exec2_running/eg_run checks
        # above/below, this only tracks the automation LOOP watching for
        # wafers to finish (armed, or paused waiting on a yield/error
        # decision) - all three still mean "don't let go of this folder/
        # system/window right now" just as much as a run actually moving.
        cassette = getattr(ui, "cassette_panel", None)
        if cassette is not None and (
                getattr(cassette, "_armed", False)
                or getattr(cassette, "_paused_for_yield", False)
                or getattr(cassette, "_paused_for_error", False)):
            return True
        accr = getattr(ui, "accr_wafer", None)
        if accr is not None and getattr(accr, "_running", False):
            return True
        eg_run = getattr(ui, "eg_pma_run", None)
        if eg_run is not None and getattr(eg_run, "_running", False):
            return True
        # NanoZ is no longer a MainLayout tab (see gui/nanoz_mode.py) - its
        # panel, when one exists, hangs off the NanoZ-mode container
        # instead, and a run there can still be in progress even if the
        # operator has switched back to normal mode mid-run, OR toggled
        # the Accretech/Electroglas switch while still in NanoZ mode (both
        # systems have their own real, run-capable NanoZPanel now - see
        # NanozModeLayout.any_running, which checks every built holder,
        # not just whichever one is currently on screen).
        nanoz_mode_ui = getattr(self, "_nanoz_mode_ui", None)
        if nanoz_mode_ui is not None and nanoz_mode_ui.any_running():
            return True
        return False

    def _on_close_request(self):
        """Bound to the window's WM_DELETE_WINDOW (title-bar X / Alt-F4) -
        a plain self.destroy() would otherwise kill the process mid-run
        with no warning, leaving the prober wherever it was and the
        results file half-written. Only asks when something is actually
        going; closing an idle window still works with no popup, exactly
        as before."""
        if self._any_run_in_progress():
            if not messagebox.askyesno(
                    "Run In Progress",
                    "A run (or armed cassette automation) is still going. "
                    "Closing now will kill it mid-run without a clean stop.\n\n"
                    "Close anyway?", icon="warning", default="no"):
                return
        self.destroy()

    def _set_prober_ready(self, stb):
        self._prober_stb = stb
        self._prober_ready = (stb == 65) if stb is not None else None
        self.check_system_ready()

    def _build_splash_screen(self):
        """A small always-on-top window shown (logo + "Electrical Prober")
        while the main window builds and the startup instrument sweep runs -
        see _dismiss_splash_screen, called once _startup_sweep finishes.

        Self-contained and off the SHOW_SPLASH_SCREEN switch at the top of
        this file: with it False, this just returns and self._splash stays
        None, so _dismiss_splash_screen's deiconify() is the only thing that
        still runs - harmless on a window that was never withdrawn.
        """
        if not SHOW_SPLASH_SCREEN:
            return
        self.withdraw()
        self._splash = self._make_splash_toplevel("Starting up…")

    def _make_splash_toplevel(self, message):
        """Build one always-on-top logo splash window, shared by the
        startup splash (_build_splash_screen) and the bench/system/ATA-
        folder switch splash (_show_switch_splash) - same look, different
        caller and message. Does not touch the main window's own
        withdraw/deiconify state; the caller decides that."""
        splash = tk.Toplevel(self)
        # No title() - overrideredirect windows show no title bar anyway,
        # and this keeps it out of _find_other_instance_window's title match.
        splash.overrideredirect(True)
        # overrideredirect windows get no OS border, so the black border is
        # faked here: the Toplevel itself is black, and an inner frame in
        # the real splash colour is packed inset by BORDER px on every side.
        BORDER = 3
        splash.configure(bg="black")
        w, h = 420, 260
        sw, sh = splash.winfo_screenwidth(), splash.winfo_screenheight()
        splash.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        try:
            splash.attributes("-topmost", True)
        except Exception:
            pass

        body = tk.Frame(splash, bg="#374558")
        body.pack(fill="both", expand=True, padx=BORDER, pady=BORDER)

        logo_path = os.path.join(os.path.dirname(__file__), "logo_otto.jpg")
        if os.path.exists(logo_path):
            try:
                from PIL import Image, ImageTk
                pil_img = Image.open(logo_path).convert("RGBA")
                target_h = 140
                scale = target_h / pil_img.height
                pil_img = pil_img.resize(
                    (max(1, int(pil_img.width * scale)), target_h))
                img = ImageTk.PhotoImage(pil_img)
                # bg matches the splash background exactly - the PNG's own
                # transparent areas are the same colour, so this makes the
                # edges disappear instead of showing a mismatched box.
                lbl_img = tk.Label(body, image=img, bg="#374558", bd=0,
                                   highlightthickness=0)
                lbl_img.image = img
                lbl_img.pack(pady=(20, 8))
            except Exception:
                pass
        tk.Label(body, text="Electrical Prober", bg="#374558", fg="#f0a020",
                 font=("Arial", 16)).pack()
        msg_lbl = tk.Label(body, text=message, bg="#374558", fg="#cbd5e1",
                           font=("Consolas", 12))
        msg_lbl.pack(pady=(14, 0))
        splash._msg_label = msg_lbl
        splash.update()
        return splash

    def _dismiss_splash_screen(self):
        splash = self._splash
        self._splash = None
        if splash is not None:
            try:
                splash.destroy()
            except Exception:
                pass
        self.deiconify()
        self.lift()

    def _show_switch_splash(self, message):
        """Same splash window as startup, but for a bench/system switch or
        an ATA folder (re)load - anything that pings hardware or reads a
        folder off the network share on the UI thread and would otherwise
        leave the window looking frozen with no explanation. Unlike
        _build_splash_screen this never withdraws the main window - it's
        already up and the user is actively looking at it.

        Reentrant: nested show/dismiss calls (e.g. a system switch that
        itself triggers an ATA folder reload) share one window and a depth
        counter, so the splash only actually closes once the outermost
        caller is done."""
        if not SHOW_SPLASH_SCREEN:
            return
        self._switch_splash_depth = getattr(self, "_switch_splash_depth", 0) + 1
        splash = getattr(self, "_switch_splash", None)
        if splash is None:
            self._switch_splash = self._make_splash_toplevel(message)
        else:
            try:
                splash._msg_label.config(text=message)
                splash.update()
            except Exception:
                pass

    def _dismiss_switch_splash(self):
        if not SHOW_SPLASH_SCREEN:
            return
        self._switch_splash_depth = max(0, getattr(self, "_switch_splash_depth", 0) - 1)
        if self._switch_splash_depth:
            return
        splash = getattr(self, "_switch_splash", None)
        self._switch_splash = None
        if splash is not None:
            try:
                splash.destroy()
            except Exception:
                pass

    def _build_brand_header(self):
        hdr = tk.Frame(self, bg="#374558", height=55)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        # Atomica logo first (left), Otto logo right after it - two
        # separate files (logo2.jpg / logo_otto.jpg) since the splash
        # screen only shows the Otto one. Otto's own file is pre-cropped
        # to its actual content (gui/logo_otto.jpg, cropped from
        # references/ottologo4.png - a version with no background at all
        # - down to its own ~379x212 bounding box) so scaling it to a
        # given height shows a visibly bigger mark instead of mostly
        # resizing blank margin. Otto's label bg has to match this bar's
        # own background colour exactly (#374558, not the old #0E0E0F)
        # for its transparent PNG edges to actually disappear instead of
        # showing a mismatched box. Atomica's own size is bumped up a
        # little too, just enough to fill the bar instead of looking
        # small/flush against it.
        for filename, target_h, pad in (("logo2.jpg", 44, (10, 4)),
                                        ("logo_otto.jpg", 36, (0, 6))):
            logo_path = os.path.join(os.path.dirname(__file__), filename)
            if not os.path.exists(logo_path):
                continue
            try:
                from PIL import Image, ImageTk
                pil_img = Image.open(logo_path).convert("RGBA")
                scale = target_h / pil_img.height
                pil_img = pil_img.resize((max(1, int(pil_img.width * scale)), target_h))
                img = ImageTk.PhotoImage(pil_img)
                lbl_img = tk.Label(hdr, image=img, bg="#374558", bd=0,
                                   highlightthickness=0)
                lbl_img.image = img
                lbl_img.pack(side="left", padx=pad, pady=1)
            except Exception:
                pass
        tk.Label(hdr, text="Electrical Prober",
                 bg="#374558", fg="#f0a020",
                 font=("Arial", 13)).pack(side="left", padx=4)

        toggle_frame = tk.Frame(hdr, bg="#374558")
        toggle_frame.pack(side="right", padx=12, pady=10)
        self._system_buttons = {}
        self._system_buttons["accretech"] = tk.Button(
            toggle_frame, text="Accretech", bd=1, relief="flat",
            font=("Arial", 9, "bold"), padx=10, pady=3,
            command=lambda: self.cmd_set_active_system("accretech"))
        self._system_buttons["accretech"].pack(side="left")
        self._system_buttons["electroglas"] = tk.Button(
            toggle_frame, text="Electroglas", bd=1, relief="flat",
            font=("Arial", 9, "bold"), padx=10, pady=3,
            command=lambda: self.cmd_set_active_system("electroglas"))
        self._system_buttons["electroglas"].pack(side="left")
        self._style_system_toggle()

    def _style_system_toggle(self):
        for system, btn in self._system_buttons.items():
            active = system == self.active_system
            btn.config(
                bg="#f0a020" if active else "#4b5768",
                fg="#1f2937" if active else "#d1d5db",
                activebackground="#f0a020" if active else "#5a6779",
                relief="sunken" if active else "flat")

    def _build_bottom_routing(self):
        lf = ttk.LabelFrame(self._main_pane, text="Switch Routing")
        self._bottom_routing_frame = lf
        self._routing_visible = False
        holder, self.bottom_routing = scrollable_routing(lf, self)
        holder.pack(fill="both", expand=True)

    def accretech_benches(self) -> list:
        try:
            names = accretech_profiles.profile_names()
        except Exception:
            names = []
        return names or list(ACCRETECH_BENCHES)

    def electroglas_benches(self) -> list:
        try:
            return eg_profiles.profile_names()
        except Exception:
            return []

    def apply_prober(self, system: str, bench: str):
        """Switch the whole GUI to a prober: system first, then bench.

        Order matters - cmd_set_eg_profile reconnects instruments and pokes
        the Electroglas UI, so the Electroglas side has to be the active one
        before it runs.
        """
        if system not in self._by_system:
            self.log(f"[SYSTEM] Unknown system {system!r}")
            return
        if system != self.active_system:
            self.cmd_set_active_system(system)
        if system == "electroglas" and bench:
            try:
                if bench != eg_profiles.active_name():
                    self.cmd_set_eg_profile(bench)
            except Exception as e:
                self.log(f"[SYSTEM] Could not select {bench!r}: {e}")
        elif system == "accretech" and bench:
            try:
                if bench != accretech_profiles.active_name():
                    self.cmd_set_accretech_bench(bench)
            except Exception as e:
                self.log(f"[SYSTEM] Could not select {bench!r}: {e}")
        self._refresh_bench_picker()

    def _apply_default_prober(self):
        """Startup only. Silent when nothing is set - Accretech stays the
        fallback, which is what the app did before this setting existed."""
        system, bench = app_settings.get_default_prober()
        if not system:
            return
        self.log(f"[SYSTEM] Default prober: {system} / {bench}")
        self.apply_prober(system, bench)

    def _refresh_buzzer_clear_button(self):
        """Accretech-only - see create_toolbar's comment on this button."""
        btn = getattr(self, "_buzzer_clear_btn", None)
        if btn is None:
            return
        if self.active_system == "electroglas":
            btn.pack_forget()
        else:
            btn.pack(side="left", padx=(0, 6), pady=2, after=self._abort_btn)

    def _refresh_routing_button(self):
        """Switch Routing is an Accretech-only view, so hide its toggle on the
        Electroglas side rather than leaving a button that opens a pane with
        nothing relevant in it. Collapses the pane first if it is open,
        otherwise it would be stranded with no way to close it."""
        btn = getattr(self, "_routing_toggle_btn", None)
        if btn is None:
            return
        if self.active_system == "electroglas":
            if getattr(self, "_routing_visible", False):
                self.cmd_toggle_routing()
            btn.pack_forget()
        else:
            btn.pack(side="right", padx=6, pady=2)

    def cmd_toggle_routing(self):
        if self._routing_visible:
            self._main_pane.forget(self._bottom_routing_frame)
            self._routing_toggle_btn.config(text="▸ Show Routing")
        else:
            self._main_pane.add(self._bottom_routing_frame, weight=0)
            self._routing_toggle_btn.config(text="▾ Hide Routing")
        self._routing_visible = not self._routing_visible

    def cmd_fit_windows(self):
        self.update_idletasks()
        self._fit_all_panes(self)
        self.log("[SYSTEM] Resized windows.")

    def _fit_all_panes(self, widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.PanedWindow):
                self._fit_one_pane(child)
                child.update_idletasks()
            self._fit_all_panes(child)

    @staticmethod
    def _fit_one_pane(pane, min_px=40):
        panes = pane.panes()
        if len(panes) < 2:
            return
        horizontal = str(pane.cget("orient")) == "horizontal"
        total = pane.winfo_width() if horizontal else pane.winfo_height()
        if total < min_px * len(panes):
            return
        reqs = []
        for p in panes:
            w = pane.nametowidget(p)
            reqs.append(max(w.winfo_reqwidth() if horizontal else w.winfo_reqheight(), 1))
        remainder = total - min_px * len(panes)
        req_sum = sum(reqs)
        sizes = [min_px + int(remainder * r / req_sum) for r in reqs]
        sizes[-1] += total - sum(sizes)
        pos = 0
        for i in range(len(panes) - 1):
            pos += sizes[i]
            try:
                pane.sashpos(i, pos)
            except tk.TclError:
                pass

    def log(self, message):
        txt = getattr(getattr(self, "ui", None), "log_text", None)
        if txt is None:
            print(message)
            return
        # Only auto-scroll if the view was already at (or effectively at)
        # the bottom before this line arrived - otherwise every new log
        # line yanked the user back down to "live", making it impossible
        # to scroll up and read past output during a run.
        at_bottom = txt.yview()[1] >= 0.999
        txt.configure(state="normal")
        txt.insert(tk.END, message + "\n")
        if at_bottom:
            txt.see(tk.END)
        txt.configure(state="disabled")

    def _set_status(self, ui, name, mark, colour):
        """Update one roster row, tolerating a name with no label.

        The status labels are built from a fixed name list while the sweep
        works off the bench profile, so the two can drift. A KeyError here used
        to escape the connect loop's own except clause - which repeats the
        lookup - and abort the whole sweep, leaving every instrument after the
        missing one stuck on its previous result. A drifted name is a bug worth
        logging, but never one worth losing the rest of the bench over.
        """
        lbl = ui.status_labels.get(name)
        if lbl is None:
            self.log(f"[SYSTEM] {name} has no status row — add it to the "
                     f"instrument name list for this system")
            return
        lbl.config(text=f"{mark} {name}", foreground=colour)

    def _connect_instruments(self, ui, drivers, connections):
        ui.set_visible_instruments([name for name, _key, _drv in connections])
        # Reset the text, not just the colour. Leaving the previous sweep's
        # tick or cross showing meant an orange row was ambiguous - it could be
        # "not pinged yet" or a stale result from another bench entirely.
        for inst_name, lbl in ui.status_labels.items():
            lbl.config(text=f"⏳ {inst_name}", foreground="orange")
        self.update_idletasks()
        for name, key, driver in connections:
            try:
                response = driver.get_id() if hasattr(driver, "get_id") else driver.query("*IDN?")
                if response:
                    drivers[key] = driver
                    self._set_status(ui, name, "✅", "green")
                    self.log(f"[SYSTEM] Connected: {name}")
                else:
                    raise Exception("No response")
            except Exception as e:
                self._set_status(ui, name, "❌", "red")
                self.log(f"[ERROR] {name}: {e}")

    def _connect_instruments_eg(self, ui, drivers, connections):
        """Electroglas connect. Takes driver *factories*, not instances.

        Separate from _connect_instruments so the Accretech path keeps its
        existing behaviour untouched. Two things differ here:

        - Each driver is built inside the try. The 2400 sends *RST from its
          __init__, which raises when it is switched off, and with the whole
          list built up front that single failure aborted the sequence before
          any status label was updated - the tab just sat on "Pinging...".
        - Presence is settled by a serial poll before any ID query. It answers
          in milliseconds, where an absent instrument otherwise costs a full
          ID-query timeout; this loop runs on the UI thread, so that froze the
          window for ~30s whenever something on the bench was powered off.
        """
        # Only what this bench actually carries. Unfitted instruments used to
        # sit here as permanently grey "(not fitted)" rows; they are now simply
        # absent, and the Instruments tab still reports the full roster.
        ui.set_visible_instruments([name for name, _key, _factory in connections])
        # Reset the text, not just the colour. Leaving the previous sweep's
        # tick or cross showing meant an orange row was ambiguous - it could be
        # "not pinged yet" or a stale result from another bench entirely.
        for inst_name, lbl in ui.status_labels.items():
            lbl.config(text=f"⏳ {inst_name}", foreground="orange")
        self.update_idletasks()
        for name, key, build_driver in connections:
            try:
                driver = build_driver()
                if not driver.is_present():
                    raise Exception("no answer to serial poll")
                response = driver.get_id()
                if response:
                    drivers[key] = driver
                    self._set_status(ui, name, "✅", "green")
                    self.log(f"[SYSTEM] Connected: {name}")
                else:
                    raise Exception("No response")
            except Exception as e:
                self._set_status(ui, name, "❌", "red")
                self.log(f"[ERROR] {name}: {e}")
            # Each row settles as it is pinged rather than all at the end, so a
            # slow instrument reads as "still going" instead of "hung".
            self.update_idletasks()
        if "prober" in drivers and hasattr(ui, "_exec2_refresh_die_size"):
            ui._exec2_refresh_die_size()

    def _startup_sweep(self):
        """Connect whichever system is active (Accretech, unless a default
        prober picked Electroglas above) - not both. See cmd_set_active_system
        for how the other one connects on demand, the first time it is
        actually selected.

        Skips the system if it is already in _connected_systems - a default
        prober set at startup switches the active system via
        cmd_set_active_system before this runs, which already scheduled its
        own connect; sweeping again here would just ping the bus twice."""
        try:
            if self.active_system in self._connected_systems:
                pass
            elif self.active_system == "electroglas":
                self.init_hardware_eg()
            else:
                self.init_hardware()
        finally:
            self._startup_done = True
            self._dismiss_splash_screen()

    def slots_for_family(self, family: str) -> list:
        """Every currently-fitted slot (on whichever system is active)
        whose model belongs to `family` ("SMU"/"DMM"/"WGEN") -
        (drivers_dict_key, model_name, display_label) tuples, in profile
        order. Feeds the Recipe tab's Instrument dropdown so a step can
        target a SPECIFIC instrument when more than one of the same
        family is fitted (a second SMU added via Setup tab's + Add
        Instrument on Accretech, or Electroglas's existing dual-DMM case
        - 3458A and E1326B VXI can both be fitted on the same bench). A
        bench with exactly one instrument of a family still returns
        exactly one entry - same shape either way, nothing downstream
        has to special-case "only one."""
        if self.active_system == "electroglas":
            try:
                fitted = set(eg_profiles.fitted_keys())
            except Exception:
                return []
            out = []
            for prof_key in _EG_FAMILY_KEYS.get(family, ()):
                if prof_key not in fitted:
                    continue
                entry = self._EG_DRIVERS.get(prof_key)
                if not entry:
                    continue
                display, drv_key, _factory = entry
                out.append((drv_key, display, display))
            return out

        family_key = _ACCRETECH_FAMILY_KEYS.get(family)
        if not family_key:
            return []
        bench = accretech_profiles.active_name()
        try:
            fitted = accretech_profiles.fitted_keys(bench)
            profile_instruments = accretech_profiles.instruments(bench)
        except Exception:
            return []
        family_models = set(_ACCRETECH_MODELS.get(family_key, {}).keys())
        out = []
        for key in fitted:
            entry = profile_instruments.get(key) or {}
            model = entry.get("model") or accretech_profiles.DEFAULT_MODEL.get(
                key, accretech_profiles.GENERIC_MODEL)
            # The core slot itself always counts toward its own family,
            # whatever model happens to be registered there - a custom
            # slot only counts if its model is one of this family's
            # known models (see _ACCRETECH_MODELS).
            if key != family_key and model not in family_models:
                continue
            if key in _ACCRETECH_SLOT_INFO:
                display, drv_key = _ACCRETECH_SLOT_INFO[key]
            else:
                display, drv_key = entry.get("name") or key, key
            out.append((drv_key, model, display))
        return out

    def init_hardware(self):
        self._connected_systems.add("accretech")
        bench = accretech_profiles.active_name() or ACCRETECH_BENCHES[0]
        self.log(f"[SYSTEM] Pinging Accretech hardware connections ({bench})...")
        # accretech_probers.yaml is the source of truth (address AND model
        # per slot); instruments.yaml's flat Accretech keys are derived from
        # it, same relationship Electroglas's profile has to instruments.yaml
        # - make sure it matches the active bench before any driver reads an
        # address out of it.
        try:
            accretech_profiles.apply_to_instruments_yaml(bench)
        except Exception as e:
            self.log(f"[SYSTEM] Could not apply Accretech profile {bench!r}: {e}")

        connections = []
        try:
            profile_instruments = accretech_profiles.instruments(bench)
            fitted = accretech_profiles.fitted_keys(bench)
        except Exception as e:
            self.log(f"[SYSTEM] Could not read Accretech profile {bench!r}: {e}")
            profile_instruments, fitted = {}, []
        # fitted_keys() already excludes anything marked not-fitted (Setup
        # tab's own Fitted checkbox - e.g. a disconnected wave gen on a
        # bench that genuinely doesn't have one) - those are skipped
        # entirely here, not pinged and not shown red in the sidebar.
        for key in fitted:
            entry = profile_instruments.get(key) or {}
            model = entry.get("model") or accretech_profiles.DEFAULT_MODEL.get(
                key, accretech_profiles.GENERIC_MODEL)
            # This slot's own registered model first (the normal case for
            # one of the five core keys); otherwise fall back to ANY known
            # model with that exact name, wherever it's normally used - a
            # custom slot set to e.g. "Keithley707B" (Setup tab offers
            # every already-coded model, not just Generic - see
            # accretech_profiles.model_choices_for) resolves to the real
            # driver this way, config_key-bound to ITS OWN slot.
            slot_factory = (_ACCRETECH_MODELS.get(key, {}).get(model)
                            or _ALL_ACCRETECH_MODEL_FACTORIES.get(model))
            if key in _ACCRETECH_SLOT_INFO:
                display, drv_key = _ACCRETECH_SLOT_INFO[key]
            else:
                # A custom slot (Setup tab's "+ Add Instrument") - no fixed
                # display/drivers-dict entry exists for it, so use the
                # slot's own key/name for both.
                display, drv_key = entry.get("name") or key, key
            if slot_factory is None:
                # No real driver class for this (slot, model) at all - the
                # driverless Generic model, or a core slot set to an
                # unrecognized one. Either way, still worth trying: a bare
                # GPIBInstrument opens the address and answers *IDN?/serial-
                # poll (see _connect_instruments's driver.get_id() fallback)
                # without anyone having written a real driver for it yet -
                # see accretech_profiles.GENERIC_MODEL's own comment.
                slot_factory = GPIBInstrument
            # Never let one instrument's constructor take the whole connect
            # sweep down - see Keysight33512B/Keithley2636B/Keysight34461A's
            # own comments (VI_ERROR_NLISTENERS out of an unguarded reset()
            # used to do exactly this, hanging the GUI, whenever that
            # instrument was simply powered off). A driver that raises here
            # shows up as failed-to-connect below, same as any other
            # connect failure, instead of aborting every OTHER instrument
            # still left to try.
            try:
                driver = slot_factory(key)
            except Exception as e:
                self.log(f"[SYSTEM] {display}: could not construct driver — {e}")
                continue
            connections.append((f"{display} ({model})", drv_key, driver))

        acc_ui = self._by_system["accretech"]["ui"]
        try:
            acc_ui.set_bench_label(bench)
        except Exception:
            pass
        self._connect_instruments(acc_ui,
                                  self._by_system["accretech"]["drivers"], connections)
        self.check_system_ready()
        # A fresh connect should never inherit whatever crosspoints were left
        # closed from a previous session/run - start every connect from a
        # known, fully-open state.
        switch_drv = self._by_system["accretech"]["drivers"].get("switch")
        if switch_drv and switch_drv.inst:
            try:
                switch_drv.open_all()
                self.log("[SYSTEM] SW_MATRIX: opened all crosspoints.")
            except Exception as e:
                self.log(f"[SYSTEM] SW_MATRIX open-all failed: {e}")
        if "prober" in self._by_system["accretech"]["drivers"] \
                and hasattr(acc_ui, "_exec2_get_xy"):
            acc_ui._exec2_get_xy()

    # Driver per profile key. Which of these actually get connected depends on
    # the active bench profile - see GUI System/eg_probers.yaml. A key marked
    # not-fitted there is skipped rather than reported as a failure, because the
    # benches genuinely differ: probe02 has a Keithley 2400 and a working VXI
    # multimeter, probe03 has neither.
    _EG_DRIVERS = {
        "prober_eg":     ("Electroglas 2001X",  "prober",  Electroglas2001X),
        # Keithley2400.__init__ takes no config key - it hardcodes 'smu_eg'.
        # Passing one raised TypeError inside the connect loop, so the SMU went
        # red on a bench where it answers perfectly well.
        "smu_eg":        ("Keithley 2400",      "smu",     Keithley2400),
        "dmm_eg":        ("HP 3458A",           "dmm",     HP3458A),
        "dmm_vxi_eg":    ("HP E1326B (VXI)",    "dmm_vxi", lambda: HPE1326B("dmm_vxi_eg")),
        "relay1_eg":     ("HP Switchbox 1",     "relay1",  lambda: HPSwitchbox("relay1_eg")),
        "relay2_eg":     ("HP Switchbox 2",     "relay2",  lambda: HPSwitchbox("relay2_eg")),
        "relay3_eg":     ("HP Switchbox 3",     "relay3",  lambda: HPSwitchbox("relay3_eg")),
        "power_supply_eg": ("Agilent 6634B", "power_supply", Agilent6634B),
    }

    def init_hardware_eg(self):
        self._connected_systems.add("electroglas")
        profile = eg_profiles.active_name()
        self.log(f"[SYSTEM] Pinging Electroglas hardware — {eg_profiles.label(profile)}")
        # instruments.yaml is derived from the profile, so make sure it matches
        # the active bench before any driver reads an address out of it.
        try:
            eg_profiles.apply_to_instruments_yaml(profile)
        except Exception as e:
            self.log(f"[SYSTEM] Could not apply profile {profile!r}: {e}")

        eg_ui = self._by_system["electroglas"]["ui"]
        try:
            eg_ui.set_bench_label(profile)
        except Exception:
            pass

        connections = []
        for key in eg_profiles.fitted_keys(profile):
            entry = self._EG_DRIVERS.get(key)
            if entry is None:
                self.log(f"[SYSTEM] {key} is in the profile but has no driver — skipped")
                continue
            display, drv_key, factory = entry
            connections.append((display, drv_key, factory))

        self._connect_instruments_eg(self._by_system["electroglas"]["ui"],
                                     self._by_system["electroglas"]["drivers"],
                                     connections)
        self.check_system_ready()

    def cmd_set_eg_profile(self, name: str):
        """Switch the Electroglas bench and reconnect against it."""
        try:
            changed = eg_profiles.set_active(name)
        except Exception as e:
            self.log(f"[SYSTEM] Could not switch to {name!r}: {e}")
            return
        # Old sessions point at the previous bench's addresses; drop them rather
        # than leave stale handles that would talk to the wrong instrument.
        drivers = self._by_system["electroglas"]["drivers"]
        for drv in list(drivers.values()):
            try:
                drv.close()
            except Exception:
                pass
        drivers.clear()
        self.log(f"[SYSTEM] Electroglas bench -> {eg_profiles.label(name)}"
                 + (f" ({len(changed)} address(es) updated)" if changed else ""))
        self.log(eg_profiles.summary(name))
        # The Recipe tab only offers instruments the bench actually has, so it
        # has to be told the bench changed.
        ui = self._by_system["electroglas"]["ui"]
        panel = getattr(ui, "recipe_panel", None)
        refresh = getattr(panel, "refresh_bench_instruments", None)
        if refresh:
            try:
                refresh()
            except Exception as e:
                self.log(f"[SYSTEM] Recipe tab instrument refresh failed: {e}")
        # refresh_bench_instruments() above only updates the Recipe TAB's
        # own display (it already re-picks/clears itself for the new
        # bench via _refresh_picker). The Run tab keeps its own separate
        # cached copy (_exec2_steps/_exec2_recipe_var, loaded once when a
        # recipe was picked from ITS OWN dropdown) that nothing was
        # telling to reload - so switching probe02 -> probe03 left the
        # Run tab still armed with probe02's recipe/steps, runnable
        # against the wrong bench, even though the Recipe tab itself had
        # already moved on.
        active_recipe = ""
        try:
            active_recipe = panel.get_active_recipe() if panel else ""
        except Exception:
            pass
        if hasattr(ui, "_exec2_recipe_var"):
            try:
                if active_recipe:
                    ui._exec2_load_recipe_by_name(active_recipe)
                else:
                    ui._exec2_recipe_var.set("")
                    ui._exec2_steps = []
                    if hasattr(ui, "_exec2_steps_tree"):
                        ui._exec2_steps_tree.delete(*ui._exec2_steps_tree.get_children())
                    if hasattr(ui, "_exec2_steps_var"):
                        ui._exec2_steps_var.set(f"No recipe for bench '{name}' yet")
            except Exception as e:
                self.log(f"[SYSTEM] Run tab recipe refresh failed: {e}")
        # During startup the scheduled sweep has not run yet and will pick this
        # bench up, so connecting here as well would just sweep the bus twice.
        if self._startup_done:
            self._show_switch_splash(f"Connecting to {eg_profiles.label(name)}…")
            try:
                self.init_hardware_eg()
            finally:
                self._dismiss_switch_splash()

    def cmd_set_accretech_bench(self, name: str):
        """Switch the Accretech bench and reconnect against it - same shape
        as cmd_set_eg_profile. Probe cards themselves are shared (one
        probe_cards\\ folder, not per-bench), but individual RECIPEs on a
        card CAN be bench-tagged (see RecipePanel._visible_recipe_names -
        lampaccr_probe08new and friends exist precisely because probe08's
        recipes aren't automatically valid on probe08new's single-channel
        2400/no-wave-gen wiring), so the Recipe tab's picker and the Run
        tab's separately-cached steps both need telling, same as
        Electroglas already does here."""
        try:
            changed = accretech_profiles.set_active(name)
        except Exception as e:
            self.log(f"[SYSTEM] Could not switch to {name!r}: {e}")
            return
        drivers = self._by_system["accretech"]["drivers"]
        for drv in list(drivers.values()):
            try:
                drv.close()
            except Exception:
                pass
        drivers.clear()
        self.log(f"[SYSTEM] Accretech bench -> {accretech_profiles.label(name)}"
                 + (f" ({len(changed)} address(es) updated)" if changed else ""))
        self.log(accretech_profiles.summary(name))
        # switch_topology is bench-scoped too (probe08new's single-channel
        # 2400 is wired nothing like probe08's dual-channel 2636B) - the
        # Switch Routing view has to follow the newly active bench's own
        # row wiring, not whatever it last drew for the previous one.
        self.refresh_probe_routing_panels()
        # Any panel with its own "which bench is active" label/highlight -
        # Setup tab and Switch Settings both let you EDIT a bench other than
        # the live one, so their pickers stay put, but the "(currently
        # active)" annotation next to whichever entry matches the toolbar
        # has to track it.
        acc_ui = self._by_system["accretech"]["ui"]
        for attr in ("setup_panel", "switch_settings"):
            panel = getattr(acc_ui, attr, None)
            refresh = getattr(panel, "refresh_active_bench", None)
            if refresh:
                try:
                    refresh()
                except Exception as e:
                    self.log(f"[SYSTEM] {attr} active-bench refresh failed: {e}")
        # The Recipe tab only offers recipes tagged for the active bench (or
        # untagged). See cmd_set_eg_profile's identical block.
        panel = getattr(acc_ui, "recipe_panel", None)
        refresh = getattr(panel, "refresh_bench_instruments", None)
        if refresh:
            try:
                refresh()
            except Exception as e:
                self.log(f"[SYSTEM] Recipe tab instrument refresh failed: {e}")
        # refresh_bench_instruments() above only updates the Recipe TAB's
        # own display. The Run tab keeps its own separate cached copy
        # (_exec2_steps/_exec2_recipe_var, loaded once when a recipe was
        # picked from ITS OWN dropdown) that nothing was telling to
        # reload - so switching probe08 -> probe08new left the Run tab
        # still armed with probe08's recipe/steps, runnable against the
        # wrong bench, even though the Recipe tab itself had already
        # moved on.
        active_recipe = ""
        try:
            active_recipe = panel.get_active_recipe() if panel else ""
        except Exception:
            pass
        if hasattr(acc_ui, "_exec2_recipe_var"):
            try:
                if active_recipe:
                    acc_ui._exec2_load_recipe_by_name(active_recipe)
                else:
                    acc_ui._exec2_recipe_var.set("")
                    acc_ui._exec2_steps = []
                    if hasattr(acc_ui, "_exec2_steps_tree"):
                        acc_ui._exec2_steps_tree.delete(*acc_ui._exec2_steps_tree.get_children())
                    if hasattr(acc_ui, "_exec2_steps_var"):
                        acc_ui._exec2_steps_var.set(f"No recipe for bench '{name}' yet")
            except Exception as e:
                self.log(f"[SYSTEM] Run tab recipe refresh failed: {e}")
        # During startup the scheduled sweep has not run yet and will pick this
        # bench up, so connecting here as well would just sweep the bus twice.
        if self._startup_done:
            self._show_switch_splash(f"Connecting to {accretech_profiles.label(name)}…")
            try:
                self.init_hardware()
            finally:
                self._dismiss_switch_splash()

    def refresh_probe_routing_panels(self):
        """Redraw every live Switch Routing view from the active bench's
        current switch_topology - called after a Switch Settings save/reset
        and after switching Accretech bench (see cmd_set_accretech_bench).
        There are two instances alive at once: the bottom collapsible panel
        (system-agnostic, built once in _build_bottom_routing) and the
        Accretech tab's own Debug > Switch Routing (only ever built for the
        Accretech MainLayout - Electroglas has no switch matrix)."""
        panels = [getattr(self, "bottom_routing", None)]
        acc_ui = self._by_system.get("accretech", {}).get("ui")
        panels.append(getattr(acc_ui, "probe_routing", None))
        for panel in panels:
            if panel is None:
                continue
            try:
                panel.refresh_topology()
            except Exception as exc:
                self.log(f"[SYSTEM] Switch Routing refresh failed: {exc}")

    def accretech_required_drivers(self, bench: str = None) -> tuple:
        """Which controller.drivers keys a RUN on `bench` (default: the
        active one) actually needs right now - the drv_key
        (_ACCRETECH_SLOT_INFO, see init_hardware's connections.append)
        for every slot accretech_profiles.fitted_keys(bench) says this
        bench has fitted, not the fixed ACCRETECH_REQUIRED_DRIVERS five
        unconditionally. A bench with wave_gen marked not-fitted (Setup
        tab's Fitted checkbox - e.g. probe08new, which has no wave gen
        wired at all) or with it removed entirely (this "drivers" branch's
        flexible Setup tab - a bench can drop a slot, or carry more than
        one of a kind, e.g. a second DMM) must not need it connected to be
        READY or to start a run - see check_system_ready and
        instrument_panel._exec2_can_start, which both call this instead
        of hardcoding the five. Falls back to the fixed list if the
        profile can't be read at all."""
        try:
            fitted = accretech_profiles.fitted_keys(bench)
        except Exception:
            return ACCRETECH_REQUIRED_DRIVERS
        if not fitted:
            return ACCRETECH_REQUIRED_DRIVERS
        return tuple(_ACCRETECH_SLOT_INFO.get(k, (None, k))[1] for k in fitted)

    def check_system_ready(self):
        missing = []
        exec2_wm = getattr(self.ui, "_exec2_wafer_map", None)
        if not (exec2_wm and exec2_wm._last_dies):
            missing.append("wafer map")
        if not getattr(self.ui, "_exec2_steps", None):
            missing.append("recipe")
        required_instruments = (self.accretech_required_drivers(accretech_profiles.active_name())
                                if self.active_system == "accretech"
                                else ELECTROGLAS_REQUIRED_DRIVERS)
        if not all(k in self.drivers for k in required_instruments):
            missing.append("instruments")

        ready = not missing
        if ready:
            self.ui.status_label.config(text="SYSTEM READY", foreground="green")
        else:
            self.ui.status_label.config(text=f"PENDING: {', '.join(missing)}", foreground="red")

        if ready != self._sys_ready_prev:
            if ready:
                self.ui.exec_panel.log("[SYSTEM] System ready.")
            elif self._sys_ready_prev is not None:
                self.ui.exec_panel.log(f"[SYSTEM] No longer ready — missing: {', '.join(missing)}")
            self._sys_ready_prev = ready

        self._update_prober_status_label()

    def _update_prober_status_label(self):
        lbl = getattr(self.ui, "prober_status_label", None)
        if lbl is None:
            return
        if "prober" not in self.drivers:
            text = "Prober: not connected"
        elif self._prober_ready is True:
            text = f"Prober: ready to probe (STB={self._prober_stb})"
        elif self._prober_ready is False:
            text = f"Prober: not ready (STB={self._prober_stb})"
        else:
            text = "Prober: status unknown (waiting on STB read)"
        lbl.config(text=text, foreground="orange")

    def create_toolbar(self):
        toolbar = ttk.Frame(self, relief="raised", padding=2)
        toolbar.grid(row=1, column=0, sticky="ew")
        style = ttk.Style()
        style.configure("Abort.TButton", foreground="red", font=("Arial", 9, "bold"))
        self._abort_btn = ttk.Button(toolbar, text="⏹ Abort", style="Abort.TButton",
                                     command=self.cmd_abort)
        self._abort_btn.pack(side="left", padx=6, pady=2)
        # Accretech-only: "E + es" (buzzer_clear) is a UF200R mnemonic with
        # no Electroglas equivalent at all (the EG driver has no
        # buzzer_clear method - error handling there is ?E, read-and-
        # clear, a different mechanism). Left hidden rather than shown-but-
        # broken - see _refresh_buzzer_clear_button.
        self._buzzer_clear_btn = ttk.Button(
            toolbar, text="🔕 Buzzer Clear", command=self.cmd_buzzer_clear)
        self._buzzer_clear_btn.pack(side="left", padx=(0, 6), pady=2)

        ttk.Label(toolbar, text="ATA Folder:").pack(side="left", padx=(6, 2), pady=2)
        self._ata_picker_var = tk.StringVar()
        self._ata_picker_label_to_name: dict[str, str] = {}
        self._ata_picker = ttk.Combobox(
            toolbar, textvariable=self._ata_picker_var, state="readonly",
            width=24, postcommand=self._refresh_ata_picker)
        self._ata_picker.pack(side="left", padx=(0, 4), pady=2)
        self._ata_picker.bind("<<ComboboxSelected>>",
                              lambda _e: self._on_ata_picker_selected())

        # Moved to the Internal tab's own toolbar, next to Load/New ATA
        # Folder.
        # Not packed - the "ATA Folder:" picker above already names the
        # loaded folder, so this text was a second copy of the same
        # information. Left instantiated (just not shown) rather than
        # removed outright, so nothing has to change everywhere else in
        # this file that updates it via .config().
        self._ata_lbl = ttk.Label(toolbar, text="No ATA loaded", foreground="gray",
                                  font=("Segoe UI", 9))

        # Which physical prober the active system is pointed at. The Electroglas
        # benches carry different instruments at different addresses, so this
        # decides what gets connected - see GUI System/eg_probers.yaml.
        # Accretech has only probe08 for now, so its list is a single entry and
        # the control is inert rather than hidden, to keep the toolbar stable.
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y",
                                                       padx=4, pady=3)
        ttk.Label(toolbar, text="Prober:").pack(side="left", padx=(2, 2), pady=2)
        self._bench_picker_var = tk.StringVar()
        self._bench_picker = ttk.Combobox(
            toolbar, textvariable=self._bench_picker_var, state="readonly",
            width=10, postcommand=self._refresh_bench_picker)
        self._bench_picker.pack(side="left", padx=(0, 4), pady=2)
        self._bench_picker.bind("<<ComboboxSelected>>",
                                lambda _e: self._on_bench_picker_selected())
        self._bench_lbl = ttk.Label(toolbar, text="", foreground="gray",
                                    font=("Segoe UI", 9))
        self._bench_lbl.pack(side="left", padx=(2, 8), pady=2)
        self._refresh_bench_picker()
        self._routing_toggle_btn = ttk.Button(
            toolbar, text="▸ Show Routing", command=self.cmd_toggle_routing)
        self._refresh_routing_button()
        ttk.Button(toolbar, text="Fit Windows", command=self.cmd_fit_windows).pack(
            side="right", padx=2, pady=2)
        self.after(200, self._refresh_ata_picker)

    def _find_ata_folders(self):
        working_dir = self.ui.working_dir_var.get() if hasattr(self, "ui") else ""
        if not working_dir or not os.path.isdir(working_dir):
            return []
        found = []
        try:
            for name in os.listdir(working_dir):
                full = os.path.join(working_dir, name)
                if os.path.isdir(full) and name.lower().endswith("ata"):
                    found.append((os.path.getmtime(full), name))
        except OSError:
            return []
        found.sort(key=lambda t: t[0], reverse=True)
        return [name for _mtime, name in found]

    @staticmethod
    def _ata_display_name(name: str) -> str:
        """Toolbar picker shows the trailing "ata" (case-insensitive)
        stripped, e.g. "NautATA" -> "Naut" - display only, the real folder
        name is still what's stored/opened everywhere else."""
        if name and name.lower().endswith("ata"):
            return name[:-3] or name
        return name

    def _refresh_ata_picker(self):
        names = self._find_ata_folders()
        self._ata_picker_label_to_name = {self._ata_display_name(n): n for n in names}
        self._ata_picker.configure(values=list(self._ata_picker_label_to_name.keys()))

    # -- prober bench picker ------------------------------------------------
    #
    # Only Electroglas has real profiles today. Accretech is a single machine,
    # so its "list" is one entry - the control still shows which prober you are
    # on, which is the point, and it grows the day a second Accretech appears.

    def _bench_names(self) -> list:
        if self.active_system == "electroglas":
            try:
                return eg_profiles.profile_names()
            except Exception as e:
                self.log(f"[SYSTEM] Could not read prober profiles: {e}")
                return []
        try:
            return self.accretech_benches()
        except Exception as e:
            self.log(f"[SYSTEM] Could not read Accretech prober profiles: {e}")
            return list(ACCRETECH_BENCHES)

    def _active_bench(self) -> str:
        if self.active_system == "electroglas":
            try:
                return eg_profiles.active_name()
            except Exception:
                return ""
        try:
            return accretech_profiles.active_name() or ACCRETECH_BENCHES[0]
        except Exception:
            return ACCRETECH_BENCHES[0]

    def _refresh_bench_picker(self):
        names = self._bench_names()
        self._bench_picker.configure(values=names)
        active = self._active_bench()
        if self._bench_picker_var.get() != active:
            self._bench_picker_var.set(active)
        if self.active_system == "electroglas" and active:
            try:
                fitted = eg_profiles.fitted_keys(active)
                self._bench_lbl.config(text=f"{len(fitted)} instruments",
                                       foreground="#1d4ed8")
            except Exception:
                self._bench_lbl.config(text="", foreground="gray")
        elif self.active_system == "accretech" and active:
            try:
                inst = accretech_profiles.instruments(active)
                self._bench_lbl.config(text=f"{len(inst)} instruments",
                                       foreground="#1d4ed8")
            except Exception:
                self._bench_lbl.config(text="", foreground="gray")
        else:
            self._bench_lbl.config(text="", foreground="gray")
        # A single-entry list is not a choice; make that visible rather than
        # letting someone click at it expecting something to happen.
        self._bench_picker.configure(
            state="readonly" if len(names) > 1 else "disabled")

    def _on_bench_picker_selected(self):
        name = self._bench_picker_var.get()
        if self.active_system == "accretech":
            if name == accretech_profiles.active_name():
                return
            self.cmd_set_accretech_bench(name)
            self._refresh_bench_picker()
            return
        if self.active_system != "electroglas":
            return
        if name == eg_profiles.active_name():
            return
        self.cmd_set_eg_profile(name)
        self._refresh_bench_picker()
        # Keep the Instruments tab's own copy of this selector in step.
        panel = getattr(self._by_system["electroglas"]["ui"], "instruments_eg", None)
        for method in ("_refresh_bench_label", "_rebuild_addresses"):
            fn = getattr(panel, method, None)
            if fn:
                try:
                    fn()
                except Exception:
                    pass
        if panel is not None and hasattr(panel, "_bench_var"):
            panel._bench_var.set(name)

    def _on_ata_picker_selected(self):
        label = self._ata_picker_var.get()
        if not label:
            return
        name = self._ata_picker_label_to_name.get(label, label)
        folder = os.path.join(self.ui.working_dir_var.get(), name)
        self._do_load_ata_folder(folder)

    def update_statistics_visuals(self):
        # total_dies is the wafer-map SQUARE count (set from the loaded map/
        # recipe's touchdown list at run start) - on a Minor Moves recipe
        # each square is a SHOT, and dies_tested counts individual die
        # measurements, several per shot, so it can legitimately end up
        # bigger than total_dies well before the run is actually done.
        # Displaying total_dies - dies_tested there gives a negative
        # "Untested" count and a >100% progress fraction - not a real
        # problem with the run/count data itself, just this display doing
        # arithmetic against the wrong-shaped total. Grow the displayed
        # total to whatever's actually bigger so neither ever goes
        # negative/over 100%, without touching total_dies or how tested/
        # passed/failed are actually counted.
        display_total = max(self.total_dies, self.dies_tested)
        untested = display_total - self.dies_tested
        self.ui.lbl_stats_text.config(text=f"Pass: {self.dies_passed}  |  Fail: {self.dies_failed}\nUntested: {untested}")
        self.ui.lbl_progress.config(text=f"Progress: {self.dies_tested} / {display_total} tested")
        self.ui.lbl_results_large.config(text=f"Total Passed: {self.dies_passed}     |     Total Failed: {self.dies_failed}     |     Untested: {untested}")
        self.ui.draw_donut(self.ui.sidebar_canvas, 120, self.dies_passed, self.dies_failed, untested)
        if hasattr(self.ui, "results_canvas"):
            self.ui.draw_donut(self.ui.results_canvas, 300, self.dies_passed, self.dies_failed, untested)
 
    def on_exec_stats_change(self, tested, passed, failed, total):
        self.dies_tested  = tested
        self.dies_passed  = passed
        self.dies_failed  = failed
        self.total_dies   = total
        self.update_statistics_visuals()

    def _do_load_ata_folder(self, folder):
        folder_name = os.path.basename(folder)
        if self._any_run_in_progress():
            messagebox.showwarning(
                "Run In Progress",
                "A run (or armed cassette automation) is still going. Stop "
                "or finish it before loading a different ATA folder - "
                "switching now would pull the recipe/wafer map/results out "
                "from under it mid-run.")
            return
        self._show_switch_splash(f"Loading ATA folder '{folder_name}'…")
        try:
            n_dies = self.ui.load_ata_folder(folder)
        finally:
            self._dismiss_switch_splash()
        self.total_dies = n_dies
        self.dies_tested = self.dies_passed = self.dies_failed = 0
        self.ui.clear_results()
        self.update_statistics_visuals()
        self._ata_lbl.config(text=f"ATA: {folder_name}  ({n_dies} dies)",
                             foreground="#1d4ed8")
        self._refresh_ata_picker()
        self._ata_picker_var.set(self._ata_display_name(folder_name))
        self.ui.exec_panel.log(f"[SYSTEM] ATA folder '{folder_name}' loaded — {n_dies} dies found.")
        self.ui.exec_panel.set_wafer_map(self.ui.wafer_map, wafer_id=folder_name)
        self.ui.wafer_id_var.set(folder_name)
        self.check_system_ready()

    def cmd_import_map(self):
        initial = self.ui.working_dir_var.get() if hasattr(self, "ui") else None
        folder = filedialog.askdirectory(
            title="Select ATA Output Folder",
            initialdir=initial if initial and os.path.isdir(initial) else None)
        if not folder:
            return
        self._do_load_ata_folder(folder)

    def cmd_new_ata_folder(self):
        working_dir = self.ui.working_dir_var.get()
        if not working_dir:
            messagebox.showerror("No Working Directory",
                                 "Set a Working Directory first.")
            return
        if not os.path.isdir(working_dir):
            try:
                os.makedirs(working_dir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("Working Directory",
                                     f"Could not create working directory:\n{exc}")
                return
        name = simpledialog.askstring("New ATA Folder", "Folder name:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if not name.lower().endswith("ata"):
            name = f"{name}ATA"
        folder = os.path.join(working_dir, name)
        if os.path.exists(folder):
            messagebox.showerror("Already Exists", f"{folder}\nalready exists.")
            return
        try:
            os.makedirs(folder)
        except OSError as exc:
            messagebox.showerror("Could Not Create Folder", str(exc))
            return
        self.log(f"[SYSTEM] Created new ATA folder: {folder}")
        self._refresh_ata_picker()
        self._do_load_ata_folder(folder)

    def cmd_refresh_ata(self):
        folder = self.ui._ata_folder
        if not folder:
            self.log("[SYSTEM] No ATA folder loaded.")
            return
        if not os.path.isdir(folder):
            self.log(f"[SYSTEM] ATA folder no longer exists: {folder}")
            return
        self.log(f"[SYSTEM] Refreshing from ATA folder: {folder}")
        self._do_load_ata_folder(folder)

    def cmd_load_pads(self):
        folder = self.ui._ata_folder or filedialog.askdirectory(title="Select ATA Output Folder")
        if not folder:
            return
        n_pads = self.ui.load_pad_layout(folder)
        self.ui.exec_panel.load_recipe()
        self.ui.exec_panel.lbl_route.config(text="P1 (VDD)  -> SMU_HI\nP2 (GND)  -> SMU_LO\nP6 (OUT)  -> DMM_HI")
        folder_name = os.path.basename(folder)
        self.ui.exec_panel.log(f"[SYSTEM] Pad layout loaded from '{folder_name}' — {n_pads} pads.")

    def cmd_browse_export(self):
        selected_dir = filedialog.askdirectory(initialdir=self.ui.export_path_var.get(), title="Select Export Directory")
        if selected_dir:
            self.ui.export_path_var.set(selected_dir)

    def cmd_browse_working_dir(self):
        selected_dir = filedialog.askdirectory(
            initialdir=self.ui.working_dir_var.get(), title="Select Working Directory")
        if selected_dir:
            self.ui.working_dir_var.set(selected_dir)

    def cmd_pick_working_dir_preset(self, label: str):
        """The Working Directory dropdown's named presets (see
        workdir.PRESETS) - a plain label like "proberautomation", not a
        path, since the dropdown shows names, not full UNC paths."""
        path = workdir.PRESETS.get(label)
        if path:
            self.ui.working_dir_var.set(path)

    def cmd_set_default_working_dir(self):
        """Persist the CURRENT working directory as this PC's own default -
        stored next to the app itself (not inside GUI System), since GUI
        System now lives inside whichever working directory is picked and
        can't record which one to start with on its own. Applies from the
        next launch on; does not move anything already loaded this run."""
        path = self.ui.working_dir_var.get()
        if not path:
            return
        workdir.set_default_working_dir(path)
        self.log(f"[SETUP] '{os.path.basename(path)}' set as this computer's default "
                "working directory. Takes effect the next time the app is launched.")

    # kind=META rows use these (one row, none repeated per RESULT/DIE row -
    # see cmd_import_results_csv for the matching read side). kind=RESULT
    # and kind=DIE share the rest of the header, each only filling in its
    # own columns - same multi-kind-rows-in-one-CSV shape recipe_panel's
    # RECIPE/STEP/SITE rows already use elsewhere in this codebase.
    #
    # die/type/value lead the header (the three columns someone skimming
    # the file actually wants first) - everything else follows in its old
    # order. Purely a WRITE-side change: cmd_import_results_csv (and
    # anything else that reads this file) goes through csv.DictReader,
    # keyed by column NAME, so column order here has zero effect on
    # reading a file back - old exports with the old column order still
    # import fine.
    _RESULTS_CSV_FIELDS = [
        "die", "type", "value",
        "kind", "system", "ata_folder", "map_source", "probe_card", "recipe",
        "lot_id", "wafer_id", "total_dies", "dies_tested", "dies_passed",
        "dies_failed",
        "timestamp", "step", "mode", "unit",
        "die_id", "switch", "set_voltage", "voltage", "connection",
        "instrument", "row", "col", "status",
    ]

    def cmd_save_csv(self):
        """Writes <Lot>[_<Wafer>]_results.csv - self-contained enough for
        cmd_import_results_csv to rebuild the whole Results tab (wafer map,
        recipe, results table, pass/fail counts and colours) from this file
        alone, even after the GUI has been relaunched. One META row carries
        what would otherwise repeat identically down every line (system,
        ATA folder, active recipe/probe card, running totals); RESULT rows
        are the measurement history (same data "Save to CSV" always wrote,
        now with row/col/die_id kept instead of dropped); DIE rows are the
        per-die PASS/FAIL verdicts painted onto the wafer maps, which never
        used to be saved anywhere.
        """
        export_dir = self.ui.export_path_var.get()
        current_lot = self.ui.lot_id.get()
        if not os.path.exists(export_dir):
            self.ui.exec_panel.log("[ERROR] The selected export directory does not exist.")
            return None
        if not current_lot:
            self.ui.exec_panel.log("[ERROR] Please enter a valid Lot ID.")
            return None
        if not self.results_data:
            self.ui.exec_panel.log("[ERROR] No measurement results yet.")
            return None
        wafer_id = self.ui.wafer_id_var.get().strip()
        name_parts = [current_lot] + ([wafer_id] if wafer_id else []) + ["results"]
        filepath = os.path.join(export_dir, "_".join(name_parts) + ".csv")
        try:
            # Explicit utf-8: without it Python uses the Windows locale
            # encoding (cp1252 here), which wrote an em-dash (used as a die
            # placeholder) as a lone 0x97 byte - not valid UTF-8, so
            # cmd_import_results_csv's own explicit utf-8 read failed on
            # it and the whole import silently came back empty. Same class
            # of bug cmd_export_sql's CSV path already carries this fix
            # for - this path just never got it.
            with open(filepath, mode='w', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=self._RESULTS_CSV_FIELDS,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerow({
                    "kind": "META",
                    "system": self.active_system,
                    "ata_folder": getattr(self.ui, "_ata_folder", "") or "",
                    "map_source": getattr(self.ui, "_exec2_map_source_var", None).get()
                                  if hasattr(self.ui, "_exec2_map_source_var") else "",
                    "probe_card": self.ui.pin_wiring.get_active_card()
                                  if hasattr(self.ui, "pin_wiring") else "",
                    "recipe": getattr(self.ui, "_exec2_recipe_var", None).get()
                              if hasattr(self.ui, "_exec2_recipe_var") else "",
                    "lot_id": current_lot,
                    "wafer_id": wafer_id,
                    "total_dies": self.total_dies,
                    "dies_tested": self.dies_tested,
                    "dies_passed": self.dies_passed,
                    "dies_failed": self.dies_failed,
                })
                for row in self.results_data:
                    out = dict(row)
                    out["kind"] = "RESULT"
                    writer.writerow(out)
                for (row, col), status in self.die_status.items():
                    writer.writerow({"kind": "DIE", "row": row, "col": col,
                                     "status": status})

            self.ui.exec_panel.log(
                f"[RESULTS] Export Succesful {len(self.results_data)} result(s), "
                f"{len(self.die_status)} die verdict(s)")
            return filepath
        except Exception as e:
            self.ui.exec_panel.log(f"[ERROR] Failed to save CSV file: {e}")
            return None

    def cmd_import_results_csv(self):
        """The reverse of cmd_save_csv - reads one of its files and puts the
        GUI back the way it looked right after that run: ATA folder and
        wafer map source reloaded, probe card and recipe reselected, the
        results table and pass/fail totals rebuilt, and every die's
        PASS/FAIL colour repainted on both wafer maps.

        Best-effort on each piece independently (wrapped so one missing
        probe card or moved ATA folder does not abort the rest) - this is
        explicitly for a machine that may not have any of that state left,
        per the "all you have is the CSV" scenario this exists for.
        """
        path = filedialog.askopenfilename(
            title="Import Results CSV",
            filetypes=[("Results CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            self.ui.exec_panel.log(f"[ERROR] Could not read {os.path.basename(path)}: {e}")
            return
        meta = next((r for r in rows if r.get("kind") == "META"), None)
        if meta is None:
            messagebox.showerror(
                "Not a Results CSV",
                "No META row found - this doesn't look like a file "
                "'💾 Save to CSV' wrote (or it predates this Import feature).")
            return

        system = meta.get("system") or self.active_system
        if system in self._by_system and system != self.active_system:
            self.cmd_set_active_system(system)
        ui = self.ui

        folder = (meta.get("ata_folder") or "").strip()

        def _same_folder(a: str, b: str) -> bool:
            if not a or not b:
                return False
            return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

        # If this ATA folder is ALREADY the one loaded, the wafer map on
        # screen right now is already correct - do not touch it at all.
        # Otherwise, load it exactly the way picking it from the toolbar
        # dropdown would - load_ata_folder() draws its own correct map
        # (confirmed: this is the same call an ordinary folder load/app
        # relaunch makes, gaps between dies and all) with no help needed.
        # This used to ALSO force the map onto the CSV's own saved
        # map_source afterward - a second pass that kept coming out
        # looking wrong (dies packed with no gaps, overlay labels no
        # longer centered on their square) instead of identical. Not
        # worth chasing why a specific redraw sometimes disagrees with
        # itself when simply never doing one main this function does not
        # need sidesteps it entirely - a plain load is enough to get a
        # correct map, on the folder this file names or any other.
        if folder and _same_folder(folder, getattr(ui, "_ata_folder", "")):
            pass
        elif folder and os.path.isdir(folder):
            try:
                n_dies = ui.load_ata_folder(folder)
                self.total_dies = n_dies
                self._ata_lbl.config(text=f"ATA: {os.path.basename(folder)}  ({n_dies} dies)",
                                     foreground="#1d4ed8")
                self._refresh_ata_picker()
                self._ata_picker_var.set(self._ata_display_name(os.path.basename(folder)))
                ui.exec_panel.set_wafer_map(ui.wafer_map, wafer_id=os.path.basename(folder))
            except Exception as e:
                ui.exec_panel.log(f"[SETUP] Could not load ATA folder {os.path.basename(folder)!r}: {e}")
        elif folder:
            ui.exec_panel.log(
                f"[SETUP] ATA folder {os.path.basename(folder)!r} not found on this machine - "
                "continuing without it.")

        probe_card = (meta.get("probe_card") or "").strip()
        if probe_card and hasattr(ui, "pin_wiring"):
            try:
                ui.pin_wiring.switch_to_card(probe_card)
            except Exception as e:
                ui.exec_panel.log(f"[SETUP] Could not switch to probe card "
                                  f"{probe_card!r}: {e}")

        recipe = (meta.get("recipe") or "").strip()
        if recipe and hasattr(ui, "_exec2_load_recipe_by_name"):
            try:
                ui._exec2_load_recipe_by_name(recipe)
            except Exception as e:
                ui.exec_panel.log(f"[SETUP] Could not load recipe {recipe!r}: {e}")

        lot_id = (meta.get("lot_id") or "").strip()
        wafer_id = (meta.get("wafer_id") or "").strip()
        if lot_id:
            ui.lot_id.set(lot_id)
        if wafer_id:
            ui.wafer_id_var.set(wafer_id)

        results = []
        die_status = {}
        for r in rows:
            kind = r.get("kind")
            if kind == "RESULT":
                clean = {k: v for k, v in r.items()
                        if k not in ("kind", "system", "ata_folder", "map_source",
                                    "probe_card", "lot_id", "wafer_id",
                                    "total_dies", "dies_tested", "dies_passed",
                                    "dies_failed", "status") and v != ""}
                # csv.DictReader hands back every field as a string, but
                # _results_show_die (the Results tab's per-die click table)
                # compares "row"/"col" against the wafer map's own integer
                # row/col with == - a die's readings never matched after an
                # import, even though the die's PASS/FAIL colour did (that
                # path already casts to int explicitly below).
                for key in ("row", "col"):
                    if key in clean:
                        try:
                            clean[key] = int(clean[key])
                        except (TypeError, ValueError):
                            pass
                results.append(clean)
            elif kind == "DIE":
                try:
                    rc = (int(r["row"]), int(r["col"]))
                except (KeyError, ValueError, TypeError):
                    continue
                die_status[rc] = r.get("status") or "FAIL"

        self.results_data.clear()
        self.results_data.extend(results)
        self.die_status.clear()
        self.die_status.update(die_status)
        if hasattr(ui, "_results_tree"):
            ui._results_tree.delete(*ui._results_tree.get_children())
            for row in results:
                ui._results_tree.insert("", "end", values=(
                    row.get("timestamp", ""), row.get("recipe", ""),
                    row.get("die", ""), row.get("step", ""), row.get("type", ""),
                    row.get("value", ""), row.get("unit", "")))
        for (r, c), status in die_status.items():
            if hasattr(ui, "_exec2_update_die_color"):
                try:
                    ui._exec2_update_die_color(r, c, status == "PASS")
                except Exception:
                    pass

        def _int(v, default=0):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default
        self.total_dies = _int(meta.get("total_dies"), self.total_dies)
        self.dies_tested = _int(meta.get("dies_tested"), len(die_status))
        self.dies_passed = _int(meta.get("dies_passed"),
                                sum(1 for s in die_status.values() if s == "PASS"))
        self.dies_failed = _int(meta.get("dies_failed"),
                                sum(1 for s in die_status.values() if s == "FAIL"))
        self.update_statistics_visuals()
        self.check_system_ready()
        ui.exec_panel.log(
            f"[SETUP] Loaded {len(results)} result(s), {len(die_status)} die "
            f"verdict(s) — recipe '{recipe or '?'}', "
            f"probe card '{probe_card or '?'}'.")

    def cmd_export_sql(self):
        export_dir = self.ui.export_path_var.get()
        current_lot = self.ui.lot_id.get()
        if not os.path.exists(export_dir):
            self.ui.exec_panel.log("[ERROR] The selected export directory does not exist.")
            return None
        if not current_lot:
            self.ui.exec_panel.log("[ERROR] Please enter a valid Lot ID.")
            return None
        fmt = self.ui.get_selected_export_format()
        if not fmt:
            self.ui.exec_panel.log("[ERROR] No export format selected")
            return None
        wafer_id = self.ui.wafer_id_var.get().strip()
        fmt_type = fmt.get("type", "sql")
        # Export formats (unlike "Save as CSV", which dumps the whole
        # session's history) only ever cover the most recently started run —
        # re-running shouldn't silently pile old runs' rows into a new export.
        # A run that never actually started (e.g. the Full Die/Minor Moves
        # refusal - see _exec2_start_full_die) leaves this at zero rows,
        # which lands here and returns None with no file written - if a
        # caller (cassette_panel) expected a run to have happened for this
        # wafer and didn't get one, this is silently why nothing exported.
        last_run_results = self.ui.get_last_run_results()
        if not xfmt.has_data_for_format(fmt, last_run_results):
            if fmt_type == "csv":
                reason = "at least one current or resistance reading from a die touchdown"
            else:
                reason = ("readings that carry a device-ID string"
                         if fmt.get("requires_die_id", True) else "measurement results")
            self.ui.exec_panel.log(
                f"[ERROR] No matching results yet from the last run for '{fmt['name']}' — "
                f"this format needs {reason}.")
            return None
        ext = "csv" if fmt_type == "csv" else "sql"
        # "Lot+Wafer join" lets a format join them directly (e.g. "-w", to
        # match Cenfire's LabVIEW-generated "LOTID-wWAFERID" convention)
        # instead of always splitting them into two separate "_"-joined
        # name parts - every other format's saved JSON has no "wafer_join"
        # key at all, so it keeps exactly its previous two-part behavior.
        wafer_join = fmt.get("wafer_join") or "_"
        if wafer_join == "_" or not wafer_id:
            name_parts = [current_lot] + ([wafer_id] if wafer_id else [])
        else:
            name_parts = [f"{current_lot}{wafer_join}{wafer_id}"]
        # A table name is sometimes itself written with a trailing "_" (so
        # its own text reads correctly right up against the timestamp) -
        # stripped here so joining below can't ever double it up into "__".
        name_parts.append((fmt["table"] or "export").strip("_"))
        if fmt.get("append_recipe"):
            recipe_panel = getattr(self.ui, "recipe_panel", None)
            recipe_name = (recipe_panel.get_active_recipe() if recipe_panel else "") or ""
            if recipe_name and recipe_name != "(unsaved)":
                name_parts.append(recipe_name)
        if fmt.get("append_date"):
            now = dt.datetime.now()
            name_parts.append(now.strftime("%Y%m%d_%H%M%S") if fmt.get("append_time")
                              else now.strftime("%Y%m%d"))
        filepath = os.path.join(export_dir, "_".join(name_parts) + f".{ext}")

        ata_folder = getattr(self.ui, "_ata_folder", "") or ""
        try:
            if fmt_type == "csv":
                rows = xfmt.build_csv_rows(fmt, last_run_results, current_lot, wafer_id, ata_folder)
                fieldnames = [c["field"] for c in fmt["columns"]]
                # Explicit utf-8: without it Python uses the Windows locale
                # encoding (cp1252 here), which wrote an em-dash as a lone
                # 0x97 byte - not valid UTF-8, so the export would not reopen.
                with open(filepath, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                self.ui.exec_panel.log(
                    f"[RESULTS] Export Succesful {len(rows)} '{fmt['name']}' row(s)")
                return filepath
            else:
                statements = xfmt.build_insert_statements(
                    fmt, last_run_results, current_lot, wafer_id, ata_folder)
                # Explicit utf-8: without it Python uses the Windows locale
                # encoding (cp1252 here), which wrote an em-dash as a lone
                # 0x97 byte - not valid UTF-8, so the export would not reopen.
                with open(filepath, "w", newline="", encoding="utf-8") as f:
                    f.write("\n".join(statements) + "\n")
                self.ui.exec_panel.log(
                    f"[RESULTS] Export Succesful {len(statements)} '{fmt['name']}' row(s)")
                return filepath
        except Exception as e:
            self.ui.exec_panel.log(f"[ERROR] Failed to save {ext.upper()} file: {e}")
            return None

    def cmd_buzzer_clear(self):
        if self.active_system == "electroglas":
            self.log("[PROBER] Electroglas has no buzzer_clear (E + es is a "
                     "UF200R-only mnemonic) - nothing sent.")
            return
        drv = self.drivers.get("prober")
        if not (drv and drv.inst):
            self.log("[PROBER] Prober not connected.")
            return
        import threading
        def _run():
            try:
                self.log("[PROBER] >> E + es  (read error code, clear alarm)")
                code = drv.buzzer_clear()
                self.log(f"[PROBER] Cleared — error code: {code or '(none pending)'}")
            except Exception as e:
                self.log(f"[PROBER] Error: {e}")
        threading.Thread(target=_run, daemon=True).start()

    def cmd_abort(self):
        self.ui.exec_panel.abort()
        drv = self.drivers.get("prober")
        if drv and drv.inst and self.active_system != "accretech":
            self.log(f"[PROBER] {self.active_system.capitalize()} prober stop command "
                    "not yet implemented.")
        if drv and drv.inst and self.active_system == "accretech":
            import threading
            def _send_k():
                try:
                    drv.write("K")
                    self.log("[PROBER] K sent (emergency stop)")
                except Exception as e:
                    self.log(f"[PROBER] K error: {e}")
                try:
                    drv.send_es()
                    self.log("[PROBER] es sent (buzzer clear)")
                except Exception as e:
                    self.log(f"[PROBER] es error: {e}")
            threading.Thread(target=_send_k, daemon=True).start()

_SINGLE_INSTANCE_MUTEX_NAME = "Global\\AtomicaTesterSingleInstanceMutex"
_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9
_APP_WINDOW_TITLES = ("Electrical Prober",)


def _find_other_instance_window() -> int:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value in _APP_WINDOW_TITLES:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_proc, 0)
    return found[0] if found else 0


def _ensure_single_instance() -> bool:
    if sys.platform != "win32":
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
    if kernel32.GetLastError() != _ERROR_ALREADY_EXISTS:
        return True
    hwnd = _find_other_instance_window()
    if hwnd:
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, _SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    return False


if __name__ == "__main__":
    if _ensure_single_instance():
        app = AtomicaDashboard()
        app.mainloop()
