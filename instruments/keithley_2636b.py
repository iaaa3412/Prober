import time
import os
import sys
from instruments.gpib_base import GPIBInstrument

class Keithley2636B(GPIBInstrument):
    def __init__(self, config_key='smu'):
        super().__init__(config_key)
        if self.is_present():
            try:
                self.reset()
            except Exception as e:
                print(f"[SMU] reset failed: {e}")

    def reset(self):
        self.write("smua.reset()")
        self.write("smub.reset()")

    def set_voltage(self, channel, volts):
        self.write(f"{channel}.source.func = {channel}.OUTPUT_DCVOLTS")
        self.write(f"{channel}.source.levelv = {volts}")

    def turn_output_on(self, channel):
        self.write(f"{channel}.source.output = {channel}.OUTPUT_ON")

    def turn_output_off(self, channel):
        self.write(f"{channel}.source.output = {channel}.OUTPUT_OFF")

    def set_current(self, channel, amps):
        self.write(f"{channel}.source.func = {channel}.OUTPUT_DCAMPS")
        self.write(f"{channel}.source.leveli = {amps}")

    def set_current_limit(self, channel, amps):
        self.write(f"{channel}.source.limiti = {amps}")

    def get_current_limit(self, channel):
        reading = self.query(f"print({channel}.source.limiti)")
        try:
            return float(reading)
        except Exception:
            return None

    def set_voltage_limit(self, channel, volts):
        self.write(f"{channel}.source.limitv = {volts}")

    def measure_current(self, channel):
        reading = self.query(f"print({channel}.measure.i())")
        try:
            return float(reading)
        except Exception:
            return 0.0

    def measure_voltage(self, channel):
        reading = self.query(f"print({channel}.measure.v())")
        try:
            return float(reading)
        except Exception:
            return 0.0

    def measure_resistance(self, channel):
        reading = self.query(f"print({channel}.measure.r())")
        try:
            return float(reading)
        except Exception:
            return 0.0

    def set_nplc(self, channel: str, nplc: float):
        self.write(f"{channel}.measure.nplc = {nplc}")

    def set_averages(self, channel: str, count: int):
        count = max(1, int(count))
        if count > 1:
            self.write(f"{channel}.measure.filter.count = {count}")
            self.write(f"{channel}.measure.filter.type = {channel}.FILTER_REPEAT_AVG")
            self.write(f"{channel}.measure.filter.enable = {channel}.FILTER_ON")
        else:
            self.write(f"{channel}.measure.filter.enable = {channel}.FILTER_OFF")

    def in_compliance(self, channel) -> bool:
        reading = self.query(f"print({channel}.source.compliance)")
        return str(reading).strip().lower() == "true"