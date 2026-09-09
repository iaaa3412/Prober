import threading
import tkinter as tk
from tkinter import messagebox, ttk

from instruments.gpib_base import (
    load_all_instrument_configs, set_instrument_address, ping_address, send_raw_command,
    discover_bus,
)

_DEFAULT_ID_QUERIES = ("*IDN?", "ID?")


def build_address_panel(parent, instruments, log_fn, reconnect_fn, height=220):
    outer = ttk.LabelFrame(parent, text="GPIB / VISA Addresses (instruments.yaml)", padding=6)

    body = ttk.Frame(outer)

    canvas = tk.Canvas(body, highlightthickness=0, height=height)
    vsb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    inner = tk.Frame(canvas)
    win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
    inner.columnconfigure(0, weight=1)
    inner.columnconfigure(1, weight=1)

    def _wheel(e):
        canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
    canvas.bind("<MouseWheel>", _wheel)
    inner.bind("<MouseWheel>", _wheel)

    try:
        current = load_all_instrument_configs()
    except Exception as e:
        ttk.Label(inner, text=f"Could not read instruments.yaml: {e}",
                  foreground="red").pack(anchor="w")
        current = {}

    entries = [(e[0], e[1],
                e[2] if len(e) > 2 else _DEFAULT_ID_QUERIES,
                e[3] if len(e) > 3 else True,
                e[4] if len(e) > 4 else None)
               for e in instruments]

    addr_vars = {}
    ping_vars = {}
    ping_lbls = {}

    def _ping(name, addr_var, status_var, status_lbl, id_queries, write_probe):
        address = addr_var.get().strip()
        if not address:
            status_var.set("no address entered")
            status_lbl.config(foreground="gray")
            return
        status_var.set("pinging…")
        status_lbl.config(foreground="#0077cc")
        def _run():
            ok, msg = ping_address(address, id_queries=id_queries,
                                   write_probe=write_probe)
            text = msg
            def _apply():
                status_var.set(text)
                status_lbl.config(foreground="green" if ok else "red")
            inner.after(0, _apply)
            log_fn(f"[INSTRUMENT] {name} ({address}): {text}")
        threading.Thread(target=_run, daemon=True).start()


    def _ping_all():
        targets = []
        for name, key, id_queries, fitted, write_probe in entries:
            if not fitted:
                ping_vars[key].set("not fitted — skipped")
                ping_lbls[key].config(foreground="gray")
                continue
            targets.append((name, key, addr_vars[key].get().strip(),
                            id_queries, write_probe))
            ping_vars[key].set("queued…")
            ping_lbls[key].config(foreground="gray")

        def _run():
            for name, key, address, id_queries, write_probe in targets:
                if not address:
                    inner.after(0, lambda k=key: ping_vars[k].set("no address entered"))
                    continue
                inner.after(0, lambda k=key: ping_vars[k].set("pinging…"))
                ok, msg = ping_address(address, id_queries=id_queries,
                                       write_probe=write_probe)
                text = msg
                def _apply(k=key, t=text, good=ok):
                    ping_vars[k].set(t)
                    ping_lbls[k].config(foreground="green" if good else "red")
                inner.after(0, _apply)
                log_fn(f"[INSTRUMENT] {name} ({address}): {text}")
        threading.Thread(target=_run, daemon=True).start()

    def _scan_bus():
        log_fn("[INSTRUMENT] Identifying every instrument on the bus…")
        configured = {addr_vars[key].get().strip().upper(): name
                      for name, key, _, _, _ in entries if addr_vars[key].get().strip()}

        def _run():
            try:
                found = discover_bus()
            except Exception as e:
                log_fn(f"[INSTRUMENT] failed: {e}")
                return
            if not found:
                log_fn("[INSTRUMENT] nothing answering — check the GPIB adapter and that "
                       "the instruments are powered on")
                return

            for item in found:
                address = item["address"]
                known = configured.get(address.upper())
                label = known or "NOT IN instruments.yaml"
                identity = item["identity"] or "(answers no ID query)"
                log_fn(f"[INSTRUMENT]   {address:<22} {identity}   [{label}]")
                for line in item["detail"]:
                    log_fn(f"[INSTRUMENT]   {'':<22}   {line}")

            seen = {item["address"].upper() for item in found}
            for address, name in configured.items():
                if address not in seen:
                    log_fn(f"[INSTRUMENT]   {name}: {address} is configured but nothing answers there")
            log_fn(f"[INSTRUMENT] {len(found)} instrument(s) responding.")
        threading.Thread(target=_run, daemon=True).start()

    def _send(name, addr_var, cmd_var, status_var):
        address = addr_var.get().strip()
        cmd = cmd_var.get().strip()
        if not address or not cmd:
            return
        status_var.set("sending…")
        def _run():
            try:
                text = str(send_raw_command(address, cmd))
            except Exception as e:
                text = f"ERROR: {e}"
            inner.after(0, lambda: status_var.set(text))
            log_fn(f"[{name}] >> {cmd!r}  << {text}")
        threading.Thread(target=_run, daemon=True).start()

    def _save_all():
        try:
            for name, key, _, _, _ in entries:
                address = addr_vars[key].get().strip()
                if not address:
                    messagebox.showerror("Invalid Address", f"Address for '{name}' cannot be empty.")
                    return
                set_instrument_address(key, address)
        except Exception as e:
            messagebox.showerror("Save Failed", str(e))
            return
        log_fn("[SYSTEM] GPIB addresses saved to instruments.yaml — reconnecting...")
        reconnect_fn()

    btns = ttk.Frame(outer)
    btns.pack(fill="x", pady=(0, 6))
    ttk.Button(btns, text="Scan Bus", command=_scan_bus).pack(side="left")
    ttk.Button(btns, text="Ping All", command=_ping_all).pack(side="left", padx=(6, 0))
    ttk.Button(btns, text="Save & Reconnect All", command=_save_all).pack(side="right")

    body.pack(fill="both", expand=True)

    for idx, (name, key, id_queries, fitted, write_probe) in enumerate(entries):
        row_lf = ttk.LabelFrame(inner, text=name if fitted else f"{name}  (not fitted)",
                                padding=4)
        row_lf.grid(row=idx // 2, column=idx % 2, sticky="nsew", padx=2, pady=2)

        addr_row = ttk.Frame(row_lf)
        addr_row.pack(fill="x")
        ttk.Label(addr_row, text="Address:", width=8, anchor="w").pack(side="left")
        address = (current.get(key) or {}).get("address", "")
        addr_var = tk.StringVar(value=address)
        ttk.Entry(addr_row, textvariable=addr_var, width=26, font=("Consolas", 9)).pack(
            side="left", padx=(2, 6))
        ping_status = tk.StringVar(value="—" if fitted else "not fitted on this prober")
        ping_lbl = ttk.Label(addr_row, textvariable=ping_status, foreground="gray",
                             font=("Consolas", 8), wraplength=260, justify="left")
        ttk.Button(addr_row, text="Ping", width=6,
                   command=lambda n=name, av=addr_var, sv=ping_status, sl=ping_lbl,
                   iq=id_queries, wp=write_probe:
                   _ping(n, av, sv, sl, iq, wp)).pack(side="left")
        ping_lbl.pack(side="left", padx=(6, 0))
        addr_vars[key] = addr_var
        ping_vars[key] = ping_status
        ping_lbls[key] = ping_lbl

        cmd_row = ttk.Frame(row_lf)
        cmd_row.pack(fill="x", pady=(2, 0))
        ttk.Label(cmd_row, text="Command:", width=8, anchor="w").pack(side="left")
        cmd_var = tk.StringVar()
        cmd_entry = ttk.Entry(cmd_row, textvariable=cmd_var, width=20, font=("Consolas", 9))
        cmd_entry.pack(side="left", padx=(2, 6))
        send_status = tk.StringVar(value="")
        cmd_entry.bind("<Return>", lambda e, n=name, av=addr_var, cv=cmd_var, sv=send_status:
                        _send(n, av, cv, sv))
        ttk.Button(cmd_row, text="Send", width=6,
                   command=lambda n=name, av=addr_var, cv=cmd_var, sv=send_status:
                   _send(n, av, cv, sv)).pack(side="left")
        ttk.Label(cmd_row, textvariable=send_status, foreground="gray",
                  font=("Consolas", 8), wraplength=260, justify="left").pack(
                  side="left", padx=(6, 0))

    return outer
