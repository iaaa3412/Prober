import json
import os
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import electroglas_pma as egpma

PMA_SOURCE_SUBDIR = "pma_source"
PMA_DEFAULTS_FILENAME = "pma_source_defaults.json"


class PmaProcessPanel(ttk.Frame):
    def __init__(self, parent, controller, main_layout):
        super().__init__(parent)
        self.controller = controller
        self._main_layout = main_layout
        self._pma_path = ""
        self._fields = {}
        self._touchdowns = []
        self._pma_choices = []
        self._xls_choices = []

        self.recipe_name_var = tk.StringVar()
        self._production_die_var = tk.StringVar(value="—")
        self._pma_picker_var = tk.StringVar()
        self._xls_picker_var = tk.StringVar()

        # MM Pitch - calculated ONCE from the loaded file (major: shot-to-
        # shot spacing from the .xls's own touchdown positions; minor:
        # within-shot die spacing from the .PMA's DieSizeX/Y), then left
        # editable - the operator can correct either before it is ever
        # wired into a real MM move. See _calc_mm_pitch.
        self._mm_major_x_var = tk.StringVar(value="")
        self._mm_major_y_var = tk.StringVar(value="")
        self._mm_minor_x_var = tk.StringVar(value="")
        self._mm_minor_y_var = tk.StringVar(value="")

        self.rowconfigure(3, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_toolbar()
        self._build_source_picker()
        self._build_info_section()
        self._build_body()

    def _log(self, msg: str):
        self.controller.log(msg)

    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        # A .PMA is a one-time import onto the Wafer Builder tab now,
        # nothing more - LOAD ALL used to also build a recipe and adopt
        # the touchdown list straight onto the Run tab, but that made this
        # tab an ongoing dependency instead of a source a project only
        # ever needs once. The recipe and the touchdown list are built by
        # hand on the Recipe tab against the map this produces, same as
        # any Accretech recipe is.
        self._load_all_btn = ttk.Button(bar, text="⚙  LOAD ALL",
                                        command=self.load_all)
        self._load_all_btn.pack(side="left")
        ttk.Label(bar, text="build a Wafer Builder map from the selected PMA "
                            "- review it there, then Save Wafer Map yourself",
                  foreground="#6b7280", font=("Arial", 8)).pack(side="left", padx=(8, 0))
        self._path_lbl = ttk.Label(bar, text="No PMA file loaded", foreground="gray")
        self._path_lbl.pack(side="left", padx=10)

    def _build_source_picker(self):
        bar = ttk.LabelFrame(
            self, text=f"ATA Folder Source ({PMA_SOURCE_SUBDIR}\\)", padding=6)
        bar.grid(row=1, column=0, sticky="ew", padx=6, pady=(2, 4))

        ttk.Label(bar, text="PMA:").pack(side="left")
        self._pma_picker = ttk.Combobox(
            bar, textvariable=self._pma_picker_var, state="readonly", width=28)
        self._pma_picker.pack(side="left", padx=(4, 2))
        self._pma_picker.bind("<<ComboboxSelected>>", self._on_pma_picked)
        # Each browse button sits with the dropdown it feeds: it adds a file to
        # that dropdown's list, which was not obvious with both stranded on a
        # separate toolbar.
        ttk.Button(bar, text="📥 Load…", command=self._load_pma).pack(
            side="left", padx=(0, 2))
        ttk.Button(bar, text="🗑", width=3, command=self._delete_pma).pack(
            side="left", padx=(0, 12))

        ttk.Label(bar, text="Recipe Generator:").pack(side="left")
        self._xls_picker = ttk.Combobox(
            bar, textvariable=self._xls_picker_var, state="readonly", width=28)
        self._xls_picker.pack(side="left", padx=(4, 2))
        self._xls_picker.bind("<<ComboboxSelected>>", self._on_xls_picked)
        ttk.Button(bar, text="📥 Load…", command=self._open_recipe_generator).pack(
            side="left", padx=(0, 2))
        ttk.Button(bar, text="🗑", width=3, command=self._delete_xls).pack(
            side="left", padx=(0, 12))

        # One default, not two. The PMA and the workbook describe the same
        # wafer and are only correct together - defaulting them separately let
        # a folder come up with a PMA from one product and a workbook from
        # another, which reads as a working setup until the die IDs disagree.
        ttk.Button(bar, text="⭐ Set Both as Default",
                   command=self._set_defaults).pack(side="left")

    def _build_info_section(self):
        """One read-only table for everything that used to be spread over
        three separate LabelFrames (Run Setup, Wafer Info, Align Site) - the
        operator never typed into any of those that actually did anything
        (Operator/Process Step/Prober Name/Wafer Size/Test Die # were never
        read by anything downstream; Lot ID/Wafer ID are the real toolbar
        StringVars, editable there, not here), so there was no reason for
        this tab to offer its own copy of edit boxes. This just shows what
        LOAD ALL/the .PMA/the recipe generator actually produced."""
        lf = ttk.LabelFrame(self, text="Wafer / Run Info", padding=6)
        lf.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 4))
        lf.columnconfigure(0, weight=1)

        self._info_tree = ttk.Treeview(
            lf, columns=("field", "value"), show="headings", height=8,
            selectmode="none")
        self._info_tree.heading("field", text="Field")
        self._info_tree.heading("value", text="Value")
        self._info_tree.column("field", width=200, anchor="w")
        self._info_tree.column("value", width=460, anchor="w")
        self._info_tree.grid(row=0, column=0, sticky="ew")
        self._info_tree.tag_configure("mismatch", foreground="#b91c1c")
        self._info_tree.tag_configure("normal", foreground="#111827")

        self._info_rows = {}
        for key, label in (
            ("lot_id", "Lot ID"),
            ("wafer_id", "Wafer ID"),
            ("recipe_name", "Recipe Name (matched)"),
            ("touchdowns", "Touchdowns"),
            ("align_die", "Align Die (Recipe Gen)"),
            ("align_td", "Touchdown at Align Site"),
            ("align_offset", "Offset to First Touchdown"),
            ("align_source", "Align Source"),
        ):
            self._info_rows[key] = self._info_tree.insert(
                "", "end", values=(label, "—"), tags=("normal",))

        self._set_info("lot_id", self._main_layout.lot_id.get() or "—")
        self._set_info("wafer_id", self._main_layout.wafer_id_var.get() or "—")
        self._main_layout.lot_id.trace_add(
            "write", lambda *a: self._set_info(
                "lot_id", self._main_layout.lot_id.get() or "—"))
        self._main_layout.wafer_id_var.trace_add(
            "write", lambda *a: self._set_info(
                "wafer_id", self._main_layout.wafer_id_var.get() or "—"))
        self.recipe_name_var.trace_add(
            "write", lambda *a: self._set_info(
                "recipe_name", self.recipe_name_var.get() or "—"))
        self._production_die_var.trace_add(
            "write", lambda *a: self._set_info(
                "touchdowns", self._production_die_var.get()))

    def _set_info(self, key: str, value: str, tag: str = "normal"):
        iid = self._info_rows.get(key)
        if iid is None:
            return
        self._info_tree.set(iid, "value", value)
        self._info_tree.item(iid, tags=(tag,))

    def _workbook_align_die(self) -> str:
        """The 'Align Die' cell from the recipe-generator workbook, if loaded.

        The workbook lives on the PMA Wafer tab, so it may be loaded before or
        after the .PMA - hence the re-read in refresh_align_site() on load.
        """
        wafer = getattr(self._main_layout, "pma_wafer", None)
        if wafer is None:
            return ""
        # Only the wafer-defining sources. workbook_data is whichever source
        # the Wafer Map tab is displaying, so it can be the .PMA's own data.
        for attr in ("_xls_shot_data", "_csv_shot_data"):
            data = getattr(wafer, attr, None)
            if isinstance(data, dict) and data.get("align_die"):
                return str(data["align_die"])
        return ""

    def refresh_align_site(self):
        if not self._fields:
            self._set_info("align_die", "—")
            self._set_info("align_td", "—")
            self._set_info("align_offset", "—")
            self._set_info("align_source",
                           "Load a .PMA file (and a recipe generator .xls "
                           "for the die ID).")
            return

        align_die = self._workbook_align_die()
        info = egpma.align_site_info(self._fields, self._touchdowns, align_die)

        self._set_info("align_die",
                       egpma.format_quad(align_die) if info["die_ids"]
                       else "— (no recipe generator .xls loaded)")

        td = info["touchdown"]
        if td is not None:
            # The touchdown's OWN grid position, not the PMA-derived one - on
            # a mismatch those differ, and showing the derived one next to
            # the workbook's die would be actively misleading.
            grid_xy = ""
            try:
                grid_xy = (f"   grid ({td['x'] / float(self._fields['DieSizeX']):.0f},"
                          f"{td['y'] / float(self._fields['DieSizeY']):.0f})")
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
            self._set_info("align_td",
                           f"#{td['seq']}  {egpma.format_quad(td['device_id'])}{grid_xy}")
        elif info["quad"] is not None:
            self._set_info("align_td",
                           f"grid ({info['quad'][0]:.0f},{info['quad'][1]:.0f}) "
                           f"— no touchdown probes the align site")
        else:
            self._set_info("align_td", "— (PMA has no align-site offset)")

        if info["offset_um"]:
            ox, oy = info["offset_um"]
            self._set_info("align_offset",
                           f"X {egpma.fmt_num(ox)} um, Y {egpma.fmt_num(oy)} um")
        else:
            self._set_info("align_offset", "—")

        if info["agree"] is False:
            self._set_info(
                "align_source",
                f"MISMATCH: the workbook names #{info['named_touchdown']['seq']} "
                f"but the PMA offset points at #{info['quad_touchdown']['seq']} — "
                f"using the workbook.", tag="mismatch")
        else:
            note = "  (both sources agree)" if info["agree"] else ""
            self._set_info(
                "align_source",
                f"Source: {info['source'] or 'unknown'}{note}   "
                f"— the operator aligns and lands the chuck here; the Run tab "
                f"anchors from it.")

    def _build_body(self):
        split = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        split.grid(row=3, column=0, sticky="nsew", padx=6, pady=(0, 6))

        fields_lf = ttk.LabelFrame(split, text="Parsed PMA Fields", width=320)
        split.add(fields_lf, weight=0)
        fields_lf.pack_propagate(False)
        cols = ("field", "value")
        self._fields_tree = ttk.Treeview(
            fields_lf, columns=cols, show="headings", height=16, selectmode="browse")
        self._fields_tree.heading("field", text="Field")
        self._fields_tree.heading("value", text="Value")
        self._fields_tree.column("field", width=170)
        self._fields_tree.column("value", width=140)
        vsb1 = ttk.Scrollbar(fields_lf, orient="vertical", command=self._fields_tree.yview)
        self._fields_tree.configure(yscrollcommand=vsb1.set)
        vsb1.pack(side="right", fill="y")
        self._fields_tree.pack(fill="both", expand=True, padx=(4, 0), pady=4)

        move_lf = ttk.LabelFrame(split, text="Move List (G / J sequence)")
        split.add(move_lf, weight=1)
        cols2 = ("step", "command", "device_ids", "major_x", "major_y",
                "minor_x", "minor_y")
        self._move_tree = ttk.Treeview(
            move_lf, columns=cols2, show="headings", height=16, selectmode="browse")
        for cid, text, w in (("step", "#", 40), ("command", "Cmd", 40),
                             ("device_ids", "Device ID(s)", 110),
                             ("major_x", "MovesMajorX", 85), ("major_y", "MovesMajorY", 85),
                             ("minor_x", "MovesMinorX", 85), ("minor_y", "MovesMinorY", 85)):
            self._move_tree.heading(cid, text=text)
            self._move_tree.column(cid, width=w, anchor="center" if cid in
                                   ("step", "command") else "w")
        vsb2 = ttk.Scrollbar(move_lf, orient="vertical", command=self._move_tree.yview)
        self._move_tree.configure(yscrollcommand=vsb2.set)
        vsb2.pack(side="right", fill="y")
        self._move_tree.pack(fill="both", expand=True, padx=(4, 0), pady=4)

        # The micron pitch "microns (MM)" motion mode (eg_pma_run_panel's
        # Chuck Position radio button) actually uses at runtime - not a GUI
        # setting, but whatever the loaded .xls's own MajorMoves header
        # cells say (pma_wafer_panel.read_moves_grid). Shown here so that
        # number is visible somewhere instead of only living inside
        # eg_pma_run._touchdowns[i]["x"/"y"], read straight from there (the
        # SAME list _move_um walks) rather than re-deriving it, so this
        # table can never disagree with what a real MM run would do.
        mm_lf = ttk.LabelFrame(split, text="Move MM (µm, from the .xls)")
        split.add(mm_lf, weight=1)

        pitch_bar = ttk.Frame(mm_lf)
        pitch_bar.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(pitch_bar, text="MM Pitch (µm) — calculated once, editable:",
                 font=("Segoe UI", 8, "bold")).grid(
                 row=0, column=0, columnspan=5, sticky="w")
        ttk.Label(pitch_bar, text="Major X:").grid(row=1, column=0, sticky="e")
        ttk.Entry(pitch_bar, textvariable=self._mm_major_x_var, width=8).grid(
            row=1, column=1, padx=(2, 8))
        ttk.Label(pitch_bar, text="Major Y:").grid(row=1, column=2, sticky="e")
        ttk.Entry(pitch_bar, textvariable=self._mm_major_y_var, width=8).grid(
            row=1, column=3, padx=(2, 8))
        ttk.Label(pitch_bar, text="Minor X:").grid(row=2, column=0, sticky="e")
        ttk.Entry(pitch_bar, textvariable=self._mm_minor_x_var, width=8).grid(
            row=2, column=1, padx=(2, 8), pady=(2, 0))
        ttk.Label(pitch_bar, text="Minor Y:").grid(row=2, column=2, sticky="e")
        ttk.Entry(pitch_bar, textvariable=self._mm_minor_y_var, width=8).grid(
            row=2, column=3, padx=(2, 8), pady=(2, 0))
        ttk.Button(pitch_bar, text="↻ Recalculate",
                  command=self._calc_mm_pitch).grid(row=1, column=4, rowspan=2,
                                                     padx=(6, 0))
        ttk.Label(pitch_bar,
                 text="Major = shot-to-shot spacing (from the .xls). Minor = "
                      "within-shot die spacing (DieSizeX/Y ÷ shot dims, from "
                      "the .PMA). Not wired into a real move yet.",
                 font=("Segoe UI", 7), foreground="#6b7280", wraplength=420,
                 justify="left").grid(row=3, column=0, columnspan=5, sticky="w",
                                     pady=(2, 0))

        cols3 = ("step", "seq", "device_ids", "x_um", "y_um", "dx_um", "dy_um")
        self._move_mm_tree = ttk.Treeview(
            mm_lf, columns=cols3, show="headings", height=16, selectmode="browse")
        for cid, text, w in (("step", "#", 40), ("seq", "Seq", 50),
                             ("device_ids", "Device ID(s)", 110),
                             ("x_um", "X (µm)", 85), ("y_um", "Y (µm)", 85),
                             ("dx_um", "ΔX (µm)", 75), ("dy_um", "ΔY (µm)", 75)):
            self._move_mm_tree.heading(cid, text=text)
            self._move_mm_tree.column(cid, width=w, anchor="center" if cid in
                                      ("step", "seq") else "w")
        vsb3 = ttk.Scrollbar(mm_lf, orient="vertical", command=self._move_mm_tree.yview)
        self._move_mm_tree.configure(yscrollcommand=vsb3.set)
        vsb3.pack(side="right", fill="y")
        self._move_mm_tree.pack(fill="both", expand=True, padx=(4, 0), pady=4)

    def _pma_source_dir(self) -> str:
        folder = getattr(self._main_layout, "_ata_folder", "")
        return os.path.join(folder, PMA_SOURCE_SUBDIR) if folder else ""

    def _list_pma_source_files(self):
        src_dir = self._pma_source_dir()
        pma_files, xls_files = [], []
        if src_dir and os.path.isdir(src_dir):
            for fname in sorted(os.listdir(src_dir)):
                path = os.path.join(src_dir, fname)
                if not os.path.isfile(path):
                    continue
                low = fname.lower()
                if low.endswith(".pma"):
                    pma_files.append(path)
                elif low.endswith(".xls"):
                    xls_files.append(path)
        return pma_files, xls_files

    def _refresh_pickers(self):
        pma_files, xls_files = self._list_pma_source_files()
        self._pma_choices = pma_files
        self._pma_picker.config(values=[""] + [os.path.basename(p) for p in pma_files])
        self._xls_choices = xls_files
        self._xls_picker.config(values=[""] + [os.path.basename(p) for p in xls_files])

    def _defaults_path(self) -> str:
        src_dir = self._pma_source_dir()
        return os.path.join(src_dir, PMA_DEFAULTS_FILENAME) if src_dir else ""

    def _load_defaults(self) -> dict:
        path = self._defaults_path()
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass
        return {}

    def _save_defaults(self, data: dict):
        src_dir = self._pma_source_dir()
        if not src_dir:
            return
        try:
            os.makedirs(src_dir, exist_ok=True)
            with open(self._defaults_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            self._log(f"[PMA] Could not save default source selection: {exc}")

    def _set_defaults(self):
        """Default the PMA and the workbook together, as one pairing."""
        pma, xls = self._pma_picker_var.get(), self._xls_picker_var.get()
        if not pma and not xls:
            self._log("[PMA] Pick a PMA file and a recipe generator file first.")
            return
        if not (pma and xls) and not messagebox.askokcancel(
                "Set default",
                f"Only the {'PMA' if pma else 'recipe generator'} file is "
                "selected.\n\nThese two describe the same wafer and are meant "
                "to be defaulted as a pair. Set the default with the other "
                "half empty?"):
            return
        defaults = self._load_defaults()
        defaults["pma"], defaults["xls"] = pma, xls
        self._save_defaults(defaults)
        self._log(f"[PMA] Default set to PMA '{pma or '—'}' + recipe generator "
                  f"'{xls or '—'}' — both auto-load whenever this ATA folder "
                  "is opened.")

    def scan_ata_folder(self):
        self._refresh_pickers()
        pma_files, xls_files = self._pma_choices, self._xls_choices
        defaults = self._load_defaults()

        pma_default = next(
            (p for p in pma_files if os.path.basename(p) == defaults.get("pma")), None)
        if pma_default:
            self._pma_picker_var.set(os.path.basename(pma_default))
            self.load_path(pma_default)
        elif len(pma_files) == 1:
            self._pma_picker_var.set(os.path.basename(pma_files[0]))
            self.load_path(pma_files[0])
        else:
            self._pma_picker_var.set("")
            if len(pma_files) > 1:
                self._log(f"[PMA] {len(pma_files)} .PMA file(s) found in "
                          f"{PMA_SOURCE_SUBDIR}\\ — pick one from the PMA dropdown "
                          "(or Set Default to auto-load it next time).")

        xls_default = next(
            (p for p in xls_files if os.path.basename(p) == defaults.get("xls")), None)
        if xls_default:
            self._xls_picker_var.set(os.path.basename(xls_default))
            self._load_recipe_generator_path(xls_default)
        elif len(xls_files) == 1:
            self._xls_picker_var.set(os.path.basename(xls_files[0]))
            self._load_recipe_generator_path(xls_files[0])
        else:
            self._xls_picker_var.set("")
            if len(xls_files) > 1:
                self._log(f"[PMA] {len(xls_files)} recipe-generator .xls file(s) found "
                          f"in {PMA_SOURCE_SUBDIR}\\ — pick one from the Recipe "
                          "Generator dropdown (or Set Default to auto-load it next time).")


    def _copy_if_missing(self, src: str, dest_dir: str) -> str:
        if not os.path.isfile(src):
            return src
        if os.path.abspath(os.path.dirname(src)) == os.path.abspath(dest_dir):
            return src
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            self._log(f"[PMA] Could not create {PMA_SOURCE_SUBDIR}\\: {exc}")
            return src
        dest = os.path.join(dest_dir, os.path.basename(src))
        if not os.path.exists(dest):
            try:
                shutil.copy2(src, dest)
                self._log(f"[PMA] Copied {os.path.basename(src)} → {PMA_SOURCE_SUBDIR}\\")
            except OSError as exc:
                self._log(f"[PMA] Could not copy {os.path.basename(src)} to {PMA_SOURCE_SUBDIR}\\: {exc}")
                return src
        return dest

    def _ensure_recipe_gen_in_pma_source(self, path: str) -> str:
        src_dir = self._pma_source_dir()
        if not src_dir:
            return path
        return self._copy_if_missing(path, src_dir)

    def _ensure_pma_set_in_pma_source(self, path: str) -> str:
        src_dir = self._pma_source_dir()
        if not src_dir:
            return path
        new_path = self._copy_if_missing(path, src_dir)
        try:
            fields = egpma.parse_pma_file(path)
        except OSError:
            return new_path
        for sib in egpma.sibling_file_paths(path, fields):
            self._copy_if_missing(sib, src_dir)
        return new_path

    def _delete_pma(self):
        name = self._pma_picker_var.get()
        path = next((p for p in self._pma_choices if os.path.basename(p) == name), None)
        if not path:
            self._log("[PMA] No PMA file selected to delete.")
            return
        if not messagebox.askyesno(
            "Delete PMA File",
            f"Delete '{name}' and its .PMV/.PMS moveset files from "
            f"{PMA_SOURCE_SUBDIR}\\?\nThis cannot be undone."
        ):
            return
        try:
            fields = egpma.parse_pma_file(path)
            for sib in egpma.sibling_file_paths(path, fields):
                if os.path.isfile(sib):
                    try:
                        os.remove(sib)
                    except OSError:
                        pass
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError as exc:
            self._log(f"[PMA] Could not delete {name}: {exc}")
            return
        if self._pma_path == path:
            self._clear_pma()
        defaults = self._load_defaults()
        if defaults.get("pma") == name:
            del defaults["pma"]
            self._save_defaults(defaults)
        self._log(f"[PMA] Deleted {name} from {PMA_SOURCE_SUBDIR}\\")
        self._refresh_pickers()
        self._pma_picker_var.set("")

    def _delete_xls(self):
        name = self._xls_picker_var.get()
        path = next((p for p in self._xls_choices if os.path.basename(p) == name), None)
        if not path:
            self._log("[PMA] No recipe generator file selected to delete.")
            return
        if not messagebox.askyesno(
            "Delete Recipe Generator",
            f"Delete '{name}' from {PMA_SOURCE_SUBDIR}\\?\nThis cannot be undone."
        ):
            return
        try:
            os.remove(path)
        except OSError as exc:
            self._log(f"[PMA] Could not delete {name}: {exc}")
            return
        pma_wafer = getattr(self._main_layout, "pma_wafer", None)
        xls_data = getattr(pma_wafer, "_xls_shot_data", None) if pma_wafer else None
        if pma_wafer is not None and xls_data and xls_data.get("path") == path:
            self._clear_xls()
        defaults = self._load_defaults()
        if defaults.get("xls") == name:
            del defaults["xls"]
            self._save_defaults(defaults)
        self._log(f"[PMA] Deleted {name} from {PMA_SOURCE_SUBDIR}\\")
        self._refresh_pickers()
        self._xls_picker_var.set("")

    def _clear_pma(self):
        self._pma_path = ""
        self._fields = {}
        self._touchdowns = []
        self._move_list = []
        self._fields_tree.delete(*self._fields_tree.get_children())
        self._move_tree.delete(*self._move_tree.get_children())
        self._path_lbl.config(text="No PMA file loaded", foreground="gray")
        self._production_die_var.set("—")
        self.recipe_name_var.set("")
        self.refresh_align_site()
        pma_wafer = getattr(self._main_layout, "pma_wafer", None)
        if pma_wafer is not None:
            pma_wafer.clear_pma_source()

    def _clear_xls(self):
        pma_wafer = getattr(self._main_layout, "pma_wafer", None)
        if pma_wafer is not None:
            pma_wafer.clear_xls_source()
        self.refresh_align_site()

    def _on_pma_picked(self, _evt=None):
        name = self._pma_picker_var.get()
        if not name:
            self._clear_pma()
            return
        path = next((p for p in self._pma_choices if os.path.basename(p) == name), None)
        if path:
            self.load_path(path)

    def _on_xls_picked(self, _evt=None):
        name = self._xls_picker_var.get()
        if not name:
            self._clear_xls()
            return
        path = next((p for p in self._xls_choices if os.path.basename(p) == name), None)
        if path:
            self._load_recipe_generator_path(path)

    def _load_pma(self):
        path = filedialog.askopenfilename(
            title="Load PMA File",
            filetypes=[("PMA recipe files", "*.PMA *.pma"), ("All files", "*.*")])
        if not path:
            return
        path = self._ensure_pma_set_in_pma_source(path)
        self._refresh_pickers()
        self._pma_picker_var.set(os.path.basename(path))
        self.load_path(path)

    def _open_recipe_generator(self):
        pma_wafer = getattr(self._main_layout, "pma_wafer", None)
        if pma_wafer is None:
            self._log("[PMA] PMA Wafer tab is not available.")
            return
        path = filedialog.askopenfilename(
            title="Open Recipe Generator (.xls)",
            filetypes=[("Excel 97-2003 Workbook", "*.xls"), ("All files", "*.*")])
        if not path:
            return
        path = self._ensure_recipe_gen_in_pma_source(path)
        self._refresh_pickers()
        self._xls_picker_var.set(os.path.basename(path))
        pma_wafer.load_workbook_path(path)
        self.refresh_align_site()

    def _load_recipe_generator_path(self, path: str):
        pma_wafer = getattr(self._main_layout, "pma_wafer", None)
        if pma_wafer is None:
            self._log("[PMA] PMA Wafer tab is not available.")
            return
        pma_wafer.load_workbook_path(path)
        self.refresh_align_site()

    def load_all(self):
        """PMA -> a Wafer Builder map. Nothing else.

        This is the ONLY thing PMA import does now - the Run tab, a
        recipe, and the probe card's saved touchdown list are none of
        them touched here. Mirrors recipe_gen_panel.RecipeGenPanel's own
        Import PMA button (_import_pma) exactly, just reading the file
        this tab already has selected (self._pma_path) instead of
        prompting for one again: parse into local variables, hand them to
        load_touchdowns_as_map, done - nothing kept afterward. The Wafer
        Builder map stays in memory on that tab until the operator
        reviews it and presses ITS OWN "Save Wafer Map" button; only that
        explicit action publishes it to the Run tab, same as building a
        map by hand would.
        """
        if not self._pma_path:
            self._log("[PMA] LOAD ALL: no PMA file loaded")
            return
        gen = getattr(self._main_layout, "recipe_gen", None)
        if gen is None or not hasattr(gen, "load_touchdowns_as_map"):
            self._log("[PMA] LOAD ALL: the Wafer Builder tab is not available.")
            return
        try:
            fields = egpma.parse_pma_file(self._pma_path)
            touchdowns = egpma.load_touchdowns(self._pma_path, fields)
        except Exception as exc:
            self._log(f"[PMA] LOAD ALL: could not read "
                      f"{os.path.basename(self._pma_path)}: {exc}")
            return
        if not touchdowns:
            self._log("[PMA] LOAD ALL: no touchdowns found — are the .PMV "
                      "and .PMS siblings next to the .PMA?")
            return
        name = os.path.basename(self._pma_path)
        gen.load_touchdowns_as_map(touchdowns, name, "PMA recipe")
        self._log(f"[PMA] LOAD ALL: built a Wafer Builder map from '{name}' "
                  f"({len(touchdowns)} touchdown(s)) — review it on the Wafer "
                  "Builder tab, then Save Wafer Map when ready.")

    def _refresh_move_mm_table(self):
        """Move MM table - reads eg_pma_run._touchdowns directly (whatever
        the Run tab currently has adopted - the published Wafer Builder
        map, same as any other run) rather than re-deriving x_um/y_um
        here, since that list is the exact one _move_um ("microns (MM)"
        motion mode) walks at runtime - this table can never show a
        number a real MM run would not actually use. This tab no longer
        pushes its own parsed .PMA onto the Run tab (see load_path/
        load_all), so this reflects whatever's actually loaded there, not
        necessarily the .PMA this tab has open.
        """
        self._move_mm_tree.delete(*self._move_mm_tree.get_children())
        run = getattr(self._main_layout, "eg_pma_run", None)
        touchdowns = getattr(run, "_touchdowns", None) or []
        prev = None
        for i, t in enumerate(touchdowns):
            x_um, y_um = t.get("x", 0.0), t.get("y", 0.0)
            dx = "" if prev is None else egpma.fmt_num(x_um - prev[0])
            dy = "" if prev is None else egpma.fmt_num(y_um - prev[1])
            self._move_mm_tree.insert("", "end", values=(
                i + 1, t.get("seq", ""), t.get("device_id", ""),
                egpma.fmt_num(x_um), egpma.fmt_num(y_um), dx, dy))
            prev = (x_um, y_um)

    def _calc_mm_pitch(self):
        """Major/minor MM pitch, calculated ONCE from whatever is currently
        loaded, then left in editable Entries (see the fields themselves) -
        not re-derived per move, and not (yet) wired into a real move.

        Major: smallest positive gap between distinct shot x_um/y_um
        positions in eg_pma_run._touchdowns (the same .xls-derived list
        the Move MM table reads) - the shot-to-shot spacing.

        Minor: the .PMA's own DieSizeX/Y (the quad/shot pitch - see
        electroglas_pma's own note that DieSizeX/Y is the BLOCK pitch, not
        one die) divided by the shot's own dimensions, giving one die's
        share of it - EXCEPT when a dimension's shot size is 1 (LAMP: a
        1x1 shot), where there is no second die to step to within the shot
        at all, so that axis is forced to 0 rather than DieSizeX/1
        (=DieSizeX itself, wrong - there is no "minor" axis to divide a
        single die's own pitch across).
        """
        def _min_gap(values):
            uniq = sorted({round(v, 3) for v in values})
            gaps = [uniq[i + 1] - uniq[i] for i in range(len(uniq) - 1)
                    if uniq[i + 1] != uniq[i]]
            return min(gaps) if gaps else 0.0

        run = getattr(self._main_layout, "eg_pma_run", None)
        touchdowns = getattr(run, "_touchdowns", None) or []
        major_x = _min_gap(t.get("x", 0.0) for t in touchdowns)
        major_y = _min_gap(t.get("y", 0.0) for t in touchdowns)
        self._mm_major_x_var.set(egpma.fmt_num(major_x) if major_x else "")
        self._mm_major_y_var.set(egpma.fmt_num(major_y) if major_y else "")

        minor_x = minor_y = ""
        try:
            die_x = float(self._fields.get("DieSizeX") or 0)
            die_y = float(self._fields.get("DieSizeY") or 0)
            shot_rows, shot_cols = (run.shot_layout()
                                    if run is not None and hasattr(run, "shot_layout")
                                    else (1, 1))
            minor_x = egpma.fmt_num(die_x / shot_cols) if die_x and shot_cols > 1 else "0"
            minor_y = egpma.fmt_num(die_y / shot_rows) if die_y and shot_rows > 1 else "0"
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        self._mm_minor_x_var.set(minor_x)
        self._mm_minor_y_var.set(minor_y)

    def load_path(self, path: str):
        try:
            fields = egpma.parse_pma_file(path)
        except OSError as exc:
            self._log(f"[PMA] Error reading {os.path.basename(path)}: {exc}")
            return
        self._pma_path = path
        self._fields = fields
        self._path_lbl.config(text=path, foreground="black")

        self._fields_tree.delete(*self._fields_tree.get_children())
        for key in egpma.ALL_FIELDS:
            if key in fields:
                self._fields_tree.insert("", "end", values=(key, fields[key]))
        others = sorted(k for k in fields if k not in egpma.ALL_FIELDS)
        for key in others:
            self._fields_tree.insert("", "end", values=(key, fields[key]))

        touchdowns = egpma.load_touchdowns(path, fields)
        self._touchdowns = touchdowns

        self._production_die_var.set(str(len(touchdowns)))

        pma_wafer = getattr(self._main_layout, "pma_wafer", None)
        if pma_wafer is not None and touchdowns:
            shot_data = egpma.to_shot_data(path, fields, touchdowns)
            prior = getattr(pma_wafer, "_xls_shot_data", None) or pma_wafer.workbook_data
            if prior and prior.get("align_die"):
                shot_data["align_die"] = prior["align_die"]
            pma_wafer.show_touchdowns(shot_data)

        self.refresh_align_site()
        self._refresh_move_mm_table()
        self._calc_mm_pitch()

        # Display only from here down, same as the fields table above -
        # this tab parses a .PMA and shows it in its own tables, nothing
        # more. It used to also write a wafer-map CSV into the ATA folder,
        # auto-select/load a matching recipe on the Recipe tab, and save
        # the move list onto the active probe card - all removed: those
        # are exactly the "quietly does something every time you pick a
        # file" side effects that made a normal folder load look like it
        # was still depending on a .PMA. Building an actual Wafer Builder
        # map from a .PMA is LOAD ALL's job now, and only LOAD ALL's.
        move_list = egpma.build_move_list(touchdowns)
        self._move_list = move_list
        self._move_tree.delete(*self._move_tree.get_children())
        for m in move_list:
            self._move_tree.insert("", "end", values=(
                m["step"], m["command"], m["device_ids"],
                egpma.fmt_num(m["MovesMajorX"]), egpma.fmt_num(m["MovesMajorY"]),
                m["MovesMinorX"], m["MovesMinorY"]))

        self._log(f"[PMA] Loaded {os.path.basename(path)}: {len(touchdowns)} "
                  f"touchdown(s), {len(move_list)} move(s)")
