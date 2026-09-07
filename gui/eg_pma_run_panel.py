"""Drive a .PMA recipe on the Electroglas as relative die steps.

HOW THE MACHINE DIVIDES THE WORK. The prober stores no wafer map. The operator
aligns and lands the chuck on a known die; from there the PC holds the map and
walks the recipe. So this panel needs one thing from the operator that it cannot
work out for itself - WHERE THE CHUCK IS RIGHT NOW - and everything after that
is arithmetic.

THE FRAME IS NOW PINNED DOWN (operator, from the original LaMP exe):

  1. the operator aligns and lands the chuck on the ALIGN SITE, near the middle
     of the wafer, and tells the prober that point is its 0,0;
  2. the exe then moves to the TOP-LEFT of the wafer grid and calls THAT 0,0
     internally - the prober never agrees, and does not need to;
  3. everything after is worked out from that top-left origin.

So XMoveFirstFromAlignSite/Y... is the align site -> MAP ORIGIN vector, NOT
align site -> first touchdown as previously recorded here. Confirmed on three
recipes: negating it lands on the wafer's extent centre (exactly for GIAL5,
within the half-pitch grid parity for the other two), while the first touchdown
is nowhere near the origin in any of them. Hence:

    prober_um = (XMoveFirstFromAlignSite + map_x,
                 YMoveFirstFromAlignSite + map_y)      [prober 0,0 = align site]

WHY THIS PANEL STILL STEPS RELATIVELY. The original exe drove absolute MICRON
moves (MA) off that transform and never used die moves at all. Relative MD
steps are what has been verified on this bench, so they stay the default; the
micron path is the more faithful one and removes the die-size trap below, but
it has not yet been run against hardware.

MD STEPS BY THE PROBER'S OWN DIE SIZE, not by anything in the recipe. For a LaMP
electrical recipe that must be the QUAD pitch (7042 x 3284 um for HP LaMP),
twice the physical die, because each touchdown covers a 2x2 shot. Set it wrong
and every step lands between quads. This panel checks the recipe's die size
against a value you confirm, and refuses to run until you have.

Verified on the bench: MD +1/-1 on both axes tracked ?P exactly, 0.5-0.8 s per
move, and returned to the start position exactly. Recipe +X/+Y match MD +1 (+X
right, +Y up).
"""

import os
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from electroglas_pma import (parse_pma_file, load_touchdowns, align_site_info,
                             format_quad, expand_touchdowns_to_dies, die_grid_index,
                             measurement_plan, workbook_touchdowns, QUAD_ORDER,
                             shot_geometry, slot_names, slot_grid,
                             quad_positions)
from recipe_gen_panel import shot_die_rc

# Where LaMP kept its recipes, then the repo's own copies.
_RECIPE_DIRS = (r"C:\_local\data\debug\LaMPElectrical",
                os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "pma"))

_POS_RE = re.compile(r"X(-?\d+)Y(-?\d+)")

MOTION_DIE = "die"      # MD - relative die indices, the prober does the pitch
MOTION_UM = "um"        # MM - relative microns, the PC does the pitch

# Largest single micron hop before it gets split. A whole-wafer row flyback is
# legitimately ~130 mm, so this is not a "no move is this big" guard like the
# die-step cap - it just keeps any one command bounded.
_MAX_UM_HOP = 150000


