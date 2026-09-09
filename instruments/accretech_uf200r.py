import time
from instruments.gpib_base import GPIBInstrument

STB_DESCRIPTIONS = {
    64:  "GP-IB Initial Setting Done",
    65:  "Absolute Value Travel Done (A cmd) — Chuck DOWN at end",
    66:  "Coordinate Travel Done — Chuck DOWN at end (J/S/C/M)",
    67:  "Z UP / Test Start — Chuck UP at end (Z/A/G/J/M/P/S) — wafer in CONTACT with probe card",
    68:  "Z DOWN done (D cmd) — wafer separated from probe card",
    69:  "Marking Done (C/M cmd)",
    70:  "Wafer Loading Done (G/L/N/j2 cmd) — start die positioned, Chuck DOWN",
    71:  "Wafer Unloading Done (U/U0/U9)",
    74:  "Out of Probing Area — X/Y/Z unchanged (A/J/S/js cmd)",
    75:  "Prober Initial Setting Done",
    76:  "Error — check alarm screen",
    77:  "Index Setting Done (I cmd)",
    78:  "Pass Counting Up Done (P cmd)",
    79:  "Fail Counting Up Done (F cmd)",
    80:  "Wafer Count — output at unloading wafer",
    81:  "Wafer End — all dice or sample dice complete",
    82:  "Cassette End",
    84:  "Alignment Rejection Error (j2)",
    85:  "Stop Command Received (K cmd)",
    86:  "Print Data Receiving Done (p cmd)",
    87:  "Warning Error — prober continues working",
    88:  "Test Start (Count Not Needed) — Fail check-back, Chuck UP, result not counted",
    89:  "Needle Cleaning Done (W/jc cmd)",
    90:  "Probing Stop — intended stop, yield NG, or stop on start die",
    91:  "Probing Restart from stop condition",
    92:  "Z Up/Down Fine Adjustment Done (Z± cmd)",
    93:  "Hot Chuck Temp Command Received (h cmd)",
    94:  "Lot Done (jv cmd)",
    98:  "Command Normally Done",
    99:  "Command Abnormally Done / Data Error",
    100: "Test Done received from Tester",
    101: "Alarm Buzzer ON (em cmd)",
    103: "Map Data Download Normally Done",
    104: "Map Data Download Abnormally Done",
    105: "Able to Adjust Needle Height",
    107: "Binary Data Upload Start (du cmd)",
    108: "Binary Data Upload Finish (du cmd)",
    109: "j2 Command Receive OK",
    110: "Needle/Fail Mark OK (jp/jm cmd)",
    111: "Needle/Fail Mark NG (jp/jm cmd)",
    113: "Re-Alignment Done (N1 cmd)",
    114: "Auto Needle Alignment Normally Done (N2 cmd)",
    115: "Auto Needle Alignment Abnormally Done (N2 cmd)",
    116: "Chuck Height Setting Done (z cmd)",
    117: "Continuous Fail Error (custom STB change required)",
    118: "Wafer Loading Done without alignment (L1/L9 cmd)",
    119: "Error Recovery Done / Wafer Centering Complete (es/N9 cmd)",
    120: "Prober Start Normally Done (st cmd)",
    121: "Prober Start Abnormally Done (st cmd)",
    122: "Probe-mark Inspection Finish (np cmd)",
    123: "Fail-mark Inspection Finish (fp cmd)",
}


