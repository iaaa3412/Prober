"""Operator GUI - a simplified front end for starting and saving runs.

Launch:  python OperatorGUI/operator_app.py

Reuses EngineerGUI's dashboard and run engine without modifying them. The
prober/system comes from this PC's default in the GUI System folder, and the
ATA folder, recipe, probe card, wafer map and export settings start from the
defaults an engineer set in the Engineer GUI. The operator gets only the Run,
Results and Cassette tabs, and can change the ATA folder, recipe, probe card
and export folder (from its dropdown) - not the prober or wafer map.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_ENGINEER_DIR = os.path.join(_ROOT, "EngineerGUI")
for _path in (_ROOT, _ENGINEER_DIR, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)
_ASSET_DIR = getattr(sys, "_MEIPASS", _ENGINEER_DIR)

import tkinter as tk
from tkinter import ttk

import app as engineer_app
import app_settings
import workdir
from instruments import accretech_profiles

from operator_layout import OperatorMainLayout


class OperatorDashboard(engineer_app.AtomicaDashboard):

    def __init__(self):
        tk.Tk.__init__(self)
        self.title("Electrical Prober")
        self.geometry("1400x800")
        try:
            icon_path = os.path.join(_ASSET_DIR, "app_icon.png")
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

        self.instrument_panel = OperatorMainLayout(
            parent=self._main_pane, controller=self,
            instrument_names=engineer_app.ACCRETECH_INSTRUMENT_NAMES,
            init_hardware_fn=self.init_hardware, system="accretech")
        self._by_system["accretech"]["ui"] = self.instrument_panel
        self._main_pane.add(self.instrument_panel, weight=1)
        self._displayed_widget = self.instrument_panel

        self.instrument_panel_eg = OperatorMainLayout(
            parent=self._main_pane, controller=self,
            instrument_names=engineer_app.ELECTROGLAS_INSTRUMENT_NAMES,
            init_hardware_fn=self.init_hardware_eg, system="electroglas")
        self._by_system["electroglas"]["ui"] = self.instrument_panel_eg

        self.gui_mode = "normal"
        self._nanoz_mode_ui = None
        for layout in (self.instrument_panel, self.instrument_panel_eg):
            layout._exec_wafer_map_var.trace_add("write", self._refresh_defaults_notice)
        self._place_controls()

        self._build_bottom_routing()
        if getattr(self, "_pending_setup_log", None):
            self.log(self._pending_setup_log)
            self._pending_setup_log = None
        self.log(f"[SYSTEM] Operator GUI - working directory: "
                 f"{workdir.get_current_working_dir()}")
        self._autoload_default_ata_folders()
        self._apply_default_prober()
        self._apply_default_gui_mode()
        self._refresh_defaults_notice()
        self.after(500, self._startup_sweep)
        self.update_statistics_visuals()
        self.check_system_ready()
        self.after(2000, self._system_ready_loop)
        self.after(1500, self._poll_prober_ready)

    def _build_brand_header(self):
        hdr = tk.Frame(self, bg="#374558", height=55)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        for filename, target_h, pad in (("logo2.jpg", 44, (10, 4)),
                                        ("logo_otto.jpg", 36, (0, 6))):
            logo_path = os.path.join(_ASSET_DIR, filename)
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

        badge_frame = tk.Frame(hdr, bg="#374558")
        badge_frame.pack(side="right", padx=12, pady=10)
        tk.Label(badge_frame, text="OPERATOR", bg="#f0a020", fg="#1f2937",
                 font=("Arial", 9, "bold"), padx=10, pady=3).pack(side="left")
        self._system_badge = tk.Label(badge_frame, text="", bg="#4b5768",
                                      fg="#d1d5db", font=("Arial", 9, "bold"),
                                      padx=10, pady=3)
        self._system_badge.pack(side="left")
        self._system_buttons = {}
        self._style_system_toggle()

    def _style_system_toggle(self):
        badge = getattr(self, "_system_badge", None)
        if badge is not None:
            badge.config(text=self.active_system.capitalize())

    def create_toolbar(self):
        box = self._controls_box = ttk.Frame(self)
        self._ata_picker_var = tk.StringVar()
        self._ata_picker_label_to_name = {}
        self._ata_picker = ttk.Combobox(
            box, textvariable=self._ata_picker_var, state="readonly",
            width=12, postcommand=self._refresh_ata_picker)
        self._ata_picker.pack(fill="x", pady=(0, 4))
        self._ata_picker.bind("<<ComboboxSelected>>",
                              lambda _e: self._on_ata_picker_selected())
        self._ata_lbl = ttk.Label(box, text="No ATA loaded", foreground="gray",
                                  font=("Segoe UI", 9))

        self._bench_lbl = ttk.Label(box, text="", foreground="gray",
                                    font=("Segoe UI", 9))
        self._bench_lbl.pack(anchor="w")
        self._bench_picker_var = tk.StringVar()
        self._refresh_bench_picker()
        self._defaults_notice_lbl = ttk.Label(box, text="", foreground="#b91c1c",
                                              font=("Segoe UI", 9, "bold"),
                                              wraplength=150, justify="left")
        box.bind("<Configure>", lambda e: self._defaults_notice_lbl.config(
            wraplength=max(e.width - 4, 100)))
        self.after(200, self._refresh_ata_picker)

    def _place_controls(self):
        self._controls_box.pack_forget()
        self._controls_box.pack(in_=self.ui._operator_controls_slot, fill="x")
        self._controls_box.lift()

    def cmd_set_active_system(self, system):
        super().cmd_set_active_system(system)
        self._place_controls()
        self._refresh_defaults_notice()


    def _autoload_default_ata_folders(self):
        kept = {system: entry["ui"].wafer_id_var.get()
                for system, entry in self._by_system.items()}
        super()._autoload_default_ata_folders()
        for system, wafer_id in kept.items():
            self._by_system[system]["ui"].wafer_id_var.set(wafer_id)

    def _do_load_ata_folder(self, folder):
        wafer_id = self.ui.wafer_id_var.get()
        super()._do_load_ata_folder(folder)
        self.ui.wafer_id_var.set(wafer_id)
        self._refresh_defaults_notice()

    def _refresh_bench_picker(self):
        active = self._active_bench()
        self._bench_picker_var.set(active)
        self._bench_lbl.config(text=f"Prober: {active}" if active else "Prober: —",
                               foreground="#1d4ed8" if active else "gray")

    def _apply_default_gui_mode(self):
        if app_settings.get_default_gui_mode() == "nanoz":
            self.log("[SYSTEM] This PC defaults to NanoZ mode, which the Operator "
                     "GUI doesn't include yet - showing Run/Results/Cassette.")

    def _refresh_defaults_notice(self, *_):
        ui = self.ui
        if not ui._ata_folder:
            msg = "No ATA folder loaded - pick one from the ATA Folder list."
        elif not ui._exec_wafer_map_var.get():
            msg = ("This ATA folder has no wafer map - ask an engineer to set "
                   "one up in the Engineer GUI.")
        else:
            msg = ""
        if msg:
            self._defaults_notice_lbl.config(text=f"⚠ {msg}")
            self._defaults_notice_lbl.pack(anchor="w", pady=(4, 0))
        else:
            self._defaults_notice_lbl.pack_forget()


def main():
    if engineer_app._ensure_single_instance():
        OperatorDashboard().mainloop()


if __name__ == "__main__":
    main()
