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

ELECTROGLAS_INSTRUMENT_NAMES = ["Electroglas 2001X", "Keithley 2400", "HP 3458A",
                                "HP E1326B (VXI)", "HP Switchbox 1", "HP Switchbox 2",
                                "HP Switchbox 3", "Agilent 6634B"]

ACCRETECH_BENCHES = ("probe08",)

_ACCRETECH_MODELS = {
    "prober":        {"AccretechUF200R": lambda key: AccretechUF200R(config_key=key)},
    "smu":           {"Keithley2636B": lambda key: Keithley2636B(config_key=key),
                      "Keithley2400":  lambda key: Keithley2400(config_key=key)},
    "dmm":           {"Keysight34461A": lambda key: Keysight34461A(config_key=key)},
    "switch_matrix": {"Keithley707B": lambda key: Keithley707B(config_key=key)},
    "wave_gen":      {"Keysight33512B": lambda key: Keysight33512B(config_key=key)},
}

_ALL_ACCRETECH_MODEL_FACTORIES = {}
for _slot_models in _ACCRETECH_MODELS.values():
    _ALL_ACCRETECH_MODEL_FACTORIES.update(_slot_models)

_ACCRETECH_FAMILY_KEYS = {"SMU": "smu", "DMM": "dmm", "WGEN": "wave_gen"}

_EG_FAMILY_KEYS = {"SMU": ("smu_eg",), "DMM": ("dmm_eg", "dmm_vxi_eg")}
_ACCRETECH_SLOT_INFO = {
    "prober":        ("UF200R Prober",      "prober"),
    "smu":           ("SMU",                "smu"),
    "dmm":           ("DMM",                "dmm"),
    "switch_matrix": ("SW_MATRIX",          "switch"),
    "wave_gen":      ("Wave Gen",           "wave_gen"),
}

ACCRETECH_INSTRUMENT_NAMES = [
    f"{display} ({model})"
    for key, (display, _drv_key) in _ACCRETECH_SLOT_INFO.items()
    for model in _ACCRETECH_MODELS.get(key, {})
]

ACCRETECH_REQUIRED_DRIVERS = ("prober", "smu", "dmm", "switch", "wave_gen")
ELECTROGLAS_REQUIRED_DRIVERS = ("prober", "dmm", "relay1", "relay2", "relay3")

SHOW_SPLASH_SCREEN = True


class AtomicaDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Electrical Prober")
        self.geometry("1400x800")
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
            "accretech":   {"drivers": {}, "results": [], "ui": None,
                            "total": 0, "tested": 0, "passed": 0, "failed": 0,
                            "die_status": {}},
            "electroglas": {"drivers": {}, "results": [], "ui": None,
                            "total": 0, "tested": 0, "passed": 0, "failed": 0,
                            "die_status": {}},
        }
        self._startup_done = False
        self._connected_systems = set()
        self._sys_ready_prev = None
        self._prober_ready = None
        self._prober_stb = None
        self.working_dir_var = tk.StringVar(value=workdir.get_current_working_dir())
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
        self._displayed_widget = self.instrument_panel

        self.instrument_panel_eg = MainLayout(
            parent=self._main_pane, controller=self,
            instrument_names=ELECTROGLAS_INSTRUMENT_NAMES,
            init_hardware_fn=self.init_hardware_eg, system="electroglas")
        self._by_system["electroglas"]["ui"] = self.instrument_panel_eg

        self.gui_mode = "normal"
        self._nanoz_mode_ui = None

        self._build_bottom_routing()
        if getattr(self, "_pending_setup_log", None):
            self.log(self._pending_setup_log)
            self._pending_setup_log = None
        self.log(f"[SYSTEM] Working directory: {workdir.get_current_working_dir()}")
        self._autoload_default_ata_folders()
        self._apply_default_prober()
        self._apply_default_gui_mode()
        self.after(500, self._startup_sweep)
        self.update_statistics_visuals()
        self.check_system_ready()
        self.after(2000, self._system_ready_loop)
        self.after(1500, self._poll_prober_ready)

    def _check_machine_config_folder(self):
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
                  "directory is C:\\automationproject or default defined "
                  "in json file.")
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
                ui.wafer_id_var.set(folder_name)
            self.log(
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
            self._nanoz_mode_ui.refresh_for_system()
        else:
            self._main_pane.forget(old_widget)
            if self._main_pane.panes():
                self._main_pane.insert(0, self.ui, weight=1)
            else:
                self._main_pane.add(self.ui, weight=1)
            self._displayed_widget = self.ui
        self._style_system_toggle()
        if not self.ui._ata_folder:
            default_folder = app_settings.get_default_ata_folder()
            if default_folder and os.path.isdir(default_folder):
                self._do_load_ata_folder(default_folder)
            elif carry_over_folder:
                self._do_load_ata_folder(carry_over_folder)
        if self.ui._ata_folder:
            folder_name = os.path.basename(self.ui._ata_folder)
            self._ata_lbl.config(text=f"ATA: {folder_name}", foreground="#1d4ed8")
            self._refresh_ata_picker()
            self._ata_picker_var.set(self._ata_display_name(folder_name))
        else:
            self._ata_lbl.config(text="No ATA loaded", foreground="gray")
            self._ata_picker_var.set("")
        self._refresh_bench_picker()
        self._refresh_routing_button()
        self._refresh_buzzer_clear_button()
        self.update_statistics_visuals()
        self.check_system_ready()
        self.log(f"[SYSTEM] Switched active system to {system.capitalize()} "
                 f"— prober {self._active_bench()}.")
        if system == "accretech" and system in self._connected_systems \
                and hasattr(self.ui, "_exec_get_xy"):
            self.ui._exec_get_xy()
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
        mode = app_settings.get_default_gui_mode()
        if mode and mode != self.gui_mode:
            self.cmd_set_gui_mode(mode)

    def notify_nanoz_ata_folder_loaded(self, folder_path: str):
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
        if getattr(ui, "_exec_running", False):
            return True
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
        nanoz_mode_ui = getattr(self, "_nanoz_mode_ui", None)
        if nanoz_mode_ui is not None and nanoz_mode_ui.any_running():
            return True
        return False

    def _on_close_request(self):
        if self._any_run_in_progress():
            if not messagebox.askyesno(
                    "Run In Progress",
                    "A run (or armed cassette automation) is still going. "
                    "Closing now will kill it mid-run without a clean stop.\n\n"
                    "Close anyway?", icon="warning", default="no"):
                return
        self._release_all_to_local_on_exit()
        self.destroy()

    def _release_all_to_local_on_exit(self):
        released = []
        for system, state in self._by_system.items():
            for key, drv in (state.get("drivers") or {}).items():
                if not drv or not getattr(drv, "inst", None):
                    continue
                try:
                    if drv.go_to_local():
                        released.append(f"{system}:{key}")
                except Exception:
                    pass
        if released:
            try:
                self.log("[SYSTEM] Released to local on exit: "
                         + ", ".join(released))
            except Exception:
                pass

    def _set_prober_ready(self, stb):
        self._prober_stb = stb
        self._prober_ready = (stb == 65) if stb is not None else None
        self.check_system_ready()

    def _build_splash_screen(self):
        if not SHOW_SPLASH_SCREEN:
            return
        self.withdraw()
        self._splash = self._make_splash_toplevel("Starting up…")

    def _make_splash_toplevel(self, message):
        splash = tk.Toplevel(self)
        splash.overrideredirect(True)
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
        system, bench = app_settings.get_default_prober()
        if not system:
            return
        self.log(f"[SYSTEM] Default prober: {system} / {bench}")
        self.apply_prober(system, bench)

    def _refresh_buzzer_clear_button(self):
        btn = getattr(self, "_buzzer_clear_btn", None)
        if btn is None:
            return
        if self.active_system == "electroglas":
            btn.pack_forget()
        else:
            btn.pack(side="left", padx=(0, 6), pady=2, after=self._abort_btn)

    def _refresh_routing_button(self):
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
        at_bottom = txt.yview()[1] >= 0.999
        txt.configure(state="normal")
        txt.insert(tk.END, message + "\n")
        if at_bottom:
            txt.see(tk.END)
        txt.configure(state="disabled")

    def _set_status(self, ui, name, mark, colour):
        lbl = ui.status_labels.get(name)
        if lbl is None:
            self.log(f"[SYSTEM] {name} has no status row — add it to the "
                     f"instrument name list for this system")
            return
        lbl.config(text=f"{mark} {name}", foreground=colour)

    def _connect_instruments(self, ui, drivers, connections):
        ui.set_visible_instruments([name for name, _key, _drv in connections])
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
        ui.set_visible_instruments([name for name, _key, _factory in connections])
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
            self.update_idletasks()
        if "prober" in drivers and hasattr(ui, "_exec_refresh_die_size"):
            ui._exec_refresh_die_size()

    def _startup_sweep(self):
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
        for key in fitted:
            entry = profile_instruments.get(key) or {}
            model = entry.get("model") or accretech_profiles.DEFAULT_MODEL.get(
                key, accretech_profiles.GENERIC_MODEL)
            slot_factory = (_ACCRETECH_MODELS.get(key, {}).get(model)
                            or _ALL_ACCRETECH_MODEL_FACTORIES.get(model))
            if key in _ACCRETECH_SLOT_INFO:
                display, drv_key = _ACCRETECH_SLOT_INFO[key]
            else:
                display, drv_key = entry.get("name") or key, key
            if slot_factory is None:
                slot_factory = GPIBInstrument
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
        switch_drv = self._by_system["accretech"]["drivers"].get("switch")
        if switch_drv and switch_drv.inst:
            try:
                switch_drv.open_all()
                self.log("[SYSTEM] SW_MATRIX: opened all crosspoints.")
            except Exception as e:
                self.log(f"[SYSTEM] SW_MATRIX open-all failed: {e}")
        if "prober" in self._by_system["accretech"]["drivers"] \
                and hasattr(acc_ui, "_exec_get_xy"):
            acc_ui._exec_get_xy()

    _EG_DRIVERS = {
        "prober_eg":     ("Electroglas 2001X",  "prober",  Electroglas2001X),
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
        try:
            changed = eg_profiles.set_active(name)
        except Exception as e:
            self.log(f"[SYSTEM] Could not switch to {name!r}: {e}")
            return
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
        ui = self._by_system["electroglas"]["ui"]
        panel = getattr(ui, "recipe_panel", None)
        refresh = getattr(panel, "refresh_bench_instruments", None)
        if refresh:
            try:
                refresh()
            except Exception as e:
                self.log(f"[SYSTEM] Recipe tab instrument refresh failed: {e}")
        active_recipe = ""
        try:
            active_recipe = panel.get_active_recipe() if panel else ""
        except Exception:
            pass
        if hasattr(ui, "_exec_recipe_var"):
            try:
                if active_recipe:
                    ui._exec_load_recipe_by_name(active_recipe)
                else:
                    ui._exec_recipe_var.set("")
                    ui._exec_steps = []
                    if hasattr(ui, "_exec_steps_tree"):
                        ui._exec_steps_tree.delete(*ui._exec_steps_tree.get_children())
                    if hasattr(ui, "_exec_steps_var"):
                        ui._exec_steps_var.set(f"No recipe for bench '{name}' yet")
            except Exception as e:
                self.log(f"[SYSTEM] Run tab recipe refresh failed: {e}")
        if self._startup_done:
            self._show_switch_splash(f"Connecting to {eg_profiles.label(name)}…")
            try:
                self.init_hardware_eg()
            finally:
                self._dismiss_switch_splash()

    def cmd_set_accretech_bench(self, name: str):
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
        self.refresh_probe_routing_panels()
        acc_ui = self._by_system["accretech"]["ui"]
        for attr in ("setup_panel", "switch_settings"):
            panel = getattr(acc_ui, attr, None)
            refresh = getattr(panel, "refresh_active_bench", None)
            if refresh:
                try:
                    refresh()
                except Exception as e:
                    self.log(f"[SYSTEM] {attr} active-bench refresh failed: {e}")
        panel = getattr(acc_ui, "recipe_panel", None)
        refresh = getattr(panel, "refresh_bench_instruments", None)
        if refresh:
            try:
                refresh()
            except Exception as e:
                self.log(f"[SYSTEM] Recipe tab instrument refresh failed: {e}")
        active_recipe = ""
        try:
            active_recipe = panel.get_active_recipe() if panel else ""
        except Exception:
            pass
        if hasattr(acc_ui, "_exec_recipe_var"):
            try:
                if active_recipe:
                    acc_ui._exec_load_recipe_by_name(active_recipe)
                else:
                    acc_ui._exec_recipe_var.set("")
                    acc_ui._exec_steps = []
                    if hasattr(acc_ui, "_exec_steps_tree"):
                        acc_ui._exec_steps_tree.delete(*acc_ui._exec_steps_tree.get_children())
                    if hasattr(acc_ui, "_exec_steps_var"):
                        acc_ui._exec_steps_var.set(f"No recipe for bench '{name}' yet")
            except Exception as e:
                self.log(f"[SYSTEM] Run tab recipe refresh failed: {e}")
        if self._startup_done:
            self._show_switch_splash(f"Connecting to {accretech_profiles.label(name)}…")
            try:
                self.init_hardware()
            finally:
                self._dismiss_switch_splash()

    def refresh_probe_routing_panels(self):
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
        try:
            fitted = accretech_profiles.fitted_keys(bench)
        except Exception:
            return ACCRETECH_REQUIRED_DRIVERS
        if not fitted:
            return ACCRETECH_REQUIRED_DRIVERS
        return tuple(_ACCRETECH_SLOT_INFO.get(k, (None, k))[1] for k in fitted)

    def check_system_ready(self):
        missing = []
        exec_wm = getattr(self.ui, "_exec_wafer_map", None)
        if not (exec_wm and exec_wm._last_dies):
            missing.append("wafer map")
        if not getattr(self.ui, "_exec_steps", None):
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
                self.log("[SYSTEM] System ready.")
            elif self._sys_ready_prev is not None:
                self.log(f"[SYSTEM] No longer ready — missing: {', '.join(missing)}")
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

        self._ata_lbl = ttk.Label(toolbar, text="No ATA loaded", foreground="gray",
                                  font=("Segoe UI", 9))

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
        if name and name.lower().endswith("ata"):
            return name[:-3] or name
        return name

    def _refresh_ata_picker(self):
        names = self._find_ata_folders()
        self._ata_picker_label_to_name = {self._ata_display_name(n): n for n in names}
        self._ata_picker.configure(values=list(self._ata_picker_label_to_name.keys()))


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
        self.log(f"[SYSTEM] ATA folder '{folder_name}' loaded — {n_dies} dies found.")
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
        folder_name = os.path.basename(folder)
        self.log(f"[SYSTEM] Pad layout loaded from '{folder_name}' — {n_pads} pads.")

    def cmd_browse_export(self):
        selected_dir = filedialog.askdirectory(initialdir=self.ui.export_path_var.get(), title="Select Export Directory")
        if selected_dir:
            self.ui.export_path_var.set(selected_dir)

    def cmd_browse_working_dir(self):
        selected_dir = filedialog.askdirectory(
            initialdir=self.ui.working_dir_var.get(), title="Select Working Directory")
        if selected_dir:
            self.ui.working_dir_var.set(selected_dir)
            self._refresh_after_working_dir_change()

    def cmd_pick_working_dir_preset(self, label: str):
        path = workdir.PRESETS.get(label)
        if path:
            self.ui.working_dir_var.set(path)
            self._refresh_after_working_dir_change()

    def _refresh_after_working_dir_change(self):
        self.log(f"[SYSTEM] Working directory switched to: "
                f"{workdir.get_current_working_dir()}")
        self._autoload_default_ata_folders()

    def cmd_set_default_working_dir(self):
        path = self.ui.working_dir_var.get()
        if not path:
            return
        workdir.set_default_working_dir(path)
        self.log(f"[SETUP] '{os.path.basename(path)}' set as this computer's default "
                "working directory (also applies to future launches).")
        self._refresh_after_working_dir_change()

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
        export_dir = self.ui.export_path_var.get()
        current_lot = self.ui.lot_id.get()
        if not os.path.exists(export_dir):
            self.log("[ERROR] The selected export directory does not exist.")
            return None
        if not current_lot:
            self.log("[ERROR] Please enter a valid Lot ID.")
            return None
        if not self.results_data:
            self.log("[ERROR] No measurement results yet.")
            return None
        wafer_id = self.ui.wafer_id_var.get().strip()
        name_parts = [current_lot] + ([wafer_id] if wafer_id else []) + ["results"]
        filepath = os.path.join(export_dir, "_".join(name_parts) + ".csv")
        try:
            with open(filepath, mode='w', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=self._RESULTS_CSV_FIELDS,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerow({
                    "kind": "META",
                    "system": self.active_system,
                    "ata_folder": getattr(self.ui, "_ata_folder", "") or "",
                    "map_source": getattr(self.ui, "_exec_map_source_var", None).get()
                                  if hasattr(self.ui, "_exec_map_source_var") else "",
                    "probe_card": self.ui.pin_wiring.get_active_card()
                                  if hasattr(self.ui, "pin_wiring") else "",
                    "recipe": getattr(self.ui, "_exec_recipe_var", None).get()
                              if hasattr(self.ui, "_exec_recipe_var") else "",
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

            self.log(
                f"[RESULTS] Export Succesful {len(self.results_data)} result(s), "
                f"{len(self.die_status)} die verdict(s)")
            return filepath
        except Exception as e:
            self.log(f"[ERROR] Failed to save CSV file: {e}")
            return None

    def cmd_import_results_csv(self):
        path = filedialog.askopenfilename(
            title="Import Results CSV",
            filetypes=[("Results CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            self.log(f"[ERROR] Could not read {os.path.basename(path)}: {e}")
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
            except Exception as e:
                self.log(f"[SETUP] Could not load ATA folder {os.path.basename(folder)!r}: {e}")
        elif folder:
            self.log(
                f"[SETUP] ATA folder {os.path.basename(folder)!r} not found on this machine - "
                "continuing without it.")

        probe_card = (meta.get("probe_card") or "").strip()
        if probe_card and hasattr(ui, "pin_wiring"):
            try:
                ui.pin_wiring.switch_to_card(probe_card)
            except Exception as e:
                self.log(f"[SETUP] Could not switch to probe card "
                                  f"{probe_card!r}: {e}")

        recipe = (meta.get("recipe") or "").strip()
        if recipe and hasattr(ui, "_exec_load_recipe_by_name"):
            try:
                ui._exec_load_recipe_by_name(recipe)
            except Exception as e:
                self.log(f"[SETUP] Could not load recipe {recipe!r}: {e}")

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
            if hasattr(ui, "_exec_update_die_color"):
                try:
                    ui._exec_update_die_color(r, c, status == "PASS")
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
        self.log(
            f"[SETUP] Loaded {len(results)} result(s), {len(die_status)} die "
            f"verdict(s) — recipe '{recipe or '?'}', "
            f"probe card '{probe_card or '?'}'.")

    def cmd_export_sql(self):
        export_dir = self.ui.export_path_var.get()
        current_lot = self.ui.lot_id.get()
        if not os.path.exists(export_dir):
            self.log("[ERROR] The selected export directory does not exist.")
            return None
        if not current_lot:
            self.log("[ERROR] Please enter a valid Lot ID.")
            return None
        fmt = self.ui.get_selected_export_format()
        if not fmt:
            self.log("[ERROR] No export format selected")
            return None
        wafer_id = self.ui.wafer_id_var.get().strip()
        fmt_type = fmt.get("type", "sql")
        last_run_results = self.ui.get_last_run_results()
        if not xfmt.has_data_for_format(fmt, last_run_results):
            if fmt_type == "csv":
                reason = "at least one current or resistance reading from a die touchdown"
            else:
                reason = ("readings that carry a device-ID string"
                         if fmt.get("requires_die_id", True) else "measurement results")
            self.log(
                f"[ERROR] No matching results yet from the last run for '{fmt['name']}' — "
                f"this format needs {reason}.")
            return None
        ext = "csv" if fmt_type == "csv" else "sql"
        wafer_join = fmt.get("wafer_join") or "_"
        if wafer_join == "_" or not wafer_id:
            name_parts = [current_lot] + ([wafer_id] if wafer_id else [])
        else:
            name_parts = [f"{current_lot}{wafer_join}{wafer_id}"]
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
                with open(filepath, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                self.log(
                    f"[RESULTS] Export Succesful {len(rows)} '{fmt['name']}' row(s)")
                return filepath
            else:
                statements = xfmt.build_insert_statements(
                    fmt, last_run_results, current_lot, wafer_id, ata_folder)
                with open(filepath, "w", newline="", encoding="utf-8") as f:
                    f.write("\n".join(statements) + "\n")
                self.log(
                    f"[RESULTS] Export Succesful {len(statements)} '{fmt['name']}' row(s)")
                return filepath
        except Exception as e:
            self.log(f"[ERROR] Failed to save {ext.upper()} file: {e}")
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
        self.log("[PROBER] Run Stopped.")
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