class AccretechUF200R(GPIBInstrument):
    def __init__(self, config_key='prober'):
        super().__init__(config_key)
        self.z_is_up = None
        if self.inst:
            self.inst.timeout = 30000
            self.inst.write_termination = '\r\n'
            self.inst.read_termination  = '\r\n'


    def get_id(self) -> str:
        return self.get_prober_id()

    def get_prober_id(self) -> str:
        return self.query("B") or ""

    def get_error_code(self) -> str:
        return self.query("E") or ""

    def get_error_message(self) -> str:
        return self.query("e") or ""

    def get_prober_status(self) -> str:
        return self.query("ms") or ""

    def get_xy_position(self) -> str:
        return self.query("Q") or ""

    def get_die_position(self) -> tuple:
        import re
        raw = (self.get_xy_position() or "").strip()
        m = re.search(r'Y\s*([+-]?\d+)\s*X\s*([+-]?\d+)', raw)
        if m:
            return float(m.group(2)), float(m.group(1))
        parts = re.findall(r'[+-]?\d+\.?\d*', raw)
        if len(parts) >= 2:
            return float(parts[1]), float(parts[0])
        raise ValueError(f"Cannot parse Q response: {raw!r}")

    def get_xy_absolute(self) -> str:
        return self.query("R") or ""

    def get_on_wafer_info(self) -> str:
        return self.query("O") or ""

    def get_lot_number(self) -> str:
        return self.query("V") or ""

    def get_wafer_number(self) -> str:
        return self.query("X") or ""

    def get_wafer_id(self) -> str:
        return self.query("b") or ""

    def get_pass_fail_counts(self) -> str:
        return self.query("c") or ""

    def get_gross_value(self) -> str:
        return self.query("Y") or ""

    def get_wafer_status(self) -> str:
        return self.query("w") or ""

    def get_cassette_status(self) -> str:
        return self.query("x") or ""

    def get_yield_data(self) -> str:
        return self.query("y") or ""

    def get_hot_chuck_status(self) -> str:
        return self.query("r") or ""

    def get_chuck_temperature(self) -> str:
        return self.query("f") or ""

    def get_start_die_coords(self) -> str:
        return self.query("q") or ""

    def get_multisite_info(self) -> str:
        return self.query("H") or ""

    def buzzer_clear(self) -> str:
        code = ""
        if not self.inst:
            return code
        old_timeout = self.inst.timeout
        try:
            self.inst.timeout = 3000
            code = (self.query("E") or "").strip()
        except Exception:
            pass
        finally:
            self.inst.timeout = old_timeout
        self.write("es")
        start = time.time()
        while time.time() - start < 5.0:
            try:
                if self.inst.read_stb() == 119:
                    break
            except Exception:
                break
            time.sleep(0.05)
        return code

    def send_es(self):
        self.write("es")

    def _maybe_auto_clear_buzzer(self):
        if not self.inst:
            return
        try:
            old_timeout = self.inst.timeout
            try:
                self.inst.timeout = 3000
                code = (self.query("E") or "").strip()
            finally:
                self.inst.timeout = old_timeout
            if "0691" in code:
                self.send_es()
        except Exception:
            pass

    def confirm_and_clear_alarm(self) -> bool:
        if not self.inst:
            return False
        try:
            if self._confirm_alarm() == 76:
                self.send_es()
                return True
        except Exception:
            pass
        return False

    def read_stb_decoded(self) -> tuple:
        if not self.inst:
            return 0, "Not connected"
        stb = self.inst.read_stb()
        desc = STB_DESCRIPTIONS.get(stb, f"Unknown STB code")
        return stb, desc


    def z_up(self):
        self.write("Z")
        return self._wait_motion_stb({67})

    def z_down(self):
        self.write("D")
        return self._wait_motion_stb({68})

    def emergency_stop(self):
        self.write("K")
        self._wait_for_stb(target_stb=85)
        self.z_is_up = None

    _CASSETTE_TIMEOUT_S = 240

    def unload_wafer(self, timeout_s: float = _CASSETTE_TIMEOUT_S):
        self.write("U")
        stb = self._wait_for_stb_any({71}, timeout_s)
        self.z_is_up = None
        return stb


    def cassette_wait_for_wafer_ready(self, timeout_s=None):
        try:
            return self._wait_for_stb_any({65}, timeout_s)
        except TimeoutError:
            return None

    def cassette_next_die(self, timeout_s=None):
        self.write("J")
        try:
            return self._wait_for_stb_any({66, 67}, timeout_s)
        except TimeoutError:
            return None

    def cassette_unload_and_load_next(self, timeout_s: float = _CASSETTE_TIMEOUT_S):
        self.write("L")
        try:
            stb = self._wait_for_stb_any({70, 77, 82, 94}, timeout_s)
        except TimeoutError:
            return None
        return stb if stb == 70 else None


    def next_die(self):
        self.write("J")
        return self._wait_motion_stb({66, 67, 81, 90})

    def set_index_size(self, x_um: float, y_um: float):
        xi, yi = int(round(x_um)), int(round(y_um))
        if not (0 <= xi <= 99999 and 0 <= yi <= 99999):
            raise ValueError("I: index sizes must be 0–99999 µm")
        self.write(f"IY{yi:05d}X{xi:05d}")
        self._wait_for_stb(target_stb=77)

    def move_xy_absolute(self, dx_um: float, dy_um: float):
        xi, yi = int(round(dx_um)), int(round(dy_um))
        if not (-999999 <= xi <= 999999 and -999999 <= yi <= 999999):
            raise ValueError("A: travel distance must be within ±999999 µm")
        self.write(f"AY{yi:+07d}X{xi:+07d}")
        stb = self._wait_motion_stb({65, 67, 74})
        if stb == 74:
            raise RuntimeError("A: target outside probing area (STB=74) — chuck did not move")
        return stb

    def move_to_start_die(self):
        self.write("G")
        return self._wait_motion_stb({67, 70})

    def move_to_die_xy(self, x_die: int, y_die: int):
        xi, yi = int(x_die), int(y_die)
        if not (-99 <= xi <= 511 and -99 <= yi <= 511):
            raise ValueError("J: die coordinates must be within -99…511")
        self.write(f"JY{yi:03d}X{xi:03d}")
        stb = self._wait_motion_stb({66, 67, 74, 81, 90})
        if stb == 74:
            raise RuntimeError("J: target die outside probing area (STB=74) — chuck did not move")
        return stb

    def move_xy_relative(self, dx_index: int, dy_index: int):
        xi, yi = int(dx_index), int(dy_index)
        if not (-9999 <= xi <= 9999 and -9999 <= yi <= 9999):
            raise ValueError("S: relative travel must be within ±9999 die indexes")
        self.write(f"SY{yi:+05d}X{xi:+05d}")
        stb = self._wait_motion_stb({66, 67, 74})
        if stb == 74:
            raise RuntimeError("S: target outside probing area (STB=74) — chuck did not move")
        return stb

    def mark_current_die(self, category: str = ""):
        cmd = f"C{category}" if category else "C"
        self.write(cmd)
        return self._wait_motion_stb({66, 67, 69, 80, 81})


    def _wait_motion_stb(self, target_stbs: set, timeout_s: float = None) -> int:
        try:
            stb = self._wait_for_stb_any(target_stbs, timeout_s)
        except Exception:
            self.z_is_up = None
            raise
        if stb == 67:
            self.z_is_up = True
        elif stb in (65, 66, 68, 70, 90):
            self.z_is_up = False
        elif stb == 69:
            self.z_is_up = None
        return stb

    def _confirm_alarm(self) -> int:
        time.sleep(0.1)
        return self.inst.read_stb()

    def _wait_for_stb_any(self, target_stbs: set, timeout_s: float = None) -> int:
        if not self.inst:
            return 0
        timeout_seconds = timeout_s if timeout_s is not None else self.inst.timeout / 1000.0
        start_time = time.time()
        while (time.time() - start_time) < timeout_seconds:
            try:
                stb = self.inst.read_stb()
                if stb in target_stbs:
                    return stb
                if stb == 76:
                    confirm = self._confirm_alarm()
                    if confirm in target_stbs:
                        return confirm
                    if confirm == 76:
                        self._maybe_auto_clear_buzzer()
                        raise RuntimeError("PROBER HARDWARE ERROR: STB=76 (Check alarm screen)")
                    continue
                time.sleep(0.05)
            except Exception as e:
                print(f"[PROBER] Error reading STB: {e}")
                raise
        raise TimeoutError(f"Prober timed out waiting for STB in {target_stbs}")

    def _wait_for_stb(self, target_stb: int, timeout_s: float = None):
        if not self.inst:
            return False
        timeout_seconds = timeout_s if timeout_s is not None else self.inst.timeout / 1000.0
        start_time = time.time()
        while (time.time() - start_time) < timeout_seconds:
            try:
                current_stb = self.inst.read_stb()
                if current_stb == target_stb:
                    return True
                if current_stb == 76:
                    confirm = self._confirm_alarm()
                    if confirm == target_stb:
                        return True
                    if confirm == 76:
                        self._maybe_auto_clear_buzzer()
                        raise RuntimeError("PROBER HARDWARE ERROR: STB=76 (Check alarm screen)")
                    continue
                time.sleep(0.05)
            except Exception as e:
                print(f"[PROBER] Error reading STB: {e}")
                raise
        raise TimeoutError(f"Prober timed out waiting for STB {target_stb}")