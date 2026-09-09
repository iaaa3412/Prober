"""HP/Agilent E1326B 5.5-digit multimeter in the E1300A mainframe."""

from instruments.gpib_base import GPIBInstrument


class HPE1326B(GPIBInstrument):

    def get_id(self) -> str:
        try:
            return self.query("*IDN?") or ""
        except Exception:
            return ""

    def reset(self):
        self.write("*RST")

    def clear_status(self):
        self.write("*CLS")

    def error(self) -> str:
        return self.query("SYST:ERR?") or ""

    def drain_errors(self, limit: int = 10) -> list:
        found = []
        for _ in range(limit):
            entry = self.error()
            if not entry or entry.strip().startswith(("+0,", "0,")):
                break
            found.append(entry)
        return found


    def _measure(self, command: str) -> float:
        resp = self.query(command)
        if resp is None:
            raise RuntimeError(f"no response to {command!r}")
        return float(str(resp).strip().split(",")[0])

    def measure_voltage_dc(self, rng=None, resolution=None) -> float:
        return self._measure(self._with_range("MEAS:VOLT:DC?", rng, resolution))

    def measure_voltage_ac(self, rng=None, resolution=None) -> float:
        return self._measure(self._with_range("MEAS:VOLT:AC?", rng, resolution))

    def measure_resistance_4w(self, rng=None, resolution=None) -> float:
        return self._measure(self._with_range("MEAS:FRES?", rng, resolution))

    def measure_temperature(self, transducer: str, type_: str) -> float:
        return self._measure(f"MEAS:TEMP? {transducer},{type_}")

    @staticmethod
    def _with_range(base: str, rng, resolution) -> str:
        if rng is None:
            return base
        if resolution is None:
            return f"{base} {rng}"
        return f"{base} {rng},{resolution}"
