import tkinter as tk
from tkinter import ttk, messagebox

import switch_topology as topo
from instruments import accretech_profiles


class SwitchSettingsPanel(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        # Which bench this panel is EDITING - independent of whichever bench
        # the toolbar currently has live, same relationship
        # AccretechSetupPanel already has to the active bench (see its own
        # module docstring). probe08 and probe08new are wired completely
        # differently now (probe08new's single-channel 2400 has no row C/D,
        # and no wave gen at all - see instruments/accretech_profiles.py),
        # so editing one must never silently apply to the other - that
        # silent cross-application through one shared global file/cache is
        # exactly what made "save settings" feel broken when switching
        # between the two probers before switch_topology.py became
        # bench-scoped.
        self._bench_var = tk.StringVar(value=self._active_bench())
        self._slots: list = []
        self._roles: dict = {}

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        self._build_bench_bar()
        self._build_header()
        self._build_slots_section()
        self._build_roles_section()
        self._build_footer()
        self._load_bench(self._bench_var.get())

    def _active_bench(self) -> str:
        try:
            return accretech_profiles.active_name() or "probe08"
        except Exception:
            return "probe08"

    def _log(self, msg: str):
        self.controller.log(msg)

    def _build_bench_bar(self):
        bar = ttk.Frame(self, padding=(10, 8, 10, 0))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="Editing bench:").pack(side="left")
        self._bench_cb = ttk.Combobox(bar, textvariable=self._bench_var,
                                      state="readonly", width=16)
        self._bench_cb.pack(side="left", padx=(4, 8))
        self._bench_cb.bind("<<ComboboxSelected>>",
                            lambda _e: self._load_bench(self._bench_var.get()))
        self._refresh_bench_choices()

    def _refresh_bench_choices(self):
        try:
            names = accretech_profiles.profile_names()
        except Exception:
            names = []
        names = names or [self._bench_var.get()]
        self._bench_cb.config(values=names)

    def _load_bench(self, bench: str):
        self._slots = [dict(s) for s in topo.slots(bench)]
        self._roles = {k: dict(v) for k, v in topo.row_roles(bench).items()}
        self._refresh_bench_choices()
        self._row_count_var.set(str(len(self._roles)))
        self._rebuild_slot_row_checkboxes()
        self._refresh_slots_tree()
        self._refresh_roles_tree()
        self._role_row_cb.config(values=self._row_letters())
        self._role_row_var.set(self._row_letters()[0] if self._roles else "")
        self._status_var.set("")

    def refresh_active_bench(self):
        """Called by AtomicaDashboard after the TOOLBAR's bench picker
        switches - this panel's own bench picker stays wherever the
        operator left it (it edits whichever bench it's set to, independent
        of the live one), but the '(currently active)' note has to track
        reality."""
        self._refresh_bench_choices()

    def _row_letters(self) -> list:
        return sorted(self._roles.keys())

    def _build_header(self):
        hdr = ttk.Frame(self, padding=(10, 10, 10, 4))
        hdr.grid(row=1, column=0, sticky="ew")

    def _build_slots_section(self):
        lf = ttk.LabelFrame(self, text="Slots / Cards", padding=8)
        lf.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 4))
        lf.columnconfigure(0, weight=1)

        self._slots_tree = ttk.Treeview(
            lf, columns=("slot", "cols", "rows"), show="headings", height=4)
        for cid, text, width in [("slot", "Slot", 80), ("cols", "Columns (pins)", 120),
                                 ("rows", "Rows used", 320)]:
            self._slots_tree.heading(cid, text=text)
            self._slots_tree.column(cid, width=width, anchor="center" if cid != "rows" else "w")
        self._slots_tree.grid(row=0, column=0, sticky="ew")
        self._slots_tree.bind("<<TreeviewSelect>>", lambda _e: self._load_selected_slot())

        add_row = ttk.Frame(lf)
        add_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(add_row, text="Slot ID:").pack(side="left")
        self._slot_id_var = tk.StringVar()
        ttk.Entry(add_row, textvariable=self._slot_id_var, width=6).pack(
            side="left", padx=(2, 10))
        ttk.Label(add_row, text="Columns:").pack(side="left")
        self._slot_cols_var = tk.StringVar(value="12")
        ttk.Entry(add_row, textvariable=self._slot_cols_var, width=6).pack(
            side="left", padx=(2, 10))

        ttk.Separator(add_row, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(add_row, text="Number of rows:").pack(side="left")
        self._row_count_var = tk.StringVar(value=str(len(self._roles)))
        ttk.Spinbox(add_row, from_=topo.MIN_ROW_COUNT, to=topo.MAX_ROW_COUNT,
                   textvariable=self._row_count_var, width=5).pack(side="left", padx=(4, 8))
        ttk.Button(add_row, text="Apply", command=self._apply_row_count).pack(side="left")

        rows_row = ttk.Frame(lf)
        rows_row.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(rows_row, text="Rows on this card:").pack(side="left")
        self._slot_rows_frame = ttk.Frame(rows_row)
        self._slot_rows_frame.pack(side="left")
        self._slot_row_vars: dict = {}
        self._rebuild_slot_row_checkboxes()

        btn_row = ttk.Frame(lf)
        btn_row.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(btn_row, text="+ Add / Update Slot",
                  command=self._add_or_update_slot).pack(side="left")
        ttk.Button(btn_row, text="Remove Selected Slot",
                  command=self._remove_slot).pack(side="left", padx=(6, 0))

        self._refresh_slots_tree()

    def _rebuild_slot_row_checkboxes(self):
        for child in self._slot_rows_frame.winfo_children():
            child.destroy()
        letters = self._row_letters()
        old_vars = self._slot_row_vars
        self._slot_row_vars = {}
        for letter in letters:
            var = tk.BooleanVar(value=old_vars[letter].get() if letter in old_vars else False)
            self._slot_row_vars[letter] = var
            ttk.Checkbutton(self._slot_rows_frame, text=letter, variable=var).pack(
                side="left", padx=2)

    def _refresh_slots_tree(self):
        self._slots_tree.delete(*self._slots_tree.get_children())
        for spec in self._slots:
            self._slots_tree.insert("", "end", iid=spec["slot"], values=(
                spec["slot"], spec.get("cols", 0), ",".join(spec.get("rows", []))))

    def _load_selected_slot(self):
        sel = self._slots_tree.selection()
        if not sel:
            return
        spec = next((s for s in self._slots if s["slot"] == sel[0]), None)
        if not spec:
            return
        self._slot_id_var.set(spec["slot"])
        self._slot_cols_var.set(str(spec.get("cols", 12)))
        active_rows = set(spec.get("rows", []))
        for letter, var in self._slot_row_vars.items():
            var.set(letter in active_rows)

    def _add_or_update_slot(self):
        slot_id = self._slot_id_var.get().strip()
        if not slot_id:
            messagebox.showerror("Missing Slot ID", "Enter a slot ID (e.g. 1, 2, 3...).")
            return
        try:
            cols = int(self._slot_cols_var.get().strip())
            if cols <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Columns", "Columns must be a positive integer.")
            return
        rows = [letter for letter in self._row_letters() if self._slot_row_vars[letter].get()]
        if not rows:
            messagebox.showerror("No Rows Selected", "Pick at least one row for this slot.")
            return
        existing = next((s for s in self._slots if s["slot"] == slot_id), None)
        if existing:
            existing["cols"] = cols
            existing["rows"] = rows
        else:
            self._slots.append({"slot": slot_id, "cols": cols, "rows": rows})
        self._refresh_slots_tree()
        self._log(f"[SETUP] Slot '{slot_id}' set: {cols} columns, rows {','.join(rows)}")

    def _remove_slot(self):
        sel = self._slots_tree.selection()
        if not sel:
            return
        slot_id = sel[0]
        self._slots = [s for s in self._slots if s["slot"] != slot_id]
        self._refresh_slots_tree()
        self._log(f"[SETUP] Slot '{slot_id}' removed")

    def _build_roles_section(self):
        lf = ttk.LabelFrame(self, text="Row Wiring (which instrument each row connects to)",
                            padding=8)
        lf.grid(row=3, column=0, sticky="nsew", padx=10, pady=(4, 4))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)

        self._roles_tree = ttk.Treeview(
            lf, columns=("row", "instrument", "channel", "polarity", "label"),
            show="headings", height=8)
        for cid, text, width in [("row", "Row", 50), ("instrument", "Instrument", 90),
                                 ("channel", "Channel", 80), ("polarity", "Polarity", 80),
                                 ("label", "Label", 160)]:
            self._roles_tree.heading(cid, text=text)
            self._roles_tree.column(cid, width=width, anchor="center")
        self._roles_tree.grid(row=0, column=0, sticky="nsew")
        self._roles_tree.bind("<<TreeviewSelect>>", lambda _e: self._load_selected_role())

        edit_row = ttk.Frame(lf)
        edit_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(edit_row, text="Row:").pack(side="left")
        self._role_row_var = tk.StringVar(value="A")
        self._role_row_cb = ttk.Combobox(edit_row, textvariable=self._role_row_var,
                                         values=self._row_letters(), width=4, state="readonly")
        self._role_row_cb.pack(side="left", padx=(2, 10))

        ttk.Label(edit_row, text="Instrument:").pack(side="left")
        self._role_instrument_var = tk.StringVar()
        inst_cb = ttk.Combobox(edit_row, textvariable=self._role_instrument_var,
                               values=["", "SMU", "DMM", "WGEN"], width=8, state="readonly")
        inst_cb.pack(side="left", padx=(2, 10))
        inst_cb.bind("<<ComboboxSelected>>", lambda _e: self._sync_role_channel_choices())

        ttk.Label(edit_row, text="Channel:").pack(side="left")
        self._role_channel_var = tk.StringVar()
        self._role_channel_cb = ttk.Combobox(edit_row, textvariable=self._role_channel_var,
                                             values=[], width=6, state="readonly")
        self._role_channel_cb.pack(side="left", padx=(2, 10))

        ttk.Label(edit_row, text="Polarity:").pack(side="left")
        self._role_polarity_var = tk.StringVar()
        self._role_polarity_cb = ttk.Combobox(edit_row, textvariable=self._role_polarity_var,
                                              values=list(topo.POLARITIES), width=6,
                                              state="readonly")
        self._role_polarity_cb.pack(side="left", padx=(2, 10))

        ttk.Button(edit_row, text="Apply to Row", command=self._apply_role).pack(
            side="left", padx=(8, 0))

        self._refresh_roles_tree()

    def _sync_role_channel_choices(self):
        instrument = self._role_instrument_var.get()
        if instrument == "SMU":
            self._role_channel_cb.config(values=list(topo.SMU_CHANNELS), state="readonly")
        elif instrument == "WGEN":
            self._role_channel_cb.config(values=list(topo.WGEN_CHANNELS), state="readonly")
        else:
            self._role_channel_var.set("")
            self._role_channel_cb.config(values=[], state="disabled")
        if instrument == "WGEN":
            self._role_polarity_var.set("HI")
            self._role_polarity_cb.config(state="disabled")
        else:
            self._role_polarity_cb.config(state="readonly")

    def _refresh_roles_tree(self):
        self._roles_tree.delete(*self._roles_tree.get_children())
        for letter in self._row_letters():
            role = self._roles.get(letter, {})
            self._roles_tree.insert("", "end", iid=letter, values=(
                letter, role.get("instrument", ""), role.get("channel", ""),
                role.get("polarity", ""), topo.role_label(role)))

    def _apply_row_count(self):
        try:
            n = int(self._row_count_var.get())
        except ValueError:
            messagebox.showerror("Invalid Row Count", "Enter a whole number.")
            return
        if not (topo.MIN_ROW_COUNT <= n <= topo.MAX_ROW_COUNT):
            messagebox.showerror(
                "Invalid Row Count",
                f"Number of rows must be between {topo.MIN_ROW_COUNT} and "
                f"{topo.MAX_ROW_COUNT}.")
            self._row_count_var.set(str(len(self._roles)))
            return
        current = self._row_letters()
        if n == len(current):
            return
        if n > len(current):
            added = topo.ROW_LETTERS_POOL[len(current):n]
            for letter in added:
                self._roles[letter] = {"instrument": "", "channel": "", "polarity": ""}
            self._log(f"[SETUP] Added row(s) {','.join(added)}")
        else:
            removed = current[n:]
            for letter in removed:
                self._roles.pop(letter, None)
            for spec in self._slots:
                spec["rows"] = [r for r in spec.get("rows", []) if r not in removed]
            self._log(f"[SETUP] Removed row(s) {','.join(removed)} from roles and slots")
        self._row_count_var.set(str(len(self._roles)))
        self._role_row_cb.config(values=self._row_letters())
        if self._role_row_var.get() not in self._roles:
            self._role_row_var.set(self._row_letters()[0] if self._roles else "")
        self._rebuild_slot_row_checkboxes()
        self._refresh_roles_tree()
        self._refresh_slots_tree()

    def _load_selected_role(self):
        sel = self._roles_tree.selection()
        if not sel:
            return
        letter = sel[0]
        role = self._roles.get(letter, {})
        self._role_row_var.set(letter)
        self._role_instrument_var.set(role.get("instrument", ""))
        self._sync_role_channel_choices()
        self._role_channel_var.set(role.get("channel", ""))
        self._role_polarity_var.set(role.get("polarity", ""))

    def _apply_role(self):
        letter = self._role_row_var.get()
        instrument = self._role_instrument_var.get()
        channel = self._role_channel_var.get() if instrument in ("SMU", "WGEN") else ""
        polarity = "HI" if instrument == "WGEN" else self._role_polarity_var.get()
        if instrument and not polarity:
            messagebox.showerror("Missing Polarity", "Pick HI or LO for this row.")
            return
        self._roles[letter] = {"instrument": instrument, "channel": channel,
                               "polarity": polarity}
        self._refresh_roles_tree()
        self._log(f"[SETUP] Row {letter} set to "
                  f"{topo.role_label(self._roles[letter])}")

    def _build_footer(self):
        bar = ttk.Frame(self, padding=(10, 4, 10, 10))
        bar.grid(row=4, column=0, sticky="ew")
        ttk.Button(bar, text="💾 Save Settings", command=self._save).pack(side="left")
        ttk.Button(bar, text="↺ Reset to Defaults", command=self._reset).pack(
            side="left", padx=(6, 0))
        self._status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._status_var, foreground="#6b7280",
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 0))

    def _notify_routing(self):
        try:
            self.controller.refresh_probe_routing_panels()
        except Exception as exc:
            self._log(f"[SETUP] Could not refresh Switch Routing view: {exc}")

    def _save(self):
        bench = self._bench_var.get()
        data = {"slots": [dict(s) for s in self._slots],
               "row_roles": {k: dict(v) for k, v in self._roles.items()}}
        topo.save_topology(data, bench)
        self._status_var.set(f"Saved {bench!r} to {topo.TOPOLOGY_PATH}")
        self._log(f"[SETUP] Switch topology saved for {bench!r}")
        self._notify_routing()

    def _reset(self):
        bench = self._bench_var.get()
        if not messagebox.askyesno(
                "Reset to Defaults",
                f"Discard {bench!r}'s changes and restore the default 2-slot "
                "Keithley 707B layout?"):
            return
        data = topo.reset_topology(bench)
        self._slots = [dict(s) for s in data["slots"]]
        self._roles = {k: dict(v) for k, v in data["row_roles"].items()}
        self._row_count_var.set(str(len(self._roles)))
        self._role_row_cb.config(values=self._row_letters())
        self._role_row_var.set(self._row_letters()[0] if self._roles else "")
        self._rebuild_slot_row_checkboxes()
        self._refresh_slots_tree()
        self._refresh_roles_tree()
        self._status_var.set(f"{bench!r} reset to defaults and saved.")
        self._log(f"[SETUP] Switch topology for {bench!r} reset to defaults.")
        self._notify_routing()
