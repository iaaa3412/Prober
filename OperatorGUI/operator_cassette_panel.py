"""Cassette tab for the Operator GUI.

All cassette automation (arming, advancing slots, auto-export, yield pause,
lot summary) is inherited unchanged from EngineerGUI's CassettePanel. Only
the widget-building methods are copied here, so controls can be removed for
operators in this file without touching EngineerGUI. Controls the engineer
code still touches are built into a hidden frame rather than deleted.
"""
import tkinter as tk
from tkinter import ttk

from cassette_panel import CassettePanel


class OperatorCassettePanel(CassettePanel):

    def _operator_hidden(self):
        # Never geometry-managed, so nothing inside it is ever drawn.
        holder = getattr(self, "_operator_hidden_holder", None)
        if holder is None:
            holder = self._operator_hidden_holder = ttk.Frame(self)
        return holder

    def _disarm(self, reason: str = ""):
        # CassettePanel's version compares with "is", but a bound method is a
        # new object on every access, so it never matched: the cassette's
        # run-finished hook stayed attached after automation stopped and
        # single ▶ Runs lost their end-of-run popup until a restart.
        self._armed = False
        self._set_paused_for_yield(False)
        self._set_paused_for_error(False)
        if getattr(self.ui, "_exec_on_run_finished", None) == self._on_wafer_finished:
            self.ui._exec_on_run_finished = None
            claim = getattr(self.ui, "_autoexport_claim_hook", None)
            if claim is not None:
                claim()
        self._set_locked(False)
        self._redraw_slots()
        if reason:
            self._log_event(self._slot_idx + 1, "", reason)

    def _build_topbar(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.grid(row=0, column=0, sticky="ew")

        self._go_btn = ttk.Button(bar, text="▶  Cassette Automation",
                                  command=self._arm)
        self._go_btn.pack(side="left", padx=4)
        self._stop_btn = ttk.Button(bar, text="⏹  Stop Automation", state="disabled",
                                    command=lambda: self._disarm("Stopped by user."))
        self._stop_btn.pack(side="left", padx=4)
        # Operator: no Reset to Slot #1, Move to Selected Slot, Load Next
        # Wafer or pass-yield setting. The yield threshold still loads from
        # the ATA folder, and Continue still resumes after a yield or error
        # pause.
        self._move_slot_btn = ttk.Button(self._operator_hidden(),
                                         text="Move to Selected Slot",
                                         command=self._move_selected_slot_button)
        self._yield_var = tk.StringVar(value="0")
        self._continue_btn = ttk.Button(bar, text="▶ Continue", state="disabled",
                                        command=self._continue_after_pause)
        self._continue_btn.pack(side="left", padx=4)

        self._state_var = tk.StringVar(value="IDLE")
        self._state_lbl = ttk.Label(bar, textvariable=self._state_var,
                                    font=("Consolas", 11, "bold"), foreground="#6b7280")
        self._state_lbl.pack(side="right", padx=8)

    def _build_wafer_list(self):
        lf = ttk.LabelFrame(self, text="Cassette Slots", padding=6)
        lf.grid(row=1, column=0, sticky="ew", padx=6, pady=(4, 2))
        lf.columnconfigure(0, weight=1)
        self._slots_lf = lf

        btns = ttk.Frame(lf)
        btns.grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(btns, text="Lot ID (all wafers):").pack(side="left")
        self._lot_id_var = tk.StringVar()
        ttk.Entry(btns, textvariable=self._lot_id_var, width=20).pack(
            side="left", padx=(4, 0))
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(btns, text="＋ Add Slot", command=self._add_slot).pack(side="left", padx=2)
        ttk.Button(btns, text="✎ Edit", command=self._edit_slot).pack(side="left", padx=2)
        ttk.Button(btns, text="Remove", command=self._remove_slot).pack(side="left", padx=2)
        ttk.Button(btns, text="▲", width=3, command=lambda: self._move_slot(-1)).pack(
            side="left", padx=(10, 2))
        ttk.Button(btns, text="▼", width=3, command=lambda: self._move_slot(1)).pack(
            side="left", padx=2)
        ttk.Button(btns, text="Clear All", command=self._clear_slots).pack(side="left", padx=(10, 2))

        cols = ("slot", "lot", "wafer")
        self._slot_tree = ttk.Treeview(lf, columns=cols, show="headings", height=5,
                                       selectmode="browse")
        heads = [("slot", "Slot #", 60), ("lot", "Lot ID", 160), ("wafer", "Wafer ID", 160)]
        for cid, text, width in heads:
            self._slot_tree.heading(cid, text=text)
            self._slot_tree.column(cid, width=width, anchor="center" if cid == "slot" else "w")
        self._slot_tree.grid(row=1, column=0, sticky="ew")
        self._slot_tree.bind("<Double-1>", lambda _e: self._edit_slot())
        self._slot_tree.bind("<<TreeviewSelect>>", self._on_move_slot_row_selected)

    def _build_export(self):
        ef = ttk.Frame(self._slots_lf)
        ef.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        self._auto_export_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ef, text="Auto Export",
                       variable=self._auto_export_var).pack(side="left", padx=(0, 16))
        self._auto_export_csv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ef, text="Also Save CSV",
                       variable=self._auto_export_csv_var).pack(side="left", padx=(0, 16))

        # Operator: no export directory or format pickers - cassette exports
        # use the Results tab's export path and format (the same variables).
        # The engineer code still refreshes this hidden format dropdown.
        self._export_format_cb = ttk.Combobox(
            self._operator_hidden(), textvariable=self.ui.export_format_var,
            state="readonly", width=32)

    def _build_progress(self):
        pf = ttk.LabelFrame(self, text="Cassette Automation Log", padding=6)
        pf.grid(row=3, column=0, sticky="nsew", padx=6, pady=(2, 6))
        pf.rowconfigure(0, weight=1)
        pf.columnconfigure(0, weight=1)

        cols = ("timestamp", "slot", "lot", "event")
        self._tree = ttk.Treeview(pf, columns=cols, show="headings",
                                  height=10, selectmode="browse")
        heads = [("timestamp", "Time", 150), ("slot", "Slot", 50),
                 ("lot", "Lot ID", 120), ("event", "Event", 400)]
        for cid, text, width in heads:
            self._tree.heading(cid, text=text)
            self._tree.column(cid, width=width,
                              anchor="center" if cid == "slot" else "w")
        self._tree.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(pf, orient="vertical", command=self._tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=tsb.set)
