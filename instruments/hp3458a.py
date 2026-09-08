"""HP/Agilent 3458A 8.5-digit multimeter.

RESISTANCE IS A SOURCED-CURRENT MEASUREMENT. From the manual, chapter 3:
"The multimeter measures resistance by supplying a known current through the
unknown resistance being measured. The current passing through the resistance
generates a voltage across it. The multimeter measures this voltage and
calculates the unknown resistance." That is force-current-read-voltage - which
is exactly the probe03 isolation test - so the 3458A does it natively, on any
ohms range, with no external source.

Table 14, resistance ranges and the current each one sources:

    range     full scale     max resolution   current sourced
    10        12.00000          10 uOhm          10 mA
    100       120.00000         10 uOhm           1 mA
    1k        1.2000000k       100 uOhm           1 mA
    10k       12.000000k         1 mOhm         100 uA
    100k      120.00000k        10 mOhm          50 uA
    1M        1.2000000M       100 mOhm           5 uA
    10M       12.000000M         1 Ohm          500 nA
    100M      120.00000M        10 Ohm          500 nA
    1G        1.2000000G       100 Ohm          500 nA

The 1.2 GOhm ceiling is the headroom that matters for an isolation test; the
E1326B stops at 1.048 MOhm, which a good isolation reading would sail past.

2-WIRE vs 4-WIRE. 4-wire (OHMF) removes lead and contact resistance by sensing
at the far end, and needs the Sense HI/Sense LO pair run all the way to the
device. probe03 lands only two pins per die, so the sense leads could only
terminate back at the relay, which gains nothing. Use 2-wire (OHM) there: at
the megohms an isolation test is looking for, a few ohms of lead resistance is
irrelevant. Do not send OHMF with the sense leads unconnected - the reading is
meaningless rather than merely imprecise.

TERMINALS. Front and rear sets, chosen by the front-panel Terminals switch, and
the driver cannot change it - it is mechanical. Each set carries HI, LO, Sense
HI, Sense LO, Guard, and a fused I terminal for current. The rear set is the
one to wire into the relay card.
"""

from instruments.gpib_base import GPIBInstrument

# What the 3458A returns instead of a number when the input is over range. On an
# ohms function that means "open circuit", which for an isolation test is the
# PASS result - so it must be recognised rather than treated as a bad reading.
# Measured on this bench with nothing connected: OHM 1E9 -> 1E+38.
OVERLOAD = 1e38


def is_overload(value: float) -> bool:
    return abs(value) >= 1e37

# Ranges accepted by OHM/OHMF, in ohms. AUTO is also valid.
OHMS_RANGES = (10, 100, 1e3, 10e3, 100e3, 1e6, 10e6, 100e6, 1e9)

# (command, label, unit, ranges) for every measurement function the 3458A has.
# Sending the command name selects the function; an optional range follows it.
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

# DC current ranges and the shunt each one switches in. The shunt sets the
# burden voltage, which matters when the source is a fixed 10 V: 545.2 kOhm at
# 100 nA full scale is only ~65 mV of burden, i.e. negligible.
DCI_SHUNT = {1e-7: 545.2e3, 1e-6: 45.2e3, 1e-5: 5.2e3, 1e-4: 730.0,
             1e-3: 100.0, 1e-2: 10.0, 0.1: 1.0, 1.0: 0.1}

# What each range sources, for judging whether the current is safe on a device.
OHMS_TEST_CURRENT = {
    10: 10e-3, 100: 1e-3, 1e3: 1e-3, 10e3: 100e-6, 100e3: 50e-6,
    1e6: 5e-6, 10e6: 500e-9, 100e6: 500e-9, 1e9: 500e-9,
}


