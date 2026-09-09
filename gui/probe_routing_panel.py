import tkinter as tk
from tkinter import ttk

import switch_topology as topo

_PALETTE = ["#60a5fa", "#93c5fd", "#fb923c", "#fdba74",
           "#86efac", "#22c55e", "#e879f9", "#f0abfc"]
_UNUSED_COLOR = "#9ca3af"

_N_PROBES = 24


def _probe_slot_col(probe: int):
    if probe <= 12:
        return ("2", probe)
    return ("4", probe - 12)


_LBL_W    = 108
_DOT_R    = 8
_DOT_STEP = 22
_GAP      = 14
_HDR_H    = 38

_CANVAS_W = _LBL_W + _N_PROBES * _DOT_STEP + _GAP + _DOT_R * 2 + 8

_C_OPEN      = "#6b7566"
_C_CLOSED    = "#22c55e"
_C_HL_OPEN   = "#9ba89a"
_C_HL_CLOSED = "#86efac"


def _cx(probe: int) -> int:
    extra = _GAP if probe > 12 else 0
    return _LBL_W + (probe - 1) * _DOT_STEP + _DOT_R + extra


def _cy(row_idx: int) -> int:
    return _HDR_H + row_idx * _DOT_STEP + _DOT_R


def scrollable_routing(parent, controller):
    holder = ttk.Frame(parent)
    holder.rowconfigure(0, weight=1)
    holder.columnconfigure(0, weight=1)

    canvas = tk.Canvas(holder, highlightthickness=0)
    hsb = ttk.Scrollbar(holder, orient="horizontal", command=canvas.xview)
    vsb = ttk.Scrollbar(holder, orient="vertical",   command=canvas.yview)
    canvas.configure(xscrollcommand=hsb.set, yscrollcommand=vsb.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")

    panel = ProbeRoutingPanel(canvas, controller=controller)
    win = canvas.create_window((0, 0), window=panel, anchor="nw")

    def _sync(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
    panel.bind("<Configure>", _sync)
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfig(
                    win, width=max(e.width, panel.winfo_reqwidth())))
    return holder, panel


class ProbeRoutingPanel(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        self._state: dict = {}
        self._dot_ids: dict = {}
        self._row_map: list = []
        self._canvas_h = _HDR_H + 8

        self._scpi_history: list = []
        self._scpi_hist_idx: int = -1

        self.rowconfigure(0, weight=0)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_topbar()
        self._build_matrix()
        self.refresh_topology()


    def _drv(self):
        drv = self.controller.drivers.get("switch")
        return drv if (drv and drv.inst) else None

    def _log(self, msg: str):
        self.controller.log(msg)

    def _active_bench(self) -> str:
        try:
            from instruments import accretech_profiles
            return accretech_profiles.active_name() or "probe08"
        except Exception:
            return "probe08"

    def refresh_topology(self):
        bench = self._active_bench()
        roles = topo.row_roles(bench)
        letters = sorted(roles.keys())
        self._row_map = [
            (letter, topo.role_label(roles.get(letter)),
             _PALETTE[i % len(_PALETTE)] if (roles.get(letter) or {}).get("instrument")
             else _UNUSED_COLOR)
            for i, letter in enumerate(letters)
        ]
        self._canvas_h = _HDR_H + max(1, len(self._row_map)) * _DOT_STEP + 8
        self._redraw_matrix(bench)

    def _build_topbar(self):
        bar = tk.Frame(self, bg="#c8c8c8", relief="flat")
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        bar.columnconfigure(6, weight=1)

        tk.Label(bar, text="Probe Routing Matrix",
                 bg="#c8c8c8", fg="#374151",
                 font=("Segoe UI", 8)).grid(
                 row=0, column=0, columnspan=7, sticky="w", padx=2)

        tk.Button(
            bar, text="Open All",
            bg="#d4d4d4", activebackground="#bdbdbd",
            relief="raised", bd=2,
            font=("Segoe UI", 10),
            command=self._open_all,
        ).grid(row=1, column=0, sticky="w", padx=2, pady=(2, 6))

        ttk.Button(
            bar, text="↻ Read State",
            command=self._read_all,
        ).grid(row=1, column=1, sticky="w", padx=(4, 8), pady=(2, 6))

        ttk.Separator(bar, orient="vertical").grid(
            row=1, column=2, sticky="ns", padx=4, pady=(2, 6))
        tk.Label(bar, text="SCPI/TSP:", bg="#c8c8c8", fg="#374151",
                 font=("Segoe UI", 8)).grid(
                 row=1, column=3, sticky="w", padx=(2, 2), pady=(2, 6))

        self._scpi_entry = ttk.Entry(bar, font=("Consolas", 9), width=16)
        self._scpi_entry.grid(row=1, column=4, sticky="w", padx=2, pady=(2, 6))
        self._scpi_entry.bind("<Return>", lambda _: self._scpi_send())
        self._scpi_entry.bind("<Up>",     self._scpi_hist_prev)
        self._scpi_entry.bind("<Down>",   self._scpi_hist_next)

        ttk.Button(bar, text="Send", width=5,
                   command=self._scpi_send).grid(
                   row=1, column=5, sticky="w", padx=(2, 4), pady=(2, 6))

        self._scpi_resp_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self._scpi_resp_var, bg="#c8c8c8",
                 fg="#1d4ed8", font=("Consolas", 8),
                 anchor="w").grid(
                 row=1, column=6, sticky="ew", padx=(2, 4), pady=(2, 6))


    def _build_matrix(self):
        outer = tk.Frame(self, bg="#b8b8b8", relief="groove", bd=2)
        outer.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self._matrix_outer = outer

        c = tk.Canvas(outer, width=_CANVAS_W, height=self._canvas_h,
                      bg="#8c9688", highlightthickness=0, cursor="hand2")
        c.pack(padx=8, pady=8)
        self._canvas = c
        c.bind("<Button-1>", self._on_click)

    def _redraw_matrix(self, bench_label: str = ""):
        c = self._canvas
        c.delete("all")
        self._dot_ids = {}
        c.config(height=self._canvas_h)

        cx_s2_mid = (_cx(1) + _cx(12)) // 2
        cx_s4_mid = (_cx(13) + _cx(24)) // 2
        title = f"Slot 2  (Probes 1–12)"
        title2 = f"Slot 4  (Probes 13–24)"
        c.create_text(cx_s2_mid, 6, text=title,
                      fill="#1f2937", font=("Segoe UI", 7, "bold"), anchor="n")
        c.create_text(cx_s4_mid, 6, text=title2,
                      fill="#1f2937", font=("Segoe UI", 7, "bold"), anchor="n")
        if bench_label:
            c.create_text(4, 6, text=bench_label, fill="#1f2937",
                          font=("Segoe UI", 7, "bold"), anchor="nw")

        for p in range(1, _N_PROBES + 1):
            c.create_text(_cx(p), 22, text=str(p),
                          fill="#374151", font=("Courier", 7), anchor="n")

        sep_x = (_cx(12) + _cx(13)) // 2
        c.create_line(sep_x, 0, sep_x, self._canvas_h,
                      fill="#6b7280", width=1, dash=(4, 3))

        if not self._row_map:
            c.create_text(_LBL_W, _HDR_H + 12, text="No rows configured for this "
                          "bench - see Switch Settings.", fill="#374151",
                          font=("Segoe UI", 8, "italic"), anchor="w")
            return

        for ri, (row_letter, label, color) in enumerate(self._row_map):
            cy = _cy(ri)
            c.create_text(_LBL_W - 4, cy,
                          text=f"{label}  ({row_letter})",
                          fill=color, font=("Segoe UI", 7, "bold"),
                          anchor="e")
            for probe in range(1, _N_PROBES + 1):
                slot, card_col = _probe_slot_col(probe)
                closed = self._state.get((slot, row_letter, card_col), False)
                self._draw_dot(c, slot, row_letter, card_col, _cx(probe), cy, closed=closed)

    def _draw_dot(self, canvas, slot, row, col, cx, cy, closed: bool):
        key = (slot, row, col)
        old = self._dot_ids.get(key)
        if old:
            canvas.delete(old[0])
            canvas.delete(old[1])

        r    = _DOT_R - 1
        fill = _C_CLOSED if closed else _C_OPEN
        hl   = _C_HL_CLOSED if closed else _C_HL_OPEN

        body = canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                  fill=fill, outline="#2d3748", width=1)
        hr   = max(2, r // 3)
        spec = canvas.create_oval(cx - r + 2, cy - r + 2,
                                  cx - r + 2 + hr, cy - r + 2 + hr,
                                  fill=hl, outline="")
        self._dot_ids[key] = (body, spec)

    def _refresh_dot(self, slot: str, row: str, col: int):
        key = (slot, row, col)
        ids = self._dot_ids.get(key)
        if not ids:
            return
        closed = self._state.get(key, False)
        fill = _C_CLOSED if closed else _C_OPEN
        hl   = _C_HL_CLOSED if closed else _C_HL_OPEN
        self._canvas.itemconfig(ids[0], fill=fill)
        self._canvas.itemconfig(ids[1], fill=hl)

    def _on_click(self, event):
        x, y = event.x, event.y
        if y < _HDR_H:
            return

        ri = (y - _HDR_H) // _DOT_STEP
        if not (0 <= ri < len(self._row_map)):
            return

        probe = None
        for p in range(1, _N_PROBES + 1):
            if abs(x - _cx(p)) <= _DOT_R + 2:
                probe = p
                break
        if probe is None:
            return

        row_letter   = self._row_map[ri][0]
        slot, card_col = _probe_slot_col(probe)
        ch = f"{slot}{row_letter}{card_col:02d}"
        key = (slot, row_letter, card_col)

        drv = self._drv()
        if self._state.get(key, False):
            if drv:
                drv.open_crosspoint(f"{slot}{row_letter}", f"{card_col:02d}")
            self._state[key] = False
            self._log(f"[INSTRUMENT] open  {ch}  (probe {probe})")
        else:
            if drv:
                drv.close_channel(ch)
            self._state[key] = True
            self._log(f"[INSTRUMENT] close {ch}  (probe {probe})")
        self._refresh_dot(slot, row_letter, card_col)


    def _open_all(self):
        drv = self._drv()
        if drv:
            drv.open_all()
        self.mark_all_open()
        self._log("[INSTRUMENT] All channels open")


    @staticmethod
    def _parse_channel(ch: str):
        ch = (ch or "").strip()
        if len(ch) >= 3 and ch[0].isdigit() and ch[1].isalpha() and ch[2:].isdigit():
            return ch[0], ch[1].upper(), int(ch[2:])
        return None, None, None

    def mark_closed(self, ch: str):
        slot, row, col = self._parse_channel(ch)
        if slot is None:
            return
        self._state[(slot, row, col)] = True
        self._refresh_dot(slot, row, col)

    def mark_open(self, ch: str):
        slot, row, col = self._parse_channel(ch)
        if slot is None:
            return
        self._state[(slot, row, col)] = False
        self._refresh_dot(slot, row, col)

    def mark_all_open(self):
        for key in list(self._state):
            self._state[key] = False
        for row_letter, _label, _color in self._row_map:
            for probe in range(1, _N_PROBES + 1):
                slot, card_col = _probe_slot_col(probe)
                self._refresh_dot(slot, row_letter, card_col)

    def read_state(self):
        self._read_all()

    def _read_all(self):
        drv = self._drv()
        if not drv:
            self._log("[INSTRUMENT] Read State: switch not connected")
            return
        rows_in_card = [r[0] for r in self._row_map]
        n_col = 12
        for slot_str in ("2", "4"):
            try:
                chan_list = ",".join(
                    f"{slot_str}{r}{c:02d}"
                    for r in rows_in_card
                    for c in range(1, n_col + 1)
                )
                raw = drv.query_state(chan_list)
                self._log(f"[INSTRUMENT] getstate slot {slot_str}: {raw!r}")
                n_exp = len(rows_in_card) * n_col
                clean = (raw.replace(",", "").replace(";", "")
                            .replace(" ", "").replace("\n", ""))
                if all(ch in "01" for ch in clean) and len(clean) == n_exp:
                    idx = 0
                    for r in rows_in_card:
                        for c in range(1, n_col + 1):
                            self._state[(slot_str, r, c)] = clean[idx] == "1"
                            idx += 1
                for r in rows_in_card:
                    for c in range(1, n_col + 1):
                        self._refresh_dot(slot_str, r, c)
            except Exception as e:
                self._log(f"[INSTRUMENT] Read error slot {slot_str}: {e}")


    def _scpi_send(self):
        cmd = self._scpi_entry.get().strip()
        if not cmd:
            return
        drv = self._drv()
        if not drv:
            self._scpi_print(">> Not connected")
            return
        if not self._scpi_history or self._scpi_history[-1] != cmd:
            self._scpi_history.append(cmd)
        self._scpi_hist_idx = -1
        self._scpi_entry.delete(0, "end")
        self._log(f"[INSTRUMENT] >> {cmd}")
        try:
            if "print(" in cmd.lower():
                resp = drv.query(cmd)
                self._scpi_print(str(resp).strip() if resp else "(no response)")
            else:
                drv.write(cmd)
                self._scpi_print("(sent)")
        except Exception as exc:
            self._scpi_print(f"ERROR: {exc}")

    def _scpi_print(self, text: str):
        self._scpi_resp_var.set(text)
        self._log(f"[INSTRUMENT] << {text}")

    def _scpi_hist_prev(self, _):
        if not self._scpi_history:
            return
        if self._scpi_hist_idx == -1:
            self._scpi_hist_idx = len(self._scpi_history) - 1
        elif self._scpi_hist_idx > 0:
            self._scpi_hist_idx -= 1
        self._scpi_entry.delete(0, "end")
        self._scpi_entry.insert(0, self._scpi_history[self._scpi_hist_idx])

    def _scpi_hist_next(self, _):
        if self._scpi_hist_idx == -1:
            return
        if self._scpi_hist_idx < len(self._scpi_history) - 1:
            self._scpi_hist_idx += 1
            self._scpi_entry.delete(0, "end")
            self._scpi_entry.insert(0, self._scpi_history[self._scpi_hist_idx])
        else:
            self._scpi_hist_idx = -1
            self._scpi_entry.delete(0, "end")
