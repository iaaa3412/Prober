"""Electroglas run panel for the Operator GUI.

Everything is inherited from EngineerGUI's EgPmaRunPanel; only the die-size
confirmation dialog shown on the first Set is copied here, with plain button
labels ("Send Pitch" instead of "📤 Send to Prober Now", no icons).
"""
import tkinter as tk
from tkinter import messagebox, ttk

from eg_pma_run_panel import EgPmaRunPanel


class OperatorEgPmaRunPanel(EgPmaRunPanel):

    def _confirm_die_size(self, dx: float, dy: float, drv) -> bool:
        result = {"ok": False}
        dlg = tk.Toplevel(self)
        dlg.title("Confirm die size")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        dlg.resizable(False, False)

        body = (
            f"This recipe steps by {dx:.0f} x {dy:.0f} um "
            f"({dx / 1000:.3f} x {dy / 1000:.3f} mm).\n\n"
            "Set Pitch Parameters (Please Confirm on Prober Screen)\n\n"
        )
        ttk.Label(dlg, text=body, wraplength=380, justify="left").pack(
            padx=16, pady=(16, 10))

        btns = ttk.Frame(dlg)
        btns.pack(padx=16, pady=(0, 16), fill="x")

        def _send_now():
            if not drv:
                messagebox.showwarning(
                    "Confirm die size",
                    "Prober not connected - cannot send the die size.",
                    parent=dlg)
                return
            try:
                drv.set_die_size(dx, dy)
                self._log(f"[RUN] >> SP1X{dx:.0f}Y{dy:.0f}  "
                          "(die size sent to prober)")
            except Exception as e:
                messagebox.showerror(
                    "Confirm die size", f"Could not send die size: {e}",
                    parent=dlg)
                return
            result["ok"] = True
            dlg.destroy()

        def _already_set():
            result["ok"] = True
            dlg.destroy()

        def _cancel():
            result["ok"] = False
            dlg.destroy()

        ttk.Button(btns, text="Send Pitch", command=_send_now).pack(
            side="left", padx=(0, 6))
        ttk.Button(btns, text="Already Set", command=_already_set).pack(
            side="left", padx=(0, 6))
        ttk.Button(btns, text="Cancel", command=_cancel).pack(side="right")

        dlg.protocol("WM_DELETE_WINDOW", _cancel)
        dlg.update_idletasks()
        pw = self.winfo_toplevel()
        x = pw.winfo_x() + (pw.winfo_width() - dlg.winfo_width()) // 2
        y = pw.winfo_y() + (pw.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")
        dlg.wait_window()
        return result["ok"]
