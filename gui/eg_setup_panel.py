"""Setup tab (Electroglas)."""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from instruments import eg_profiles

_KEY_LABELS = {
    "prober_eg": "Prober",
    "smu_eg": "SMU",
    "dmm_eg": "DMM",
    "dmm_vxi_eg": "VXI DMM",
    "relay1_eg": "Relay 1",
    "relay2_eg": "Relay 2",
    "relay3_eg": "Relay 3",
    "power_supply_eg": "Power Supply",
}


class EgSetupPanel(ttk.Frame):
    def __init__(self, parent, controller, main_layout=None):
        super().__init__(parent)
        self.controller = controller
        self._main_layout = main_layout
        self._bench_var = tk.StringVar(value="")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_bench_bar()
        self._build_table()
        self._refresh_benches()

    def _log(self, msg: str):
        try:
            self.controller.log(msg)
        except Exception:
            pass


    def _build_bench_bar(self):
        bar = ttk.Frame(self, padding=6)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="Prober bench:").pack(side="left")
        self._bench_cb = ttk.Combobox(bar, textvariable=self._bench_var,
                                      state="readonly", width=16)
        self._bench_cb.pack(side="left", padx=(4, 8))
        self._bench_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_bench_picked())
        ttk.Button(bar, text="＋ Add Prober…", command=self._add_prober).pack(
            side="left", padx=2)
        self._active_lbl = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._active_lbl, foreground="#16a34a",
                 font=("Segoe UI", 8, "italic")).pack(side="left", padx=(10, 0))

    def _refresh_benches(self):
        names = eg_profiles.profile_names()
        self._bench_cb.config(values=names)
        if self._bench_var.get() not in names:
            self._bench_var.set(eg_profiles.active_name() or (names[0] if names else ""))
        self._update_active_label()
        self._refresh_table()

    def _update_active_label(self):
        active = eg_profiles.active_name()
        if self._bench_var.get() == active:
            self._active_lbl.set(f"● active ({active})")
        else:
            self._active_lbl.set(f"active bench is {active!r}")

    def _on_bench_picked(self):
        self._update_active_label()
        self._refresh_table()

    def _add_prober(self):
        source = self._bench_var.get()
        if not source:
            messagebox.showerror("No Bench", "No existing prober to copy from.")
            return
        name = simpledialog.askstring(
            "Add Prober",
            f"New prober name (starts as a copy of {source!r}):",
            parent=self)
        if not name:
            return
        try:
            eg_profiles.add_profile(name.strip(), based_on=source)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Add Prober Failed", str(exc))
            return
        self._log(f"[SETUP] Added prober {name!r} (copy of {source!r})")
        self._bench_var.set(name.strip())
        self._refresh_benches()


    def _build_table(self):
        frame = ttk.Frame(self, padding=(6, 0, 6, 6))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        cols = ("key", "name", "address", "timeout", "fitted")
        self._tree = ttk.Treeview(frame, columns=cols, show="headings",
                                  selectmode="browse")
        heads = [("key", "Slot", 90), ("name", "Instrument", 220),
                 ("address", "GPIB Address", 160), ("timeout", "Timeout (ms)", 90),
                 ("fitted", "Fitted", 60)]
        for cid, text, width in heads:
            self._tree.heading(cid, text=text)
            self._tree.column(cid, width=width,
                              anchor="center" if cid in ("timeout", "fitted") else "w")
        self._tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.bind("<Double-Button-1>", lambda _e: self._edit_selected())

        btns = ttk.Frame(frame)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="＋ Add Instrument…", command=self._add_instrument).pack(
            side="left")
        ttk.Button(btns, text="✎ Edit Selected…", command=self._edit_selected).pack(
            side="left", padx=(6, 0))
        ttk.Button(btns, text="🗑 Remove Selected", command=self._remove_selected).pack(
            side="left", padx=(6, 0))

    def _refresh_table(self):
        self._tree.delete(*self._tree.get_children())
        bench = self._bench_var.get()
        if not bench:
            return
        inst = eg_profiles.instruments(bench)
        for key in eg_profiles.EG_KEYS:
            entry = inst.get(key)
            if not entry:
                continue
            self._tree.insert("", "end", iid=key, values=(
                _KEY_LABELS.get(key, key), entry.get("name", key),
                entry.get("address", ""), entry.get("timeout_ms", 3000),
                "yes" if entry.get("fitted", True) else "no"))

    def _selected_key(self):
        sel = self._tree.selection()
        return sel[0] if sel else None

    def _add_instrument(self):
        bench = self._bench_var.get()
        if not bench:
            messagebox.showerror("No Bench", "Pick a prober bench first.")
            return
        have = set(eg_profiles.instruments(bench))
        missing = [k for k in eg_profiles.EG_KEYS if k not in have]
        if not missing:
            messagebox.showinfo("Nothing to Add",
                                f"{bench!r} already has every known instrument slot.")
            return
        dlg = _InstrumentDialog(self, title=f"Add Instrument to {bench!r}",
                                key_choices=missing)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        key, name, address, timeout_ms, fitted = dlg.result
        try:
            eg_profiles.set_instrument(bench, key, name=name, address=address,
                                       timeout_ms=timeout_ms, fitted=fitted)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Add Failed", str(exc))
            return
        self._log(f"[SETUP] {bench}: added {_KEY_LABELS.get(key, key)} "
                  f"({name!r} @ {address})")
        self._after_edit(bench)

    def _edit_selected(self):
        bench = self._bench_var.get()
        key = self._selected_key()
        if not bench or not key:
            return
        entry = eg_profiles.instruments(bench).get(key, {})
        dlg = _InstrumentDialog(
            self, title=f"Edit {_KEY_LABELS.get(key, key)} on {bench!r}",
            key_choices=None, fixed_key=key,
            initial=(entry.get("name", key), entry.get("address", ""),
                    entry.get("timeout_ms", 3000), entry.get("fitted", True)))
        self.wait_window(dlg)
        if dlg.result is None:
            return
        _key, name, address, timeout_ms, fitted = dlg.result
        try:
            eg_profiles.set_instrument(bench, key, name=name, address=address,
                                       timeout_ms=timeout_ms, fitted=fitted)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Edit Failed", str(exc))
            return
        self._log(f"[SETUP] {bench}: updated {_KEY_LABELS.get(key, key)} "
                  f"({name!r} @ {address}, fitted={fitted})")
        self._after_edit(bench)

    def _remove_selected(self):
        bench = self._bench_var.get()
        key = self._selected_key()
        if not bench or not key:
            return
        if not messagebox.askyesno(
                "Remove Instrument",
                f"Remove {_KEY_LABELS.get(key, key)} from {bench!r} entirely?\n\n"
                "This is not the same as marking it not-fitted - the slot "
                "will not appear on this bench at all."):
            return
        try:
            eg_profiles.remove_instrument(bench, key)
        except KeyError as exc:
            messagebox.showerror("Remove Failed", str(exc))
            return
        self._log(f"[SETUP] {bench}: removed {_KEY_LABELS.get(key, key)}")
        self._after_edit(bench)

    def _after_edit(self, bench: str):
        self._refresh_table()
        if bench == eg_profiles.active_name():
            try:
                eg_profiles.apply_to_instruments_yaml(bench)
            except Exception as exc:
                self._log(f"[SETUP] Could not push {bench!r} into "
                          f"instruments.yaml: {exc}")
            layout = self._main_layout
            panel = getattr(layout, "recipe_panel", None) if layout else None
            refresh = getattr(panel, "refresh_bench_instruments", None)
            if refresh:
                try:
                    refresh()
                except Exception:
                    pass