class HP3458A(GPIBInstrument):
    def __init__(self):
        super().__init__('dmm_eg')
        if self.inst:
            try:
                # Out of reset the 3458A does not assert EOI on its responses,
                # so every read sits waiting for a terminator that never comes
                # and times out - verified on this bench: "ID?" timed out until
                # this was sent, then returned 'HP3458A\r\n'. END ALWAYS only
                # changes output termination, not any measurement setting.
                self.write("END ALWAYS")
            except Exception as e:
                print(f"[DMM_EG] END ALWAYS failed, reads may time out: {e}")

    def get_id(self) -> str:
        return self.query("ID?") or ""

    def _triggered_reading(self, func: str, rng=None) -> float:
        """Set the function, take one triggered reading, return it.

        Raises rather than substituting a value. An earlier version returned
        0.0 on any error, which on an isolation test reads as a dead short -
        a comms failure would have been reported as the worst possible result.
        """
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
        """DC amps - needs the signal on the fused I terminal, not HI."""
        return self._triggered_reading("DCI", rng)

    def measure_resistance_2w(self, rng=None) -> float:
        """OHM - sources current and reads voltage on HI/LO alone.

        The right call for probe03: two pins per die leaves nowhere useful to
        sense from, and lead resistance is noise at isolation-test values.
        """
        return self._triggered_reading("OHM", rng)

    def measure_resistance_4w(self, rng=None) -> float:
        """OHMF - as above but senses on Sense HI/Sense LO.

        Only valid with the sense pair actually run to the device.
        """
        return self._triggered_reading("OHMF", rng)

    def measure_resistance(self, wire_mode=2, rng=None) -> float:
        return (self.measure_resistance_4w(rng) if wire_mode == 4
                else self.measure_resistance_2w(rng))

    def measure(self, func: str, rng=None) -> float:
        """One triggered reading on any function in FUNCTIONS."""
        return self._triggered_reading(func, rng)

    # -- identity and state -------------------------------------------------

    def terminals(self) -> str:
        """TERM? - which input terminals the front-panel switch has selected.

        READ-ONLY BY DESIGN. The manual: "the 3458's input terminals cannot be
        controlled from remote" - it accepts TERM only for language
        compatibility with older meters and generates an error if you try to
        set it. So this reports the mechanical switch; it cannot move it.
        """
        return self.query("TERM?") or ""

    def revision(self) -> str:
        return self.query("REV?") or ""

    def error_string(self) -> str:
        """ERRSTR? - one error at a time, clearing as it goes.

        Returns '0,"NO ERROR"' when both error registers are clear.
        """
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
        """TEST - the full internal self test. Takes a while and disturbs the
        current configuration."""
        self.write("TEST")
        return self.error_string()

    def autocal(self, mode: str = "ALL"):
        """ACAL - internal recalibration. DCV ~1 min, OHMS ~10 min, ALL ~11.

        Disconnect the input first; the manual warns a connected signal can
        spoil the constants. Do not reset or power-cycle while it runs.
        """
        self.write(f"ACAL {mode}")

    def _arm_eoi(self):
        """Re-assert END ALWAYS - see __init__ for why it is needed.

        RESET and PRESET both put the 3458A back to not asserting EOI on
        its responses, and every read then sits waiting for a terminator
        that never arrives until it times out. __init__ sends this once, at
        connect, so a reset ANY time after that silently broke every later
        reading - the instrument answers nothing and looks dead, which is
        exactly what "the DMM stopped responding" looks like from the Run
        tab. Sent after the reset, not before, because the reset is what
        clears it.
        """
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

    # -- configuration ------------------------------------------------------

    def set_nplc(self, nplc: float):
        """Integration time in power line cycles - higher is slower and quieter."""
        self.write(f"NPLC {nplc}")

    def set_ndig(self, digits: int):
        """Digits displayed, 3 to 8."""
        self.write(f"NDIG {int(digits)}")

    def set_nrdgs(self, count: int, event: str = "AUTO"):
        self.write(f"NRDGS {int(count)},{event}")

    # -- averaging ----------------------------------------------------------
    #
    # UNVERIFIED ON HARDWARE. Probed 2026-08-12: this meter does support the
    # pieces - NRDGS? answered "1, 1" and MATH? answered "0,0" - but the full
    # burst-then-RMATH sequence was never run against a known source, and on
    # this bench the 3458A is not wired to the probe path at all.
    #
    # It also does not work like the others. NRDGS n makes ONE trigger return n
    # READINGS, not their mean; the mean only exists because MATH STAT
    # accumulates statistics over the burst, and it is read back with
    # RMATH MEAN. So set_averages() has to arm the burst and the reader has to
    # consume it - which is why averaging state is tracked here rather than
    # left to the instrument alone.
    #
    # Until someone confirms it against a known input, averaged_reading_ok
    # stays False and the runner falls back to averaging in software. That is
    # slower but cannot silently return a number nobody has checked.

    averaged_reading_ok = False

    def set_averages(self, channel, count: int):
        """Arm an internal burst average of COUNT readings. See caveat above."""
        count = max(1, int(count))
        self._averages = count
        if count > 1 and self.averaged_reading_ok:
            self.write("MATH STAT")
            self.set_nrdgs(count, "AUTO")
        else:
            self.write("MATH OFF")
            self.set_nrdgs(1, "AUTO")

    def read_average(self) -> float:
        """The mean of the armed burst (RMATH MEAN). See caveat above."""
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
        """AZERO - leave ON for 4-wire ohms; the manual is explicit that
        disabling it makes those readings inaccurate."""
        self.write(f"AZERO {'ON' if on else 'OFF'}")

    def offset_compensation(self, on: bool = True):
        """OCOMP - measures twice with the source on and off and subtracts, to
        cancel thermal EMFs. Worth it on low ranges, pointless at megohms."""
        self.write(f"OCOMP {'ON' if on else 'OFF'}")

    def fixed_input_z(self, on: bool = True):
        """FIXEDZ - pin the DCV input at 10 MOhm instead of >10 GOhm."""
        self.write(f"FIXEDZ {'ON' if on else 'OFF'}")

    def level_filter(self, on: bool = True):
        self.write(f"LFILTER {'ON' if on else 'OFF'}")

    def set_acband(self, low: float, high: float):
        self.write(f"ACBAND {low},{high}")

    def set_delay(self, seconds: float):
        self.write(f"DELAY {seconds}")

    def beep(self):
        self.write("TONE")

    # -- escape hatch -------------------------------------------------------

    def raw(self, command: str) -> str:
        """Send anything. Queries (trailing '?') are read back."""
        if not self.inst:
            raise RuntimeError("3458A is not connected")
        command = command.strip()
        if command.endswith("?"):
            return self.query(command) or ""
        self.write(command)
        return ""
