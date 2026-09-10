"""Setup tab (Accretech)."""

import os
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from instruments import accretech_profiles

_KEY_LABELS = {
    "prober": "Prober", "smu": "SMU", "dmm": "DMM",
    "switch_matrix": "Switch Matrix", "wave_gen": "Wave Gen",
}


class AccretechSetupPanel(ttk.Frame):
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
        ttk.Button(bar, text="✎ Rename…", command=self._rename_prober).pack(
            side="left", padx=2)
        ttk.Button(bar, text="🗑 Delete…", command=self._delete_prober).pack(
            side="left", padx=2)
        self._active_lbl = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._active_lbl, foreground="#16a34a",
                 font=("Segoe UI", 8, "italic")).pack(side="left", padx=(10, 0))

    def _refresh_benches(self):
        names = accretech_profiles.profile_names()
        self._bench_cb.config(values=names)
        if self._bench_var.get() not in names:
            self._bench_var.set(accretech_profiles.active_name()
                                or (names[0] if names else ""))
        self._update_active_label()
        self._refresh_table()

    def _update_active_label(self):
        active = accretech_profiles.active_name()
        if self._bench_var.get() == active:
            self._active_lbl.set(f"● active ({active})")
        else:
            self._active_lbl.set(f"active bench is {active!r}")

    def _on_bench_picked(self):
        self._update_active_label()
        self._refresh_table()

    def refresh_active_bench(self):
        self._update_active_label()

    def _add_prober(self):
        source = self._bench_var.get()
        if not source:
            messagebox.showerror("No Bench", "No existing prober to copy from.")
            return
        name = simpledialog.askstring(
            "Add Prober",
            f"New prober name (starts as a copy of {source!r} - "
            "every instrument, address, and model comes along, ready to edit):",
            parent=self)
        if not name:
            return
        name = name.strip()
        try:
            accretech_profiles.add_profile(name, based_on=source)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Add Prober Failed", str(exc))
            return
        self._log(f"[SETUP] Added Accretech prober {name!r} (copy of {source!r})")
        try:
            from wafer_map_view import clone_bench_recipes
            cloned = clone_bench_recipes(source, name)
        except Exception as exc:
            cloned = []
            self._log(f"[SETUP] Could not clone {source!r}'s recipes to {name!r}: {exc}")
        if cloned:
            by_file = {}
            for path, recipe_name in cloned:
                by_file.setdefault(path, []).append(recipe_name)
            for path, names in by_file.items():
                card = os.path.splitext(os.path.basename(path))[0]
                self._log(f"[SETUP]   {card}: {', '.join(names)}")
            self._log(f"[SETUP] Cloned {len(cloned)} recipe(s) from {source!r} to {name!r}.")
        self._bench_var.set(name)
        self._refresh_benches()

    def _rename_prober(self):
        old = self._bench_var.get()
        if not old:
            messagebox.showerror("No Bench", "No prober selected to rename.")
            return
        new = simpledialog.askstring(
            "Rename Prober",
            f"New name for {old!r}:",
            parent=self, initialvalue=old)
        if not new:
            return
        new = new.strip()
        if new == old:
            return
        try:
            accretech_profiles.rename_profile(old, new)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Rename Failed", str(exc))
            return
        self._log(f"[SETUP] Renamed Accretech prober {old!r} -> {new!r}")
        self._bench_var.set(new)
        self._refresh_benches()
        try:
            self.controller._refresh_bench_picker()
        except Exception:
            pass

    def _delete_prober(self):
        name = self._bench_var.get()
        if not name:
            messagebox.showerror("No Bench", "No prober selected to delete.")
            return
        if len(accretech_profiles.profile_names()) <= 1:
            messagebox.showerror("Delete Failed",
                                 "This is the only prober profile - at least one must remain.")
            return
        if not messagebox.askyesno(
                "Delete Prober",
                f"Delete prober profile {name!r}?\n\n"
                "This removes its instrument addresses/models from "
                "accretech_probers.yaml. Recipes and switch wiring already "
                "tagged with this bench name are left as they are.",
                parent=self):
            return
        try:
            accretech_profiles.remove_profile(name)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Delete Failed", str(exc))
            return
        self._log(f"[SETUP] Deleted Accretech prober {name!r}")
        self._bench_var.set(accretech_profiles.active_name())
        self._refresh_benches()
        try:
            self.controller._refresh_bench_picker()
        except Exception:
            pass


    def _build_table(self):
        frame = ttk.Frame(self, padding=(6, 0, 6, 6))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        cols = ("key", "model", "name", "address", "timeout", "fitted")
        self._tree = ttk.Treeview(frame, columns=cols, show="headings",
                                  selectmode="browse")
        heads = [("key", "Slot", 100), ("model", "Model", 190),
                 ("name", "Instrument", 170), ("address", "GPIB Address", 190),
                 ("timeout", "Timeout (ms)", 90), ("fitted", "Fitted", 55)]
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
        ttk.Button(btns, text="✎ Edit Selected…", command=self._edit_selected).pack(
            side="left")
        ttk.Button(btns, text="＋ Add Instrument…", command=self._add_instrument).pack(
            side="left", padx=(6, 0))
        self._remove_btn = ttk.Button(btns, text="🗑 Remove", command=self._remove_instrument)
        self._remove_btn.pack(side="left", padx=(6, 0))
        self._tree.bind("<<TreeviewSelect>>", lambda _e: self._update_remove_state())

    def _refresh_table(self):
        self._tree.delete(*self._tree.get_children())
        bench = self._bench_var.get()
        if not bench:
            return
        try:
            inst = accretech_profiles.instruments(bench)
            keys = accretech_profiles.all_keys(bench)
        except KeyError:
            return
        for key in keys:
            entry = inst.get(key)
            if not entry:
                continue
            self._tree.insert("", "end", iid=key, values=(
                _KEY_LABELS.get(key, key),
                entry.get("model", accretech_profiles.DEFAULT_MODEL.get(
                    key, accretech_profiles.GENERIC_MODEL)),
                entry.get("name", key), entry.get("address", ""),
                entry.get("timeout_ms", 3000),
                "yes" if entry.get("fitted", True) else "no"))
        self._update_remove_state()

    def _selected_key(self):
        sel = self._tree.selection()
        return sel[0] if sel else None

    def _update_remove_state(self):
        key = self._selected_key()
        removable = bool(key) and key not in accretech_profiles.MANDATORY_KEYS
        self._remove_btn.config(state="normal" if removable else "disabled")

    def _edit_selected(self):
        bench = self._bench_var.get()
        key = self._selected_key()
        if not bench or not key:
            return
        entry = accretech_profiles.instruments(bench).get(key, {})
        dlg = _InstrumentDialog(
            self, title=f"Edit {_KEY_LABELS.get(key, key)} on {bench!r}", key=key,
            initial=(entry.get("model", accretech_profiles.DEFAULT_MODEL.get(
                        key, accretech_profiles.GENERIC_MODEL)),
                    entry.get("name", key), entry.get("address", ""),
                    entry.get("timeout_ms", 3000), entry.get("fitted", True)))
        self.wait_window(dlg)
        if dlg.result is None:
            return
        model, name, address, timeout_ms, fitted = dlg.result
        try:
            accretech_profiles.set_instrument(
                bench, key, name=name, address=address,
                timeout_ms=timeout_ms, model=model, fitted=fitted)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Edit Failed", str(exc))
            return
        self._log(f"[SETUP] {bench}: updated {_KEY_LABELS.get(key, key)} "
                  f"({model}, {name!r} @ {address}, fitted={fitted})")
        self._after_edit(bench)

    def _add_instrument(self):
        bench = self._bench_var.get()
        if not bench:
            messagebox.showerror("No Bench", "Pick a prober bench first.")
            return
        dlg = _InstrumentDialog(
            self, title=f"Add Instrument to {bench!r}", key="__new__",
            initial=(accretech_profiles.GENERIC_MODEL, "", "", 3000, True))
        self.wait_window(dlg)
        if dlg.result is None:
            return
        model, name, address, timeout_ms, fitted = dlg.result
        try:
            key = accretech_profiles.add_instrument(
                bench, name.strip(), address=address, timeout_ms=timeout_ms,
                fitted=fitted)
            if model != accretech_profiles.GENERIC_MODEL:
                accretech_profiles.set_instrument(bench, key, model=model)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Add Failed", str(exc))
            return
        self._log(f"[SETUP] {bench}: added instrument {name!r} (slot {key!r}, {model})")
        self._after_edit(bench)
        self._tree.selection_set(key)

    def _remove_instrument(self):
        bench = self._bench_var.get()
        key = self._selected_key()
        if not bench or not key:
            return
        if key in accretech_profiles.MANDATORY_KEYS:
            return
        entry = accretech_profiles.instruments(bench).get(key, {})
        if not messagebox.askyesno(
                "Remove Instrument",
                f"Remove {entry.get('name', key)!r} from {bench!r}?"):
            return
        try:
            accretech_profiles.remove_instrument(bench, key)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Remove Failed", str(exc))
            return
        self._log(f"[SETUP] {bench}: removed instrument {key!r}")
        self._after_edit(bench)

    def _after_edit(self, bench: str):
        self._refresh_table()
        if bench == accretech_profiles.active_name():
            try:
                accretech_profiles.apply_to_instruments_yaml(bench)
            except Exception as exc:
                self._log(f"[SETUP] Could not push {bench!r} into "
                          f"instruments.yaml: {exc}")


