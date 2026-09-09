import time
import os
import sys
from instruments.gpib_base import GPIBInstrument

class Keysight33512B(GPIBInstrument):
    def __init__(self, config_key='wave_gen'):
        super().__init__(config_key)
        if self.is_present():
            try:
                self.reset()
            except Exception as e:
                print(f"[WAVE_GEN] *RST failed: {e}")

    def reset(self):
        self.write("*RST")
        self.write("*CLS")
        self.turn_output_off()

    def set_waveform_ch(self, ch: int = 1, shape="SIN", frequency=1000, amplitude=1.0, offset=0.0):
        src = f"SOURce{ch}"
        self.write(f"{src}:FUNCtion {shape}")
        if shape.upper() != "DC":
            self.write(f"{src}:FREQuency {frequency}")
        self.write(f"{src}:VOLTage {amplitude}")
        self.write(f"{src}:VOLTage:OFFSet {offset}")

    def set_waveform(self, shape="SIN", frequency=1000, amplitude=1.0, offset=0.0):
        self.set_waveform_ch(1, shape, frequency, amplitude, offset)

    def set_dc_voltage_ch(self, ch: int = 1, volts: float = 0.0):
        self.write(f"SOURce{ch}:FUNCtion DC")
        self.write(f"SOURce{ch}:VOLTage:OFFSet {volts}")

    def set_dc_voltage(self, volts):
        self.set_dc_voltage_ch(1, volts)

    def set_voltage_limit_ch(self, ch: int = 1, limit_v: float = None):
        src = f"SOURce{ch}"
        if not limit_v:
            self.write(f"{src}:VOLTage:LIMit:STATe OFF")
            return
        self.write(f"{src}:VOLTage:LIMit:HIGH {abs(limit_v)}")
        self.write(f"{src}:VOLTage:LIMit:LOW {-abs(limit_v)}")
        self.write(f"{src}:VOLTage:LIMit:STATe ON")

    def turn_output_on_ch(self, ch: int = 1):
        self.write(f"OUTPut{ch} ON")

    def turn_output_off_ch(self, ch: int = 1):
        self.write(f"OUTPut{ch} OFF")

    def turn_output_on(self):
        self.turn_output_on_ch(1)

    def turn_output_off(self):
        self.turn_output_off_ch(1)