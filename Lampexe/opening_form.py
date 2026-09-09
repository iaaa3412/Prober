import os
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import data_stub
from layout import ModeAwareMixin, VB_FIELD_BG, VB_FONT, VB_FONT_BOLD, VB_FORM_BG, client_size, make_frame, place

VERSION_TEXT = "Software rev: 1.00.0001"

MSG_TITLE = "IMT LampElectrical Probing"


class OpeningForm(tk.Tk, ModeAwareMixin):

    def __init__(self):
        tk.Tk.__init__(self)
        ModeAwareMixin.__init__(self)

        self.title("Opening Form")
        w, h = client_size(8730, 5895)
        self.geometry(f"{w}x{h}")
        self.configure(bg=VB_FORM_BG)

        self.mode_var = tk.StringVar(value="Full")
        self.suppress_debug_var = tk.BooleanVar(value=False)
        self._dots_running = False

        self._build_menu()
        self._build_controls()
        self.apply_mode(self.mode_var.get())
        self._form_load()


    def _build_menu(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Set Prober Name", command=self.set_prober_name_click)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.prober_exit_click)
        menubar.add_cascade(label="File", menu=file_menu)
        self.config(menu=menubar)

    def _build_controls(self):
        lbl1 = tk.Label(
            self, text="LampElectrical Probing", bg=VB_FORM_BG, font=("MS Sans Serif", 12, "bold"), anchor="w"
        )
        place(lbl1, 480, 120, 3135, 495)

        self.lbl_version = tk.Label(self, text=VERSION_TEXT, bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        place(self.lbl_version, 3960, 120, 4215, 255)

        lbl5 = tk.Label(
            self,
            text="Load the wafer and conduct the pre-alignment",
            bg=VB_FORM_BG,
            font=VB_FONT_BOLD,
            anchor="w",
            justify="left",
            wraplength=225,
        )
        place(lbl5, 120, 720, 3375, 495)

        lbl7 = tk.Label(
            self,
            text="Make sure the prober is Online (yellow On Line Key) - display says 'X I/O...ONLINE'",
            bg=VB_FORM_BG,
            font=VB_FONT_BOLD,
            anchor="w",
            justify="left",
            wraplength=240,
        )
        place(lbl7, 120, 1320, 3735, 495)

        lbl6 = tk.Label(
            self, text="Click the GO button below", bg=VB_FORM_BG, font=VB_FONT_BOLD, anchor="w"
        )
        place(lbl6, 120, 1920, 3375, 375)

        lbl_hotwire = tk.Label(
            self,
            text="NOT FOR PRODUCTION: This is a hot wired debug version only",
            bg=VB_FORM_BG,
            font=("MS Sans Serif", 14),
            anchor="w",
            justify="left",
            wraplength=310,
        )
        place(lbl_hotwire, 3720, 2520, 4695, 735)

        frame1 = make_frame(self, "Operational Mode", 3840, 720, 4335, 1695)
        opt1 = tk.Radiobutton(
            frame1,
            text="Run a full production probe with data collection",
            variable=self.mode_var,
            value="Full",
            bg=VB_FORM_BG,
            font=VB_FONT_BOLD,
            anchor="w",
            justify="left",
            wraplength=230,
            command=self._on_mode_changed,
        )
        place(opt1, 240, 360, 3855, 375)
        opt2 = tk.Radiobutton(
            frame1,
            text="Engineering Mode with display and quick data logging only",
            variable=self.mode_var,
            value="Manual",
            bg=VB_FORM_BG,
            font=VB_FONT_BOLD,
            anchor="w",
            justify="left",
            wraplength=225,
            command=self._on_mode_changed,
        )
        place(opt2, 240, 840, 3735, 615)

        self.cmd_go = tk.Button(self, text="GO", font=VB_FONT_BOLD, command=self.cmd_go_click)
        place(self.cmd_go, 360, 2760, 1215, 495)

        chk = tk.Checkbutton(
            self,
            text="Suppress Debug Messages",
            variable=self.suppress_debug_var,
            bg=VB_FORM_BG,
            font=VB_FONT,
            anchor="w",
        )
        place(chk, 240, 3840, 2775, 255)

        lbl_operator = tk.Label(self, text="Operator ID", bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        self.register_view_tag(lbl_operator, "Full", 3360, 3480, 1215, 255)

        self.txt_operator_id = tk.Entry(self, bg=VB_FIELD_BG, font=VB_FONT)
        self.txt_operator_id.insert(0, "Operator Name")
        self.register_view_tag(self.txt_operator_id, "Full", 4680, 3480, 3015, 285)

        lbl_process = tk.Label(self, text="Process Step", bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        self.register_view_tag(lbl_process, "Full", 3360, 4440, 1215, 255)

        self.txt_process_step = tk.Entry(self, bg=VB_FIELD_BG, font=VB_FONT)
        self.txt_process_step.insert(0, "The Process Step")
        self.register_view_tag(self.txt_process_step, "Full", 4680, 4440, 3015, 285)

        lbl_wafer = tk.Label(self, text="Wafer ID", bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        self.register_view_tag(lbl_wafer, "Full", 3360, 3960, 1215, 255)

        self.txt_wafer_id = tk.Entry(self, bg=VB_FIELD_BG, font=VB_FONT)
        self.txt_wafer_id.insert(0, "The Wafer ID")
        self.register_view_tag(self.txt_wafer_id, "Full", 4680, 3960, 3015, 285)

        lbl_recipes = tk.Label(self, text="Recipes Available", bg=VB_FORM_BG, font=VB_FONT, anchor="e")
        self.register_view_tag(lbl_recipes, "Full", 840, 4920, 2055, 255)

        self.combo_recipes = ttk.Combobox(self, font=VB_FONT, state="readonly")
        place(self.combo_recipes, 3240, 4920, 5175, 315)

        self.lbl_info = tk.Label(self, text="", bg=VB_FORM_BG, font=VB_FONT, anchor="w")
        place(self.lbl_info, 240, 5400, 8415, 375)


    def _on_mode_changed(self):
        self.apply_mode(self.mode_var.get())


    def _timer1_tick(self):
        if not self._dots_running:
            return
        current = self.lbl_info.cget("text")
        dots = current.count(".") if set(current) <= {"."} else 0
        self.lbl_info.configure(text="." * ((dots + 1) % 11 or 1))
        self.after(500, self._timer1_tick)

    def _start_dots(self):
        self._dots_running = True
        self.lbl_info.configure(text="")
        self._timer1_tick()

    def _stop_dots(self):
        self._dots_running = False
        self.lbl_info.configure(text="")

    def _form_load(self):
        recipes = data_stub.scan_recipes()
        self.combo_recipes["values"] = recipes
        if recipes:
            self.combo_recipes.current(0)

    def cmd_go_click(self):
        wafer_id = self.txt_wafer_id.get().strip()
        if self.mode_var.get() == "Full" and wafer_id in ("", "The Wafer ID"):
            messagebox.showinfo(MSG_TITLE, "Please enter the Wafer ID.")
            return

        if self.mode_var.get() == "Full":
            operator_check = self.txt_operator_id.get().strip()
            if operator_check in ("", "Operator Name"):
                messagebox.showinfo(MSG_TITLE, "Please enter your Operator ID.")
                return
            process_check = self.txt_process_step.get().strip()
            if process_check in ("", "The Process Step"):
                messagebox.showinfo(MSG_TITLE, "Please enter the Process Step.")
                return

        recipe = self.combo_recipes.get()
        if not recipe:
            messagebox.showinfo(
                "Tell an Engineer!",
                "This file is needed, even in Engineering Mode, to establish the die moves.",
            )
            return

        missing = data_stub.missing_recipe_files(recipe)
        if missing:
            missing_names = "\n".join(os.path.basename(p) for p in missing)
            messagebox.showinfo(
                MSG_TITLE,
                f"ERROR is not a valid recipe name!\nThis is caused by the system not "
                f"locating the following file(s) in {data_stub.REAL_RECIPE_DIR}:\n"
                f"{missing_names}",
            )
            self.combo_recipes.set("")
            self._form_load()
            return

        operator_id = self.txt_operator_id.get().strip() if self.mode_var.get() == "Full" else ""
        process_step = self.txt_process_step.get().strip() if self.mode_var.get() == "Full" else ""

        self._start_dots()
        self.after(
            1200,
            lambda: self._go_to_runtime(recipe, wafer_id, operator_id, process_step),
        )

    def _go_to_runtime(self, recipe, wafer_id, operator_id, process_step):
        self._stop_dots()

        from runtime_form import RuntimeForm

        self.withdraw()
        run_form = RuntimeForm(
            self,
            recipe_name=recipe,
            wafer_id=wafer_id or "N/A",
            operator_id=operator_id or data_stub.get_prober_name(),
            process_step=process_step,
            mode=self.mode_var.get(),
        )
        run_form.protocol("WM_DELETE_WINDOW", lambda: (run_form.destroy(), self.destroy()))

    def set_prober_name_click(self):
        current = data_stub.get_prober_name()
        new_name = simpledialog.askstring(
            "Prober Name",
            "Enter a new name for this prober so as to identify its data in the database.",
            initialvalue=current,
            parent=self,
        )
        if new_name:
            data_stub.set_prober_name(new_name)
            messagebox.showinfo(MSG_TITLE, f"Prober Name '{new_name}' saved.")

    def prober_exit_click(self):
        self.destroy()
