"""Switchbox debug panel - one view per card family, following the active bench.

WHY THIS IS NOT ONE FIXED LAYOUT. Two different card families are fitted, they
behave nothing alike, and all three cards answer *IDN? with the identical
string. Which card sits at which secondary address also changes with the bench:
9::15 is an E1345A multiplexer on probe02 and an E1343A on probe03, and
probe03's wired form C card at 9::10 does not exist on probe02 at all. So the
card list comes from the active profile, the card TYPE is read back with
SYST:CTYP?, and the controls drawn depend on what actually answered.

The dangerous version of getting this wrong is quiet: present form C controls
for a multiplexer and closing "channel 3" appears to work while connecting
nothing, because a mux channel reaches the outside world only through its tree
switch. That looks exactly like a healthy open circuit.

  MULTIPLEXER (E1343A / E1345A)
    16 channels of High/Low/Guard in two banks, reaching the AT / BT / AT2 tree
    switches - themselves channels 90, 91 and 92:

      ch00..07  Bank 0 --[ AT 90 ]-- analog bus H / L / G     (sense)
                              |
                         [ AT2 92 ]
                              |
      ch08..15  Bank 1 --[ BT 91 ]-- analog bus I+ / I- / IG  (source)

    2-wire  = AT + one Bank 0 channel.
    4-wire  = AT + BT + channel N + channel N+8.

  FORM C (E1364A)
    16 independent SPDT relays. Closed = Common to NO, open = Common to NC.
    The relays LATCH, so the card powers up however it was left; nothing may be
    assumed until *RST has actually been sent. On probe03 every NC is chained
    to ground, so an open channel GROUNDS its probe pin and all-open is a
    guarded state rather than a floating one.
"""

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import app_settings
from instruments import eg_profiles
from instruments.hp_switchbox import (
    BANK0, BANK1, CHANNELS, COAX_OF_CHANNEL, DIE_SETS, FAMILY_FORM_C,
    FAMILY_MUX, GROUND_TERMINAL_CHANNEL, LAMP_SWITCH_OF_CHANNEL, NODE_LABELS,
    NODE_OF_CHANNEL, POLARITY_OF_CHANNEL, TREE_AT, TREE_AT2, TREE_BT,
    TREE_LABELS, TREE_SWITCHES, bench_wiring, conflicts_with, describe_channel,
    describe_channel_on, die_of_channel, die_of_channel_on, fres_partner,
)

# Driver keys, in the order the profile lists them.
_RELAY_KEYS = ("relay1_eg", "relay2_eg", "relay3_eg")
_DRIVER_KEY = {"relay1_eg": "relay1", "relay2_eg": "relay2", "relay3_eg": "relay3"}

# Canvas palette, matching the Accretech routing matrix so the two read alike.
_C_BG = "#8c9688"
_C_OPEN = "#6b7566"
_C_CLOSED = "#22c55e"
_C_HL_OPEN = "#9ba89a"
_C_HL_CLOSED = "#86efac"
_C_TREE_OPEN = "#7a6f56"
_C_TREE_CLOSED = "#f59e0b"
_C_PATH = "#22c55e"
_C_DEAD = "#b45309"
_C_TEXT = "#1f2937"
_C_DIM = "#4b5563"

_DOT_R = 9
_ROW_H = 26
_LBL_W = 118


