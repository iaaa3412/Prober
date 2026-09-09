"""HP/Agilent 3458A 8.5-digit multimeter."""

from instruments.gpib_base import GPIBInstrument

OVERLOAD = 1e38


def is_overload(value: float) -> bool:
    return abs(value) >= 1e37

OHMS_RANGES = (10, 100, 1e3, 10e3, 100e3, 1e6, 10e6, 100e6, 1e9)

FUNCTIONS = (
    ("DCV",   "DC volts",         "V", (0.1, 1, 10, 100, 1000)),
    ("ACV",   "AC volts (RMS)",   "V", (0.01, 0.1, 1, 10, 100, 1000)),
    ("ACDCV", "AC+DC volts",      "V", (0.01, 0.1, 1, 10, 100, 1000)),
    ("OHM",   "2-wire ohms",      "Ohm", OHMS_RANGES),
    ("OHMF",  "4-wire ohms",      "Ohm", OHMS_RANGES),
    ("DCI",   "DC amps",          "A", (1e-7, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1)),
    ("ACI",   "AC amps (RMS)",    "A", (1e-4, 1e-3, 1e-2, 0.1, 1)),
    ("ACDCI", "AC+DC amps",       "A", (1e-4, 1e-3, 1e-2, 0.1, 1)),
    ("FREQ",  "Frequency",        "Hz", ()),
    ("PER",   "Period",           "s", ()),
)

DCI_SHUNT = {1e-7: 545.2e3, 1e-6: 45.2e3, 1e-5: 5.2e3, 1e-4: 730.0,
             1e-3: 100.0, 1e-2: 10.0, 0.1: 1.0, 1.0: 0.1}

OHMS_TEST_CURRENT = {
    10: 10e-3, 100: 1e-3, 1e3: 1e-3, 10e3: 100e-6, 100e3: 50e-6,
    1e6: 5e-6, 10e6: 500e-9, 100e6: 500e-9, 1e9: 500e-9,
}


class HP3458A(GPIBInstrument):
    def __init__(self):
        super().__init__('dmm_eg')
        if self.inst:
            try:
                self.write("END ALWAYS")
            except Exception as e:
                print(f"[DMM_EG] END ALWAYS failed, reads may time out: {e}")

    def get_id(self) -> str:
        return self.query("ID?") or ""

    def _triggered_reading(self, func: str, rng=None) -> float:
        if not self.inst:
            raise RuntimeError("3458A is not connected")
        command = func if rng is None else f"{func} {rng}"
        self.write(command)
        self.write("TRIG SGL")
        return float(self.inst.read())

    def measure_voltage_dc(self, rng=None) -> float:
        return self._triggered_reading("DCV", rng)

    def measure_voltage_ac(self, rng=None) -> float:
        return self._triggered_reading("ACV", rng)

    def measure_current_dc(self, rng=None) -> float:
        return self._triggered_reading("DCI", rng)

    def measure_resistance_2w(self, rng=None) -> float:
        return self._triggered_reading("OHM", rng)

    def measure_resistance_4w(self, rng=None) -> float:
        return self._triggered_reading("OHMF", rng)

    def measure_resistance(self, wire_mode=2, rng=None) -> float:
        return (self.measure_resistance_4w(rng) if wire_mode == 4
                else self.measure_resistance_2w(rng))

    def measure(self, func: str, rng=None) -> float:
        return self._triggered_reading(func, rng)


    def terminals(self) -> str:
        return self.query("TERM?") or ""

    def revision(self) -> str:
        return self.query("REV?") or ""

    def error_string(self) -> str:
        return self.query("ERRSTR?") or ""

    def drain_errors(self, limit: int = 12) -> list:
        found = []
        for _ in range(limit):
            entry = self.error_string()
            if not entry or entry.strip().startswith(("0,", '0 ,')):
                break
            found.append(entry)
        return found

    def self_test(self) -> str:
        self.write("TEST")
        return self.error_string()

    def autocal(self, mode: str = "ALL"):
        self.write(f"ACAL {mode}")

    def _arm_eoi(self):
        try:
            self.write("END ALWAYS")
        except Exception as e:
            print(f"[DMM_EG] END ALWAYS after reset failed, "
                  f"reads may time out: {e}")

    def reset(self):
        self.write("RESET")
        self._arm_eoi()

    def preset(self, mode: str = "NORM"):
        self.write(f"PRESET {mode}")
        self._arm_eoi()


    def set_nplc(self, nplc: float):
        self.write(f"NPLC {nplc}")

    def set_ndig(self, digits: int):
        self.write(f"NDIG {int(digits)}")

    def set_nrdgs(self, count: int, event: str = "AUTO"):
        self.write(f"NRDGS {int(count)},{event}")


    averaged_reading_ok = False

    def set_averages(self, channel, count: int):
        count = max(1, int(count))
        self._averages = count
        if count > 1 and self.averaged_reading_ok:
            self.write("MATH STAT")
            self.set_nrdgs(count, "AUTO")
        else:
            self.write("MATH OFF")
            self.set_nrdgs(1, "AUTO")

    def read_average(self) -> float:
        self.write("TARM SGL")
        for _ in range(max(1, getattr(self, "_averages", 1))):
            try:
                self.read()
            except Exception:
                break
        return float(self.query("RMATH MEAN"))

    def autorange(self, on: bool = True):
        self.write(f"ARANGE {'ON' if on else 'OFF'}")

    def autozero(self, on: bool = True):
        self.write(f"AZERO {'ON' if on else 'OFF'}")

    def offset_compensation(self, on: bool = True):
        self.write(f"OCOMP {'ON' if on else 'OFF'}")

    def fixed_input_z(self, on: bool = True):
        self.write(f"FIXEDZ {'ON' if on else 'OFF'}")

    def level_filter(self, on: bool = True):
        self.write(f"LFILTER {'ON' if on else 'OFF'}")

    def set_acband(self, low: float, high: float):
        self.write(f"ACBAND {low},{high}")

    def set_delay(self, seconds: float):
        self.write(f"DELAY {seconds}")

    def beep(self):
        self.write("TONE")


    def raw(self, command: str) -> str:
        if not self.inst:
            raise RuntimeError("3458A is not connected")
        command = command.strip()
        if command.endswith("?"):
            return self.query(command) or ""
        self.write(command)
        return ""
