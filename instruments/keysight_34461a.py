import time
import os
import sys
from instruments.gpib_base import GPIBInstrument

class Keysight34461A(GPIBInstrument):
    def __init__(self, config_key='dmm'):
        super().__init__(config_key)
        self._averages = 1
        if self.is_present():
            try:
                self.reset()
            except Exception as e:
                print(f"[DMM] reset failed: {e}")

    def reset(self):
        self.write("*RST")
        self.write("*CLS")
        self._averages = 1


    def measure_voltage_dc(self):
        if self._averages > 1:
            return self._averaged_read("VOLT:DC")
        reading = self.query("MEASure:VOLTage:DC?")
        try:
            return float(reading)
        except (ValueError, TypeError):
            return 0.0

    def measure_current_dc(self):
        if self._averages > 1:
            return self._averaged_read("CURR:DC")
        reading = self.query("MEASure:CURRent:DC?")
        try:
            return float(reading)
        except (ValueError, TypeError):
            return 0.0

    def set_nplc(self, nplc: float):
        self.write(f"VOLT:DC:NPLC {nplc}")
        self.write(f"CURR:DC:NPLC {nplc}")

    def set_current_range(self, range_a: float):
        self.write(f"CURR:DC:RANG {range_a}")

    def set_sample_count(self, n: int):
        self.write(f"SAMP:COUN {max(1, int(n))}")

    def set_averages(self, channel, count: int):
        count = max(1, int(count))
        self._averages = count
        self.write(f"SAMP:COUN {count}")
        self.write("CALC:AVER:STAT ON" if count > 1 else "CALC:AVER:STAT OFF")

    def _averaged_read(self, func: str):
        self.write(f"CONF:{func}")
        self.write("CALC:AVER:CLE")
        self.write("INIT")
        self.query("*OPC?")
        reading = self.query("CALC:AVER:AVER?")
        try:
            return float(reading)
        except (ValueError, TypeError):
            return 0.0

    def measure_current_dc_avg(self, averages: int = 1) -> float:
        averages = max(1, int(averages))
        if averages > 1:
            try:
                self.set_averages(None, averages)
                return self._averaged_read("CURR:DC")
            except Exception:
                pass
        total = 0.0
        for _ in range(averages):
            reading = self.query("MEASure:CURRent:DC?")
            try:
                total += float(reading)
            except (ValueError, TypeError):
                pass
        return total / averages

    def measure_resistance(self, wire_mode=2):
        if wire_mode == 4:
            reading = self.query("MEASure:FRESistance?")
        else:
            reading = self.query("MEASure:RESistance?")

        try:
            return float(reading)
        except (ValueError, TypeError):
            return 0.0