from instruments.gpib_base import GPIBInstrument


class Agilent6634B(GPIBInstrument):
    def __init__(self):
        super().__init__('power_supply_eg')

    def get_id(self) -> str:
        return self.query("ID?") or ""

    def set_voltage(self, volts):
        self.write(f"VOLT {volts}")

    def set_current_limit(self, amps):
        self.write(f"CURR {amps}")

    def turn_output_on(self):
        self.write("OUTP ON")

    def turn_output_off(self):
        self.write("OUTP OFF")

    def measure_voltage(self) -> float:
        reading = self.query("MEAS:VOLT?")
        try:
            return float(reading)
        except (TypeError, ValueError):
            return 0.0

    def measure_current(self) -> float:
        reading = self.query("MEAS:CURR?")
        try:
            return float(reading)
        except (TypeError, ValueError):
            return 0.0