class _InstrumentDialog(tk.Toplevel):

    def __init__(self, parent, title: str, key_choices, fixed_key: str = None,
                initial: tuple = None):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        row = 0
        self._key_var = tk.StringVar(value=fixed_key or (key_choices[0] if key_choices else ""))
        ttk.Label(body, text="Slot:").grid(row=row, column=0, sticky="e", pady=3)
        if fixed_key:
            ttk.Label(body, text=_KEY_LABELS.get(fixed_key, fixed_key)).grid(
                row=row, column=1, sticky="w", padx=6, pady=3)
        else:
            cb = ttk.Combobox(body, textvariable=self._key_var, state="readonly",
                              values=key_choices, width=20)
            cb.grid(row=row, column=1, sticky="w", padx=6, pady=3)

        name0, addr0, timeout0, fitted0 = initial or ("", "", 3000, True)
        row += 1
        ttk.Label(body, text="Name:").grid(row=row, column=0, sticky="e", pady=3)
        self._name_var = tk.StringVar(value=name0)
        ttk.Entry(body, textvariable=self._name_var, width=30).grid(
            row=row, column=1, sticky="w", padx=6, pady=3)

        row += 1
        ttk.Label(body, text="GPIB Address:").grid(row=row, column=0, sticky="e", pady=3)
        self._addr_var = tk.StringVar(value=addr0)
        ttk.Entry(body, textvariable=self._addr_var, width=30).grid(
            row=row, column=1, sticky="w", padx=6, pady=3)

        row += 1
        ttk.Label(body, text="Timeout (ms):").grid(row=row, column=0, sticky="e", pady=3)
        self._timeout_var = tk.StringVar(value=str(timeout0))
        ttk.Entry(body, textvariable=self._timeout_var, width=10).grid(
            row=row, column=1, sticky="w", padx=6, pady=3)

        row += 1
        self._fitted_var = tk.BooleanVar(value=bool(fitted0))
        ttk.Checkbutton(body, text="Fitted (will ping on startup)",
                       variable=self._fitted_var).grid(
                       row=row, column=0, columnspan=2, sticky="w", pady=(6, 3))

        row += 1
        btns = ttk.Frame(body)
        btns.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(btns, text="OK", command=self._on_ok).pack(side="left")

        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _on_ok(self):
        key = self._key_var.get().strip()
        name = self._name_var.get().strip()
        address = self._addr_var.get().strip()
        if not key or not name or not address:
            messagebox.showerror("Missing Info", "Slot, name, and address are all required.",
                                 parent=self)
            return
        try:
            timeout_ms = int(self._timeout_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Timeout", "Timeout (ms) must be a whole number.",
                                 parent=self)
            return
        self.result = (key, name, address, timeout_ms, self._fitted_var.get())
        self.destroy()
