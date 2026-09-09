from instruments.gpib_base import GPIBInstrument


def _read_element(raw, index, default=0.0):
    try:
        parts = str(raw).strip().split(",")
        return float(parts[index])
    except (ValueError, TypeError, IndexError):
        return default


class Keithley2400(GPIBInstrument):
    def __init__(self, config_key='smu_eg'):
        super().__init__(config_key)
        if self.is_present():
            try:
                self.reset()
            except Exception as e:
                print(f"[SMU_EG] *RST failed: {e}")

    def get_id(self) -> str:
        return self.query("*IDN?") or ""

    TERMINALS = "REAR"

    _FIXED_SETUP = (
        "sour:clear:auto on",
        "sens:aver:tcon rep",
        "syst:rsen off",
        "syst:azer on",
        "syst:azer:cach off",
        "syst:guar cabl",
        "SOUR:VOLT:RANG 100",
    )

    def use_measurement_terminals(self):
        self.write(f":ROUT:TERM {self.TERMINALS}")

    def set_terminals(self, which: str):
        which = (which or "").strip().upper()
        if which not in ("FRONT", "REAR"):
            raise ValueError(f"terminals must be FRONT or REAR, got {which!r}")
        self.TERMINALS = which
        self.write(f":ROUT:TERM {self.TERMINALS}")

    def get_terminals(self) -> str:
        return self.TERMINALS

    def configure_for_measurement(self):
        self.use_measurement_terminals()
        for cmd in self._FIXED_SETUP:
            self.write(cmd)

    def reset(self):
        self.write("*RST")
        self.configure_for_measurement()

    def set_voltage(self, channel, volts):
        self.use_measurement_terminals()
        self.write(":SOUR:FUNC VOLT")
        self.write(":SOUR:VOLT:MODE FIX")
        self.write(f":SOUR:VOLT:LEV {volts}")

    def set_source_delay(self, seconds: float):
        self.write(f":SOUR:DEL {float(seconds)}")

    def set_averages(self, channel, count: int):
        self.write(f":SENS:AVER:COUN {int(count)}")
        if int(count) > 1:
            self.write(":SENS:AVER:TCON REP")
            self.write(":SENS:AVER:STATE ON")
        else:
            self.write(":SENS:AVER:STATE OFF")

    def set_current_range(self, channel, amps):
        self.write(f":SENS:CURR:RANGE {amps}")

    def turn_output_on(self, channel):
        self.write(":OUTP ON")

    def turn_output_off(self, channel):
        self.write(":OUTP OFF")

    def set_current(self, channel, amps):
        self.write(":SOUR:FUNC CURR")
        self.write(":SOUR:CURR:MODE FIXED")
        self.write(f":SOUR:CURR {amps}")

    def set_current_limit(self, channel, amps):
        self.write(f":SENS:CURR:PROT {amps}")

    def set_voltage_limit(self, channel, volts):
        self.write(f":SENS:VOLT:PROT {volts}")

    def set_nplc(self, channel, nplc: float):
        self.write(f":SENS:CURR:NPLC {nplc}")
        self.write(f":SENS:VOLT:NPLC {nplc}")
        self.write(f":SENS:RES:NPLC {nplc}")

    def set_auto_zero(self, enabled: bool):
        self.write(f"syst:azer {'on' if enabled else 'off'}")

    def set_source_clear_auto(self, enabled: bool):
        self.write(f"sour:clear:auto {'on' if enabled else 'off'}")

    def measure_current(self, channel):
        self.write(":SENS:FUNC 'CURR'")
        self.write(":FORM:ELEM CURR")
        return _read_element(self.query(":READ?"), 0)

    def measure_voltage(self, channel):
        self.write(":SENS:FUNC 'VOLT'")
        self.write(":FORM:ELEM VOLT")
        return _read_element(self.query(":READ?"), 0)

    def measure_current_and_voltage(self, channel):
        self.write(":SENS:FUNC 'CURR','VOLT'")
        self.write(":FORM:ELEM CURR,VOLT")
        raw = self.query(":READ?")
        return _read_element(raw, 1), _read_element(raw, 0)

    def measure_resistance(self, channel, manual=False):
        self.write(f":SENS:RES:MODE {'MANUAL' if manual else 'AUTO'}")
        self.write(":SENS:FUNC 'RESISTANCE'")
        self.write(":FORM:ELEM RES")
        return _read_element(self.query(":READ?"), 0)

    def in_compliance(self, channel) -> bool:
        for q in (":SENS:CURR:PROT:TRIP?", ":SENS:VOLT:PROT:TRIP?"):
            reading = self.query(q)
            if str(reading).strip() in ("1", "true", "True"):
                return True
        return False