class _InstrumentDialog(tk.Toplevel):

    def __init__(self, parent, title: str, key: str, initial: tuple):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        model0, name0, addr0, timeout0, fitted0 = initial

        row = 0
        ttk.Label(body, text="Model:").grid(row=row, column=0, sticky="e", pady=3)
        self._model_var = tk.StringVar(value=model0)
        choices = accretech_profiles.model_choices_for(key)
        model_cb = ttk.Combobox(body, textvariable=self._model_var,
                                state="readonly" if len(choices) > 1 else "disabled",
                                values=choices, width=29)
        model_cb.grid(row=row, column=1, sticky="w", padx=6, pady=3)

        row += 1
        ttk.Label(body, text="Name:").grid(row=row, column=0, sticky="e", pady=3)
        self._name_var = tk.StringVar(value=name0)
        ttk.Entry(body, textvariable=self._name_var, width=32).grid(
            row=row, column=1, sticky="w", padx=6, pady=3)

        row += 1
        ttk.Label(body, text="Address:").grid(row=row, column=0, sticky="e", pady=3)
        self._addr_var = tk.StringVar(value=addr0)
        ttk.Entry(body, textvariable=self._addr_var, width=32).grid(
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
                       row=row, column=0, columnspan=2, sticky="w", pady=3)

        row += 1
        btns = ttk.Frame(body)
        btns.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(btns, text="OK", command=self._on_ok).pack(side="left")

        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        x = max(0, px + (pw - w) // 2)
        y = max(0, py + (ph - h) // 2)
        self.geometry(f"+{x}+{y}")
        self.lift()
        self.focus_force()
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _on_ok(self):
        name = self._name_var.get().strip()
        address = self._addr_var.get().strip()
        if not name or not address:
            messagebox.showerror("Missing Info", "Name and address are required.",
                                 parent=self)
            return
        try:
            timeout_ms = int(self._timeout_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Timeout", "Timeout (ms) must be a whole number.",
                                 parent=self)
            return
        self.result = (self._model_var.get(), name, address, timeout_ms,
                       self._fitted_var.get())
        self.destroy()
