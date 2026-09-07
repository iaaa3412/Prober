import tkinter as tk
from tkinter import ttk

_CARDS = [
    {"slot": "2", "rows": list("ABCDGH"), "cols": 12},
    {"slot": "4", "rows": list("ABCDGH"), "cols": 12},
]

_CONN_LETTERS = list("ABCDEFGH")

_CONN_OPTIONS = {
    "A": ["2A01", "2A11", "4A03", "4A09"],
    "B": ["2B01", "2B11", "4B01", "4B11"],
    "C": [f"2C{c:02d}" for c in range(1, 13)] + [f"4C{c:02d}" for c in range(1, 13)],
    "D": [f"2D{c:02d}" for c in range(1, 13)] + [f"4D{c:02d}" for c in range(1, 13)],
    "E": [],
    "F": [],
    "G": ["4G03"],
    "H": ["4H09"],
}

_DOT_R    = 8
_DOT_STEP = 22
_ROW_LBL  = 30
_TOP_PAD  = 8

_C_OPEN   = "#6b7566"
_C_CLOSED = "#22c55e"
_C_HL_OPEN   = "#9ba89a"
_C_HL_CLOSED = "#86efac"


class SwitchDebugPanel(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        self._conn_vars: dict = {lbl: tk.StringVar() for lbl in _CONN_LETTERS}

        self._state: dict = {}

        self._dot_ids: dict = {}

        self._canvases: dict = {}

        self._scpi_history: list = []
        self._scpi_hist_idx: int = -1

        self.rowconfigure(0, weight=0)
        self.rowconfigure(1, weight=0)
        self.rowconfigure(2, weight=1)
        self.rowconfigure(3, weight=0)
        self.rowconfigure(4, weight=0)
        self.columnconfigure(0, weight=1)

        self._build_topbar()
        self._build_connections()
        self._build_cluster()
        self._build_scpi_terminal()
        self._build_recipe_connections()


    def _drv(self):
        drv = self.controller.drivers.get("switch")
        return drv if (drv and drv.inst) else None

    def _log(self, msg: str):
        self.controller.log(msg)

    def _parse_ch(self, ch: str):
        ch = ch.strip()
        if len(ch) >= 3 and ch[0].isdigit() and ch[1].isalpha() and ch[2:].isdigit():
            return ch[0], ch[1].upper(), int(ch[2:])
        return None, None, None


    def _build_topbar(self):
        bar = tk.Frame(self, bg="#c8c8c8", relief="flat")
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        bar.columnconfigure(0, weight=1)

        tk.Label(bar, text="Switch Keithley", bg="#c8c8c8",
                 fg="#374151", font=("Segoe UI", 8)).grid(
                 row=0, column=0, columnspan=2, sticky="w", padx=2)

        open_btn = tk.Button(
            bar, text="Open CH",
            bg="#d4d4d4", activebackground="#bdbdbd",
            relief="raised", bd=2,
            font=("Segoe UI", 10),
            command=self._open_all)
        open_btn.grid(row=1, column=0, sticky="ew", padx=2, pady=(2, 6))

        read_btn = ttk.Button(bar, text="↻ Read State", command=self._read_all)
        read_btn.grid(row=1, column=1, sticky="e", padx=(4, 2), pady=(2, 6))


    def _build_connections(self):
        outer = tk.Frame(self, bg="#bebebe", relief="groove", bd=2)
        outer.grid(row=1, column=0, sticky="ew", padx=8, pady=4)

        _PAIRS = [("A", "B"), ("C", "D"), ("G", "H")]

        for col_idx, (top, bot) in enumerate(_PAIRS):
            col_f = tk.Frame(outer, bg="#bebebe")
            col_f.grid(row=0, column=col_idx, sticky="ew", padx=10, pady=6)
            outer.columnconfigure(col_idx, weight=1)

            for lbl in (top, bot):
                row_f = tk.Frame(col_f, bg="#bebebe")
                row_f.pack(fill="x", pady=3)

                tk.Label(row_f, text=f"{lbl} Connection",
                         bg="#bebebe", fg="#1f2937",
                         font=("Segoe UI", 8)).pack(anchor="w")

                inp_row = tk.Frame(row_f, bg="#bebebe")
                inp_row.pack(fill="x")

                cb = ttk.Combobox(inp_row,
                                  textvariable=self._conn_vars[lbl],
                                  width=9, values=_CONN_OPTIONS.get(lbl, []))
                cb.pack(side="left", padx=(0, 4))

                tk.Button(inp_row, text="Close Connection",
                          bg="#d4d4d4", activebackground="#bdbdbd",
                          relief="raised", bd=1,
                          font=("Segoe UI", 8),
                          command=lambda l=lbl: self._close_named(l)).pack(side="left")

    def _close_named(self, letter: str):
        ch = self._conn_vars[letter].get().strip()
        if not ch:
            self._log(f"[INSTRUMENT] {letter} Connection: no channel entered")
            return
        drv = self._drv()
        if not drv:
            self._log(f"[INSTRUMENT] Switch not connected — cannot close {letter} → {ch}")
            return
        drv.close_channel(ch)
        self._log(f"[INSTRUMENT] Closed {letter} Connection → {ch}")
        slot, row, col = self._parse_ch(ch)
        if slot:
            self._state[(slot, row, col)] = True
            self._refresh_dot(slot, row, col)


    def _build_cluster(self):
        cluster_outer = tk.Frame(self, bg="#b8b8b8", relief="groove", bd=2)
        cluster_outer.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))

        tk.Label(cluster_outer, text="Cluster", bg="#b8b8b8",
                 font=("Segoe UI", 9, "bold"), fg="#1f2937").pack(
                 anchor="w", padx=10, pady=(4, 2))

        cards_frame = tk.Frame(cluster_outer, bg="#b8b8b8")
        cards_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        for card_idx, spec in enumerate(_CARDS):
            self._build_card_widget(cards_frame, spec, col=card_idx)

    def _build_card_widget(self, parent, spec: dict, col: int):
        slot  = spec["slot"]
        rows  = spec["rows"]
        n_col = spec["cols"]

        card_f = tk.Frame(parent, bg="#a8b0a8", relief="ridge", bd=2)
        card_f.grid(row=0, column=col,
                    padx=(0 if col == 0 else 16, 0), sticky="n")

        tk.Label(card_f, text=f"Switch Card {slot}", bg="#a8b0a8",
                 font=("Segoe UI", 8, "bold"), fg="#1f2937").pack(
                 anchor="w", padx=6, pady=(4, 2))

        canvas_w = _ROW_LBL + n_col * _DOT_STEP + 12
        canvas_h = _TOP_PAD + len(rows) * _DOT_STEP + 8

        c = tk.Canvas(card_f, width=canvas_w, height=canvas_h,
                      bg="#8c9688", highlightthickness=0, cursor="hand2")
        c.pack(padx=6, pady=(0, 6))
        self._canvases[slot] = c

        for ri, row_letter in enumerate(rows):
            cy = _TOP_PAD + ri * _DOT_STEP + _DOT_R

            c.create_text(_ROW_LBL // 2, cy,
                          text=f"{slot}{row_letter}",
                          fill="#1f2937", font=("Courier", 7, "bold"),
                          anchor="center")

            for ci in range(n_col):
                col_num = ci + 1
                cx = _ROW_LBL + ci * _DOT_STEP + _DOT_R
                self._draw_dot(c, slot, row_letter, col_num, cx, cy, closed=False)

        c.bind("<Button-1>",
               lambda e, s=slot, r=rows, nc=n_col:
               self._dot_click(e, s, r, nc))

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
        hr = max(2, r // 3)
        spec = canvas.create_oval(cx - r + 2, cy - r + 2,
                                  cx - r + 2 + hr, cy - r + 2 + hr,
                                  fill=hl, outline="")
        self._dot_ids[key] = (body, spec)

    def _refresh_dot(self, slot: str, row: str, col: int):
        c = self._canvases.get(slot)
        if not c:
            return
        closed = self._state.get((slot, row, col), False)
        key    = (slot, row, col)
        ids    = self._dot_ids.get(key)
        if not ids:
            return
        fill = _C_CLOSED if closed else _C_OPEN
        hl   = _C_HL_CLOSED if closed else _C_HL_OPEN
        c.itemconfig(ids[0], fill=fill)
        c.itemconfig(ids[1], fill=hl)

    def _dot_click(self, event, slot: str, rows: list, n_col: int):
        ci = (event.x - _ROW_LBL) // _DOT_STEP
        ri = (event.y - _TOP_PAD) // _DOT_STEP
        if 0 <= ri < len(rows) and 0 <= ci < n_col:
            row = rows[ri]
            col = ci + 1
            ch  = f"{slot}{row}{col:02d}"
            drv = self._drv()
            if self._state.get((slot, row, col), False):
                if drv:
                    drv.open_crosspoint(f"{slot}{row}", f"{col:02d}")
                self._state[(slot, row, col)] = False
                self._log(f"[INSTRUMENT] open {ch}")
            else:
                if drv:
                    drv.close_channel(ch)
                self._state[(slot, row, col)] = True
                self._log(f"[INSTRUMENT] close {ch}")
            self._refresh_dot(slot, row, col)


    def _open_all(self):
        drv = self._drv()
        if drv:
            drv.open_all()
        self.mark_all_open()
        self._log("[INSTRUMENT] All channels open")


    def mark_closed(self, ch: str):
        slot, row, col = self._parse_ch(ch)
        if slot is None:
            return
        self._state[(slot, row, col)] = True
        self._refresh_dot(slot, row, col)

    def mark_open(self, ch: str):
        slot, row, col = self._parse_ch(ch)
        if slot is None:
            return
        self._state[(slot, row, col)] = False
        self._refresh_dot(slot, row, col)

    def mark_all_open(self):
        for key in list(self._state):
            self._state[key] = False
        for spec in _CARDS:
            for row in spec["rows"]:
                for col in range(1, spec["cols"] + 1):
                    self._refresh_dot(spec["slot"], row, col)

    def read_state(self):
        self._read_all()

    def _read_all(self):
        drv = self._drv()
        if not drv:
            self._log("[INSTRUMENT] Read State: switch not connected")
            return
        for spec in _CARDS:
            slot   = spec["slot"]
            rows   = spec["rows"]
            n_col  = spec["cols"]
            try:
                chan_list = ",".join(
                    f"{slot}{r}{c:02d}"
                    for r in rows
                    for c in range(1, n_col + 1)
                )
                raw = drv.query_state(chan_list)
                self._log(f"[INSTRUMENT] getstate slot {slot}: {raw!r}")
                n_exp  = len(rows) * n_col
                clean  = raw.replace(",","").replace(";","").replace(" ","").replace("\n","")
                if all(ch in "01" for ch in clean) and len(clean) == n_exp:
                    idx = 0
                    for r in rows:
                        for c in range(1, n_col + 1):
                            self._state[(slot, r, c)] = clean[idx] == "1"
                            idx += 1
                else:
                    closed_set = set()
                    for tok in raw.replace(";", ",").split(","):
                        t = tok.strip().upper()
                        if len(t) >= 3 and t[0].isdigit() and t[1].isalpha() and t[2:].isdigit():
                            closed_set.add(t)
                    for r in rows:
                        for c in range(1, n_col + 1):
                            self._state[(slot, r, c)] = f"{slot}{r}{c}" in closed_set

                for r in rows:
                    for c in range(1, n_col + 1):
                        self._refresh_dot(slot, r, c)

            except Exception as e:
                self._log(f"[INSTRUMENT] Read error slot {slot}: {e}")


    def _build_recipe_connections(self):
        lf = ttk.LabelFrame(self, text="Recipe Step Connections", padding=4)
        lf.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 8))
        lf.columnconfigure(0, weight=1)
        ttk.Label(lf,
                  text="707B rows: A=SMU A HI  B=SMU A LO  E=DMM LO  F=DMM HI   •   "
                       "pins 1–12 → slot 2, 13–24 → slot 4   •   "
                       "updates live as the active recipe's steps change",
                  foreground="gray", font=("Arial", 8)).grid(row=0, column=0, sticky="w")
        self._recipe_conn_text = tk.Text(lf, height=4, font=("Consolas", 8),
                                         state="disabled", bg="#f8fafc", wrap="none")
        self._recipe_conn_text.grid(row=1, column=0, sticky="ew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._recipe_conn_text.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self._recipe_conn_text.configure(yscrollcommand=sb.set)

    def set_recipe_connections(self, text: str):
        self._recipe_conn_text.config(state="normal")
        self._recipe_conn_text.delete("1.0", "end")
        self._recipe_conn_text.insert("1.0", text or "— no steps —")
        self._recipe_conn_text.config(state="disabled")

    def _build_scpi_terminal(self):
        lf = ttk.LabelFrame(self, text="SCPI / TSP Terminal", padding=(6, 4))
        lf.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        lf.columnconfigure(0, weight=1)

        inp_row = tk.Frame(lf)
        inp_row.grid(row=0, column=0, sticky="ew")
        inp_row.columnconfigure(0, weight=1)

        self._scpi_entry = ttk.Entry(inp_row, font=("Consolas", 10))
        self._scpi_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._scpi_entry.bind("<Return>",   lambda _: self._scpi_send())
        self._scpi_entry.bind("<Up>",       self._scpi_hist_prev)
        self._scpi_entry.bind("<Down>",     self._scpi_hist_next)

        ttk.Button(inp_row, text="Send", width=8,
                   command=self._scpi_send).grid(row=0, column=1)

        out_frame = tk.Frame(lf)
        out_frame.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        out_frame.columnconfigure(0, weight=1)

        self._scpi_out = tk.Text(
            out_frame, height=5,
            bg="#0f172a", fg="#dbeafe",
            font=("Consolas", 9), wrap="word",
            insertbackground="white", state="disabled")
        self._scpi_out.grid(row=0, column=0, sticky="ew")

        sb = ttk.Scrollbar(out_frame, command=self._scpi_out.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._scpi_out.configure(yscrollcommand=sb.set)

        ttk.Button(lf, text="Clear", width=6,
                   command=self._scpi_clear).grid(
                   row=2, column=0, sticky="e", pady=(2, 0))

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

        self._scpi_print(f">> {cmd}")
        try:
            if "print(" in cmd.lower():
                resp = drv.query(cmd)
                self._scpi_print(str(resp).strip() if resp else "(no response)")
            else:
                drv.write(cmd)
        except Exception as exc:
            self._scpi_print(f"ERROR: {exc}")

    def _scpi_print(self, text: str):
        self._scpi_out.configure(state="normal")
        self._scpi_out.insert("end", text + "\n")
        self._scpi_out.see("end")
        self._scpi_out.configure(state="disabled")

    def _scpi_clear(self):
        self._scpi_out.configure(state="normal")
        self._scpi_out.delete("1.0", "end")
        self._scpi_out.configure(state="disabled")

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