def parse_position(reply) -> tuple:
    """'X30Y38' -> (30, 38), or None."""
    m = _POS_RE.match(str(reply or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def chunk_step(dx: int, dy: int, cap: int) -> list:
    """Split one die step into hops of at most `cap` on each axis.

    The driver refuses a single MD larger than max_die_step - a guard against
    driving off the platen, added after a run of unchecked steps put the chuck
    238 mm past the edge with every one of them acknowledged. Recipe row
    flybacks are routinely bigger than the cap, so they get split here.
    """
    hops = []
    while dx or dy:
        hop_x = max(-cap, min(cap, dx))
        hop_y = max(-cap, min(cap, dy))
        hops.append((hop_x, hop_y))
        dx -= hop_x
        dy -= hop_y
    return hops


class EgPmaRunPanel(ttk.Frame):
    def __init__(self, parent, controller, main_layout=None):
        super().__init__(parent)
        self.controller = controller
        self._main_layout = main_layout

        self._recipe_path = None
        self._fields = {}
        self._touchdowns = []
        self._pma_raw_touchdowns = []
        self._die_um = (0.0, 0.0)
        self._index = None          # index of the touchdown the chuck is on
        self._anchored = False
        # (dx, dy) between the prober's REAL ?P reading and this module's
        # own THEORETICAL grid position (_grid_xy) at the moment of the
        # last anchor - see _finish_anchor/_expected_position. The prober's
        # own coordinate origin is whatever Set First Die/Load last
        # established, not fixed session to session.
        self._origin_offset = (0, 0)
        self._size_confirmed = False
        self._running = False
        self._abort = False
        # Distinct from _abort: pause stops the loop but keeps the position,
        # so Run resumes; abort resets it. See _pause / _stop.
        self._paused = False
        self._rc = {}               # touchdown seq -> one representative (row, col)
        self._cells = {}            # touchdown seq -> every map cell it covers
        self._results = {}          # touchdown seq -> "PASS" / "FAIL"
        self._die_results = {}      # (seq, quad_pos) -> "PASS" / "FAIL"
        self._slot_rc = {}          # seq -> {quad_pos: (row, col)}
        self._last_seq = None       # which square currently holds CURRENT

        self._selected = None
        self._seq_at_rc = {}        # (row, col) -> touchdown seq, for map clicks
        self._die_at_rc = {}        # (row, col) -> that die's record, for naming it
        self._sel_rc = None         # the exact cell clicked, so we can name the corner
        self._shot_window_items = []   # canvas ids for the 2x2 "you are here" box
        self._sel_window_items = []    # canvas ids for the selected touchdown's box
        # ➡ Move to Selected arm/target toggle - see toggle_move_armed. Map/
        # table clicks only pick a target while armed (self._move_armed),
        # same "press the button first" process Accretech's own Move to
        # Selected uses. self._goto_btn is created and assigned by
        # instrument_panel.py (Chuck Position section), not built here.
        self._move_armed = False
        self._goto_btn = None
        # Microns asked for but not yet delivered, because MM only moves in
        # whole 2.5 um counts. Carried into the next move - see _move_um.
        self._um_residual = [0.0, 0.0]

        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        self._build_recipe_row()
        self._build_anchor()
        self._build_controls()
        self._build_selection()
        self._build_table()

    # -- plumbing -----------------------------------------------------------

    def _log(self, msg: str):
        self.controller.log(msg)

    def _prober(self):
        drv = self.controller.drivers.get("prober")
        return drv if (drv and drv.inst) else None

    def _ui(self, fn):
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    # -- layout -------------------------------------------------------------

    def _build_recipe_row(self):
        # No longer displayed - the Run tab's own Recipe dropdown/steps
        # label (instrument_panel.py's ctrl bar) already shows what's
        # loaded, and having both said it twice. self._recipe_var is kept
        # alive since load_recipe()/forget_recipe() still .set() it.
        self._recipe_var = tk.StringVar(value="(none loaded)")

    def _build_anchor(self):
        # self._info_var still exists (set from _fill_info/_clear) even
        # though nothing displays it anymore - the "Recipe" text-description
        # section (die size/quad pitch/structure/measurement plan) was
        # removed rather than left as dead-but-visible clutter; _fill_info
        # itself is untouched since other code still calls it for its other
        # side effects.
        self._info_var = tk.StringVar(value="Load a .PMA to begin.")

        lf = ttk.LabelFrame(self, text="Set Initial", padding=6)
        lf.grid(row=2, column=0, sticky="ew", padx=6, pady=2)
        lf.columnconfigure(1, weight=1)

        ttk.Label(lf, text="Chuck is on:").grid(row=1, column=0, sticky="w")
        self._anchor_var = tk.StringVar()
        # Editable, not readonly: a whole-wafer recipe has thousands of sites
        # and scrolling to one is hopeless, so typing filters the list. A die
        # ID can also just be typed straight in - see _set_anchor.
        self._anchor_cb = ttk.Combobox(lf, textvariable=self._anchor_var, width=44)
        self._anchor_cb.grid(row=1, column=1, sticky="ew", padx=6)
        self._anchor_cb.bind("<KeyRelease>", self._on_anchor_typed)
        ttk.Button(lf, text="Set", command=self._set_anchor).grid(row=1, column=2)

        self._anchor_state_var = tk.StringVar(value="not set")
        ttk.Label(lf, textvariable=self._anchor_state_var, font=("Consolas", 8),
                  foreground="#b45309").grid(row=2, column=0, columnspan=3,
                                             sticky="w", pady=(4, 0))

    def _build_controls(self):
        # Run + Selected die combined into one LabelFrame - both are small
        # once their grey explanatory text is gone, so two titled boxes was
        # more chrome than content.
        lf = ttk.LabelFrame(self, text="Run", padding=6)
        lf.grid(row=3, column=0, sticky="ew", padx=6, pady=2)

        # ⏮ Back / ⏭ Next moved to the Chuck Position section, ▶ Run / ⏹ Stop
        # to the top bar (▶ Run next to Test Die; ⏹ Stop Run there now also
        # stops this pane's run) - see instrument_panel._tab_execution2.
        btns = ttk.Frame(lf)
        btns.pack(fill="x")
        ttk.Button(btns, text="↻ Sync ?P", command=self._sync_position).pack(
            side="left")
        ttk.Button(btns, text="Sync Run map", command=self._sync_run_map).pack(
            side="left", padx=(6, 0))

        mode = ttk.Frame(lf)
        mode.pack(fill="x", pady=(6, 0))
        ttk.Label(mode, text="Move by:").pack(side="left")
        self._motion_var = tk.StringVar(value=MOTION_DIE)
        ttk.Radiobutton(mode, text="die steps (MD)", value=MOTION_DIE,
                        variable=self._motion_var,
                        command=self._on_motion_mode).pack(side="left", padx=(6, 0))
        # MM is a fine positional move, but its count is NOT one micron - a
        # 7042 command travelled 17605 um, a scale of 2.5. The driver now
        # converts microns to MM counts via MM_UNIT_UM, whose value is still
        # unconfirmed between 0.1 mil and 2.5 um; see electroglas_2001x.
        self._um_radio = ttk.Radiobutton(
            mode, text="microns (MM) — scale UNCONFIRMED",
            value=MOTION_UM, variable=self._motion_var,
            command=self._on_motion_mode)
        self._um_radio.pack(side="left", padx=(8, 0))
        self._on_motion_mode()

        self._status_var = tk.StringVar(value="idle")
        ttk.Label(lf, textvariable=self._status_var, font=("Consolas", 9)
                  ).pack(anchor="w", pady=(6, 0))
        self._pos_var = tk.StringVar(value="—")
        ttk.Label(lf, textvariable=self._pos_var, font=("Consolas", 9),
                  foreground="#0077cc").pack(anchor="w")
        self._shot_window_var = tk.StringVar(value="Shot window: chuck not set")
        ttk.Label(lf, textvariable=self._shot_window_var, font=("Consolas", 8),
                  foreground="#2563eb", wraplength=430, justify="left").pack(
                  anchor="w", pady=(2, 0))
        # "Selected die" (heading/help text/status line/➤ Move to selected
        # button) used to live here - moved to the Chuck Position section
        # instead (instrument_panel._tab_execution2 builds the actual
        # ➡ Move to Selected button there and assigns it to self._goto_btn),
        # matching where Accretech's own Move to Selected lives. It is now
        # an arm/target toggle rather than a passive "click updates a status
        # line" control - see toggle_move_armed.

    def _build_selection(self):
        # Folded into _build_controls's "Run" LabelFrame above - kept as a
        # no-op so __init__'s build sequence and any external callers of
        # _build_selection don't need to change.
        pass

    def _build_table(self):
        # "Die list", not "Touchdowns": it lists every position on the wafer,
        # the same set the Set Initial Chuck dropdown offers, with the ones
        # this recipe actually probes marked. It used to show only the
        # recipe's own touchdowns, which made it disagree with that dropdown
        # for no reason a user could see - you could pick a die there that
        # this table said did not exist.
        lf = ttk.LabelFrame(self, text="Die list", padding=4)
        lf.grid(row=5, column=0, sticky="nsew", padx=6, pady=(2, 6))
        lf.rowconfigure(1, weight=1)
        lf.columnconfigure(0, weight=1)

        bar = ttk.Frame(lf)
        bar.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        self._table_all_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="show every die on the wafer",
                        variable=self._table_all_var,
                        command=self._fill_table).pack(side="left")
        self._table_count_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._table_count_var, foreground="#6b7280",
                  font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))

        cols = ("seq", "grid", "run", "step", "devices")
        self._tree = ttk.Treeview(lf, columns=cols, show="headings", height=10)
        for col, head, width, stretch in (("seq", "#", 46, False),
                                          ("grid", "grid x,y", 74, False),
                                          ("run", "run", 40, False),
                                          ("step", "MD", 64, False),
                                          ("devices", "devices", 260, True)):
            self._tree.heading(col, text=head)
            self._tree.column(col, width=width, anchor="w", stretch=stretch)
        self._tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.tag_configure("here", background="#fde68a")
        self._tree.tag_configure("done", foreground="#9ca3af")
        # A position on the wafer that this recipe does not probe. Still
        # listed, and still selectable - Go To / Set Initial Chuck can send
        # the chuck anywhere - just not part of the run.
        self._tree.tag_configure("offrun", foreground="#9ca3af")
        self._tree.bind("<<TreeviewSelect>>", self._on_table_click)

    # -- recipe -------------------------------------------------------------

    def adopt_from_process(self, quiet: bool = True) -> bool:
        """Pull the PMA Process tab's recipe. Returns True if one was taken.

        `quiet` suppresses the "nothing loaded" dialogs so this can run at
        startup, where an empty PMA Process tab is normal rather than an
        error worth interrupting anyone about.
        """
        proc = getattr(self._main_layout, "pma_process", None)
        if proc is None:
            if not quiet:
                messagebox.showinfo("PMA", "The PMA Process tab is not available.")
            return False
        path = getattr(proc, "_pma_path", None)
        fields = getattr(proc, "_fields", None)
        touchdowns = getattr(proc, "_touchdowns", None)
        if not (path and fields and touchdowns):
            if not quiet:
                messagebox.showinfo(
                    "PMA", "No recipe is loaded in the PMA Process tab yet.")
            return False
        self._adopt(path, fields, touchdowns)
        return True

    def _use_loaded_pma(self):
        """Button handler - same as adopt_from_process, but it says so when
        there is nothing to take."""
        self.adopt_from_process(quiet=False)

    def _align_die_from_wafer_tab(self):
        """The align die named by the recipe-generator workbook, if one is loaded.

        Better than inferring it from XMoveFirstFromAlignSite, because it is
        stated rather than derived - so it is preferred when present.
        """
        data = self.wafer_definition_data()
        if isinstance(data, dict) and data.get("align_die"):
            return str(data["align_die"])
        return None

    def _load_recipe(self):
        initial = next((d for d in _RECIPE_DIRS if os.path.isdir(d)), None)
        path = filedialog.askopenfilename(
            title="Load a .PMA recipe", initialdir=initial,
            filetypes=[("PMA recipe", "*.PMA"), ("All files", "*.*")])
        if not path:
            return
        try:
            fields = parse_pma_file(path)
            touchdowns = load_touchdowns(path, fields)
            die = (float(fields["DieSizeX"]), float(fields["DieSizeY"]))
        except Exception as e:
            messagebox.showerror("Recipe", f"Could not load:\n{e}")
            return
        if not touchdowns:
            messagebox.showerror("Recipe", "No touchdowns found — are the .PMV "
                                           "and .PMS siblings next to the .PMA?")
            return
        self._adopt(path, fields, touchdowns)

    def _adopt(self, path: str, fields: dict, touchdowns: list):
        # First thing: _pma_order_keys below calls _grid_xy, which reads the
        # published-map lookup. Clearing the cache further down (with the
        # rest of the per-recipe state) left those keys built from the
        # PREVIOUS recipe's map on every adopt after the first.
        self._builder_grid_cache = None
        self._builder_offset_cache = None
        self._grid_fallback_warned = False
        self._shot_corner_warned = False
        self._recipe_path = path
        self._fields = fields
        self._touchdowns = touchdowns
        # The WAFER's pitch, not the .PMA header's. Measured on LaMP
        # 2026-08-21: the 21PCM .PMA carries DieSizeX/Y = 2785 x 1585 while
        # the wafer it probes is on a 3521 x 1642 grid (every touchdown x is
        # an exact multiple of 3521, the wafer definition says 3521/1642, and
        # the prober's own SP1 measured 3521 x 1642). Dividing a 3521-spaced
        # grid by 2785 does not merely shift the result, it makes it
        # NON-LINEAR - the index drifts and skips (...19, 20, 21, 23, 24...) -
        # so touchdowns land on the wrong die by an amount that grows across
        # the wafer, which is exactly the reported symptom. The .PMA header
        # stays the fallback for a folder with no wafer definition loaded.
        wd = self.wafer_definition_data() or {}
        try:
            wafer_pitch = (float(wd.get("die_size_x") or 0),
                           float(wd.get("die_size_y") or 0))
        except (TypeError, ValueError):
            wafer_pitch = (0.0, 0.0)
        pma_pitch = (float(fields["DieSizeX"]), float(fields["DieSizeY"]))
        if wafer_pitch[0] > 0 and wafer_pitch[1] > 0:
            self._die_um = wafer_pitch
            if wafer_pitch != pma_pitch:
                self._log(
                    f"[PMA] die pitch {wafer_pitch[0]:.0f} x {wafer_pitch[1]:.0f} um "
                    f"taken from the wafer map; the .PMA header says "
                    f"{pma_pitch[0]:.0f} x {pma_pitch[1]:.0f}. The map wins - it is "
                    "what the die positions and the prober's own die size agree with.")
        else:
            self._die_um = pma_pitch
        # The chuck can be parked on - and driven to - ANY die on the wafer,
        # not only the ones this recipe probes. So the position list becomes
        # the whole wafer whenever a workbook is loaded, and the .PMA stops
        # being that list: it is now an ORDER over it, applied by
        # _enabled_indices. Keyed on quad coordinates rather than seq, because
        # the workbook and the .PMA number their shots independently.
        self._pma_order_keys = [self._grid_xy(t) for t in touchdowns]
        # Kept separately from self._touchdowns (about to be widened to the
        # whole wafer below) - this is the .PMA's own touchdown list, one
        # entry per real physical landing, each with its own device_id/x/y.
        # pma_process_panel._push_touchdowns_to_recipe needs THIS (not the
        # workbook's shot-granular list) to attach the recipe's touchdowns -
        # see _die_grid_lookup for why matching against the shot-level list
        # directly silently dropped most of a multi-die-per-shot recipe's
        # touchdowns.
        self._pma_raw_touchdowns = touchdowns
        self._touchdowns = self._map_source_touchdowns()
        self._index = None
        self._anchored = False
        self._origin_offset = (0, 0)
        self._size_confirmed = False
        self._rc = {}
        self._cells = {}
        # Rebuilt from the published map on next use - it changes
        # whenever the folder, recipe or map does.
        self._builder_grid_cache = None
        self._builder_offset_cache = None
        self._results = {}
        self._die_results = {}
        self._last_seq = None
        self._build_rc_index()
        self._recipe_var.set(os.path.basename(path))
        self._fill_info()
        self._fill_anchor_choices()
        self._fill_table()
        self._anchor_state_var.set("not set — pick where the chuck is, then Set")
        self._log(f"[PMA] Loaded {os.path.basename(path)}: {len(touchdowns)} touchdowns, "
                  f"die {self._die_um[0]:.0f} x {self._die_um[1]:.0f} um")

    def forget_recipe(self):
        """Drop the adopted recipe - called when the ATA folder changes.

        Everything here is wafer-specific: the touchdowns, the anchor list, the
        row/col index the map is painted through, and where the chuck is
        believed to be. Carrying any of it across to a different wafer means
        the Run tab and the map disagree about what a square is, which is worse
        than an empty Run tab.
        """
        self._recipe_path = ""
        self._fields = {}
        self._touchdowns = []
        self._pma_order_keys = []
        self._pma_raw_touchdowns = []
        self._index = None
        self._anchored = False
        self._origin_offset = (0, 0)
        self._size_confirmed = False
        self._rc = {}
        self._cells = {}
        # Rebuilt from the published map on next use - it changes
        # whenever the folder, recipe or map does.
        self._builder_grid_cache = None
        self._builder_offset_cache = None
        self._slot_rc = {}
        self._results = {}
        self._die_results = {}
        self._last_seq = None
        self._anchor_choices = []
        # Move to Selected's own target - self._selected would otherwise
        # outlive the touchdowns list it indexed into, and stay armed
        # pointing nowhere for a wafer the operator just left.
        self._move_armed = False
        self._selected = None
        self._sel_rc = None
        try:
            self._recipe_var.set("(none)")
            self._anchor_cb.config(values=[])
            self._anchor_var.set("")
            self._anchor_state_var.set("not set — load a recipe for this wafer")
            self._info_var.set("No recipe loaded.")
            self._tree.delete(*self._tree.get_children())
            self._clear_selection_window()
            self.update_shot_window()
            self._update_move_button()
        except Exception:
            pass
        self._log("[PMA] Run tab cleared — the ATA folder changed, so the "
                  "previous wafer's touchdowns no longer apply.")

    def _fill_info(self):
        f, dx, dy = self._fields, *self._die_um
        n = len(self._touchdowns)
        align = self._align_grid_xy()
        lines = [
            f"die size (grid pitch)  {dx:.0f} x {dy:.0f} um   "
            f"= {dx / 1000:.3f} x {dy / 1000:.3f} mm",
            f"touchdowns this run    {len(self._enabled_indices())}",
        ]
        if len(self._enabled_indices()) != n:
            lines.append(f"wafer positions        {n}  (the chuck can be set "
                         "to, or moved to, any of them)")
        if align:
            lines.append(f"align site             grid ({align[0]:.0f},{align[1]:.0f})")
        n_minor = int(f.get("CountMovesMinor") or 1)
        lines.append(
            f"structure              {f.get('CountMovesMajor', '?')} major"
            + (f" x {n_minor} minor sub-sites per touchdown" if n_minor > 1
               else " moves, ONE die per touchdown (no minor moves)"))
        plan = measurement_plan(f)
        lines.append(f"measurement            {plan['summary']}")
        if plan["wires"]:
            lines.append(f"probe pins needed      {plan['wires']}")
        self._info_var.set("\n".join(lines))

    def _align_info(self):
        """Shared with the PMA Process tab, so the two tabs cannot disagree."""
        return align_site_info(self._fields, self._touchdowns,
                               self._align_die_from_wafer_tab() or "")

    def _align_grid_xy(self):
        """Die-grid coords of the align site, from the ...FromAlignSite fields."""
        return self._align_info()["quad"]

    def _builder_grid_lookup(self) -> dict:
        """die_id -> (col, row) from the Wafer Builder-published map.

        Built once per rc-index rebuild: _grid_xy is called per touchdown
        (634 of them on LaMP) and rebuilding this dict each time turned an
        O(n) pass into O(n^2).
        """
        cached = getattr(self, "_builder_grid_cache", None)
        if cached is not None:
            return cached
        wm = self._run_map()
        dies = getattr(wm, "_last_dies", None) or []
        cached = {}
        for d in dies:
            die_id = (d.get("die_id") or "").strip()
            if die_id and d.get("row") is not None and d.get("col") is not None:
                cached.setdefault(die_id, (int(d["col"]), int(d["row"])))
        self._builder_grid_cache = cached
        return cached

    def _builder_grid_xy(self, t):
        """This touchdown's top-left cell as (col, row) on the PUBLISHED map,
        or None if none of its dies are on it.

        MEASURED on the machine (2026-08-21): from 54-00, MD +1,0 landed on
        54-01 and MD 0,+1 landed on 44-71 - one die right and one die DOWN.
        The published map's own (col, row) reproduces exactly that, on all
        four dies of the quad, so col/row ARE the MD grid and no micron
        division or sign convention is involved. That matters because the
        map stores y NEGATED for its own drawing (recipe_gen_panel writes
        "y_um": -d["y"]), so deriving the grid from its microns instead
        would invert every Y move.

        Normalised to the shot's TOP-LEFT via each die's slot offset inside
        the shot, not simply "the first die found": a shot whose top-left
        corner is NA (LaMP has many, e.g. NA/NA/NA/81-10) would otherwise
        report the corner that happens to be populated, and shots would sit
        one slot apart from each other in the grid.
        """
        lut = self._builder_grid_lookup()
        if not lut:
            return None
        rows, cols = self.shot_layout()
        grid = slot_grid(rows, cols)
        corners = []
        for ent in quad_positions(t["device_id"], rows, cols):
            cell = lut.get((ent["device"] or "").strip())
            if cell is None:
                continue
            slot_c, slot_r = grid.get(ent["pos"], (0, 0)) if ent["pos"] else (0, 0)
            corners.append(((cell[0] - slot_c, cell[1] - slot_r), ent["device"]))
        if not corners:
            return None
        # Every die of a shot must agree on where the shot's top-left is.
        # When they do not, the recipe's idea of which dies form one shot
        # disagrees with how the published map lays those dies out - e.g.
        # the gauge's quad "NA/92-74/NA/93-70" calls those two adjacent,
        # while a map published from the 21PCM definition has 92-75 sitting
        # between them. Take the majority so the answer is at least
        # deterministic, and say so once: silently picking one would put
        # this shot a slot away from all its neighbours.
        distinct = {c for c, _ in corners}
        if len(distinct) > 1 and not getattr(self, "_shot_corner_warned", False):
            self._shot_corner_warned = True
            detail = ", ".join(f"{d}->{c}" for c, d in corners)
            self._log(
                f"[PMA] ⚠ shot '{t.get('device_id')}' does not sit on the Wafer "
                f"Builder map as one block ({detail}). The recipe groups those "
                "dies into one touchdown but the published map spaces them "
                "differently, so this shot's position is a best guess. Republish "
                "the map from the definition this recipe was built for.")
        best = max(distinct, key=lambda c: sum(1 for x, _ in corners if x == c))
        return best

    def _grid_xy(self, t) -> tuple:
        """This touchdown's position in die-grid units (die-pitch steps from
        the origin) - not specific to a 2x2 shot. A single-die probe card's
        touchdowns get exactly the same coordinate, one die-grid step each.

        Read off the Wafer Builder map wherever that map knows this
        touchdown (see _builder_grid_xy). The .xls/.PMA microns below are a
        FALLBACK for a wafer that has no published map yet - they describe
        a different frame from the published map (measured on LaMP: 1 die
        out in X, 9 in Y), and mixing the two is what sent moves to the
        wrong die while the map showed something else. A constant frame
        offset is harmless on its own, because _finish_anchor derives
        origin_offset from a real ?P read and every MD move is a delta -
        what is not harmless is taking positions from one frame and
        row/col/die IDs from the other.
        """
        cell = self._builder_grid_xy(t)
        if cell is not None:
            return cell
        # Not on the published map. The .xls frame is self-consistent, so with
        # NO map loaded this is simply the answer. With a map loaded the two
        # frames are offset from each other (measured on LaMP: 1 column in X),
        # so returning a raw .xls grid here would leave this one touchdown a
        # die away from every other - the exact mixing this method exists to
        # stop. Shift it into the map's frame by the offset the two agree on.
        raw = (round(t["x"] / self._die_um[0]), round(t["y"] / self._die_um[1]))
        ox, oy = self._builder_frame_offset()
        if (ox, oy) != (0, 0) and not getattr(self, "_grid_fallback_warned", False):
            self._grid_fallback_warned = True
            self._log(
                f"[PMA] '{t.get('device_id')}' (and possibly others) is not on the "
                f"Wafer Builder map; placing it from the .xls, shifted by "
                f"({ox:+d},{oy:+d}) into the map's frame. Republish the map to "
                "include those dies rather than relying on this.")
        return (raw[0] + ox, raw[1] + oy)

    def _builder_frame_offset(self) -> tuple:
        """(dcol, drow) to add to an .xls-derived grid to land in the published
        map's frame.

        The two describe the same wafer from different origins - measured on
        LaMP, the map sits one column right of the .xls - so a touchdown the
        map does not carry cannot simply use its .xls coordinate: it would be
        the only one in the wrong frame. Derived from the touchdowns the two
        DO agree on (majority, so a handful of relabelled dies cannot skew
        it) rather than hard-coded, since it is a property of how that
        particular map was published. (0, 0) when there is no map, which
        makes the shift a no-op on the .xls-only path.
        """
        cached = getattr(self, "_builder_offset_cache", None)
        if cached is not None:
            return cached
        offset, votes = (0, 0), {}
        if self._builder_grid_lookup():
            for t in self._touchdowns or []:
                cell = self._builder_grid_xy(t)
                if cell is None:
                    continue
                raw = (round(t["x"] / self._die_um[0]),
                       round(t["y"] / self._die_um[1]))
                d = (cell[0] - raw[0], cell[1] - raw[1])
                votes[d] = votes.get(d, 0) + 1
            if votes:
                offset = max(votes, key=votes.get)
        self._builder_offset_cache = offset
        return offset

    def _expected_position(self, t) -> tuple:
        """What ?P SHOULD report right now if the chuck is really at
        touchdown t, given the origin offset established at the last
        anchor (_finish_anchor/self._origin_offset).

        _grid_xy alone is only this module's own THEORETICAL grid, built
        purely from the .xls/.PMA's own absolute microns - it assumes
        touchdown (0, 0) is the prober's ?P origin too, which is not
        necessarily true (the prober's own X0Y0 is wherever Set First Die/
        Load was last done, set independently, often not at the same
        physical die every time). A relative MD delta between two
        theoretical positions is unaffected (a constant offset cancels out
        in any difference) - only a comparison against a REAL ?P reading
        needs this adjustment, which is what this exists for.
        """
        qx, qy = self._grid_xy(t)
        ox, oy = self._origin_offset
        return (qx + ox, qy + oy)

    def _align_index(self):
        """Index of the touchdown sitting on the align site, if there is one."""
        td = self._align_info()["quad_touchdown"]
        if td is None:
            return None
        return next((i for i, t in enumerate(self._touchdowns)
                     if t["seq"] == td["seq"]), None)

    def _fill_anchor_choices(self):
        choices = []
        info = self._align_info()
        named = self._align_die_from_wafer_tab()
        if named and info["named_touchdown"] is not None:
            hit = info["named_touchdown"]
            choices.append(f"align die — #{hit['seq']} {hit['device_id']}")
        ai = self._align_index()
        if ai is not None:
            t = self._touchdowns[ai]
            label = f"align site — #{t['seq']} {t['device_id']}"
            if label not in choices and not any(f"#{t['seq']} " in c for c in choices):
                choices.append(label)
        # Every site, not the first 40 - the chuck can legitimately be parked
        # anywhere on the wafer, and a truncated list silently made most of
        # them unpickable.
        for t in self._touchdowns:
            choices.append(f"#{t['seq']} {t['device_id']}")
        self._anchor_choices = choices
        self._anchor_cb.config(values=choices)
        self._anchor_var.set(choices[0] if choices else "")

    _ANCHOR_MAX_LISTED = 300

    def _on_anchor_typed(self, event=None):
        """Narrow the dropdown to what the operator has typed.

        Navigation keys are ignored so arrowing through the list does not
        re-filter out from under them.
        """
        if event is not None and event.keysym in (
                "Up", "Down", "Left", "Right", "Return", "Escape", "Tab"):
            return
        text = self._anchor_var.get().strip().lower()
        if not text:
            matches = self._anchor_choices
        else:
            matches = [c for c in self._anchor_choices if text in c.lower()]
        self._anchor_cb.config(values=matches[:self._ANCHOR_MAX_LISTED])

    def _table_position(self, t) -> tuple:
        """This touchdown's position in whichever unit 'Move by:' currently
        has selected - die-grid steps (_grid_xy, what MD actually moves in)
        or raw microns (what MM actually moves in, see _move_um). The Die
        list table has to show whichever one a real move would use, or its
        deltas mean nothing once the operator switches modes - see
        _on_motion_mode, which re-fills the table whenever this changes."""
        if self._motion_var.get() == MOTION_UM:
            return (round(t["x"]), round(t["y"]))
        return self._grid_xy(t)

    def _fill_table(self):
        """Every wafer position, the run's own first and in run order.

        The iid stays the index into _touchdowns, so selecting a row still
        resolves to a position whichever set is shown.

        Run order first, then the rest by index: the step column is the
        delta from the previous touchdown IN THE CURRENTLY SELECTED MOTION
        MODE's units (_table_position) - only means anything along the path
        the recipe actually walks, so it is filled for the run's rows and
        left blank for positions the recipe never visits.
        """
        self._tree.delete(*self._tree.get_children())
        um_mode = self._motion_var.get() == MOTION_UM
        self._tree.heading("grid", text="µm x,y" if um_mode else "grid x,y")
        self._tree.heading("step", text="MM (µm)" if um_mode else "MD")
        run_order = self._enabled_indices()
        in_run = set(run_order)
        # The first run-order row's step is the delta from wherever the
        # chuck is actually anchored (Set Initial, or a re-anchor after a
        # position mismatch - see _move_to_index), not "start" - the chuck
        # could be set from anywhere. Until an anchor actually exists, NO
        # step in this column is a real move the software could compute (a
        # move always starts from wherever the chuck currently is, which is
        # unknown pre-anchor) - so the whole column stays blank rather than
        # showing deltas that look actionable but are missing the one hop
        # that actually matters (the unknown first move from the real,
        # un-anchored position).
        anchored = (self._anchored and self._index is not None
                   and 0 <= self._index < len(self._touchdowns))
        prev = self._table_position(self._touchdowns[self._index]) if anchored else None
        for i in run_order:
            t = self._touchdowns[i]
            qx, qy = self._table_position(t)
            step = f"{qx - prev[0]:+d},{qy - prev[1]:+d}" if anchored else ""
            self._tree.insert("", "end", iid=str(i),
                              values=(t["seq"], f"{qx},{qy}", "✓", step,
                                      t["device_id"]))
            prev = (qx, qy)
        n_off = 0
        if self._table_all_var.get():
            for i, t in enumerate(self._touchdowns):
                if i in in_run:
                    continue
                qx, qy = self._table_position(t)
                self._tree.insert("", "end", iid=str(i), tags=("offrun",),
                                  values=(t["seq"], f"{qx},{qy}", "", "",
                                          t["device_id"]))
                n_off += 1
        self._table_count_var.set(
            f"{len(run_order)} probed by this recipe"
            + (f", {n_off} more on the wafer" if n_off else
               ("" if self._table_all_var.get()
                else f" — {len(self._touchdowns) - len(run_order)} others hidden")))

    # -- anchoring ----------------------------------------------------------

    def _resolve_anchor(self, choice: str):
        """Index of the touchdown the operator named, or None (with a reason).

        Accepts a picked list entry ("#123 54-00"), a bare sequence ("#123"),
        or a die ID typed straight in ("54-00") - including one die of a quad,
        since that is what is legible under the scope.
        """
        text = (choice or "").strip()
        if not text:
            messagebox.showwarning("Anchor", "Pick or type where the chuck is first.")
            return None

        m = re.search(r"#(\d+)", text)
        if m:
            seq = int(m.group(1))
            idx = next((i for i, t in enumerate(self._touchdowns)
                        if t["seq"] == seq), None)
            if idx is None:
                messagebox.showwarning("Anchor", f"No touchdown #{seq} in this recipe.")
            return idx

        want = text.upper()
        exact = [i for i, t in enumerate(self._touchdowns)
                 if want in {d.strip().upper()
                             for d in (t.get("devices") or [t["device_id"]])}
                 or want == t["device_id"].strip().upper()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            seqs = ", ".join(f"#{self._touchdowns[i]['seq']}" for i in exact[:6])
            messagebox.showwarning(
                "Anchor", f"'{text}' appears at {len(exact)} touchdowns ({seqs}"
                          f"{'…' if len(exact) > 6 else ''}).\n\n"
                          "Pick the one you want from the list instead.")
            return None
        messagebox.showwarning(
            "Anchor", f"No touchdown matches '{text}'.\n\n"
                      "Type a die ID, or pick an entry from the list.")
        return None

    def _set_anchor(self):
        if not self._touchdowns:
            return
        choice = self._anchor_var.get()
        idx = self._resolve_anchor(choice)
        if idx is None:
            return

        dx, dy = self._die_um
        # Only MD depends on the prober's own pitch, so only MD needs this
        # asked. In micron mode the question is meaningless and asking it
        # would train people to click through it.
        if not self._size_confirmed and self._motion_var.get() == MOTION_DIE:
            if not messagebox.askokcancel(
                    "Confirm die size",
                    f"This recipe steps by {dx:.0f} x {dy:.0f} um "
                    f"({dx / 1000:.3f} x {dy / 1000:.3f} mm).\n\n"
                    "MD moves by the PROBER'S configured die size, not this one. "
                    "They must match, or every step lands between quads.\n\n"
                    "Is the prober's SET PRMTR die size set to this?\n\n"
                    "(Switching 'Move by' to microns avoids this entirely.)"):
                return
            self._size_confirmed = True

        drv = self._prober()
        if not drv:
            messagebox.showwarning(
                "Anchor", "Prober not connected - cannot read its real "
                          "position to anchor against.")
            return

        def _work():
            real = self._read_position(drv)
            self._ui(lambda: self._finish_anchor(idx, real))
        threading.Thread(target=_work, daemon=True).start()

    def _finish_anchor(self, idx: int, real):
        """Second half of _set_anchor, back on the UI thread once the
        prober's REAL position has been read.

        The offset between that real reading and this touchdown's
        THEORETICAL grid position (_grid_xy, computed purely from the .xls/
        .PMA's own absolute microns) is stored and applied to every future
        real-position check (see _expected_position) - the prober's own
        coordinate origin (wherever Set First Die/Load last put X0Y0) is
        whatever the operator most recently established, not fixed, so it
        is not safe to assume it lines up with the .xls's own origin. MD
        deltas between two THEORETICAL positions are unaffected by this (a
        constant offset cancels out in any difference) - only an absolute
        comparison against a real ?P reading needs it, which is exactly
        what _move_to_index/_sync_position now do.
        """
        if real is None:
            messagebox.showwarning(
                "Anchor", "Could not read the prober's real position (?P) - "
                          "cannot anchor. Check the link and try again.")
            return
        t = self._touchdowns[idx]
        theoretical = self._grid_xy(t)
        self._origin_offset = (real[0] - theoretical[0], real[1] - theoretical[1])
        self._index = idx
        self._anchored = True
        self._um_residual = [0.0, 0.0]
        qx, qy = theoretical
        offset_note = (f", origin offset {self._origin_offset}"
                       if self._origin_offset != (0, 0) else "")
        self._anchor_state_var.set(
            f"anchored at #{t['seq']} grid ({qx},{qy}) — real X{real[0]}Y{real[1]}"
            f"{offset_note} — {t['device_id']}")
        self._mark_current()
        self._refresh_position()
        # The Die list table's own step column depends on self._anchored/
        # self._index (see _fill_table) - without this, pressing Set here
        # updated everything EXCEPT the one place an operator would actually
        # look to see the move it just made possible, which looked exactly
        # like Set had silently failed.
        self._fill_table()
        # Paint now, inside the click, rather than whenever Tk next goes
        # idle. The overlay is drawn on the map canvas by the calls above,
        # but the canvas only shows it on the next idle cycle - so after a
        # modal confirm, or with anything else queued, the box could lag the
        # button press by a visible beat.
        try:
            wmap = self._run_map()
            if wmap is not None:
                wmap.canvas.update_idletasks()
        except Exception:
            pass
        self._log(f"[PMA] Anchored at #{t['seq']} {t['device_id']} grid ({qx},{qy})"
                  f"{offset_note}")

    def _mark_current(self):
        run_order = self._enabled_indices()
        in_run = set(run_order)
        # Position WITHIN THE RUN, not the raw index: the list now also holds
        # positions the recipe never visits, and "done" means "the run has
        # already been past it", which an index comparison cannot express
        # once the two orders differ.
        done_upto = run_order.index(self._index) if self._index in in_run else None
        for iid in self._tree.get_children():
            i = int(iid)
            tags = () if i in in_run else ("offrun",)
            if self._index is not None:
                if i == self._index:
                    tags = ("here",)
                elif done_upto is not None and i in in_run \
                        and run_order.index(i) < done_upto:
                    tags = ("done",)
            self._tree.item(iid, tags=tags)
        if self._index is not None:
            self._tree.see(str(self._index))

    def _refresh_position(self):
        if self._index is None:
            self._pos_var.set("—")
            self._mark_on_wafer_map(None)
            return
        t = self._touchdowns[self._index]
        qx, qy = self._grid_xy(t)
        self._pos_var.set(f"#{t['seq']}/{len(self._touchdowns)}  grid ({qx},{qy})  "
                          f"{t['device_id']}")
        self._mark_on_wafer_map(t)
        self._highlight(self._index)

    # -- the Run tab's own wafer map ----------------------------------------
    #
    # WaferMapPanel keys its dies by (row, col), and when a CSV carries x/y but
    # no row/col it derives them by sorting the unique coordinates and indexing
    # them. The same derivation is reproduced here from the touchdown list, so
    # the run can colour the right square without the map having to hand back
    # any mapping. Statuses come from WaferMapPanel.update_die:
    # UNTESTED / CURRENT / CONTACT / TESTING / PASS / FAIL / SKIP / CONTACT_FAIL.

    # The wafer is defined by the recipe-generator .xls, or by an imported CSV
    # of die IDs - never by the .PMA, which only names the subset to visit.
    #
    # Deliberately NOT wafer.workbook_data. That attribute is not the workbook:
    # PmaWaferPanel._refresh_view assigns it whichever source the Wafer Map tab
    # is currently DISPLAYING, so with the view set to "PMA" it holds the
    # touchdown list. Reading it here redrew the whole wafer as just the
    # touchdowns and took every row/col index with it.
    _WAFER_SOURCE_ATTRS = ("_xls_shot_data", "_csv_shot_data")

    def shot_layout(self) -> tuple:
        """(rows, cols) of the die block one touchdown covers.

        Taken from the loaded wafer definition, which carries it, so a 1x5
        strip does not get expanded as though it were the 2x2 LaMP quad. Falls
        back to shot_geometry's inference from the widest device-ID list.
        """
        data = self.wafer_definition_data() or {}
        widest = max(
            (len(str(t.get("device_id", "")).split("/"))
             for t in (self._touchdowns or [])), default=1)
        return shot_geometry(widest, int(data.get("shot_rows") or 0),
                             int(data.get("shot_cols") or 0))

    def wafer_definition_data(self):
        """The loaded source that defines the whole wafer, or None."""
        wafer = getattr(self._main_layout, "pma_wafer", None)
        if wafer is None:
            return None
        for attr in self._WAFER_SOURCE_ATTRS:
            data = getattr(wafer, attr, None)
            if isinstance(data, dict) and data.get("shots"):
                return data
        return None

    def _map_source_touchdowns(self) -> list:
        """Every shot the MAP shows - the workbook's, not this recipe's.

        The recipe generator .xls is the wafer; the .PMA is the subset this
        recipe visits. Deriving the grid from self._touchdowns made a
        15-touchdown gauge recipe index a 15-shot grid, so its dies landed on
        cells 0..7 of a wafer that has hundreds - the map drew only the
        touchdowns, and every row/col was wrong for the real map.
        """
        data = self.wafer_definition_data()
        if data:
            try:
                shots = workbook_touchdowns(data)
                if shots:
                    return shots
            except Exception as e:
                self._log(f"[PMA] Could not read the wafer map from the recipe "
                          f"generator workbook ({type(e).__name__}: {e}) — "
                          "falling back to the PMA's touchdowns.")
        return self._touchdowns

    def _wafer_builder_rc_lookup(self) -> dict:
        """(x, y) in THIS module's own touchdown x/y sign convention ->
        (row, col), read straight from the Wafer Builder-published map -
        the SAME data the Run tab's own wafer map (self._run_map()) is
        drawing from, via its already-loaded _last_dies. NOT the .xls or
        .PMA: those seed/create the Wafer Builder map once (LOAD ALL) and
        are never consulted again after that for row/col or die_id - see
        _build_rc_index.

        The published CSV negates y for WaferMapPanel's own drawing
        convention (recipe_gen_panel._write_active_wafer_map_csv's
        "y_um": fmt_num(-d["y"])) - undone here (-y_um) so the key matches
        the sign expand_touchdowns_to_dies' own x/y use.
        """
        wm = self._run_map()
        dies = getattr(wm, "_last_dies", None) or []
        return {(round(d["x_um"]), round(-d["y_um"])): (d["row"], d["col"])
                for d in dies
                if d.get("row") is not None and d.get("col") is not None
                and d.get("x_um") is not None and d.get("y_um") is not None}

    def _wafer_builder_die_id_lookup(self) -> dict:
        """(row, col) -> die_id, from the same Wafer Builder-published map
        _wafer_builder_rc_lookup reads - the id an operator typed into
        Wafer Builder's Die Map (e.g. 'PCM') is frequently NOT the raw
        text the .xls/.PMA carry at that position at all, so device_id
        pulled from those files cannot be trusted once a real Wafer
        Builder label exists."""
        wm = self._run_map()
        dies = getattr(wm, "_last_dies", None) or []
        return {(d["row"], d["col"]): d["die_id"] for d in dies if d.get("die_id")}

    def _build_rc_index(self):
        """seq -> the map cells that touchdown covers.

        Row/col (and, where Wafer Builder has one, the die_id) come from
        the Wafer Builder-published map, not from re-deriving a grid out of
        the .xls/.PMA - those files are only ever the SOURCE that seeded
        the Wafer Builder map in the first place (LOAD ALL); once that is
        done, this module must never disagree with what the map/recipe
        actually show. Each .PMA-parsed die's physical (x, y) is looked up
        directly against the published map's own (x, y) -> (row, col), so
        this can never invent a row/col scheme of its own that drifts out
        of step with what WaferMapPanel/RecipeGenPanel are using.
        """
        rows, cols = self.shot_layout()
        rc_lookup = self._wafer_builder_rc_lookup()
        die_id_lookup = self._wafer_builder_die_id_lookup()
        dies = expand_touchdowns_to_dies(self._touchdowns, *self._die_um,
                                         rows=rows, cols=cols)

        self._cells = {}
        # Rebuilt from the published map on next use - it changes
        # whenever the folder, recipe or map does.
        self._builder_grid_cache = None
        self._builder_offset_cache = None
        self._rc = {}
        self._seq_at_rc = {}
        self._die_at_rc = {}
        self._anchor_rc = {}
        # seq -> {quad_pos: rc}. Every die of the shot, NA corners included, so
        # slot N always lines up with fldSwitch N even where a corner is empty.
        # This is what lets a result be filed against the die it was actually
        # taken on rather than against the shot's anchor cell.
        self._slot_rc = {}
        missing = 0
        # die_id -> (row, col), the same published map _grid_xy now reads.
        # Matching on the ID rather than on microns is what makes this
        # immune to the two files describing the wafer in different frames
        # (measured on LaMP: the .xls sits 1 die out in X and 9 in Y from
        # the published map). The micron lookup stays as the fallback for
        # dies the map carries no ID for.
        by_id = {}
        for rc_key, wb_id in die_id_lookup.items():
            if wb_id:
                by_id.setdefault(wb_id.strip(), rc_key)
        for d in dies:
            rc = by_id.get((d.get("device_id") or "").strip())
            if rc is None:
                rc = rc_lookup.get((round(d["x"]), round(d["y"])))
            if rc is None:
                # A .PMA touchdown at coordinates the Wafer Builder map has
                # no die for - the two are for different wafers, or the map
                # has not been (re)published since this recipe was loaded.
                # Skip it rather than invent a cell, and say how many.
                missing += 1
                continue
            wb_id = die_id_lookup.get(rc)
            if wb_id:
                d = dict(d, device_id=wb_id)
            self._cells.setdefault(d["seq"], []).append(rc)
            self._slot_rc.setdefault(d["seq"], {})[d["quad_pos"]] = rc
            # Only real dies get a reverse mapping - clicking an NA corner
            # should not select the shot, since nothing is probed there.
            if d["enabled"]:
                self._seq_at_rc[rc] = d["seq"]
                self._die_at_rc[rc] = d
                # One cell stands for the whole touchdown wherever a shot has
                # to be named by a single square. It must be an ENABLED die's
                # cell, because that is the only kind _seq_at_rc maps back -
                # a shot like NA/NA/NA/81-10 has just one, and it is not the
                # top-left corner.
                self._anchor_rc.setdefault(d["seq"], rc)
        if missing:
            self._log(f"[PMA] {missing} of {len(dies)} recipe dies are not on "
                      "the Wafer Builder map — the .PMA and the published map "
                      "look like they are for different wafers, or the map has "
                      "not been (re)published since this recipe loaded.")
        # Kept for anything that still wants a single representative cell.
        self._rc = {seq: cells[0] for seq, cells in self._cells.items()}

        # Correct self._touchdowns' OWN device_id too - not just the
        # per-die records above - so every consumer that reads a touchdown
        # directly (Die List table, anchor dropdown, Move MM table, log/
        # dialog text) shows the SAME label the map and recipe do, not a
        # possibly-stale .xls/.PMA text an operator has since overridden in
        # Wafer Builder (e.g. LAMP's PCM sites). Single-die shots only
        # (LAMP's own shot is 1x1) - a quad's joined "A/B/C/D" string is
        # left as its original text, since reconstructing that join
        # correctly needs its own slot-order handling this fix does not
        # attempt.
        for t in self._touchdowns:
            anchor = self._anchor_rc.get(t["seq"])
            if anchor is None or len(self._slot_rc.get(t["seq"], {})) != 1:
                continue
            wb_id = die_id_lookup.get(anchor)
            if wb_id and wb_id != t["device_id"]:
                t["device_id"] = wb_id
                t["devices"] = [wb_id]

    def _die_grid_lookup(self) -> dict:
        """(grid x, grid y) -> (row, col), one entry per real INDIVIDUAL die
        the current map defines - not one per shot.

        The workbook's own touchdown list (self._touchdowns after adoption)
        is shot/quad-granular: a 2x2 shot carries ONE coordinate for the
        whole group, with all 4 device IDs packed into one cell. The .PMA's
        own touchdowns are per-individual-die. Matching a .PMA touchdown's
        raw coordinate 1:1 against the shot-level list (what _pma_order does,
        for chuck-positioning purposes) only ever hits the one sub-position
        that happens to coincide with the shot's own anchor coordinate - the
        other ~3 of every 4 dies in a typical quad silently have no match.
        Confirmed on a real 3125-touchdown whole-wafer .PMA: only 753 (~1 in
        4) matched.

        Reuses _build_rc_index's own per-die expansion (self._die_at_rc,
        already keyed by real absolute die x/y) rather than re-deriving it,
        so this always agrees with what the map itself just drew."""
        return {(round(d["x"] / self._die_um[0]), round(d["y"] / self._die_um[1])): rc
                for rc, d in getattr(self, "_die_at_rc", {}).items()}

    def _run_map(self):
        return getattr(self._main_layout, "_exec_wafer_map", None)

    def _results_map(self):
        return getattr(self._main_layout, "_results_wafer_map", None)

    def _paint(self, seq, status: str, also_results: bool = False):
        """Colour one touchdown on the Run tab map. Best effort.

        also_results mirrors it onto the Results tab's map, which the
        Accretech flow does for verdicts only - that map is about outcomes,
        so the transient PROBING highlight does not belong on it.
        """
        self._paint_cells(self._cells.get(seq), status, also_results)

    def _paint_cells(self, cells, status: str, also_results: bool = False):
        """Colour specific map squares - one die's, or a whole touchdown's."""
        if not cells:
            return
        maps = [self._run_map()]
        if also_results:
            maps.append(self._results_map())
        for wmap in maps:
            if wmap is None:
                continue
            for rc in cells:
                try:
                    if rc in wmap.dies:
                        wmap.update_die(rc[0], rc[1], status)
                except Exception:
                    pass

    # -- 2x2 position window ------------------------------------------------
    #
    # Modelled on NanoZ's 1x20 window (nanoz_panel._update_position_window):
    # one outline over the block the head covers rather than per-cell
    # decoration, positions taken from the map's own canvas coords so it
    # follows pan/zoom, and extrapolated from the die pitch when a corner of
    # the block has no die drawn (an NA position, or the wafer edge).

    def _clear_shot_window(self):
        self._clear_items(self._shot_window_items)
        self._shot_window_items = []

    def _clear_selection_window(self):
        self._clear_items(self._sel_window_items)
        self._sel_window_items = []

    def _clear_items(self, items):
        wmap = self._run_map()
        for item in items:
            try:
                wmap.canvas.delete(item)
            except Exception:
                pass

    def _cell_pitch(self, wmap):
        """Canvas (dx, dy) between horizontally and vertically adjacent cells."""
        px = py = None
        for (r, c) in wmap.dies:
            if px is None and (r, c + 1) in wmap.dies:
                a = wmap.canvas.coords(wmap.dies[(r, c)])
                b = wmap.canvas.coords(wmap.dies[(r, c + 1)])
                if a and b:
                    px = b[0] - a[0]
            if py is None and (r + 1, c) in wmap.dies:
                a = wmap.canvas.coords(wmap.dies[(r, c)])
                b = wmap.canvas.coords(wmap.dies[(r + 1, c)])
                if a and b:
                    py = b[1] - a[1]
            if px is not None and py is not None:
                break
        return px, py

    def _cell_box(self, wmap, rc, pitch):
        """Canvas box for a cell, extrapolated from a neighbour if not drawn."""
        item = wmap.dies.get(rc)
        if item is not None:
            coords = wmap.canvas.coords(item)
            if len(coords) >= 4:
                return coords
        px, py = pitch
        if px is None or py is None:
            return None
        # Nearest drawn cell, then step over by whole pitches.
        best = None
        for (r, c), it in wmap.dies.items():
            d = abs(r - rc[0]) + abs(c - rc[1])
            if best is None or d < best[0]:
                best = (d, (r, c), it)
        if best is None:
            return None
        _d, (br, bc), it = best
        base = wmap.canvas.coords(it)
        if len(base) < 4:
            return None
        ox, oy = (rc[1] - bc) * px, (rc[0] - br) * py
        return [base[0] + ox, base[1] + oy, base[2] + ox, base[3] + oy]

    def _block_box(self, wmap, cells):
        """Bounding canvas box of a set of cells, or None if none are placeable."""
        pitch = self._cell_pitch(wmap)
        boxes = [b for b in (self._cell_box(wmap, rc, pitch) for rc in cells) if b]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def update_shot_window(self):
        """Redraw both canvas overlays this panel owns.

        The Run tab's redraw/zoom hook calls this one name, so the selected
        touchdown's outline rides along with the "you are here" box rather
        than needing its own hook - a zoom scales items in place instead of
        rebuilding, so anything not redrawn here is left behind at the wrong
        size.
        """
        self._draw_shot_window()
        self.update_selection_window()

    # Same dark blue as Accretech's own Move to Selected target highlight
    # (instrument_panel.MainLayout._EXEC2_MOVE_TARGET_COLOR) - one shared
    # colour convention for "this is the Move to Selected target" on either
    # system's map.
    _MOVE_TARGET_COLOR = "#1e3a8a"

    def update_selection_window(self):
        """Outline the touchdown the selected die belongs to.

        Deliberately a different colour/width from the chuck's box: the two
        coincide only when the selection is where the prober already is, and
        the operator needs to see at a glance which is which.
        """
        self._clear_selection_window()
        wmap = self._run_map()
        idx = self._selected
        # The index can outlive the recipe it pointed into (reload, re-sync),
        # so range-check it here rather than trusting every caller to clear it.
        if wmap is None or not self._cells or idx is None \
                or not (0 <= idx < len(self._touchdowns)):
            return
        seq = self._touchdowns[idx]["seq"]
        cells = self._cells.get(seq) or []
        box = self._block_box(wmap, cells) if cells else None
        if not box:
            return
        rect = wmap.canvas.create_rectangle(*box, outline=self._MOVE_TARGET_COLOR,
                                            width=2, dash=(4, 3))
        wmap.canvas.tag_raise(rect)
        self._sel_window_items.append(rect)
        # The one die that was actually clicked gets a tighter, thicker ring
        # inside the touchdown box - the actual Move to Selected target, so
        # "which corner am I on" is answerable AND it reads as the highlight
        # (only one at a time - _clear_selection_window above always runs
        # first) rather than just an outline.
        if self._sel_rc is not None:
            cell = self._cell_box(wmap, self._sel_rc, self._cell_pitch(wmap))
            if cell:
                inner = wmap.canvas.create_rectangle(
                    *cell, outline=self._MOVE_TARGET_COLOR, width=3)
                wmap.canvas.tag_raise(inner)
                self._sel_window_items.append(inner)

    def _draw_shot_window(self):
        """Outline the 2x2 (or 1x1) block the chuck is currently on."""
        self._clear_shot_window()
        wmap = self._run_map()
        if wmap is None or self._index is None or not self._cells:
            self._shot_window_var.set("Shot window: chuck not set")
            return
        seq = self._touchdowns[self._index]["seq"]
        cells = self._cells.get(seq) or []
        if not cells:
            self._shot_window_var.set("Shot window: not on the map")
            return
        box = self._block_box(wmap, cells)
        if not box:
            self._shot_window_var.set("Shot window: off the drawn map")
            return
        rect = wmap.canvas.create_rectangle(*box, outline="#2563eb", width=3)
        wmap.canvas.tag_raise(rect)
        self._shot_window_items.append(rect)

        t = self._touchdowns[self._index]
        drawn = sum(1 for rc in cells if rc in wmap.dies)
        self._shot_window_var.set(
            f"Shot window #{t['seq']} {len(cells)}-up "
            f"({drawn} die{'' if drawn == 1 else 's'} on the map):  "
            f"{format_quad(t['device_id'], *self.shot_layout())}")

    def mark_die_result(self, seq, quad_pos: str, passed: bool):
        """Record and paint ONE die's verdict.

        A shot carries four dies and they pass or fail independently, so the
        square that goes green or red is the die's own - not all four corners
        painted with the shot's combined verdict, which is what a per-touchdown
        mark_result() did. Counted per die too: three probed shots is twelve
        die results, not three.
        """
        rc = (self._slot_rc.get(seq) or {}).get(quad_pos)
        if rc is None:
            return
        key = (seq, quad_pos)
        was = self._die_results.get(key)
        self._die_results[key] = "PASS" if passed else "FAIL"
        self._paint_cells([rc], self._die_results[key], also_results=True)
        self._tally(was, self._die_results[key])
        # Persisted the same way instrument_panel._exec_update_die_color
        # does, so cmd_save_csv/cmd_import_results_csv see LaMP's per-die
        # verdicts too, not just the Accretech/generic Run tab's.
        try:
            self.controller.die_status[rc] = self._die_results[key]
        except Exception:
            pass

    def mark_result(self, seq, passed: bool):
        """Record and paint a whole touchdown's verdict.

        Retained for the case where nothing reported per-die verdicts - the
        shot's four squares then share one colour, which is better than none.
        """
        was = self._results.get(seq)
        self._results[seq] = "PASS" if passed else "FAIL"
        self._paint(seq, self._results[seq], also_results=True)
        self._tally(was, self._results[seq])
        try:
            for rc in self._cells.get(seq) or []:
                self.controller.die_status[rc] = self._results[seq]
        except Exception:
            pass

    def _tally(self, was: str, now: str):
        """Move one result between the PASS/FAIL columns.

        Counted once per thing measured. Re-probing the same die (Back, then
        forward again) must not inflate the totals, and a changed verdict has
        to move the count from one column to the other rather than add to both.
        """
        if was == now:
            return
        layout = self._main_layout
        add_pass = getattr(layout, "_exec_add_pass", None)
        add_fail = getattr(layout, "_exec_add_fail", None)
        if not (add_pass and add_fail):
            return
        try:
            if was in ("PASS", "FAIL"):
                var = (layout._exec_pass_var if was == "PASS"
                       else layout._exec_fail_var)
                var.set(max(0, var.get() - 1))
            (add_pass if now == "PASS" else add_fail)()
        except Exception as e:
            self._log(f"[PMA] Could not update pass/fail counts — "
                      f"{type(e).__name__}: {e}")

    def reset_results(self):
        """Drop recorded verdicts and repaint - pairs with Reset Counts."""
        for seq in list(self._results):
            self._paint(seq, "UNTESTED", also_results=True)
        self._results.clear()
        for (seq, quad), _v in list(self._die_results.items()):
            rc = (self._slot_rc.get(seq) or {}).get(quad)
            if rc is not None:
                self._paint_cells([rc], "UNTESTED", also_results=True)
        self._die_results.clear()
        if self._index is not None:
            self._last_seq = None
            self._highlight(self._index)

    def _restore_colours(self, seq):
        """Repaint a shot with whatever verdicts it actually has.

        Per die when there are per-die verdicts, otherwise the shot's own,
        otherwise untested. Reading only the per-SHOT _results here is what
        erased the run: verdicts now land in _die_results, so _results.get()
        returned "UNTESTED" and every die the run had just coloured went grey
        again the moment the chuck moved on. Only the Run map was affected,
        because this repaint does not mirror to the Results tab - which is why
        the colours survived there and vanished here.
        """
        slots = self._slot_rc.get(seq) or {}
        per_die = {quad: self._die_results.get((seq, quad)) for quad in slots}
        if any(per_die.values()):
            for quad, rc in slots.items():
                self._paint_cells([rc], per_die.get(quad) or "UNTESTED")
            return
        self._paint(seq, self._results.get(seq, "UNTESTED"))

    def _highlight(self, index):
        """Orange-ish CURRENT on the new shot; the one we left keeps its result
        colour if it has one, otherwise goes back to untested."""
        if self._last_seq is not None and self._last_seq != (
                self._touchdowns[index]["seq"] if index is not None else None):
            self._restore_colours(self._last_seq)
        if index is None:
            self._last_seq = None
            self.update_shot_window()
            return
        seq = self._touchdowns[index]["seq"]
        self._paint(seq, "PROBING")
        self._last_seq = seq
        self.update_shot_window()

    def _sync_run_map(self):
        """Point the Run tab's map at this recipe so highlighting has squares.

        Builds a Wafer Builder map (Shot/Shot Map/Die Map) from the loaded
        recipe's own touchdowns and publishes it as the Run tab's map -
        same path LOAD ALL's own map step uses (pma_process_panel._write_
        wafer_map). The old ata_wafer_map_electroglas.csv this used to
        write predates Wafer Builder entirely and is retired: Electroglas
        has no separate hardware-extracted map to fall back on, so Wafer
        Builder IS the wafer here, not a second parallel copy of it.
        """
        layout = self._main_layout
        wmap = self._run_map()
        if wmap is None or layout is None:
            messagebox.showinfo("Run map", "The Run tab's wafer map is not available.")
            return
        if not self._touchdowns:
            messagebox.showinfo("Run map", "Load a recipe first.")
            return
        folder = getattr(layout, "_exec_map_folder", None) or \
            getattr(layout, "_ata_folder", None)
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("Run map", "Load an ATA folder on the Run tab first.")
            return
        gen = getattr(layout, "recipe_gen", None)
        if gen is None or not hasattr(gen, "load_touchdowns_as_map"):
            messagebox.showinfo("Run map", "The Wafer Builder tab is not available.")
            return
        if not messagebox.askokcancel(
                "Run map",
                "Rebuild the Wafer Builder map from this recipe's touchdowns, "
                "and make it the Run tab's active map?"):
            return
        shots = self._map_source_touchdowns()
        try:
            gen.load_touchdowns_as_map(
                shots, "Sync Run Map", "the loaded recipe's touchdowns",
                save_as=os.path.splitext(os.path.basename(self._pma_path))[0]
                        if getattr(self, "_pma_path", "") else None)
        except Exception as exc:
            self._log(f"[PMA] Could not build a Wafer Builder map from these "
                      f"shots — {type(exc).__name__}: {exc}")
            return
        try:
            gen._sync_views(folder)
        except Exception as e:
            self._log(f"[PMA] Could not redraw the Run map: {type(e).__name__}: {e}")
            return
        self._build_rc_index()
        self._last_seq = None
        try:
            wmap.set_click_handler(self._on_map_click)
        except AttributeError:
            self._log("[PMA] This wafer map build has no click handler — "
                      "select dies from the table instead")
        if self._index is not None:
            self._highlight(self._index)
        self._log(f"[PMA] Run map synced — {len(self._rc)} shots, click a die to select it")

    def _mark_on_wafer_map(self, touchdown):
        """Ring the current shot on the PMA Wafer tab's map.

        The map is drawn in the same micron frame as the touchdown coordinates,
        so this is a direct hand-off. Best effort - the map is a convenience for
        matching against the scope, and a run must not fail because it is not
        loaded or matplotlib is missing.
        """
        wafer = getattr(self._main_layout, "pma_wafer", None)
        if wafer is None:
            return
        try:
            if touchdown is None:
                wafer.clear_current_shot()
            else:
                label = "/".join(d for d in touchdown["devices"]
                                 if d.strip().upper() != "NA") or touchdown["device_id"]
                wafer.mark_current_shot(touchdown["x"], touchdown["y"],
                                        f"#{touchdown['seq']}  {label}")
        except Exception as e:
            self._log(f"[PMA] wafer map marker skipped — {type(e).__name__}: {e}")

    def _sync_position(self):
        drv = self._prober()
        if not drv:
            self._log("[PMA] Prober not connected")
            return

        def _work():
            drv.recover()
            pos = drv.get_xy_position()
            status = drv.decode_status(drv.get_prober_status())
            # Actually RECONCILE against the anchor, not just display the raw
            # reply - a mismatch here means self._index (what every MD delta
            # is computed from) no longer describes where the chuck really
            # is. _move_to_index checks this too, right before every move,
            # but catching it here - the moment the operator asks to verify
            # position - means they find out immediately instead of only
            # when the next move refuses to send.
            note = ""
            if self._anchored and self._index is not None \
                    and 0 <= self._index < len(self._touchdowns):
                real = parse_position(pos)
                expect = self._expected_position(self._touchdowns[self._index])
                if real is None:
                    note = "  (could not parse ?P - unable to verify the anchor)"
                elif real != expect:
                    self._anchored = False
                    note = (f"  MISMATCH: software expected X{expect[0]}Y{expect[1]} "
                            f"(touchdown #{self._touchdowns[self._index]['seq']}, "
                            "accounting for the origin offset from the last anchor) - "
                            "re-anchor (Set Initial) before running.")
                    self._ui(self._fill_table)
                else:
                    note = "  matches the anchored touchdown."
            self._ui(lambda: (self._status_var.set(f"?P={pos}  {status}"),
                              self._log(f"[PMA] ?P={pos}  {status}{note}")))

        threading.Thread(target=_work, daemon=True).start()

    # -- running ------------------------------------------------------------

    def _guard(self) -> bool:
        if not self._anchored or self._index is None:
            messagebox.showwarning("Run", "Set where the chuck is first.")
            return False
        if not self._prober():
            self._log("[PMA] Prober not connected")
            return False
        if self._running:
            self._log("[PMA] Already running")
            return False
        return True

    # -- which touchdowns this run actually probes ---------------------------
    #
    # The .PMA's move list is the default, not the last word. A recipe may
    # carry its own touchdown list - saved from this tab's map selection - and
    # when it does it OVERRIDES the PMA: the operator picked a subset on
    # purpose. Without this the override was visible on the map and ignored by
    # the run, which is the worst of both.

    def _probe_seqs(self):
        """Touchdown seqs the loaded recipe restricts the run to, or None.

        Resolved the SAME way the Run tab map highlight already is (see
        instrument_panel._exec_resolve_site_cells) - each site's die_id is
        looked up against the loaded wafer map first, falling back to the
        recipe's own (row, col) only when that die_id is not on the map.
        This used to go straight to the recipe's raw (row, col)
        (get_sites(), no die_id at all), so a recipe whose touchdown
        resolution was wrong (e.g. 21PCM's) picked whatever touchdown
        genuinely happened to sit at that wrong position instead - the map
        highlight showed the right die (it already went through the fixed
        path), but the actual run walked a different one entirely, with no
        relationship to what was highlighted.
        """
        panel = getattr(self._main_layout, "recipe_panel", None)
        get_records = getattr(panel, "get_site_records", None)
        if not get_records:
            return None
        try:
            records = list(get_records())
        except Exception:
            return None
        if not records:
            return None
        resolve = getattr(self._main_layout, "_exec_resolve_site_cells", None)
        sites = resolve(records) if resolve else [
            (r["row"], r["col"]) for r in records]
        seqs = {self._seq_at_rc[rc] for rc in sites if rc in self._seq_at_rc}
        return seqs or None

    def _pma_order(self):
        """Indices of the .PMA's touchdowns, in the order it visits them.

        _touchdowns is the whole wafer in map order, so probing it in index
        order would abandon the route the .PMA lays out. This maps the .PMA's
        sequence onto the position list by quad coordinate.
        """
        index_of = {self._grid_xy(t): i for i, t in enumerate(self._touchdowns)}
        return [index_of[k] for k in getattr(self, "_pma_order_keys", [])
                if k in index_of]

    def _enabled_indices(self):
        """Positions this run probes, in the order it probes them."""
        order = self._pma_order() or list(range(len(self._touchdowns)))
        seqs = self._probe_seqs()
        if seqs is None:
            # No touchdown list on the recipe: fall back to the .PMA's own
            # list, NOT to every position - widening _touchdowns to the wafer
            # must not turn a 15-shot recipe into a 634-shot run.
            return order
        chosen = [i for i in order if self._touchdowns[i]["seq"] in seqs]
        # A die the operator picked that the .PMA never mentions still gets
        # probed - it is on the recipe's list, which overrides the .PMA.
        seen = set(chosen)
        chosen.extend(i for i, t in enumerate(self._touchdowns)
                      if t["seq"] in seqs and i not in seen)
        return chosen

    def _next_enabled_index(self, after):
        """The position after `after` in RUN order, not in index order."""
        order = self._enabled_indices()
        if not order:
            return None
        if after is None:
            return order[0]
        try:
            pos = order.index(after)
        except ValueError:
            # Anchored somewhere off the run list - start at its beginning.
            return order[0]
        return order[pos + 1] if pos + 1 < len(order) else None

    def _step_once(self):
        if self._guard():
            self._start(1)

    def _run_all(self):
        rp = getattr(self._main_layout, "recipe_panel", None)
        if rp is not None and getattr(rp, "is_minor_moves", None) and rp.is_minor_moves():
            self._run_minor_moves()
            return
        if not self._guard():
            return
        enabled = self._enabled_indices()
        if not enabled:
            self._log("[PMA] Run: this recipe has no touchdowns to probe.")
            return
        total = len(self._touchdowns)
        subset = (f"\n\nThe loaded recipe restricts this run to {len(enabled)} "
                  f"of the PMA's {total} touchdown(s); the rest are skipped."
                  if len(enabled) != total else "")

        # Position in RUN order, not index order - the run follows the .PMA's
        # route over a wafer-wide position list, so "ahead" is not "> index".
        # _needs_restart (set by _start's _settle, after a stop/finish/error
        # - never a pause) means the LAST run reached the end of its list or
        # was cut short - either way the next Run/Full Die/Test Selected
        # press should start the whole list over from its first touchdown,
        # not refuse with "already at the last touchdown" and not silently
        # resume mid-wafer either.
        restart = getattr(self, "_needs_restart", False)
        if not restart:
            try:
                ahead = enabled[enabled.index(self._index) + 1:]
            except ValueError:
                ahead = enabled
            restart = not ahead

        if restart:
            remaining = len(enabled)
            first = enabled[0]
            move_note = ("" if self._index == first else
                         "\n\nThe chuck is not on the first touchdown of "
                         "this run - it will move back there before "
                         "probing starts.")
            prompt = (f"Probe {remaining} touchdown(s), starting over from "
                      f"the first touchdown of this run?{move_note}{subset}")
        else:
            remaining = len(ahead)
            prompt = f"Probe {remaining} more touchdown(s)?{subset}"

        if not messagebox.askokcancel(
                "Run", f"{prompt}\n\n"
                       "THIS MEASURES. The wafer contacts the probe card and "
                       "the recipe runs on all four dies of each shot.\n\n"
                       "Z is verified against ?S before each measurement — if "
                       "the chuck is not in contact the run stops rather than "
                       "measuring open air. The chuck is separated at the end."):
            return
        if restart:
            self._needs_restart = True  # consumed by _move_next's first hop
        self._start(remaining)

    # -- Minor Moves (shot-aware single-die stepping) -----------------------
    #
    # A parallel run path, entirely separate from the .PMA-driven engine
    # above: no self._touchdowns, no anchor/quad math. A wafer-map square is
    # a Wafer Builder SHOT (several real dies, e.g. a 7x9 reticle); this
    # probe card only ever contacts one die at a time, so the chuck is
    # repositioned - by absolute die coordinate, via the driver's own
    # goto_die() (bounded/verified relative stepping under the hood, see
    # that method's docstring in instruments/electroglas_2001x.py) - to
    # whichever die # a recipe step calls for, measures just that step, and
    # moves on. Mirrors instrument_panel.py's _exec_minor_move_thread
    # (Accretech) using goto_die() instead of move_to_die_xy(). Off by
    # default (Recipe tab's Minor Moves checkbox) and not yet exercised
    # against a real Electroglas single-die-shot project - see that
    # checkbox's own docstring.

    def _run_minor_moves(self):
        if self._running:
            self._log("[PMA] Already running")
            return
        drv = self._prober()
        if drv is None:
            self._log("[PMA] Prober not connected")
            return
        rp = self._main_layout.recipe_panel
        origin = rp.get_shot_origin()
        if origin is None:
            self._log("[PMA] Minor Moves: no shot origin set for this recipe "
                      "— press Set Shot Origin on the Recipe tab (with the "
                      "chuck on shot R0C0's die R0C0), then Run again.")
            return
        gen = getattr(self._main_layout, "recipe_gen", None)
        if gen is None:
            self._log("[PMA] Minor Moves: the Wafer Builder tab is not available.")
            return
        shot_rows, shot_cols = gen._shot_dims()
        shot_cells = dict(gen._shot_cells)
        shots = sorted({(d["row"], d["col"]) for d in gen.shots_as_die_list()})
        if not shots:
            self._log("[PMA] Minor Moves: no shots on the Wafer Builder map.")
            return
        steps = self._main_layout.recipe_panel.get_steps()
        if not steps:
            self._log("[PMA] Minor Moves: the loaded recipe has no steps.")
            return
        if not messagebox.askokcancel(
                "Run (Minor Moves)",
                f"Probe {len(shots)} shot(s), visiting only the die(s) the "
                "recipe references in each?\n\nTHIS MEASURES. Z is verified "
                "before each measurement; the chuck is separated at the end."):
            return
        self._running = True
        try:
            self._main_layout._exec_set_running_buttons(True)
        except Exception:
            pass
        self._abort = False
        self._set_run_state("RUNNING (Minor Moves)", "#2563eb")
        self._log(f"[PMA] Run (Minor Moves) — {len(shots)} shot(s).")
        threading.Thread(
            target=self._minor_move_thread,
            args=(shots, origin, shot_rows, shot_cols, shot_cells),
            daemon=True).start()

    def _minor_move_thread(self, shots: list, origin: tuple,
                           shot_rows: int, shot_cols: int, shot_cells: dict):
        """One touchdown per shot, exactly like the .PMA-driven path above -
        the difference is what happens AT that touchdown. A shot lands on
        die #1 automatically, then the loaded recipe's steps run flat, top
        to bottom, once: a "move" step (recipe_panel._STEP_TYPES) is what
        repositions to any OTHER die # within that same shot. Mirrors
        instrument_panel.py's _exec_minor_move_thread (Accretech) using
        goto_die() instead of move_to_die_xy() - see that method for the
        fuller design note.
        """
        drv = self._prober()
        origin_x, origin_y = origin
        layout = self._main_layout
        error_msg = None

        # Set Shot Origin was captured with the chuck on shot (0,0)'s die
        # #1 (Wafer Builder Shot-tab numbering), not necessarily grid cell
        # (0,0) - present_slots()'s "order" can put die #1 anywhere in the
        # shot. So every absolute coordinate below is offset relative to
        # die #1's own (row, col) within a shot, not the shot's raw origin.
        die1_rc = shot_die_rc(shot_cells, shot_rows, shot_cols, 1)
        if die1_rc is None:
            self._ui(lambda: self._log(
                "[PMA] Minor Moves: this shot has no die #1 - treating "
                "grid cell (0,0) as the reference instead."))
            die1_rc = (0, 0)
        r1, c1 = die1_rc

        class _Stop(Exception):
            pass

        def goto_shot_die(shot_row, shot_col, die_num):
            rc = shot_die_rc(shot_cells, shot_rows, shot_cols, die_num)
            if rc is None:
                raise RuntimeError(f"die #{die_num} is not on shot "
                                   f"R{shot_row}C{shot_col}")
            r, c = rc
            die_x = origin_x + shot_col * shot_cols + (c - c1)
            die_y = origin_y + shot_row * shot_rows + (r - r1)
            label = f"shot R{shot_row}C{shot_col} die #{die_num} (X{die_x} Y{die_y})"
            self._ui(lambda lab=label: self._status_var.set(f"moving to {lab}"))
            drv.z_down()
            self._ui(lambda lab=label: self._log(f"[PMA] >> goto_die X={die_x} Y={die_y}"))
            drv.goto_die(die_x, die_y)
            drv.z_up()

        try:
            for shot_row, shot_col in shots:
                if self._abort:
                    break
                self._ui(lambda sr=shot_row, sc=shot_col: self._log(
                    f"[PMA] Shot R{sr}C{sc}: landing on die #1"))
                try:
                    goto_shot_die(shot_row, shot_col, 1)
                except _Stop:
                    break
                layout._exec_move_fn = (
                    lambda die_num, sr=shot_row, sc=shot_col: goto_shot_die(sr, sc, die_num))
                try:
                    ok = bool(layout._exec_run_steps_once())
                finally:
                    layout._exec_move_fn = None
                drv.z_down()
                self._ui(lambda p=ok, sr=shot_row, sc=shot_col: self._log(
                    f"[RESULTS] {'PASS' if p else 'FAIL'}  shot R{sr}C{sc}"))
        except Exception as e:
            error_msg = str(e)
            self._ui(lambda: self._log(f"[PMA] ERROR: {e}"))
        finally:
            layout._exec_move_fn = None
            self._running = False
            self._ui(lambda: self._main_layout._exec_set_running_buttons(False))
            try:
                self._make_safe(drv)
            except Exception:
                pass
            if error_msg:
                self._set_run_state(f"ERROR: {error_msg[:60]}", "#dc2626")
            else:
                self._set_run_state("FINISHED (Minor Moves)", "#16a34a")

    def _pause(self):
        """Finish what is in progress, then hold - the old ⏹ Stop behaviour.

        Position is kept, so ▶ Run carries on from the next touchdown. That
        is the safe way to interrupt a long run to look at something.
        """
        if not self._running:
            return
        self._paused = True
        self._status_var.set("pausing after this touchdown…")
        self._set_run_state("PAUSING…", "#b45309")

    def _stop(self):
        """Stop NOW - do not finish the touchdown in progress.

        The run thread checks _abort between steps, so it stops at the next
        step boundary rather than after the whole shot. It cannot interrupt a
        reading already in flight on the GPIB bus: that is one blocking call
        into the instrument, and abandoning it mid-transfer would desync the
        bus for everything after it.

        Then it makes the bench safe - every channel opened, chuck separated
        - and forgets the position, so ▶ Run restarts the recipe from its
        first touchdown instead of resuming from wherever it was cut off.
        Use ⏸ Pause to keep the position.
        """
        self._abort = True
        self._paused = False
        self._status_var.set("stopping…")
        self._set_run_state("STOPPING…", "#dc2626")

    def _set_run_state(self, text: str, color: str):
        """Drive the Run tab's big state label from this pane's own run.

        It is the one place an operator looks to know what the machine is
        doing, and a .PMA run never touched it - so the label sat on IDLE
        through an entire wafer.
        """
        layout = self._main_layout
        setter = getattr(layout, "_exec_set_state", None)
        if setter is None:
            return
        try:
            self._ui(lambda: setter(text, color))
        except Exception:
            pass

    def _make_safe(self, drv):
        """Open every channel and separate the chuck. Safe to call twice."""
        layout = self._main_layout
        opener = getattr(layout, "_exec_open_all_channels", None)
        if opener is not None:
            try:
                opener()
            except Exception as e:
                self._ui(lambda: self._log(
                    f"[PMA] Could not open the switch channels — "
                    f"{type(e).__name__}: {e}"))
        if drv is not None:
            try:
                drv.z_down()
                self._ui(lambda: self._log("[PMA] Chuck separated (Z down)."))
            except Exception as e:
                self._ui(lambda: self._log(
                    f"[PMA] Could not separate the chuck — "
                    f"{type(e).__name__}: {e}  Check Z before moving."))

    def _publish_total_dies(self) -> int:
        """Tell the stats panel how many DIES this run measures.

        _exec_total_dies was never set on Electroglas, so "untested" was
        computed as 0 - tested and went negative. It also has to be dies rather
        than touchdowns: three probed shots is twelve die results, and NA
        corners are not dies at all.
        """
        total = 0
        for i in self._enabled_indices():
            devs = self._touchdowns[i].get("devices") or []
            total += sum(1 for d in devs
                         if (d or "").strip().upper() not in ("", "NA"))
        try:
            self._main_layout._exec_total_dies = total
            self._main_layout._exec_push_stats()
        except Exception as e:
            self._log(f"[PMA] Could not publish the die total — "
                      f"{type(e).__name__}: {e}")
        return total

    def _start(self, count: int):
        self._running = True
        try:
            self._main_layout._exec_set_running_buttons(True)
        except Exception:
            pass
        self._abort = False
        self._paused = False
        # The measurement engine checks the LAYOUT's abort flag between
        # steps, so a previous stop would make every later run bail on its
        # very first step until something else happened to clear it.
        try:
            self._main_layout._exec_aborted = False
        except Exception:
            pass
        self._publish_total_dies()
        self._set_run_state("RUNNING", "#2563eb")
        drv = self._prober()
        cap = getattr(drv, "max_die_step", 5)

        def _work():
            done = 0
            err = None
            try:
                for _ in range(count):
                    if self._abort or self._paused:
                        break
                    if not self._move_next(drv, cap):
                        break
                    if not self._measure_here(drv):
                        break
                    done += 1
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:80]}"
                self._ui(lambda: self._log(f"[PMA] run aborted — {err}"))
            finally:
                # Always make the bench safe on the way out, however the loop
                # ended. The prober handles Z around its own moves, but
                # nothing moves after the last touchdown - and on a stop
                # nothing moves at all - so without this the needles would
                # stay in contact with channels still closed.
                self._make_safe(drv)
                # Cleared here, not in the Tk callback: if the window is gone the
                # callback never runs and the panel would be dead for good.
                self._running = False
                self._ui(lambda: self._main_layout._exec_set_running_buttons(False))

            stopped, paused = self._abort, self._paused
            if stopped:
                word, state, colour = "stopped", "STOPPED", "#dc2626"
            elif paused:
                word, state, colour = "paused", "PAUSED", "#b45309"
            elif err:
                word, state, colour = "error", "ERROR", "#dc2626"
            else:
                word, state, colour = "finished", "FINISHED", "#16a34a"
            self._set_run_state(state, colour)

            def _settle():
                self._status_var.set(f"{word} — {done} touchdown(s)")
                # Stopped, finished, or errored - the chuck KEEPS its real
                # physical position/anchor (only Z separated, via
                # _make_safe above) so the GUI still knows where it is, but
                # _needs_restart means the next Run/Full Die/Test Selected
                # press starts the whole list over from its first
                # touchdown instead of resuming from here - see _run_all/
                # _move_next. Pause deliberately does neither: position is
                # kept AND the next Run resumes from it, no restart flag.
                if not paused:
                    self._needs_restart = True
                self._mark_current()
                self._refresh_position()
            self._ui(_settle)

        threading.Thread(target=_work, daemon=True).start()

    def _ensure_contact(self, drv) -> bool:
        """Confirm the chuck really is UP before anything is measured.

        With a clearance set, the prober drops Z, moves, and raises it again by
        itself, so a run normally arrives already in contact - the same
        behaviour the joystick shows. Convenient, but never assumed here. If Z
        is silently down (no clearance, or Z TRAVEL MODE back on auto profile,
        which turns ZU into a no-op) every die measures open air and PASSES.
        A false pass is the one failure worth stopping a run for.
        """
        if drv is None:
            return True
        try:
            status = (drv.get_prober_status() or "").upper()
        except Exception as e:
            self._ui(lambda: self._log(
                f"[PMA] Could not read the Z state ({type(e).__name__}: {e}) — "
                "stopping rather than measuring blind."))
            return False
        if "ZU" in status:
            return True
        # Not in contact. Ask for it once - z_up() verifies against ?S and
        # raises if it did not land, so a no-op cannot pass silently.
        try:
            drv.z_up()
            return True
        except Exception as e:
            self._ui(lambda: self._log(
                f"[PMA] Chuck is not in contact ({status or 'no status'}) and ZU "
                f"failed — {e}  Run stopped; nothing was measured."))
            return False

    def _measure_here(self, drv) -> bool:
        """Measure the touchdown the chuck is on. False stops the run.

        One call covers the whole shot: the recipe repeats its block per die
        with the relay channel and probe-card pins set on each, so all four
        dies of the quad are measured before the chuck moves on.
        """
        layout = self._main_layout
        run_steps = getattr(layout, "_exec_run_steps_once", None)
        if run_steps is None:
            self._ui(lambda: self._log(
                "[PMA] No measurement engine on this layout — run stopped."))
            return False
        if not self._ensure_contact(drv):
            return False
        t = self._touchdowns[self._index]
        seq, dev = t["seq"], t["device_id"]
        rc = self._anchor_rc.get(seq)
        if rc is not None:
            layout._exec_current_rc = rc
        # device_id is already the slash-joined quad ("NA/92-74/NA/93-70"),
        # which is exactly what LaMP's fldDieID holds. Without this the export
        # took the die ID of whichever single cell anchored the shot.
        layout._exec_die_id_override = dev
        # Per-slot die ID and map cell, indexed by QUAD_ORDER so slot N is
        # fldSwitch N. The recipe's step names carry "(Die N)", so this is what
        # turns a result into "this reading belongs to die 83-71, at that
        # square" instead of four readings all filed under the shot's corner.
        slots = self._slot_rc.get(seq, {})
        layout._exec_die_ids_by_slot = list(t.get("devices") or [])
        order = slot_names(*self.shot_layout())
        layout._exec_die_rc_by_slot = [slots.get(q) for q in order]
        self._ui(lambda: layout._exec_die_var.set(f"Die: {dev}"))
        try:
            ok = bool(run_steps())
        except Exception as e:
            self._ui(lambda: self._log(
                f"[PMA] Measurement error at #{seq} {dev} — "
                f"{type(e).__name__}: {e}"))
            return False
        # Per-die verdicts when the recipe produced them, so each die's own
        # square goes green or red and the totals count dies rather than shots.
        slot_verdicts = dict(getattr(layout, "_exec_slot_verdicts", None) or {})
        if slot_verdicts:
            ids = t.get("devices") or []

            def _mark():
                for slot, passed in sorted(slot_verdicts.items()):
                    order = slot_names(*self.shot_layout())
                    quad = order[slot - 1] if 1 <= slot <= len(order) else None
                    if quad is None:
                        continue
                    die = ids[slot - 1] if slot - 1 < len(ids) else ""
                    # An NA corner is not a die. It is still measured and
                    # logged - LaMP measured all four switches too, and those
                    # readings are how a shorted corner shows up - but it must
                    # not be painted or counted, or empty positions appear as
                    # failed dies and the tally exceeds the die total.
                    if (die or "").strip().upper() in ("", "NA"):
                        self._log(f"[PMA] #{seq} {quad} (no die): "
                                  f"{'in spec' if passed else 'OUT OF SPEC'} "
                                  "— not counted")
                        continue
                    self.mark_die_result(seq, quad, passed)
                    self._log(f"[PMA] #{seq} {quad} {die}: "
                              f"{'PASS' if passed else 'FAIL'}")
            self._ui(_mark)
        else:
            self._ui(lambda: (self.mark_result(seq, ok),
                              self._log(f"[PMA] #{seq} {dev}: "
                                        f"{'PASS' if ok else 'FAIL'}")))
        return True

    @staticmethod
    def _read_position(drv):
        """?P, surviving one link stall. None if it still cannot be read.

        The 2001X intermittently stops answering mid-run - a query times out and
        the link stays wedged until it is drained or cleared. That is a link
        fault, not a motion fault: the moves either side of it land correctly.
        Letting it propagate would abort a 634-touchdown run over a hiccup, so
        one recover() is attempted. If the position still cannot be read the run
        does stop, because continuing without being able to verify where the
        chuck is means probing dies nobody has confirmed.
        """
        try:
            return parse_position(drv.get_xy_position())
        except Exception:
            pass
        try:
            drv.recover()
            return parse_position(drv.get_xy_position())
        except Exception:
            return None

    def _move_next(self, drv, cap: int) -> bool:
        if getattr(self, "_needs_restart", False):
            # Consumed on this first hop only - _move_to_index below still
            # computes the move FROM self._index (the chuck's real, kept
            # position), just TO the first touchdown instead of "next after
            # current". Every hop after this one goes back to the normal
            # _next_enabled_index(self._index) path.
            self._needs_restart = False
            order = self._enabled_indices()
            nxt = order[0] if order else None
        else:
            nxt = self._next_enabled_index(self._index)
        if nxt is None:
            self._ui(lambda: self._log(
                "[PMA] No further touchdowns in this recipe's list."))
            return False
        # Skipped touchdowns are still MOVED THROUGH by _move_to_index, which
        # steps die by die - this only decides where to stop and probe.
        return self._move_to_index(drv, cap, nxt)

    def _move_to_index(self, drv, cap: int, target: int) -> bool:
        """Move to any touchdown, forward or back. Returns False to stop.

        Direction is just the sign of the delta - the recipe order is a
        convenience, not a constraint, so stepping back or jumping to a die
        picked off the wafer map all go through here.
        """
        if not 0 <= target < len(self._touchdowns):
            return False
        i = self._index
        cur, nxt = self._touchdowns[i], self._touchdowns[target]
        cx, cy = self._grid_xy(cur)
        nx, ny = self._grid_xy(nxt)
        dx, dy = nx - cx, ny - cy
        if (dx, dy) == (0, 0):
            self._index = target
            self._ui(lambda: (self._mark_current(), self._refresh_position()))
            return True

        # Verify the chuck is REALLY at (cx, cy) - what self._index assumes -
        # BEFORE computing/sending anything from that assumption, in EITHER
        # motion mode. This is a check on the datum, not on the move that
        # follows: if self._index is stale (chuck moved by hand, a previous
        # anchor was off, accumulated drift...), the delta computed below is
        # wrong from the start regardless of whether it is sent as MD or MM,
        # and an AFTER-move check only confirms the COMMANDED delta happened,
        # never that it started from the right place - a stale anchor
        # "verified cleanly" while landing somewhere unintended (confirmed
        # cause of a real out-of-bounds move on 21PCM, in MD mode - the same
        # stale-datum failure is exactly as possible in MM mode, since both
        # start from the same self._index). Checking here, before branching
        # on motion mode, catches it before any motion is sent either way.
        real = self._read_position(drv)
        if real is None:
            self._ui(lambda: self._log(
                f"[PMA] STOPPED at #{nxt['seq']}: could not read ?P to verify "
                "the chuck's real position before moving, even after a "
                "recover() - re-anchor once the link is back rather than "
                "assuming."))
            return False
        expected = self._expected_position(cur)
        if real != expected:
            self._anchored = False
            self._ui(lambda r=real, c=expected, s=cur['seq']: self._log(
                f"[PMA] STOPPED: chuck is really at X{r[0]}Y{r[1]} but the "
                f"software expected X{c[0]}Y{c[1]} (touchdown #{s}, accounting "
                "for the origin offset from the last anchor) - re-anchor "
                "(Set Initial) before continuing. No move was sent."))
            self._ui(self._fill_table)
            return False

        if self._motion_var.get() == MOTION_UM:
            return self._move_um(drv, cur, nxt, target, (nx, ny), before=real)

        before = real
        self._ui(lambda: self._status_var.set(
            f"#{nxt['seq']}  MD {dx:+d},{dy:+d}  {nxt['device_id']}"))

        for hop_x, hop_y in chunk_step(dx, dy, cap):
            if self._abort:
                return False
            try:
                drv.move_relative_die(hop_x, hop_y)
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e).splitlines()[0][:70]}"
                self._ui(lambda: self._log(
                    f"[PMA] #{nxt['seq']} MD {hop_x:+d},{hop_y:+d} FAILED — {msg}"))
                return False

        after = self._read_position(drv)
        # The prober counts dies in its own frame, but the DELTA must match what
        # was commanded. If it does not, the map and the machine have diverged
        # and continuing would probe the wrong dies.
        if before is None or after is None:
            self._ui(lambda: self._log(
                f"[PMA] STOPPED at #{nxt['seq']}: could not read ?P to confirm the "
                "move, even after a recover(). The move itself may well have "
                "landed — re-anchor once the link is back rather than assuming."))
            return False
        got = (after[0] - before[0], after[1] - before[1])
        if got != (dx, dy):
            self._ui(lambda: self._log(
                f"[PMA] STOPPED at #{nxt['seq']}: commanded ({dx:+d},{dy:+d}) "
                f"but ?P moved ({got[0]:+d},{got[1]:+d}) — "
                f"{before} -> {after}. Map and machine have diverged."))
            return False

        self._index = target
        self._ui(lambda: (self._mark_current(), self._refresh_position()))
        self._ui(lambda: self._log(
            f"[PMA] #{nxt['seq']} MD {dx:+d},{dy:+d} -> grid ({nx},{ny})  "
            f"{nxt['device_id']}"))
        return True

    def _move_um(self, drv, cur, nxt, target, grid_xy, before) -> bool:
        """Relative MICRON move (MM), the way the original LaMP exe worked.

        The recipe's own coordinates are microns, so the delta between two
        touchdowns is the move, with no die-size arithmetic.

        That does NOT make it assumption-free. MM's count is 2.5 um, measured
        rather than documented, and why it is 2.5 is unknown - if it comes
        from a prober configuration setting then this mode swaps a dependency
        on the die size for a dependency on that, which is no better and is
        harder to notice. MD stays the default until the 2.5 is explained.

        Signs are deliberately the SAME as the MD path (delta straight from
        the recipe, no flip), because MD deltas are themselves recipe deltas
        divided by the pitch and that path is bench-verified.

        Verification is necessarily weaker. ?P counts DIES, in the prober's
        own pitch, so it can only confirm a micron move when that pitch
        happens to match the recipe - which is exactly the assumption this
        mode exists to avoid. So a ?P mismatch is reported and not treated as
        divergence; the driver's own MC/MF acknowledgement check is what
        catches a refused move.

        `before` is the real ?P reading _move_to_index already took and
        verified against the expected pre-move position - passed in rather
        than re-read here so that check (the one that catches a stale anchor
        BEFORE sending anything, in either motion mode) is not silently
        skipped just because this path used to take its own reading after
        the fact, too late to abort on.
        """
        # Carry the sub-count remainder. A quad step is 7042 um and a count is
        # 2.5 um, so 2816.8 counts - and rounding the SAME way every step makes
        # the error accumulate linearly, ~317 um over a 634-touchdown recipe.
        # Adding back what was not delivered last time keeps the total error
        # bounded by half a count instead of growing without limit.
        want_x = (nxt["x"] - cur["x"]) + self._um_residual[0]
        want_y = (nxt["y"] - cur["y"]) + self._um_residual[1]
        dx_um = int(round(want_x))
        dy_um = int(round(want_y))
        self._ui(lambda: self._status_var.set(
            f"#{nxt['seq']}  MM {dx_um:+d},{dy_um:+d} um  {nxt['device_id']}"))

        for hop_x, hop_y in chunk_step(dx_um, dy_um, _MAX_UM_HOP):
            if self._abort:
                return False
            try:
                drv.move_relative_um(hop_x, hop_y)
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e).splitlines()[0][:70]}"
                self._ui(lambda: self._log(
                    f"[PMA] #{nxt['seq']} MM {hop_x:+d},{hop_y:+d} um FAILED — {msg}"))
                return False

        after = self._read_position(drv)
        note = ""
        if before is not None and after is not None:
            got = (after[0] - before[0], after[1] - before[1])
            dxq, dyq = grid_xy[0] - self._grid_xy(cur)[0], grid_xy[1] - self._grid_xy(cur)[1]
            if got != (dxq, dyq):
                note = (f"   [?P moved ({got[0]:+d},{got[1]:+d}) dies, recipe step "
                        f"is ({dxq:+d},{dyq:+d}) — the prober's die size differs "
                        f"from the recipe's, which does not affect a micron move]")

        # What the prober was actually able to deliver, to the nearest count.
        unit = float(getattr(drv, "MM_UNIT_UM", 1.0)) or 1.0
        self._um_residual = [want_x - round(dx_um / unit) * unit,
                             want_y - round(dy_um / unit) * unit]

        self._index = target
        self._ui(lambda: (self._mark_current(), self._refresh_position()))
        self._ui(lambda: self._log(
            f"[PMA] #{nxt['seq']} MM {dx_um:+d},{dy_um:+d} um -> grid "
            f"({grid_xy[0]},{grid_xy[1]})  {nxt['device_id']}{note}"))
        return True

    def _on_motion_mode(self):
        # Grey explanatory text (Microns/MD note) removed - _motion_var
        # itself still drives which radio is selected and which move
        # command _step_once/_run_all use. The Die list table's own
        # grid/step columns show whatever the SELECTED mode would actually
        # move by (_table_position) - switching modes without refilling it
        # left MD-style die-step deltas on screen even after switching to
        # MM, which is not what a real MM run would send.
        #
        # _build_controls calls this once during construction, before
        # _build_table has run and created self._tree - guarded rather than
        # reordering __init__'s build sequence.
        if hasattr(self, "_tree"):
            self._fill_table()

    # -- selection: pick a die on the map or in the table --------------------

    def toggle_move_armed(self):
        """➡ Move to Selected - same arm/target process as Accretech's own
        button (instrument_panel._exec_move_selected_button):

          IDLE ("➡ Move to Selected") --click--> ARMED, no target
              ("✕ Cancel Move") --click a square OR a Die list row-->
              ARMED, one target, highlighted dark blue ("➡ Move to #N")

        While armed, _on_map_click/_on_table_click are the only things that
        can change the target - clicking elsewhere on the map or in the
        table does nothing outside this mode. Pressing the button with a
        target executes the move (after the existing confirm dialog) and
        disarms; with no target, it just cancels.
        """
        if not self._move_armed:
            self._move_armed = True
            self._select(None)
            return
        if self._selected is None:
            self._disarm_move()
            return
        self._goto_selected()

    def _disarm_move(self):
        self._move_armed = False
        self._select(None)

    def _update_move_button(self):
        btn = self._goto_btn
        if btn is None:
            return
        if not self._move_armed:
            btn.config(text="➡ Move to Selected")
            return
        if self._selected is None:
            btn.config(text="✕ Cancel Move")
            return
        t = self._touchdowns[self._selected]
        here = "" if self._index is None else \
            f"  ({self._selected - self._index:+d} from here)"
        btn.config(text=f"➡ Move to #{t['seq']}{here}")

    def _on_map_click(self, row: int, col: int):
        """A die was clicked on the Run tab's wafer map - only picks a Move
        to Selected target while armed (toggle_move_armed)."""
        if not self._move_armed:
            return
        rc = (row, col)
        if rc == self._sel_rc:
            # Clicking the current target again deselects it, same as
            # Accretech's own Move to Selected.
            self._select(None)
            return
        seq = self._seq_at_rc.get(rc)
        if seq is None:
            return
        idx = next((i for i, t in enumerate(self._touchdowns) if t["seq"] == seq), None)
        self._select(idx, clicked_rc=rc)
        if idx is not None:
            self._tree.selection_set(str(idx))
            self._tree.see(str(idx))

    def _on_table_click(self, _event=None):
        """A row was clicked in the Die list - the EG-specific alternative
        to clicking a map square, also only live while armed."""
        if not self._move_armed:
            return
        sel = self._tree.selection()
        idx = int(sel[0]) if sel else None
        if idx == self._selected:
            # _on_map_click's own selection_set(...) re-fires this same
            # event - not a real table click, and re-selecting here would
            # clobber the precise clicked_rc a map click just set.
            return
        # A table row names a touchdown, not one exact clicked corner - its
        # single representative cell (self._rc) still gives the map
        # highlight something concrete to point at.
        rc = self._rc.get(self._touchdowns[idx]["seq"]) if idx is not None else None
        self._select(idx, clicked_rc=rc)

    def _select(self, index, clicked_rc=None):
        self._selected = index
        self._sel_rc = clicked_rc
        self.update_selection_window()
        self._update_move_button()

    def _goto_selected(self):
        if self._selected is None:
            return
        if not self._guard():
            return
        target = self._selected
        t = self._touchdowns[target]
        cur = self._touchdowns[self._index]
        cx, cy = self._grid_xy(cur)
        nx, ny = self._grid_xy(t)
        if not messagebox.askokcancel(
                "Move", f"Move from #{cur['seq']} to #{t['seq']}?\n\n"
                        f"MD {nx - cx:+d},{ny - cy:+d} die steps\n"
                        f"{t['device_id']}"):
            return
        drv = self._prober()
        cap = getattr(drv, "max_die_step", 5)
        self._running = True
        self._abort = False
        # Disarm now (idle button, target highlight cleared) rather than
        # after the move finishes - the move itself runs in the background
        # thread below, same as Accretech's own do_move_to (disarms
        # immediately, executes after).
        self._disarm_move()

        def _work():
            try:
                ok = self._move_to_index(drv, cap, target)
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:80]}"
                self._ui(lambda: self._log(f"[PMA] move failed — {err}"))
                ok = False
            finally:
                self._running = False
            self._ui(lambda: self._status_var.set("moved" if ok else "move stopped"))

        threading.Thread(target=_work, daemon=True).start()

    def _step_back(self):
        if not self._guard():
            return
        if self._index <= 0:
            self._log("[PMA] Already at the first touchdown")
            return
        target = self._index - 1
        drv = self._prober()
        cap = getattr(drv, "max_die_step", 5)
        self._running = True
        self._abort = False

        def _work():
            try:
                self._move_to_index(drv, cap, target)
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:80]}"
                self._ui(lambda: self._log(f"[PMA] back failed — {err}"))
            finally:
                self._running = False
            self._ui(lambda: self._status_var.set("idle"))

        threading.Thread(target=_work, daemon=True).start()
