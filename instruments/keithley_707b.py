import time
import os
import sys
from instruments.gpib_base import GPIBInstrument

class Keithley707B(GPIBInstrument):
    def __init__(self, config_key='switch_matrix'):
        super().__init__(config_key)

    def open_all(self):
        self.write("channel.open('allslots')")

    def close_channel(self, channel: str):
        self.write(f"channel.close('{channel}')")

    def close_crosspoint(self, row, column):
        crosspoint = f"{row}{column}"
        self.write(f"channel.close('{crosspoint}')")

    def open_crosspoint(self, row, column):
        crosspoint = f"{row}{column}"
        self.write(f"channel.open('{crosspoint}')")

    def read_crosspoint(self, crosspoint: str) -> bool:
        resp = self.query(f"print(channel.getstate('{crosspoint}'))")
        return str(resp).strip() == "1" if resp else False

    def query_state(self, channel_list: str) -> str:
        resp = self.query(f"print(channel.getstate('{channel_list}'))")
        return str(resp).strip() if resp else ""

    def query_mainframe_idn(self) -> str:
        parts = []
        for tsp in ("localnode.model", "localnode.serialno", "localnode.revision"):
            val = self.query(f"print({tsp})")
            parts.append(str(val).strip() if val else "?")
        return "  |  ".join(parts)

    def query_slot_info(self, slot: int) -> dict:
        import re

        idn_raw = self.query(f"print(slot[{slot}].idn)")
        idn_full = str(idn_raw).strip() if idn_raw else ""

        idn_str = idn_full.split("\t")[0].strip()

        _EMPTY = {"nil", "empty slot", "no card", "empty", "none", ""}
        if not idn_str or idn_str.lower() in _EMPTY:
            return {"model": "Empty", "idn": "", "start_ch": "", "end_ch": "",
                    "rows": 0, "cols": 0}

        model = idn_str.split(",")[0].strip()

        dim = re.search(r'(\d+)\s*[x*×]\s*(\d+)', idn_str)
        rows = int(dim.group(1)) if dim else 0
        cols = int(dim.group(2)) if dim else 0

        row_letters = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        start_ch = f"{slot}A1" if rows and cols else ""
        end_ch   = f"{slot}{row_letters[rows - 1]}{cols}" if rows and cols else ""

        return {"model": model, "idn": idn_str, "start_ch": start_ch, "end_ch": end_ch,
                "rows": rows, "cols": cols}

    def query_all_slots(self) -> dict:
        return {s: self.query_slot_info(s) for s in range(1, 5)}