class SwitchboxTestPanel(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self._busy = False
        self._key = None            # active profile key, e.g. "relay1_eg"
        self._family = None
        self._card_type = ""
        self._state = {}            # channel -> bool
        self._dots = {}             # channel -> (body, spec)
        self._walk_order = [c for c in CHANNELS if c in COAX_OF_CHANNEL]

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self._build_topbar()
        self._build_canvas()
        self._build_actions()
        self._build_assignments()
        self._reload_cards()
        self._refresh_assignments()

    def _layout(self):
        """The Electroglas MainLayout, for reaching the active probe card."""
        try:
            return self.controller._by_system["electroglas"]["ui"]
        except Exception:
            return None

    def _card_die_pins(self) -> dict:
        """{slot: (hi, lo)} from the active probe card, or {}."""
        layout = self._layout()
        wiring = getattr(layout, "pin_wiring", None) if layout else None
        getter = getattr(wiring, "get_die_pins", None)
        try:
            return getter() or {} if callable(getter) else {}
        except Exception:
            return {}

    def _card_pin_names(self) -> list:
        layout = self._layout()
        wiring = getattr(layout, "pin_wiring", None) if layout else None
        try:
            return sorted({(r.get("pin") or "").strip()
                           for r in (wiring.get_wiring() or [])
                           if (r.get("pin") or "").strip()})
        except Exception:
            return []

    def _pins_for_channel(self, bench: str, channel: int) -> str:
        """The probe-card PINS this channel lands on, via the die it serves.

        Pins rather than pad labels: a pin is the physical contact wired to
        the relay, and it still identifies the same thing on the next probe
        card. "BRU" only means something inside one project's drawing.
        """
        die = die_of_channel_on(bench, channel)
        if die is None:
            return ""
        pair = self._card_die_pins().get(die) or self._card_die_pins().get(str(die))
        if not pair:
            return ""
        return f"{pair[0]} / {pair[1]}"

    # -- channel assignments ------------------------------------------------
    #
    # Every channel of every fitted card, not just the ones this project
    # wired. BENCH_WIRING knows probe02's CH00-03 and probe03's eight; the
    # rest are physically present, unused, and invisible - so claiming one for
    # a new project meant working out from scratch which were free. The table
    # lists all of them, pre-filled with what is already known, and anything
    # typed in is kept per bench.

    def _build_assignments(self):
        lf = ttk.LabelFrame(self, text="Channel assignments (all cards)",
                            padding=6)
        lf.grid(row=4, column=0, sticky="nsew", padx=8, pady=(0, 8))
        lf.rowconfigure(1, weight=1)
        lf.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        bar = ttk.Frame(lf)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Button(bar, text="↻ Refresh",
                   command=self._refresh_assignments).pack(side="right")

        cols = ("card", "chan", "pins", "known", "assignment")
        self._asg = ttk.Treeview(lf, columns=cols, show="headings", height=10)
        for cid, text, width, stretch in (("card", "Card", 200, False),
                                          ("chan", "Ch", 46, False),
                                          ("pins", "Probe-card pins", 120, False),
                                          ("known", "Known wiring", 230, False),
                                          ("assignment", "Assigned to", 240, True)):
            self._asg.heading(cid, text=text)
            self._asg.column(cid, width=width, anchor="w", stretch=stretch)
        self._asg.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._asg.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self._asg.configure(yscrollcommand=sb.set)
        self._asg.bind("<Double-1>", self._edit_assignment)

    def _bench(self) -> str:
        try:
            return eg_profiles.active_name()
        except Exception:
            return ""

    def _refresh_assignments(self):
        tree = getattr(self, "_asg", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        bench = self._bench()
        saved = app_settings.get_channel_assignments(bench)
        try:
            fitted = set(eg_profiles.fitted_keys(bench))
            inst = eg_profiles.instruments(bench)
        except Exception:
            fitted, inst = set(), {}
        wiring = bench_wiring(bench)
        wired_key = wiring.get("driver_key") or ""
        for key in _RELAY_KEYS:
            if key not in fitted:
                continue
            name = (inst.get(key, {}) or {}).get("name") or key
            for ch in CHANNELS:
                known = ""
                if key == wired_key:
                    die = die_of_channel_on(bench, ch)
                    if die is not None:
                        known = describe_channel_on(bench, ch)
                iid = f"{key}/{ch:02d}"
                pins = self._pins_for_channel(bench, ch) if key == wired_key else ""
                tree.insert("", "end", iid=iid,
                            values=(name, f"{ch:02d}", pins, known,
                                    saved.get(iid, "")))
        # Tree switches are addressable channels too, and forgetting that is
        # how a "spare" channel turns out to be the thing routing a bank.
        if wiring.get("family") == FAMILY_MUX and wired_key in fitted:
            name = (inst.get(wired_key, {}) or {}).get("name") or wired_key
            for tree_ch in TREE_SWITCHES:
                iid = f"{wired_key}/{tree_ch}"
                tree.insert("", "end", iid=iid,
                            values=(name, str(tree_ch), "",
                                    TREE_LABELS.get(tree_ch, "tree switch"),
                                    saved.get(iid, "")))

    def _edit_assignment(self, _event=None):
        tree = self._asg
        sel = tree.selection()
        if not sel:
            return
        iid = sel[0]
        card, chan, known, current = tree.item(iid, "values")
        new = simpledialog.askstring(
            "Assign channel",
            f"{card}\nchannel {chan}"
            + (f"\n\nKnown wiring: {known}" if known else "")
            + "\n\nWhat is this channel used for? (blank to clear)",
            initialvalue=current, parent=self)
        if new is None:
            return
        app_settings.set_channel_assignment(self._bench(), iid, new)
        tree.item(iid, values=(card, chan, known, new.strip()))
        self._log(f"[SETUP] {card} ch{chan} assigned to "
                  f"{new.strip() or '(cleared)'}")

    # -- plumbing -----------------------------------------------------------

    def _log(self, msg: str):
        self.controller.log(msg)

    def _drv(self, key=None):
        key = key or self._key
        drv = self.controller.drivers.get(_DRIVER_KEY.get(key, ""))
        return drv if (drv and drv.inst) else None

    def _ui(self, fn):
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _run(self, label, fn):
        if self._busy:
            self._log(f"[INSTRUMENT] Busy — {label} ignored")
            return
        drv = self._drv()
        if not drv:
            self._log(f"[INSTRUMENT] {self._key or 'card'} not connected")
            return
        self._busy = True
        self._status.set(f"… {label}")

        def _work():
            try:
                done = fn(drv)
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:90]}"
                self._ui(lambda: self._log(f"[INSTRUMENT] {label} failed — {err}"))
                done = None
            finally:
                self._busy = False
                self._ui(lambda: self._status.set("idle"))
            if callable(done):
                self._ui(done)

        threading.Thread(target=_work, daemon=True).start()

    # -- layout -------------------------------------------------------------

    def _build_topbar(self):
        bar = tk.Frame(self, bg="#c8c8c8")
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))

        top = tk.Frame(bar, bg="#c8c8c8")
        top.pack(fill="x", padx=4, pady=(4, 0))
        self._bench_lbl = tk.StringVar()
        tk.Label(top, textvariable=self._bench_lbl, bg="#c8c8c8", fg=_C_TEXT,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        ttk.Button(top, text="↻ Reload cards", command=self._reload_cards).pack(
            side="right")

        row = tk.Frame(bar, bg="#c8c8c8")
        row.pack(fill="x", padx=4, pady=4)
        tk.Label(row, text="Card:", bg="#c8c8c8", fg=_C_TEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        self._card_var = tk.StringVar()
        self._card_cb = ttk.Combobox(row, textvariable=self._card_var, width=34,
                                     state="readonly")
        self._card_cb.pack(side="left", padx=6)
        self._card_cb.bind("<<ComboboxSelected>>", lambda _e: self._select_card())

        ttk.Button(row, text="Open all", command=self._all_open).pack(
            side="left", padx=(8, 0))
        ttk.Button(row, text="↻ Read state", command=self._read_all).pack(
            side="left", padx=4)

        self._status = tk.StringVar(value="idle")
        tk.Label(row, textvariable=self._status, bg="#c8c8c8", fg=_C_DIM,
                 font=("Consolas", 8)).pack(side="right")

        self._note = tk.StringVar()
        tk.Label(bar, textvariable=self._note, bg="#c8c8c8", fg="#7f1d1d",
                 font=("Segoe UI", 8), justify="left", wraplength=880,
                 anchor="w").pack(fill="x", padx=6, pady=(0, 5))

    def _build_canvas(self):
        holder = ttk.Frame(self)
        holder.grid(row=2, column=0, sticky="nsew", padx=8, pady=6)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(holder, bg=_C_BG, highlightthickness=0,
                                 cursor="hand2")
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._canvas.bind("<Button-1>", self._on_click)

    def _build_actions(self):
        self._actions = ttk.Frame(self)
        self._actions.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))

    # -- card discovery -----------------------------------------------------

    def _reload_cards(self):
        """Rebuild the card list from the active bench profile."""
        try:
            bench = eg_profiles.active_name()
            inst = eg_profiles.instruments(bench)
        except Exception as e:
            self._bench_lbl.set(f"could not read prober profiles: {e}")
            return
        self._bench_lbl.set(f"{eg_profiles.label(bench)}")

        self._cards = []
        for key in _RELAY_KEYS:
            entry = inst.get(key)
            if not entry or not entry.get("fitted", True):
                continue
            addr = entry["address"].replace("GPIB0::", "").replace("::INSTR", "")
            self._cards.append((key, f"{entry.get('name', key)}   [{addr}]"))

        self._card_cb.config(values=[label for _k, label in self._cards])
        if self._cards:
            # Prefer the card the profile calls WIRED - on a bench where only
            # one card goes anywhere, opening on a spare wastes a click and
            # invites poking at the wrong thing.
            default = next((lab for _k, lab in self._cards
                            if "WIRED" in lab.upper()), self._cards[0][1])
            self._card_var.set(default)
            self._select_card()
        else:
            self._card_var.set("")
            self._note.set("No relay cards are fitted on this bench profile.")
            self._canvas.delete("all")

    def _select_card(self):
        label = self._card_var.get()
        self._key = next((k for k, lab in self._cards if lab == label), None)
        self._family = None
        self._card_type = ""
        self._state = {}
        self._note.set("")
        self._draw()                       # placeholder until the type is read
        drv = self._drv()
        if not drv:
            self._note.set(f"{self._key} is not connected — connect on the "
                           "Instruments tab, then Reload cards.")
            return

        def _work(d):
            ctype = (d.card_type() or "").strip()
            fam = d.family()
            states = d.mux_states() if fam == FAMILY_MUX else d.channel_states()
            port = d.scan_port() if fam == FAMILY_MUX else ""
            return lambda: self._adopt(ctype, fam, states, port)

        self._run("identify card", _work)

    def _adopt(self, card_type, family, states, scan_port):
        self._card_type = card_type
        self._family = family
        self._state = dict(states)
        self._scan_port = scan_port
        self._draw()
        self._build_family_actions()
        self._log(f"[INSTRUMENT] {self._key}: {card_type} -> {family}"
                  + (f", SCAN:PORT {scan_port}" if scan_port else ""))

    # -- drawing ------------------------------------------------------------

    def _draw(self):
        c = self._canvas
        c.delete("all")
        self._dots = {}
        if self._family == FAMILY_MUX:
            self._draw_mux()
        elif self._family == FAMILY_FORM_C:
            self._draw_form_c()
        else:
            c.create_text(20, 20, anchor="nw", fill=_C_TEXT,
                          font=("Segoe UI", 10),
                          text=("Reading the card type…" if self._key
                                else "No card selected."))
        c.configure(scrollregion=c.bbox("all"))

    def _dot(self, cx, cy, closed, key, tree=False):
        r = _DOT_R - 1
        fill = (_C_TREE_CLOSED if tree else _C_CLOSED) if closed else \
               (_C_TREE_OPEN if tree else _C_OPEN)
        hl = _C_HL_CLOSED if closed else _C_HL_OPEN
        body = self._canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                        fill=fill, outline="#2d3748", width=1)
        hr = max(2, r // 3)
        spec = self._canvas.create_oval(cx - r + 2, cy - r + 2,
                                        cx - r + 2 + hr, cy - r + 2 + hr,
                                        fill=hl, outline="")
        self._dots[key] = (body, spec, cx, cy)

    def _draw_mux(self):
        c = self._canvas
        col_ch, col_bus, col_tree = _LBL_W, _LBL_W + 210, _LBL_W + 120
        y = 30
        wiring = self._wiring()
        c.create_text(14, 10, anchor="nw", fill=_C_TEXT,
                      font=("Segoe UI", 9, "bold"),
                      text=f"{self._card_type or 'multiplexer'}    "
                           f"SCAN:PORT {getattr(self, '_scan_port', '?')}")
        if wiring:
            c.create_text(14, 26, anchor="nw", fill=_C_DEAD,
                          font=("Segoe UI", 8),
                          text=f"WIRED on {self._bench()} — {wiring['summary']}")
            y = 46

        for bank, channels, tree, bus in (
                (0, BANK0, TREE_AT, "analog bus  H / L / G   (sense)"),
                (1, BANK1, TREE_BT, "analog bus  I+ / I- / IG  (source)")):
            tree_closed = bool(self._state.get(tree))
            top = y
            for ch in channels:
                closed = bool(self._state.get(ch))
                die = die_of_channel_on(self._bench(), ch) if wiring else None
                c.create_text(_LBL_W - 12, y, anchor="e",
                              fill=_C_TEXT if (die or not wiring) else _C_DIM,
                              font=("Consolas", 9),
                              text=f"ch{ch:02d}")
                self._dot(col_ch, y, closed, ch)
                # channel -> bank common
                c.create_line(col_ch + _DOT_R, y, col_tree - 18, y,
                              fill=_C_PATH if closed else _C_DIM,
                              width=3 if closed else 1)
                if wiring:
                    if die:
                        sw = LAMP_SWITCH_OF_CHANNEL.get(ch)
                        c.create_text(col_ch + 22, y, anchor="w", fill=_C_DEAD,
                                      font=("Segoe UI", 8, "bold"),
                                      text=f"die {die}  HI+LO"
                                           + (f"   (LaMP sw {sw})" if sw else ""))
                    else:
                        c.create_text(col_ch + 22, y, anchor="w", fill=_C_DIM,
                                      font=("Segoe UI", 8), text="not wired")
                y += _ROW_H
            bottom = y - _ROW_H
            mid = (top + bottom) // 2
            # bank common bus
            c.create_line(col_tree - 18, top, col_tree - 18, bottom,
                          fill=_C_DIM, width=2)
            c.create_line(col_tree - 18, mid, col_tree - _DOT_R, mid,
                          fill=_C_DIM, width=2)
            c.create_text(col_tree - 22, top - 16, anchor="e", fill=_C_TEXT,
                          font=("Segoe UI", 8, "bold"), text=f"Bank {bank}")
            # tree switch
            self._dot(col_tree, mid, tree_closed, tree, tree=True)
            c.create_text(col_tree, mid - _DOT_R - 9, anchor="s", fill=_C_TEXT,
                          font=("Consolas", 8),
                          text=f"{'AT' if tree == TREE_AT else 'BT'} {tree}")
            c.create_line(col_tree + _DOT_R, mid, col_bus, mid,
                          fill=_C_PATH if tree_closed else _C_DIM,
                          width=3 if tree_closed else 1)
            bus_text = bus
            if wiring and not wiring.get("uses_analog_bus", True):
                # Measured, not assumed: closing a tree alone or with a channel
                # moved the reading by less than the noise on this bench.
                bus_text = bus + "   — NOT in the path on this bench"
            c.create_text(col_bus + 8, mid, anchor="w",
                          fill=_C_TEXT if tree_closed else _C_DIM,
                          font=("Segoe UI", 8), text=bus_text)
            y += 22

        # AT2 sits between the banks
        at2_closed = bool(self._state.get(TREE_AT2))
        self._dot(col_tree + 74, y, at2_closed, TREE_AT2, tree=True)
        c.create_text(col_tree + 92, y, anchor="w", fill=_C_TEXT,
                      font=("Segoe UI", 8),
                      text=f"AT2 {TREE_AT2} — Bank 1 to the AT terminals")

        y += 34
        live = [ch for ch in CHANNELS if self._state.get(ch)]
        for ch in live:
            tree = TREE_AT if ch in BANK0 else TREE_BT
            if not self._state.get(tree) and not (ch in BANK1 and at2_closed):
                c.create_text(14, y, anchor="nw", fill=_C_DEAD,
                              font=("Segoe UI", 8, "bold"),
                              text=f"ch{ch:02d} is closed but its tree switch "
                                   f"({tree}) is open — it connects to nothing.")
                y += 18

    def _draw_form_c(self):
        c = self._canvas
        col_c, col_no, col_nc = _LBL_W, _LBL_W + 150, _LBL_W + 150
        y = 34
        c.create_text(14, 10, anchor="nw", fill=_C_TEXT,
                      font=("Segoe UI", 9, "bold"),
                      text=f"{self._card_type or 'form C switch'}    "
                           "closed = Common→NO,  open = Common→NC")
        wired = bool(COAX_OF_CHANNEL) and self._is_probe03_card()
        for ch in CHANNELS:
            closed = bool(self._state.get(ch))
            node = NODE_OF_CHANNEL.get(ch)
            die = die_of_channel(ch)
            label = f"ch{ch:02d}"
            if wired and ch in COAX_OF_CHANNEL:
                label += f"  coax {COAX_OF_CHANNEL[ch]}"
            c.create_text(_LBL_W - 12, y, anchor="e", fill=_C_TEXT,
                          font=("Consolas", 9), text=label)
            self._dot(col_c, y, closed, ch)
            c.create_line(col_c + _DOT_R, y, col_no - 10, y,
                          fill=_C_PATH if closed else _C_DIM,
                          width=3 if closed else 1)
            if closed:
                target = (NODE_LABELS[node] if node else "NO")
                extra = f"   [die {die} {POLARITY_OF_CHANNEL[ch]}]" if (wired and die) else ""
                c.create_text(col_no, y, anchor="w", fill=_C_TEXT,
                              font=("Segoe UI", 8), text=f"NO → {target}{extra}")
            else:
                grounded = " (grounded)" if wired and ch in COAX_OF_CHANNEL else ""
                c.create_text(col_nc, y, anchor="w", fill=_C_DIM,
                              font=("Segoe UI", 8), text=f"NC{grounded}")
            if ch == GROUND_TERMINAL_CHANNEL and wired:
                c.create_text(col_nc + 120, y, anchor="w", fill=_C_DEAD,
                              font=("Segoe UI", 8),
                              text="NC is the ground entry for the whole bus")
            y += _ROW_H

    def _bench(self) -> str:
        try:
            return eg_profiles.active_name()
        except Exception:
            return ""

    def _wiring(self) -> dict:
        """The active bench's wiring, but ONLY when the selected card is the
        one that wiring describes. Every bench has spare cards that go
        nowhere; drawing a die map against one of those would be inventing
        connections."""
        wiring = bench_wiring(self._bench())
        if wiring.get("driver_key") and self._key == wiring["driver_key"]:
            return wiring
        return {}

    def _is_probe03_card(self) -> bool:
        """probe03's form-C coax map specifically - the E1364A drawing needs
        the coax numbers, which no other bench has."""
        return self._bench() == "probe03" and bool(self._wiring())

    # -- family-specific buttons -------------------------------------------

    def _build_family_actions(self):
        for w in self._actions.winfo_children():
            w.destroy()
        wiring = self._wiring()
        if self._family == FAMILY_MUX and wiring:
            # This card is the wired one - lead with the die buttons, which is
            # what anyone actually wants, and keep the raw routing behind them.
            ttk.Label(self._actions,
                      text=f"{self._bench()} 2×2 shot:").pack(side="left")
            for die in sorted(wiring["die_sets"]):
                ttk.Button(self._actions, text=f"Die {die}", width=7,
                           command=lambda d=die: self._route_die_mux(d)).pack(
                    side="left", padx=2)
            ttk.Button(self._actions, text="Open all", width=9,
                       command=self._open_all_dies).pack(side="left", padx=(8, 0))
            ttk.Separator(self._actions, orient="vertical").pack(
                side="left", fill="y", padx=8)
        if self._family == FAMILY_MUX:
            ttk.Label(self._actions, text="Route:").pack(side="left")
            ttk.Label(self._actions, text="ch").pack(side="left", padx=(8, 2))
            self._route_var = tk.StringVar(value="00")
            ttk.Combobox(self._actions, textvariable=self._route_var, width=4,
                         state="readonly",
                         values=[f"{c:02d}" for c in BANK0]).pack(side="left")
            ttk.Button(self._actions, text="2-wire  (AT + ch)",
                       command=self._route_2wire).pack(side="left", padx=(6, 0))
            ttk.Button(self._actions, text="4-wire  (AT+BT + ch + ch+8)",
                       command=self._route_4wire).pack(side="left", padx=4)
            ttk.Separator(self._actions, orient="vertical").pack(
                side="left", fill="y", padx=8)
            ttk.Label(self._actions, text="SCAN:PORT").pack(side="left")
            ttk.Button(self._actions, text="ABUS",
                       command=lambda: self._set_port("ABUS")).pack(side="left", padx=2)
            ttk.Button(self._actions, text="NONE",
                       command=lambda: self._set_port("NONE")).pack(side="left")
        elif self._family == FAMILY_FORM_C and self._is_probe03_card():
            ttk.Label(self._actions, text="probe03 2×2 shot:").pack(side="left")
            for die in sorted(DIE_SETS):
                ttk.Button(self._actions, text=f"Die {die}", width=7,
                           command=lambda d=die: self._route_die(d)).pack(
                    side="left", padx=2)
            ttk.Separator(self._actions, orient="vertical").pack(
                side="left", fill="y", padx=8)
            ttk.Button(self._actions, text="Save state…",
                       command=self._save_state).pack(side="left")
        else:
            ttk.Label(self._actions,
                      text="Click a channel to toggle it.").pack(side="left")

    # -- actions ------------------------------------------------------------

    def _on_click(self, event):
        x = self._canvas.canvasx(event.x)
        y = self._canvas.canvasy(event.y)
        for ch, (_b, _s, cx, cy) in self._dots.items():
            if abs(x - cx) <= _DOT_R + 3 and abs(y - cy) <= _DOT_R + 3:
                self._toggle(ch)
                return

    def _toggle(self, channel):
        closed_now = bool(self._state.get(channel))
        if not closed_now and self._family == FAMILY_FORM_C and self._is_probe03_card():
            clash = conflicts_with(channel,
                                   [c for c in CHANNELS if self._state.get(c)])
            if clash:
                messagebox.showwarning(
                    "Refused",
                    f"CH{channel:02d} and CH{clash[0]:02d} are both "
                    f"{POLARITY_OF_CHANNEL[channel]} side — closing both shorts "
                    f"coax {COAX_OF_CHANNEL[channel]} to "
                    f"coax {COAX_OF_CHANNEL[clash[0]]}.")
                return

        def _work(d):
            if closed_now:
                d.open_channel(channel)
            else:
                d.close_channel(channel)
            states = d.mux_states() if self._family == FAMILY_MUX else d.channel_states()
            return lambda: self._apply(states,
                                       f"ch{channel:02d} "
                                       f"{'opened' if closed_now else 'closed'}")

        self._run(f"toggle ch{channel:02d}", _work)

    def _apply(self, states, note=""):
        self._state = dict(states)
        self._draw()
        if note:
            self._log(f"[INSTRUMENT] {self._key}: {note}")

    def _read_all(self):
        def _work(d):
            states = d.mux_states() if self._family == FAMILY_MUX else d.channel_states()
            port = d.scan_port() if self._family == FAMILY_MUX else ""
            def _done():
                self._scan_port = port
                closed = [c for c, v in states.items() if v]
                self._apply(states, "closed: " + (
                    ", ".join(f"ch{c:02d}" for c in sorted(closed))
                    if closed else "none — all open"))
            return _done
        self._run("read state", _work)

    def _all_open(self):
        def _work(d):
            d.open_all()
            states = d.mux_states() if self._family == FAMILY_MUX else d.channel_states()
            return lambda: self._apply(states, "*RST — all channels open")
        self._run("all open", _work)

    def _set_port(self, port):
        def _work(d):
            d.set_scan_port(port)
            got = d.scan_port()
            def _done():
                self._scan_port = got
                self._draw()
                self._log(f"[INSTRUMENT] {self._key}: SCAN:PORT {got}")
            return _done
        self._run(f"SCAN:PORT {port}", _work)

    def _route_2wire(self):
        ch = int(self._route_var.get())
        def _work(d):
            d.close_2wire(ch)
            states = d.mux_states()
            return lambda: self._apply(
                states, f"2-wire: AT({TREE_AT}) + ch{ch:02d}")
        self._run(f"2-wire ch{ch:02d}", _work)

    def _route_4wire(self):
        ch = int(self._route_var.get())
        def _work(d):
            d.close_4wire(ch)
            states = d.mux_states()
            return lambda: self._apply(
                states, f"4-wire: AT+BT + ch{ch:02d} + ch{fres_partner(ch):02d}")
        self._run(f"4-wire ch{ch:02d}", _work)

    def _route_die_mux(self, die):
        """Select one die on a multiplexer bench (probe02-style).

        Exactly one channel closed at a time. Closing two does not short
        anything here - a mux channel switches a whole HI/LO pair - but it
        puts two dies in parallel, so the reading would be the pair, not the
        die. Opening the others first is what makes the number mean something.
        """
        wiring = self._wiring()
        channels = wiring["die_sets"][die]
        others = [c for d, chans in wiring["die_sets"].items()
                  for c in chans if d != die]
        def _work(d):
            for c in others:
                d.open_channel(c)
            for c in channels:
                d.close_channel(c)
            states = d.mux_states()
            return lambda: self._apply(
                states, f"die {die} selected — CH"
                        + ", CH".join(f"{c:02d}" for c in channels)
                        + " closed, the other dies open")
        self._run(f"die {die}", _work)

    def _open_all_dies(self):
        wiring = self._wiring()
        chans = [c for chans in wiring["die_sets"].values() for c in chans]
        def _work(d):
            for c in chans:
                d.open_channel(c)
            states = d.mux_states()
            return lambda: self._apply(states, "all dies open")
        self._run("open all dies", _work)

    def _route_die(self, die):
        channels = DIE_SETS[die]
        if not messagebox.askokcancel(
                f"Die {die}",
                "Close CH" + ", CH".join(f"{c:02d}" for c in channels) +
                "\n(coax " + ", ".join(str(COAX_OF_CHANNEL[c]) for c in channels) +
                ")\n\nThe other three dies are opened, i.e. grounded.\n\nProceed?"):
            return
        def _work(d):
            d.route_die(die)
            states = d.channel_states()
            return lambda: self._apply(states, f"die {die} selected")
        self._run(f"die {die}", _work)

    def _save_state(self):
        path = filedialog.asksaveasfilename(
            title="Save relay state", defaultextension=".txt",
            initialfile="relay_state.txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        lines = [f"bench   {eg_profiles.active_name()}",
                 f"card    {self._key}  {self._card_type}",
                 f"family  {self._family}", ""]
        for ch in CHANNELS:
            lines.append(f"ch{ch:02d}  "
                         f"{'CLOSED' if self._state.get(ch) else 'open  '}  "
                         f"{describe_channel_on(self._bench(), ch) if self._wiring() else ''}")
        if self._family == FAMILY_MUX:
            for t in TREE_SWITCHES:
                lines.append(f"ch{t}  "
                             f"{'CLOSED' if self._state.get(t) else 'open  '}  "
                             f"{TREE_LABELS[t]}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self._log("[INSTRUMENT] State saved")
