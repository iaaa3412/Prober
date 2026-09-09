import datetime
import os
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk

import data_stub
from layout import ModeAwareMixin, VB_FIELD_BG, VB_FONT, VB_FONT_BOLD, VB_FORM_BG, client_size, make_frame, place

MSG_TITLE = "IMT LampElectrical Probing"


class RuntimeForm(tk.Toplevel, ModeAwareMixin):

    def __init__(self, master, recipe_name, wafer_id, operator_id, process_step, mode):
        tk.Toplevel.__init__(self, master)
        ModeAwareMixin.__init__(self)

        self.recipe_name = recipe_name
        self.wafer_id = wafer_id
        self.operator_id = operator_id
        self.process_step = process_step
        self.mode_var = tk.StringVar(value=mode)

        self.chk_flush_var = tk.BooleanVar(value=False)
        self.chk_suppress_debug_var = tk.BooleanVar(value=False)
        self.chk_db_save_var = tk.BooleanVar(value=False)
        self.chk_save_text_var = tk.BooleanVar(value=False)
        self.jump_var = tk.StringVar(value="JumpFromAlign")

        hpib = data_stub.build_hpib_instruments()
        self.hpib_2001x = hpib["2001X"]
        self.hpib_relay1 = hpib["Relay1"]
        self.hpib_keithley2400 = hpib["Keithley2400"]
        self._hpib_by_name = hpib

        self.title("LampElectrical Run Time")
        w, h = client_size(11625, 9795)
        self.geometry(f"{w}x{h}")
        self.configure(bg=VB_FORM_BG)

        self._run_in_progress = False
        self._align_confirmed = False

        self._build_controls()
        self.apply_mode(self.mode_var.get())
        self._form_load()


    def _build_controls(self):
        self.lbl_info = tk.Label(
            self, text="", bg=VB_FORM_BG, font=VB_FONT, anchor="nw", justify="left"
        )
        place(self.lbl_info, 120, 240, 2655, 615)

        self.cmd_go = tk.Button(self, text="GO", font=VB_FONT_BOLD, command=self.cmd_go_click)
        place(self.cmd_go, 1800, 1800, 1500, 615)

        chk_flush = tk.Checkbutton(
            self,
            text="Cache the data for later 'Flush'",
            variable=self.chk_flush_var,
            bg=VB_FORM_BG,
            font=VB_FONT,
            anchor="w",
            justify="left",
            wraplength=85,
        )
        place(chk_flush, 120, 1800, 1575, 615)

        self.cmd_revert = tk.Button(
            self, text="Change to Engineering Mode", font=VB_FONT, wraplength=95, command=self.cmd_revert_click
        )
        place(self.cmd_revert, 1800, 3240, 1500, 615)

        chk_suppress = tk.Checkbutton(
            self,
            text="Suppress Debug Messages",
            variable=self.chk_suppress_debug_var,
            bg=VB_FORM_BG,
            font=VB_FONT,
            anchor="w",
        )
        place(chk_suppress, 1800, 4080, 2295, 255)

        self._build_frame1_engineering_mode()
        self._build_frame2_switch_powering()

        line1 = tk.Frame(self, bg="red", height=3)
        self._place_hline(line1, x1=0, y=7560, x2=11040, bordercolor="red", borderwidth=3)

        self.txt_data = tk.Text(self, bg=VB_FIELD_BG, font=("Courier New", 9), state="disabled", wrap="word")
        place(self.txt_data, 0, 7680, 11055, 1815)

    def _place_hline(self, frame, x1, y, x2, bordercolor, borderwidth):
        from layout import tw

        frame.configure(bg=bordercolor, height=borderwidth)
        frame.place(x=tw(x1), y=tw(y), width=tw(x2 - x1), height=borderwidth)

    def _build_frame1_engineering_mode(self):
        frame1 = make_frame(self, "Engineering Mode", 4080, 120, 6975, 3495, font=VB_FONT)
        self.register_view_tag(frame1, "Manual", 4080, 120, 6975, 3495)

        opt_jump = tk.Radiobutton(
            frame1,
            text="Make the jump from the align site to the chosen die.",
            variable=self.jump_var,
            value="JumpFromAlign",
            bg=VB_FORM_BG,
            font=VB_FONT,
            anchor="w",
        )
        place(opt_jump, 480, 480, 4335, 255)

        opt_no_jump = tk.Radiobutton(
            frame1,
            text="Prober stage is already at the correct die. No initial jump.",
            variable=self.jump_var,
            value="NoJump",
            bg=VB_FORM_BG,
            font=VB_FONT,
            anchor="w",
            justify="left",
            wraplength=290,
        )
        place(opt_no_jump, 480, 840, 4455, 375)

        line2 = tk.Frame(frame1, bg="black", height=1)
        self._place_hline(line2, x1=240, y=1440, x2=6600, bordercolor="black", borderwidth=1)

        lbl_filename = tk.Label(frame1, text="File Name", bg=VB_FORM_BG, font=VB_FONT, anchor="w")
        place(lbl_filename, 360, 2400, 855, 255)

        self.txt_file_name = tk.Entry(frame1, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_file_name, 1320, 2400, 5295, 285)

        chk_save_text = tk.Checkbutton(
            frame1,
            text="Save a text copy of session measurements in file.",
            variable=self.chk_save_text_var,
            bg=VB_FORM_BG,
            font=VB_FONT,
            anchor="w",
        )
        place(chk_save_text, 480, 2040, 4215, 255)

        self.cmd_view = tk.Button(frame1, text="View", font=VB_FONT, command=self.cmd_view_click)
        place(self.cmd_view, 4680, 2040, 1575, 255)

        chk_db_save = tk.Checkbutton(
            frame1,
            text="Save the session measurements to the database.",
            variable=self.chk_db_save_var,
            bg=VB_FORM_BG,
            font=VB_FONT,
            anchor="w",
            state="disabled",
        )
        place(chk_db_save, 480, 1680, 3855, 255)

        lbl_wafer = tk.Label(frame1, text="Wafer ID", bg=VB_FORM_BG, font=VB_FONT, anchor="w")
        place(lbl_wafer, 360, 3000, 855, 255)

        self.txt_wafer_id_frame1 = tk.Entry(frame1, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_wafer_id_frame1, 1320, 3000, 1215, 285)

        self.combo_dies = ttk.Combobox(frame1, font=VB_FONT, state="readonly")
        place(self.combo_dies, 4800, 480, 2055, 315)

    def _build_frame2_switch_powering(self):
        frame2 = make_frame(self, "Switch Powering", 4080, 4200, 5415, 3135, font=VB_FONT)
        self.register_view_tag(frame2, "Manual", 4080, 4200, 5415, 3135)

        lbl1 = tk.Label(frame2, text="Voltage", bg=VB_FORM_BG, font=VB_FONT, anchor="w")
        place(lbl1, 120, 480, 615, 255)
        self.combo_voltage = ttk.Combobox(frame2, font=VB_FONT)
        place(self.combo_voltage, 960, 480, 1095, 315)

        lbl3 = tk.Label(frame2, text="Delays (mSec)", bg=VB_FORM_BG, font=VB_FONT, anchor="center")
        place(lbl3, 3720, 240, 1215, 255)

        lbl5 = tk.Label(frame2, text="NOT USED", bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        place(lbl5, 2520, 480, 1335, 255)
        self.txt_delay1 = tk.Entry(frame2, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_delay1, 3960, 480, 975, 285)

        lbl6 = tk.Label(frame2, text="After relay set, pre Ping", bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        place(lbl6, 2160, 840, 1695, 255)
        self.txt_delay2 = tk.Entry(frame2, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_delay2, 3960, 840, 975, 285)

        lbl7 = tk.Label(frame2, text="NOT USED", bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        place(lbl7, 2520, 1200, 1335, 255)
        self.txt_delay3 = tk.Entry(frame2, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_delay3, 3960, 1200, 975, 285)

        lbl8 = tk.Label(
            frame2, text="Meter Delay (Keithley Param)", bg=VB_FORM_BG, font=VB_FONT, anchor="e",
            justify="right", wraplength=85,
        )
        place(lbl8, 2520, 1560, 1335, 615)
        self.txt_meter_delay = tk.Entry(frame2, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_meter_delay, 3960, 1560, 975, 285)

        lbl9 = tk.Label(
            frame2, text="Averages for Keithley", bg=VB_FORM_BG, font=VB_FONT, anchor="e",
            justify="right", wraplength=70,
        )
        place(lbl9, 360, 1320, 1095, 495)
        self.txt_averages = tk.Entry(frame2, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_averages, 1560, 1440, 975, 285)

        lbl10 = tk.Label(frame2, text="Iterations", bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        place(lbl10, 360, 1800, 975, 255)
        self.txt_iterations = tk.Entry(frame2, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_iterations, 1560, 1800, 975, 285)

        lbl11 = tk.Label(frame2, text="Meter Range", bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        place(lbl11, 120, 2280, 1335, 255)
        self.txt_meter_range = tk.Entry(frame2, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_meter_range, 1560, 2280, 975, 285)

        lbl12 = tk.Label(
            frame2, text="Meter Current Limit ", bg=VB_FORM_BG, font=VB_FONT, anchor="e",
            justify="right", wraplength=62,
        )
        place(lbl12, 2880, 2280, 975, 495)
        self.txt_meter_current_limit = tk.Entry(frame2, bg=VB_FIELD_BG, font=VB_FONT)
        place(self.txt_meter_current_limit, 3960, 2280, 975, 285)


    def _log(self, text):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt_data.configure(state="normal")
        self.txt_data.insert("end", f"[{timestamp}] {text}\n")
        self.txt_data.see("end")
        self.txt_data.configure(state="disabled")

    def _form_load(self):
        self.lbl_info.configure(
            text=f"Recipe: {self.recipe_name}\nWafer: {self.wafer_id}"
        )

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_log_name = f"C:\\ProbeData\\{self.recipe_name}_{timestamp}.txt"
        self.txt_file_name.insert(0, default_log_name)
        self.txt_wafer_id_frame1.insert(0, self.wafer_id)

        self.combo_dies["values"] = data_stub.DEMO_COMBO_DIES
        if data_stub.DEMO_COMBO_DIES:
            self.combo_dies.current(0)

        self._update_revert_caption()

        _init_status = {
            "2001X": "Initializing Electroglass",
            "Relay1": "Initializing Switches",
            "Keithley2400": "Initializing Keithley 2400",
        }
        for name, _address in data_stub.load_prober_configuration("LampElectrical"):
            if name not in self._hpib_by_name:
                continue
            self._log(_init_status.get(name, f"Initializing {name}"))

        for _order, instruction, comment, syntax in data_stub.PROBER_INIT_SEQUENCE:
            if not syntax:
                continue
            self._log(f"{instruction} ({comment}): {syntax}")
            self.hpib_2001x.write(syntax)

        for name, instrument in (
            ("Relay1", self.hpib_relay1),
            ("2001X", self.hpib_2001x),
            ("Keithley2400", self.hpib_keithley2400),
        ):
            if instrument.inst is not None:
                continue
            self._log(f"Configure HPIB Failed: HPIB_{name}")
            proceed = messagebox.askokcancel(
                MSG_TITLE,
                f"Configure HPIB Failed\n\nHPIB_{name}: the GPIB/VISA resource "
                f"{instrument.address!r} could not be opened.\n\nContinue anyway?",
            )
            if not proceed:
                self._log("Configuration cancelled by user")
                self.cmd_go.configure(state="disabled")
                return

        self._log("Ready")

    def _update_revert_caption(self):
        if self.mode_var.get() == "Full":
            self.cmd_revert.configure(text="Change to Engineering Mode")
        else:
            self.cmd_revert.configure(text="Change to Production Mode")

    def cmd_revert_click(self):
        self.mode_var.set("Manual" if self.mode_var.get() == "Full" else "Full")
        self._update_revert_caption()
        self.apply_mode(self.mode_var.get())

    def cmd_view_click(self):
        path = self.txt_file_name.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showinfo(MSG_TITLE, f"The data file '{path}' cannot be found!")
            return
        subprocess.Popen([r"C:\Windows\Notepad.exe", path])

    def cmd_go_click(self):
        if self._run_in_progress:
            return
        wafer_id = self.txt_wafer_id_frame1.get().strip()
        if not wafer_id:
            messagebox.showinfo(MSG_TITLE, "Please enter a Wafer ID")
            return

        uncollected = data_stub.uncollected_data_files()
        if uncollected:
            messagebox.showinfo(
                MSG_TITLE,
                "Data files found in C:\\ProbeData that have not been collected "
                "into the database.\nYou may continue probing, but please tell an "
                "engineer or the programmer!",
            )

        if self.chk_save_text_var.get() and not os.path.isdir(data_stub.PROBE_DATA_DIR):
            messagebox.showerror(
                "Microsoft Visual Basic",
                "Run-time error '76':\n\nPath not found",
            )
            return

        self._run_in_progress = True
        self.cmd_go.configure(state="disabled")
        self._run_steps = data_stub.real_run_steps(self._hpib_by_name)
        self._advance_run()

    def _advance_run(self):
        try:
            text, is_log_line = next(self._run_steps)
        except StopIteration:
            self._run_in_progress = False
            self.cmd_go.configure(state="normal")
            return

        if is_log_line:
            self._log(text)
        else:
            self.lbl_info.configure(text=f"Recipe: {self.recipe_name}\nWafer: {self.wafer_id}\n{text}")
            self._log(text)

        if text == "Ready" and not self._align_confirmed:
            self._align_confirmed = True
            proceed = messagebox.askokcancel(
                MSG_TITLE,
                "Align of wafer complete. Click 'OK' to commence probing, or 'Cancel' as "
                "the last chance to abort.\n\n"
                "You MUST now check the 'Z' height on the prober lower key pad \n"
                "The Green light on the edge sensor box should then be on.\n"
                " Last chance to abort!",
            )
            if not proceed:
                self._log("Run cancelled by user")
                self._run_in_progress = False
                self.cmd_go.configure(state="normal")
                return

        if text == "Probe Recipe completed normally":
            messagebox.showinfo(MSG_TITLE, "Probe recipe complete!\nClick 'OK' to send probe stage home.")

        self.after(400, self._advance_run)
