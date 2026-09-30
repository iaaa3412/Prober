from __future__ import annotations

import json
import os
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Sequence

from instruments.gpib_base import get_resource_path

_GDS_DIR = get_resource_path("gds")
if _GDS_DIR not in sys.path:
    sys.path.insert(0, _GDS_DIR)

try:
    import matplotlib
    try:
        matplotlib.use("TkAgg")
    except Exception:
        pass
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure
    from matplotlib.patches import Circle, Rectangle
    _MPL = True
except ImportError:
    _MPL = False

_CORE = False
_CORE_ERR = ""
try:
    from ata_gds_core import (
        collect_records,
        default_alignment_mark_names,
        default_die_pitch_from_summary,
        discover_layout_metadata,
        export_ata_files,
        generate_wafer_map,
        read_gds_library,
        selected_cell_references,
    )
    _CORE = True
except Exception as _e:
    _CORE_ERR = f"{type(_e).__name__}: {_e}"


class GdsParserPanel(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        self.gds_path: Optional[str] = None
        self.library: Any = None
        self.data: Dict[str, Any] = {}
        self.wafer_map_data: List[Dict[str, Any]] = []
        self.layout_metadata: Dict[str, Any] = {}
        self.layout_metadata_path: str = ""
        self.max_table_rows = 5000

        self._build_vars()
        self._build_ui()


    def _build_vars(self):
        _default_align = default_alignment_mark_names() if _CORE else "ATA_ALIGN_*, KS_LYR4, KS_Neg_LYR2"
        self.top_cell_var             = tk.StringVar()
        self.pad_layer_var            = tk.StringVar(value="")
        self.pad_datatype_var         = tk.StringVar(value="0")
        self.min_pad_size_var         = tk.StringVar(value="20")
        self.max_pad_size_var         = tk.StringVar(value="500")
        self.flatten_pads_var         = tk.BooleanVar(value=True)
        self.wafer_diameter_var       = tk.StringVar(value="200")
        self.edge_exclusion_var       = tk.StringVar(value="3")
        self.die_pitch_x_var          = tk.StringVar(value="")
        self.die_pitch_y_var          = tk.StringVar(value="")
        self.use_refs_var             = tk.BooleanVar(value=True)
        self.alignment_mark_names_var = tk.StringVar(value=_default_align)
        self.metadata_status_var      = tk.StringVar(value="No layout metadata JSON loaded")
        self.status_var               = tk.StringVar(value="Open a GDS/GDSII file to begin.")


    def _build_ui(self):
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        if not _CORE:
            msg = (
                "⚠  ata_gds_core could not be imported.\n\n"
                f"Error: {_CORE_ERR}\n\n"
                "Make sure gdstk is installed in the active interpreter:\n"
                "    .venv\\Scripts\\pip install gdstk matplotlib\n\n"
                f"GDS source dir: {_GDS_DIR}\n"
                f"Dir exists: {os.path.isdir(_GDS_DIR)}"
            )
            ttk.Label(self, text=msg, font=("Consolas", 10), justify="left",
                      foreground="red").grid(row=0, column=0, pady=40, padx=20, sticky="w")
            return

        self._build_controls()
        self._build_notebook()

        sb = ttk.Frame(self)
        sb.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 4))
        ttk.Label(sb, textvariable=self.status_var, anchor="w",
                  foreground="gray").pack(fill="x")

    def _build_controls(self):
        ctl = ttk.LabelFrame(
            self,
            text="GDS Import · ATA Convention Detection · Alignment Mark Detection · ATA Export",
            padding=6,
        )
        ctl.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))

        r1 = ttk.Frame(ctl)
        r1.pack(fill="x", pady=2)
        ttk.Button(r1, text="Open GDS File",       command=self.open_gds_file).pack(side="left", padx=(0, 4))
        ttk.Button(r1, text="Load Metadata JSON",  command=self.load_metadata_file).pack(side="left", padx=(0, 4))
        ttk.Label(r1, text="Cell:").pack(side="left")
        self.top_cell_combo = ttk.Combobox(r1, textvariable=self.top_cell_var, width=34, state="readonly")
        self.top_cell_combo.pack(side="left", padx=4)
        self.top_cell_combo.bind("<<ComboboxSelected>>", lambda _e: self.reparse_current_file())
        ttk.Button(r1, text="Parse / Refresh",     command=self.reparse_current_file).pack(side="left", padx=4)
        ttk.Button(r1, text="Generate ATA Files",  command=self.export_files).pack(side="left", padx=4)
        ttk.Button(r1, text="💾 Export App Log",   command=self.export_app_log).pack(side="left", padx=(16, 4))

        r2 = ttk.Frame(ctl)
        r2.pack(fill="x", pady=2)
        for lbl, var, w in [
            ("Pad layer:", self.pad_layer_var, 6),
            ("Pad datatype:", self.pad_datatype_var, 6),
            ("Min pad µm:", self.min_pad_size_var, 6),
            ("Max pad µm:", self.max_pad_size_var, 6),
        ]:
            ttk.Label(r2, text=lbl).pack(side="left")
            ttk.Entry(r2, textvariable=var, width=w).pack(side="left", padx=(2, 10))
        ttk.Checkbutton(r2, text="Flatten for pad extraction",
                        variable=self.flatten_pads_var).pack(side="left", padx=4)

        r3 = ttk.Frame(ctl)
        r3.pack(fill="x", pady=2)
        for lbl, var, w in [
            ("Wafer ⌀ mm:", self.wafer_diameter_var, 6),
            ("Edge excl. mm:", self.edge_exclusion_var, 6),
            ("Pitch X µm:", self.die_pitch_x_var, 9),
            ("Pitch Y µm:", self.die_pitch_y_var, 9),
        ]:
            ttk.Label(r3, text=lbl).pack(side="left")
            ttk.Entry(r3, textvariable=var, width=w).pack(side="left", padx=(2, 10))
        ttk.Checkbutton(r3, text="Use GDS refs as die locations",
                        variable=self.use_refs_var).pack(side="left", padx=4)
        ttk.Button(r3, text="Update Wafer Map",
                   command=self.update_wafer_map_and_plots).pack(side="left", padx=6)

        r4 = ttk.Frame(ctl)
        r4.pack(fill="x", pady=2)
        ttk.Label(r4, text="Alignment mark names:").pack(side="left")
        ttk.Entry(r4, textvariable=self.alignment_mark_names_var,
                  width=46).pack(side="left", padx=4)
        ttk.Label(r4, text="(comma-sep, * wildcard)", foreground="gray").pack(side="left", padx=4)

        r5 = ttk.Frame(ctl)
        r5.pack(fill="x", pady=(2, 0))
        ttk.Label(r5, text="Layout metadata:").pack(side="left")
        ttk.Label(r5, textvariable=self.metadata_status_var,
                  foreground="gray").pack(side="left", padx=4)


    def _build_notebook(self):
        self.nb = ttk.Notebook(self)
        self.nb.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 2))

        self.summary_text         = self._text_tab("Summary")
        self.cells_tree           = self._tree_tab("Cells")
        self.layers_tree          = self._tree_tab("Layers")
        self.references_tree      = self._tree_tab("References")
        self.labels_tree          = self._tree_tab("Labels")
        self.polygons_tree        = self._tree_tab("Polygons")
        self.pads_tree            = self._tree_tab("Pads")
        self.alignment_tree       = self._tree_tab("Alignment Marks")
        self.ata_alignment_tree   = self._tree_tab("ATA Alignment Marks")
        self.ata_metadata_tree    = self._tree_tab("ATA Metadata")
        self.ata_validation_tree  = self._tree_tab("ATA QA Report")
        self.ata_devices_tree     = self._tree_tab("ATA Devices/Tests")
        self.ata_channel_tree     = self._tree_tab("ATA Channel Map")
        self.layout_metadata_tree = self._tree_tab("Layout Metadata")
        self.pad_layout_tree      = self._tree_tab("ATA Pad Layout")
        self.wafer_tree           = self._tree_tab("ATA Wafer Map")

        if _MPL:
            self.device_fig, self.device_ax, self.device_canvas = self._plot_tab("Device Layout")
            self.wafer_fig,  self.wafer_ax,  self.wafer_canvas  = self._plot_tab("Wafer Map")

        self.log_text = self._text_tab("Export Log")

    def _text_tab(self, title: str) -> tk.Text:
        f = ttk.Frame(self.nb)
        txt = tk.Text(f, wrap="word", height=10, font=("Consolas", 9))
        ys = ttk.Scrollbar(f, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        txt.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.nb.add(f, text=title)
        return txt

    def _tree_tab(self, title: str) -> ttk.Treeview:
        f = ttk.Frame(self.nb)
        tree = ttk.Treeview(f, show="headings")
        ys = ttk.Scrollbar(f, orient="vertical",   command=tree.yview)
        xs = ttk.Scrollbar(f, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right",  fill="y")
        xs.pack(side="bottom", fill="x")
        self.nb.add(f, text=title)
        return tree

    def _plot_tab(self, title: str):
        f = ttk.Frame(self.nb)
        fig = Figure(figsize=(7, 5), dpi=100)
        ax  = fig.add_subplot(111)
        cv  = FigureCanvasTkAgg(fig, master=f)
        tb  = NavigationToolbar2Tk(cv, f, pack_toolbar=False)
        tb.update()
        tb.pack(side="top", fill="x")
        cv.get_tk_widget().pack(fill="both", expand=True)
        self.nb.add(f, text=title)
        return fig, ax, cv


    def export_app_log(self):
        txt = getattr(getattr(self.controller, "ui", None), "log_text", None)
        if txt is None:
            messagebox.showerror("Export App Log", "No active log panel found.")
            return
        content = txt.get("1.0", tk.END)
        path = filedialog.asksaveasfilename(
            title="Export App Log",
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
            initialfile="atomica_app_log.txt",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            messagebox.showerror("Export App Log", f"Could not write {path}: {exc}")
            return
        self._set_status(f"App log exported to {path}")
        self.controller.log("[GDS] App log exported")

    def open_gds_file(self):
        path = filedialog.askopenfilename(
            title="Open GDS/GDSII File",
            filetypes=[("GDSII files", "*.gds *.gdsii"), ("All files", "*.*")],
        )
        if not path:
            return
        self.gds_path = path
        self._set_status(f"Loading {os.path.basename(path)} …")
        self.controller.log(f"[GDS] Opening {os.path.basename(path)}")
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _load_worker(self):
        try:
            lib = read_gds_library(self.gds_path)
            self.library = lib
            metadata, meta_path = discover_layout_metadata(self.gds_path)
            data = collect_records(
                lib,
                alignment_mark_names=self.alignment_mark_names_var.get(),
                layout_metadata=metadata,
                metadata_path=meta_path,
            )
            self.after(0, lambda: self._after_load(data, metadata, meta_path))
        except Exception as exc:
            tb = traceback.format_exc()
            self.after(0, lambda e=exc, t=tb: self._show_error("Could not load GDS file", e, t))

    def _after_load(self, data, metadata, meta_path):
        self.layout_metadata      = metadata or {}
        self.layout_metadata_path = meta_path or ""
        self._apply_metadata_to_gui()
        top = data.get("top_cell_names", [])
        self.top_cell_combo.configure(values=top)
        if top:
            self.top_cell_var.set(data.get("selected_cell_name", top[0]))
        self._set_status("GDS loaded. Parsing selected cell…")
        self.reparse_current_file()

    def reparse_current_file(self):
        if self.library is None:
            messagebox.showinfo("No GDS file", "Open a GDS/GDSII file first.")
            return
        self._set_status("Parsing GDS data…")
        threading.Thread(target=self._parse_worker, daemon=True).start()

    def _parse_worker(self):
        try:
            data = collect_records(
                self.library,
                selected_cell_name=self.top_cell_var.get() or None,
                pad_layer=self._opt_int(self.pad_layer_var.get()),
                pad_datatype=self._opt_int(self.pad_datatype_var.get()),
                min_pad_size_um=self._flt(self.min_pad_size_var.get(), 0.0),
                max_pad_size_um=self._flt(self.max_pad_size_var.get(), 1e12),
                flatten_selected_for_pads=self.flatten_pads_var.get(),
                alignment_mark_names=self.alignment_mark_names_var.get(),
                layout_metadata=self.layout_metadata,
                metadata_path=self.layout_metadata_path,
            )
            self.after(0, lambda: self._after_parse(data))
        except Exception as exc:
            tb = traceback.format_exc()
            self.after(0, lambda e=exc, t=tb: self._show_error("Parse error", e, t))

    def _after_parse(self, data):
        self.data = data
        px, py = default_die_pitch_from_summary(data)
        if not self.die_pitch_x_var.get().strip() and px > 0:
            self.die_pitch_x_var.set(f"{px:.3f}")
        if not self.die_pitch_y_var.get().strip() and py > 0:
            self.die_pitch_y_var.set(f"{py:.3f}")
        self.update_wafer_map_and_plots()
        self._populate_all_views()
        s = data.get("summary", {})
        msg = (f"Parsed {s.get('cell_count',0)} cells, "
               f"{s.get('layer_count',0)} layers, "
               f"{s.get('pad_count',0)} pads, "
               f"{s.get('alignment_mark_count',0)} alignment marks.")
        self._set_status(msg)
        self.controller.log(f"[GDS] {msg}")


    def update_wafer_map_and_plots(self):
        if not self.data:
            return
        summary = self.data.get("summary", {})
        refs    = selected_cell_references(self.data)
        self.wafer_map_data = generate_wafer_map(
            selected_cell_bbox=summary.get("selected_cell_bbox"),
            selected_cell_references=refs,
            wafer_diameter_mm=self._flt(self.wafer_diameter_var.get(), 200.0),
            edge_exclusion_mm=self._flt(self.edge_exclusion_var.get(), 3.0),
            die_pitch_x_um=self._flt(self.die_pitch_x_var.get(), 0.0),
            die_pitch_y_um=self._flt(self.die_pitch_y_var.get(), 0.0),
            use_references_if_available=self.use_refs_var.get(),
        )
        self._load_tree(self.wafer_tree, self.wafer_map_data)
        if _MPL:
            self._plot_device_layout()
            self._plot_wafer_map()
        self._set_status(f"Wafer map: {len(self.wafer_map_data)} die locations.")


    def _populate_all_views(self):
        if not self.data:
            return
        self._populate_summary()
        self._load_tree(self.cells_tree,           self.data.get("cells", []))
        self._load_tree(self.layers_tree,           self.data.get("layers", []))
        self._load_tree(self.references_tree,       self.data.get("references", []))
        self._load_tree(self.labels_tree,           self.data.get("labels", []))
        self._load_tree(self.polygons_tree,         self.data.get("polygons", []))
        self._load_tree(self.pads_tree,             self.data.get("pads", []))
        self._load_tree(self.alignment_tree,        self.data.get("alignment_marks", []))
        self._load_tree(self.ata_alignment_tree,    self.data.get("ata_alignment_marks", []))
        self._load_tree(self.ata_metadata_tree,     self.data.get("ata_metadata", []))
        self._load_tree(self.ata_validation_tree,   self.data.get("ata_validation_report", []))
        device_test = (list(self.data.get("ata_devices", []))
                       + list(self.data.get("ata_test_structures", [])))
        self._load_tree(self.ata_devices_tree,      device_test)
        self._load_tree(self.ata_channel_tree,      self.data.get("ata_channel_map", []))
        self._load_tree(self.layout_metadata_tree,  self.data.get("layout_metadata", []))
        self._load_tree(self.pad_layout_tree,       self.data.get("ata_pad_layout", []))
        self._load_tree(self.wafer_tree,            self.wafer_map_data)

    def _populate_summary(self):
        from datetime import datetime
        self.summary_text.delete("1.0", tk.END)
        s = self.data.get("summary", {})
        lines = [
            "ATA Phase 1 GDSII Parse Summary",
            "================================", "",
            f"Source file  : {self.gds_path or ''}",
            f"Parsed at    : {datetime.now().isoformat(timespec='seconds')}",
            f"Selected cell: {self.data.get('selected_cell_name', '')}",
            f"GDS unit     : {s.get('unit', '')}",
            f"GDS precision: {s.get('precision', '')}", "",
            f"Cells             : {s.get('cell_count', 0)}",
            f"Top cells         : {', '.join(s.get('top_cells', []))}",
            f"Layers            : {s.get('layer_count', 0)}",
            f"Polygons          : {s.get('polygon_count', 0)}",
            f"Labels            : {s.get('label_count', 0)}",
            f"References        : {s.get('reference_count', 0)}",
            f"Pad candidates    : {s.get('pad_count', 0)}",
            f"Alignment marks   : {s.get('alignment_mark_count', 0)}",
            f"ATA_* records     : {s.get('ata_metadata_count', 0)}",
            f"ATA devices       : {s.get('ata_device_count', 0)}",
            f"ATA test structs  : {s.get('ata_test_structure_count', 0)}",
            f"Wafer map dies    : {len(self.wafer_map_data)}", "",
            "Review the ATA QA Report tab before exporting.",
            "Coordinates are in GDS user units (usually µm).",
        ]
        self.summary_text.insert(tk.END, "\n".join(lines))

    def _load_tree(self, tree: ttk.Treeview, records: Sequence[Dict[str, Any]]):
        tree.delete(*tree.get_children())
        tree["columns"] = []
        if not records:
            tree["columns"] = ["message"]
            tree.heading("message", text="message")
            tree.column("message", width=500, anchor="w")
            tree.insert("", tk.END, values=["No records to display."])
            return
        cols: List[str] = []
        for rec in records[:min(len(records), self.max_table_rows)]:
            for k in rec.keys():
                if k not in cols:
                    cols.append(k)
        tree["columns"] = cols
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=max(80, min(200, len(c) * 10)), anchor="w")
        for rec in records[:self.max_table_rows]:
            tree.insert("", tk.END, values=[self._fmt(rec.get(c, "")) for c in cols])
        if len(records) > self.max_table_rows:
            tree.insert("", tk.END, values=[
                f"Showing first {self.max_table_rows} of {len(records)} rows"
            ] + [""] * (len(cols) - 1))


    def _plot_device_layout(self):
        self.device_ax.clear()
        if not self.data:
            self.device_canvas.draw_idle()
            return
        sel      = self.data.get("selected_cell_name", "")
        polygons = [p for p in self.data.get("polygons", []) if p.get("cell") == sel]
        refs     = [r for r in self.data.get("references", []) if r.get("parent_cell") == sel]

        for poly in polygons[:10000]:
            try:
                x = float(poly.get("bbox_min_x_um", 0))
                y = float(poly.get("bbox_min_y_um", 0))
                w = float(poly.get("bbox_width_um",  0))
                h = float(poly.get("bbox_height_um", 0))
                if w > 0 and h > 0:
                    self.device_ax.add_patch(
                        Rectangle((x, y), w, h, fill=False, linewidth=0.4, alpha=0.5)
                    )
            except Exception:
                continue

        for ref in refs[:2000]:
            try:
                x = float(ref.get("bbox_min_x_um", ref.get("origin_x_um", 0)))
                y = float(ref.get("bbox_min_y_um", ref.get("origin_y_um", 0)))
                w = float(ref.get("bbox_width_um",  0))
                h = float(ref.get("bbox_height_um", 0))
                if w > 0 and h > 0:
                    self.device_ax.add_patch(
                        Rectangle((x, y), w, h, fill=False, linestyle="--", linewidth=0.8)
                    )
                else:
                    self.device_ax.plot(
                        float(ref.get("origin_x_um", 0)), float(ref.get("origin_y_um", 0)),
                        ".", ms=4,
                    )
            except Exception:
                continue

        for pad in self.data.get("pads", [])[:2000]:
            try:
                x = float(pad.get("bbox_min_x_um", 0))
                y = float(pad.get("bbox_min_y_um", 0))
                w = float(pad.get("bbox_width_um",  0))
                h = float(pad.get("bbox_height_um", 0))
                self.device_ax.add_patch(
                    Rectangle((x, y), w, h, fill=False, linewidth=1.2, edgecolor="gold")
                )
                self.device_ax.text(
                    float(pad.get("x_um", x)), float(pad.get("y_um", y)),
                    str(pad.get("pad_name", "")), fontsize=6,
                )
            except Exception:
                continue

        for mark in self.data.get("alignment_marks", [])[:200]:
            try:
                x = float(mark.get("x_um", 0))
                y = float(mark.get("y_um", 0))
                self.device_ax.plot(x, y, marker="x", markersize=10, color="red")
                self.device_ax.text(x, y, str(mark.get("mark_name", "ALIGN")), fontsize=8)
            except Exception:
                continue

        self.device_ax.set_title(f"Device Layout: {sel}")
        self.device_ax.set_xlabel("X (µm)")
        self.device_ax.set_ylabel("Y (µm)")
        self.device_ax.axis("equal")
        self.device_ax.autoscale_view()
        self.device_fig.tight_layout()
        self.device_canvas.draw_idle()

    def _plot_wafer_map(self):
        self.wafer_ax.clear()
        if not self.wafer_map_data:
            self.wafer_canvas.draw_idle()
            return
        xs, ys = [], []
        for row in self.wafer_map_data:
            try:
                xs.append(float(row.get("x_um", 0)))
                ys.append(float(row.get("y_um", 0)))
            except Exception:
                pass
        if xs and ys:
            self.wafer_ax.scatter(xs, ys, s=8)
        r_um  = self._flt(self.wafer_diameter_var.get(), 200.0) * 500.0
        er_um = max(0.0, r_um - self._flt(self.edge_exclusion_var.get(), 3.0) * 1000.0)
        self.wafer_ax.add_patch(Circle((0, 0), r_um,  fill=False, linewidth=1.0))
        self.wafer_ax.add_patch(Circle((0, 0), er_um, fill=False, linestyle="--", linewidth=0.8))
        self.wafer_ax.set_title(f"Wafer Map — {len(self.wafer_map_data)} dies")
        self.wafer_ax.set_xlabel("X (µm)")
        self.wafer_ax.set_ylabel("Y (µm)")
        self.wafer_ax.axis("equal")
        self.wafer_ax.autoscale_view()
        self.wafer_fig.tight_layout()
        self.wafer_canvas.draw_idle()


    def _ask_export_mode(self) -> Optional[str]:
        result = {"mode": None}
        dlg = tk.Toplevel(self)
        dlg.title("Generate ATA Files")
        dlg.resizable(False, False)
        dlg.transient(self.winfo_toplevel())

        ttk.Label(
            dlg, text="Create a brand-new ATA folder, or merge these files\n"
                      "into an ATA folder that already exists?\n\n"
                      "Merging overwrites only the files this export creates\n"
                      "(e.g. ata_wafer_map_gds.csv, ata_pad_layout.csv, …) — it\n"
                      "never touches anything else already in that folder\n"
                      "(probe_cards/, etc.).",
            justify="left", padding=(16, 14, 16, 8),
        ).pack()

        btns = ttk.Frame(dlg, padding=(16, 4, 16, 14))
        btns.pack(fill="x")

        def choose(mode):
            result["mode"] = mode
            dlg.destroy()

        ttk.Button(btns, text="📁 New ATA Folder…",
                  command=lambda: choose("new")).pack(fill="x", pady=2)
        ttk.Button(btns, text="🔀 Merge Into Existing ATA Folder…",
                  command=lambda: choose("merge")).pack(fill="x", pady=2)
        ttk.Button(btns, text="Cancel",
                  command=lambda: choose(None)).pack(fill="x", pady=(8, 0))

        dlg.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        dlg.update_idletasks()
        dlg.grab_set()
        dlg.wait_window()
        return result["mode"]

    def _choose_output_dir(self, mode: str) -> Optional[str]:
        if mode == "merge":
            ui = getattr(self.controller, "ui", None)
            initial = (
                (ui and getattr(ui, "_ata_folder", None))
                or (ui and ui.working_dir_var.get())
                or (os.path.dirname(self.gds_path) if self.gds_path else os.getcwd())
            )
            return filedialog.askdirectory(
                title="Select Existing ATA Folder to Merge Into", initialdir=initial)

        base = "ata_gds_export"
        if self.gds_path:
            base = os.path.splitext(os.path.basename(self.gds_path))[0] + "_ata_export"
        initial = os.path.dirname(self.gds_path) if self.gds_path else os.getcwd()
        target = filedialog.askdirectory(title="Choose Export Folder", initialdir=initial)
        if not target:
            return None
        return os.path.join(target, base)

    def export_files(self):
        if not self.data:
            messagebox.showinfo("No data", "Open and parse a GDS/GDSII file first.")
            return
        mode = self._ask_export_mode()
        if mode is None:
            return
        output_dir = self._choose_output_dir(mode)
        if not output_dir:
            return
        try:
            files = export_ata_files(
                self.data, output_dir, self.wafer_map_data, source_file=self.gds_path or ""
            )
            verb = "Merged" if mode == "merge" else "Generated"
            summary_lines = (
                f"{verb} {len(files)} ATA files in:\n{output_dir}\n\n"
                + "\n".join(f"  {k}: {os.path.basename(v)}" for k, v in files.items())
            )
            if mode == "merge":
                summary_lines += "\n\n(everything else already in that folder was left alone)"
            self._append_log(summary_lines)
            self._set_status(f"ATA export → {output_dir}")
            self.controller.log(f"[GDS] ATA export ({mode}) — {len(files)} files")

            if messagebox.askyesno(
                "ATA Files Generated",
                f"Files {'merged into' if mode == 'merge' else 'generated in'}:\n{output_dir}\n\n"
                "Load this folder into the Wafer Map, Pad Layout, and Alignment tabs now?",
            ):
                self.controller._do_load_ata_folder(output_dir)

        except Exception as exc:
            self._show_error("Export failed", exc, traceback.format_exc())

    def load_metadata_file(self):
        path = filedialog.askopenfilename(
            title="Open ATA Layout Metadata JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.layout_metadata = json.load(f)
            self.layout_metadata_path = path
            self._apply_metadata_to_gui(force=True)
            self._append_log(f"Loaded metadata: {path}\n")
            self.reparse_current_file()
        except Exception as exc:
            self._show_error("Could not load metadata JSON", exc, traceback.format_exc())

    def _apply_metadata_to_gui(self, force: bool = False):
        meta = self.layout_metadata or {}
        self.metadata_status_var.set(
            self.layout_metadata_path if self.layout_metadata_path else "No layout metadata JSON loaded"
        )
        if not meta:
            return

        def _pick(*keys):
            for k in keys:
                if k in meta:
                    return meta[k]
            for section in ("wafer", "die", "layers", "probe", "ata", "layout"):
                sub = meta.get(section)
                if isinstance(sub, dict):
                    for k in keys:
                        if k in sub:
                            return sub[k]
            return None

        def set_if(var: tk.StringVar, *keys):
            v = _pick(*keys)
            if v is not None and (force or not var.get().strip()):
                var.set(str(v))

        set_if(self.wafer_diameter_var, "wafer_diameter_mm")
        set_if(self.edge_exclusion_var, "edge_exclusion_mm")
        set_if(self.die_pitch_x_var,   "die_pitch_x_um")
        set_if(self.die_pitch_y_var,   "die_pitch_y_um")
        set_if(self.pad_layer_var,     "pad_layer", "probe_pad_layer")
        set_if(self.pad_datatype_var,  "pad_datatype", "probe_pad_datatype")


    def _set_status(self, text: str):
        self.status_var.set(text)
        self.update_idletasks()

    def _append_log(self, text: str):
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)

    def _show_error(self, title: str, exc: Exception, tb: str = ""):
        self._set_status(f"ERROR: {exc}")
        self._append_log(f"{title}: {exc}\n{tb}\n")
        self.controller.log(f"[GDS] ERROR: {exc}")
        messagebox.showerror(title, str(exc))

    @staticmethod
    def _opt_int(value: str) -> Optional[int]:
        v = value.strip()
        return int(v) if v else None

    @staticmethod
    def _flt(value: str, default: float) -> float:
        try:
            return float(value) if value and str(value).strip() else default
        except Exception:
            return default

    @staticmethod
    def _fmt(value: Any) -> str:
        return f"{value:.6g}" if isinstance(value, float) else str(value)
