"""Electroglas 2001X / 2001CX(E) prober."""

import datetime
import functools
import re
import threading
import time

from instruments.gpib_base import GPIBInstrument, open_resource


def _fmt6(value) -> str:
    return f"{float(value):07.3f}"


def _serialised(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._io_lock:
            return method(self, *args, **kwargs)
    return wrapper


PRE_LAMP_SETTINGS = [
    ("Align scan velocity", "3000",       "SP16V3000"),
    ("Z overtravel",        "1.50 mils",  "SP5Z15"),
    ("Z clearance",         "10.00 mils", "SP6Z100"),
    ("Z up limit",          "300.00 mils", "SP7Z3000"),
    ("Z down limit",        "200.00 mils", "SP8Z2000"),
    ("Z align height",      "300.00 mils", "SP9Z3000"),
    ("Z under travel",      "0.00 mils",  "SP10Z0"),
    ("Wafer diameter",      "150 mm",     "SP4D150"),
]


LAMP_INIT_SEQUENCE = [
    ("MF/MC on Rest of Commands", "on", "SM15M111100000"),
    ("Set Wafer Diameter", "150mm", "SP4D150"),
    ("Set Z Scale Factor", "3 steps per mil", "SP12S3"),
    ("Set Z Overtravel", "3.7 mils", "SP5Z37"),
    ("Set Z Clearance", "15 mils", "SP6Z150"),
    ("Set Z UpLimit", "420 mils", "SP7Z4200"),
    ("Set Z DownLimit", "200 mils", "SP8Z2000"),
    ("Set Z Align Height", "216 mils", "SP9Z2160"),
    ("Set Z Scan Align Speed", "2000 mils/sec", "SP16V2000"),
    ("Set Metric XY Units", "", "SM1U1"),
    ("Set Initial Probing Direction", "Quadrant 2", "SM2Q2"),
    ("Set Probe Mode", "1 = Edge", "SM4P1"),
    ("Set Z Travel Mode", "2 = Auto Profile", "SM5E2"),
    ("Set Ignore Vacuum", "1 = enabled", "SM22V1"),
    ("Set 30mil drop at load", "1 = enabled", "SM30L1"),
    ("Set Microprobing", "0 = disabled", "SM35B0"),
    ("Set Screen/Lamp Saver", "1 = enabled", "SM40S1"),
    ("Auto Temperature Compensation", "on", "SO01100001"),
    ("Temperature Compensation", "0 = disabled", "SX1B0"),
    ("Wafer Mapping", "0 = disabled", "WM0"),
]

REFERENCE_PROBER_SETTINGS = [
    ("Die X",           "3.52100 mm   (different product - do not copy)"),
    ("Die Y",           "1.64200 mm   (different product - do not copy)"),
    ("Preset",          "X0 Y0 die"),
    ("Wafer diameter",  "150 mm"),
    ("Align scan vel",  "3000"),
    ("Z overtravel",    "1.00 mils"),
    ("Z clearance",     "45.00 mils"),
    ("Z up limit",      "380.00 mils"),
    ("Z down limit",    "200.00 mils"),
    ("Z align",         "310.00 mils"),
    ("Z under travel",  "0.00 mils"),
]

_MACHINE_Z_OVERRIDES = {
    "SP7Z4200": ("Set Z UpLimit", "350 mils (this machine, not LaMP's 420)",
                 "SP7Z3500"),
    "SP9Z2160": ("Set Z Align Height", "350 mils (measured sharp; LaMP's 216 "
                 "is out of focus here)", "SP9Z3500"),
}

MACHINE_INIT_SEQUENCE = [
    _MACHINE_Z_OVERRIDES.get(command, (what, value, command))
    for what, value, command in LAMP_INIT_SEQUENCE
]


class Electroglas2001X(GPIBInstrument):
    DEFAULT_MAX_DIE_STEP = 5

    DEFAULT_Z_LIMITS = (2000, 4000)

    def __init__(self):
        super().__init__('prober_eg')
        self.z_is_up = None
        self.max_die_step = self.DEFAULT_MAX_DIE_STEP
        self.z_limits = self.DEFAULT_Z_LIMITS
        self._die_envelope = None
        self._io_lock = threading.RLock()

    @_serialised
    def _drain(self, settle_ms: int = 150) -> int:
        if not self.inst:
            return 0
        previous = self.inst.timeout
        dropped = 0
        try:
            self.inst.timeout = settle_ms
            while True:
                try:
                    self.inst.read_raw()
                    dropped += 1
                except Exception:
                    return dropped
        finally:
            try:
                self.inst.timeout = previous
            except Exception:
                pass

    @_serialised
    def query(self, command, _retry=True):
        if not self.inst:
            return None
        self._drain()
        try:
            return super().query(command)
        except Exception:
            if not _retry:
                raise
            try:
                self.clear_interface()
            except Exception:
                pass
            self._drain()
            return self.query(command, _retry=False)

    @_serialised
    def accepts_commands(self) -> bool:
        if not self.inst:
            return False
        try:
            self.inst.write("?S")
        except Exception:
            return False
        self._drain()
        return True

    def get_id(self) -> str:
        if not self.is_present():
            return ""
        if not self.accepts_commands():
            raise RuntimeError(
                "GPIB interface responds to serial poll but refuses every "
                "command - the prober is not servicing the bus. Check that "
                "host/remote GPIB control is enabled on the prober itself.")
        return "Electroglas 2001X (serial poll OK, no ID string)"

    def _not_implemented(self, name):
        raise NotImplementedError(
            f"Electroglas 2001X: '{name}' has no real command mapping yet. "
            f"Add it to instruments/electroglas_2001x.py once the Electroglas "
            f"command reference is available.")

    def get_prober_id(self) -> str:
        return self.get_id()

    def get_error_code(self) -> str:
        return self.query("?E") or ""

    def get_error_message(self) -> str:
        self._not_implemented("get_error_message")

    def get_prober_status(self) -> str:
        return self.query("?S") or ""

    def _wait_until_not_moving(self, timeout_s: float = 30.0) -> str:
        if not self.inst:
            return ""
        previous = self.inst.timeout
        try:
            self.inst.timeout = int(timeout_s * 1000)
            reply = (self.inst.read() or "").strip()
        except Exception as e:
            raise TimeoutError(
                f"Electroglas 2001X: no mc/mf reply within {timeout_s}s ({e})")
        finally:
            try:
                self.inst.timeout = previous
            except Exception:
                pass

        low = reply.lower()
        if low.startswith("mf"):
            raise RuntimeError(
                f"Electroglas 2001X reported MOVE FAILED ({reply!r}) - the XY "
                f"target was rejected, most likely outside the probing area. "
                f"NOTE: XY did not move, but Z may still have changed - a move "
                f"lowers Z before attempting XY, and that part happens even "
                f"when the XY move is refused (measured: ?Z went 300 -> 0 on a "
                f"refused MD). Re-read ?Z rather than assuming nothing moved.")
        if not low.startswith("mc"):
            raise RuntimeError(
                f"Electroglas 2001X: expected 'mc' or 'mf', got {reply!r}")
        return reply

    def get_xy_position(self) -> str:
        return self.query("?P") or ""

    def get_die_position(self) -> tuple:
        pos = self._parse_die_position(self.get_xy_position())
        if pos is None:
            raise ValueError("Cannot parse ?P response for current die position")
        return pos

    def get_die_counts(self) -> str:
        return self.query("?Y") or ""

    def get_cassette_status(self) -> str:
        return self.query("?C") or ""

    @staticmethod
    def decode_status(raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return "no status"
        body = text[1:] if text[:1].upper() == "S" else text

        parts, seen = [], 0
        match = re.match(r"Z([UD])", body, re.IGNORECASE)
        if match:
            seen = match.end()
            parts.append("Z UP - wafer CONTACTING the probe card"
                         if match.group(1).upper() == "U"
                         else "Z DOWN - wafer clear of the probe card")

        rest = body[seen:]
        for letter, label in (("W", "wafer"), ("C", "cassette")):
            found = re.search(letter + r"(\d+)", rest, re.IGNORECASE)
            if found:
                parts.append(f"{label} {int(found.group(1))}")

        known = re.sub(r"Z[UD]|[WC]\d+", "", body, flags=re.IGNORECASE)
        if known:
            parts.append(f"unrecognised: {known!r}")
        return "  |  ".join(parts) if parts else text

    @staticmethod
    def decode_error(raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return "no reply"
        if text.upper() in ("E0", "E00"):
            return "no error"
        code = text[1:] if text[:1].upper() == "E" else text
        if code == "35":
            return "error 35 - unsupported/invalid command"
        return f"ERROR {code}"

    @_serialised
    def read_telemetry(self) -> dict:
        out = {}
        for label, cmd in (("status", "?S"), ("position", "?P"), ("z", "?Z"),
                           ("theta", "?T"), ("error", "?E"),
                           ("wafer_info", "?I"), ("die_counts", "?Y"),
                           ("cassette", "?C"), ("run_state", "?R")):
            try:
                out[label] = self.query(cmd) or ""
            except Exception as e:
                out[label] = f"<{type(e).__name__}>"

        status = out.get("status", "")
        if status.startswith("S"):
            if "ZD" in status:
                out["z_state"] = "DOWN (wafer clear of the probe card)"
            elif "ZU" in status:
                out["z_state"] = "UP (wafer CONTACTING the probe card)"
            wafer = status.split("W", 1)[-1].split("C", 1)[0] if "W" in status else ""
            if wafer:
                out["wafer_number"] = wafer

        counts = out.get("die_counts", "")
        if counts.startswith("G"):
            try:
                good = counts.split("G", 1)[1].split("B", 1)[0]
                bad = counts.split("B", 1)[1].split("U", 1)[0]
                ugly = counts.split("U", 1)[1].split("D", 1)[0]
                out["die_tally"] = f"good {good}, bad {bad}, ugly {ugly}"
            except (IndexError, ValueError):
                pass

        info = out.get("wafer_info", "")
        if "D" in info:
            diameter = info.rsplit("D", 1)[-1]
            if diameter.isdigit():
                out["wafer_diameter_mm"] = diameter

        return out

    @_serialised
    def recover(self) -> str:
        def _talks(checks=2):
            for _ in range(checks):
                try:
                    if not (self.inst and self.query("?S", _retry=False)):
                        return False
                except Exception:
                    return False
            return True

        if not self.inst:
            return "no session open - use Refresh Connections"

        dropped = self._drain()
        if _talks():
            return f"recovered by draining ({dropped} stale reply(s))"

        try:
            self.clear_interface()
            self._drain()
        except Exception as e:
            return f"device clear failed: {e}"
        if _talks():
            return "recovered by device clear"

        try:
            self.inst.close()
        except Exception:
            pass
        try:
            self.inst, via = open_resource(self.address)
            self.inst.timeout = self.timeout
            self.inst.encoding = "latin-1"
        except Exception as e:
            self.inst = None
            return f"could not reopen {self.address}: {e}"
        if _talks():
            return f"recovered by reopening the session (via {via})"
        return ("still not responding. If another process holds a VISA session "
                "on this address, no amount of clearing here will fix it - "
                "that session has to die first. Otherwise check the prober is "
                "ON LINE.")

    @_serialised
    def write(self, command):
        return super().write(command)

    @_serialised
    def is_present(self) -> bool:
        return super().is_present()

    @_serialised
    def clear_interface(self):
        if self.inst:
            self.inst.clear()

    @_serialised
    def send_command(self, command: str, ack_timeout_s: float = 10.0):
        if not self.inst:
            return None
        self._drain()
        try:
            self.inst.write(command)
        except Exception:
            self.clear_interface()
            self._drain()
            self.inst.write(command)

        previous = self.inst.timeout
        try:
            self.inst.timeout = int(ack_timeout_s * 1000)
            ack = (self.inst.read() or "").strip()
        except Exception:
            return None
        finally:
            try:
                self.inst.timeout = previous
            except Exception:
                pass

        if ack.lower().startswith("mf"):
            raise RuntimeError(f"prober rejected {command!r} (replied {ack!r})")
        return ack

    @_serialised
    def send_init_sequence(self, log=None) -> int:
        sent = 0
        for what, value, command in MACHINE_INIT_SEQUENCE:
            ack = self.send_command(command)
            sent += 1
            if log:
                suffix = f" ({value})" if value else ""
                log(f"{command:<16} {what}{suffix}"
                    + (f"   [{ack}]" if ack else ""))
            time.sleep(0.05)
        return sent

    @_serialised
    def send_settings(self, rows, log=None) -> int:
        sent = 0
        for what, value, command in rows:
            ack = self.send_command(command)
            sent += 1
            if log:
                suffix = f" ({value})" if value else ""
                log(f"{command:<16} {what}{suffix}"
                    + (f"   [{ack}]" if ack else ""))
            time.sleep(0.05)
        return sent

    def get_wafer_info(self) -> str:
        return self.query("?I") or ""

    def get_xy_absolute(self) -> str:
        self._not_implemented("get_xy_absolute")

    def get_on_wafer_info(self) -> str:
        self._not_implemented("get_on_wafer_info")

    def get_lot_number(self) -> str:
        self._not_implemented("get_lot_number")

    def get_wafer_number(self) -> str:
        self._not_implemented("get_wafer_number")

    def get_wafer_id(self) -> str:
        self._not_implemented("get_wafer_id")

    def get_pass_fail_counts(self) -> str:
        self._not_implemented("get_pass_fail_counts")

    def get_gross_value(self) -> str:
        self._not_implemented("get_gross_value")

    def get_wafer_status(self) -> str:
        self._not_implemented("get_wafer_status")

    def get_yield_data(self) -> str:
        return self.get_die_counts()

    def get_hot_chuck_status(self) -> str:
        self._not_implemented("get_hot_chuck_status")

    def get_chuck_temperature(self) -> str:
        self._not_implemented("get_chuck_temperature")

    def get_start_die_coords(self) -> str:
        self._not_implemented("get_start_die_coords")

    def get_multisite_info(self) -> str:
        self._not_implemented("get_multisite_info")

    def buzzer_clear(self) -> str:
        self._not_implemented("buzzer_clear")

    def send_es(self):
        self._not_implemented("send_es")

    def confirm_and_clear_alarm(self) -> bool:
        self._not_implemented("confirm_and_clear_alarm")

    def read_stb_decoded(self) -> tuple:
        return 0, (self.get_prober_status() or "unknown")

    @_serialised
    def _motion(self, command: str, timeout_s: float = 30.0) -> str:
        ack = self.send_command(command, ack_timeout_s=timeout_s)
        if ack is None:
            try:
                self.clear_interface()
                self._drain()
            except Exception:
                pass
            raise TimeoutError(
                f"no mc/mf acknowledgement to {command!r} within {timeout_s}s. "
                f"This is NOT necessarily a rejection - the move may have "
                f"executed with only its acknowledgement lost. Link resynced; "
                f"re-read ?P and ?Z to see where the stage actually is.")
        return ack

    @_serialised
    def _z_move_verified(self, command: str, expect: str) -> str:
        status = self._motion(command)
        after = (self.get_prober_status() or "").upper()
        if expect not in after:
            raise RuntimeError(
                f"{command} acknowledged ({status!r}) but Z did not reach "
                f"{expect} - ?S still reads {after!r}. Z TRAVEL MODE is set to "
                f"auto profile (SM5E2), which needs a profiled wafer to know "
                f"where 'up' is. Use move_z_absolute() for a direct height, or "
                f"profile a wafer first.")
        return status

    def z_up(self):
        status = self._z_move_verified("ZU", "ZU")
        self.z_is_up = True
        return status

    def z_down(self):
        status = self._z_move_verified("ZD", "ZD")
        self.z_is_up = False
        return status

    def move_z_absolute(self, z):
        status = self._motion(f"ZM{int(z)}")
        self.z_is_up = None
        return status

    @_serialised
    def move_z_relative(self, dz):
        dz = int(dz)
        low, high = self.z_limits
        here = self._parse_z(self.query("?Z"))
        if here is not None:
            target = here + dz
            if not low <= target <= high:
                where = ("below" if target < low else "above")
                extra = ""
                if not low <= here <= high:
                    extra = (f" Z is currently parked at {here}, itself outside "
                             f"the limits - use move_z_absolute() to get back "
                             f"into range first.")
                raise ValueError(
                    f"ZR{dz:+d} from Z{here} targets Z{target}, {where} the Z "
                    f"limits [{low}..{high}] (0.1-mil units). The prober would "
                    f"refuse this.{extra}")
        status = self._motion(f"ZR{dz}")
        self.z_is_up = None
        return status

    @staticmethod
    def _parse_z(reply):
        try:
            return int(str(reply).strip().lstrip("Zz"))
        except (AttributeError, ValueError):
            return None

    def move_theta_relative(self, dtheta):
        return self._motion(f"MT{int(dtheta)}")

    def emergency_stop(self):
        self._not_implemented("emergency_stop")

    def unload_wafer(self):
        status = self._motion("U")
        self.z_is_up = False
        return status

    def load_wafer(self):
        status = self._motion("L")
        self.z_is_up = False
        return status

    def cassette_wait_for_wafer_ready(self, timeout_s=None):
        self._not_implemented("cassette_wait_for_wafer_ready")

    def cassette_next_die(self, timeout_s=None):
        self._not_implemented("cassette_next_die")

    def cassette_unload_and_load_next(self, timeout_s=None):
        self._not_implemented("cassette_unload_and_load_next")

    def next_die(self):
        status = self._motion("J")
        self.z_is_up = False
        return status

    def index_die_alt(self):
        status = self._motion("I")
        self.z_is_up = False
        return status

    def set_index_size(self, x_um: float, y_um: float):
        self._not_implemented("set_index_size")

    def move_xy_absolute(self, dx_um: float, dy_um: float):
        self._not_implemented("move_xy_absolute")

    def move_to_start_die(self):
        status = self._motion("MF")
        self.z_is_up = False
        return status

    def move_to_home(self):
        status = self._motion("HO")
        self.z_is_up = False
        return status

    def trigger_inker(self):
        return self._motion("IK")

    def auto_profile(self):
        return self._motion("PZ")

    def auto_align(self):
        return self._motion("AA")

    def move_to_die_xy(self, x_die: int, y_die: int):
        self._not_implemented("move_to_die_xy")

    def move_absolute_die(self, x_die, y_die):
        status = self._motion(f"MOX{int(x_die)}Y{int(y_die)}")
        self.z_is_up = False
        return status

    @_serialised
    def goto_die(self, x_die: int = 0, y_die: int = 0, max_moves: int = 60) -> str:
        target = (int(x_die), int(y_die))
        for _ in range(max_moves):
            here = self._parse_die_position(self.get_xy_position())
            if here is None:
                raise RuntimeError(
                    "cannot read a usable die position from ?P - refusing to "
                    "walk blind")
            if here == target:
                return f"X{target[0]}Y{target[1]}"

            cap = self.max_die_step
            dx = max(-cap, min(cap, target[0] - here[0]))
            dy = max(-cap, min(cap, target[1] - here[1]))
            before = here
            self.move_relative_die(dx, dy)

            after = self._parse_die_position(self.get_xy_position())
            if after == before:
                raise RuntimeError(
                    f"MD {dx:+d},{dy:+d} was accepted but ?P did not change "
                    f"(still X{before[0]}Y{before[1]}) - stopping rather than "
                    f"looping. The stage may be against a bound.")
        raise RuntimeError(
            f"did not reach X{target[0]}Y{target[1]} within {max_moves} moves")

    @_serialised
    def move_relative_die(self, dx_die, dy_die):
        dx, dy = int(dx_die), int(dy_die)

        if max(abs(dx), abs(dy)) > self.max_die_step:
            raise ValueError(
                f"MD move of ({dx},{dy}) dies exceeds max_die_step="
                f"{self.max_die_step}. Step in smaller increments, or raise "
                f"max_die_step deliberately if this is really intended.")

        if self._die_envelope is not None:
            here = self._parse_die_position(self.get_xy_position())
            if here is None:
                raise RuntimeError(
                    "cannot read a usable die position from ?P, so the travel "
                    "envelope cannot be enforced - refusing to move")
            x_min, x_max, y_min, y_max = self._die_envelope
            target = (here[0] + dx, here[1] + dy)
            if not (x_min <= target[0] <= x_max and y_min <= target[1] <= y_max):
                raise ValueError(
                    f"MD move would land at {target}, outside the configured "
                    f"envelope X[{x_min}..{x_max}] Y[{y_min}..{y_max}]. "
                    f"Refusing - the prober will NOT catch this for you.")

        status = self._motion(f"MDX{dx}Y{dy}")
        self.z_is_up = False
        return status

    @staticmethod
    def _parse_die_position(pos):
        try:
            return (int(pos.split("X", 1)[1].split("Y", 1)[0]),
                    int(pos.split("Y", 1)[1]))
        except (AttributeError, IndexError, ValueError):
            return None

    def set_die_envelope(self, x_min, x_max, y_min, y_max):
        self._die_envelope = (int(x_min), int(x_max), int(y_min), int(y_max))

    def clear_die_envelope(self):
        self._die_envelope = None

    MM_UNIT_UM = 2.50

    DEFAULT_MAX_UM_STEP = 200000

    def _check_um(self, dx_um, dy_um):
        limit = getattr(self, "max_um_step", self.DEFAULT_MAX_UM_STEP)
        if max(abs(dx_um), abs(dy_um)) > limit:
            raise ValueError(
                f"Electroglas 2001X: micron move of ({dx_um:.0f},{dy_um:.0f}) um "
                f"exceeds max_um_step={limit}. Raise it deliberately if this is "
                f"really intended.")

    def _um_to_counts(self, um):
        return int(round(float(um) / self.MM_UNIT_UM))

    def move_relative_um(self, dx_um, dy_um):
        self._check_um(dx_um, dy_um)
        status = self._motion(
            f"MMX{self._um_to_counts(dx_um)}Y{self._um_to_counts(dy_um)}")
        self.z_is_up = False
        return status

    def move_relative_counts(self, dx, dy):
        status = self._motion(f"MMX{int(dx)}Y{int(dy)}")
        self.z_is_up = False
        return status

    def move_relative_m(self, dx, dy):
        return self.move_relative_counts(dx, dy)

    def move_absolute_m(self, x, y):
        status = self._motion(f"MAX{int(x)}Y{int(y)}")
        self.z_is_up = False
        return status

    def move_micro(self, dx, dy):
        status = self._motion(f"FMX{int(dx)}Y{int(dy)}")
        self.z_is_up = False
        return status

    def move_xy_relative(self, dx_index: int, dy_index: int):
        self._not_implemented("move_xy_relative")

    def mark_current_die(self, category: str = ""):
        self._not_implemented("mark_current_die")

    def set_die_size(self, x, y):
        self.write(f"SP1X{int(x)}Y{int(y)}")

    def set_die_size_precise_mm(self, x_mm, y_mm):
        self.write(f"SP29X{_fmt6(x_mm)}Y{_fmt6(y_mm)}")

    @_serialised
    def infer_die_size(self, probe_x: int = 7042, probe_y: int = 3284) -> tuple:
        before = self._parse_die_position(self.get_xy_position())
        if before is None:
            raise RuntimeError("infer_die_size: cannot read ?P before probing - "
                               "refusing to write a temporary die size blind")
        if before[0] == 0 or before[1] == 0:
            raise RuntimeError(f"infer_die_size: chuck is at {before} - at least "
                               "one axis is 0, which divides by zero. Jog off "
                               "that axis and try again.")
        self.set_die_size(probe_x, probe_y)
        after = self._parse_die_position(self.get_xy_position())
        if after is None:
            raise RuntimeError(
                "infer_die_size: cannot read ?P after probing. The die size is "
                f"now the probe value X{probe_x}Y{probe_y} and CANNOT be put "
                "back automatically - nothing can read what it was. Set it "
                "manually from SET PRMTR before running.")

        exact = all((probe * a) % b == 0 and a != 0
                    for probe, b, a in ((probe_x, before[0], after[0]),
                                        (probe_y, before[1], after[1])))
        size_x = round(probe_x * after[0] / before[0]) if after[0] else 0
        size_y = round(probe_y * after[1] / before[1]) if after[1] else 0

        if not exact or size_x <= 0 or size_y <= 0:
            raise RuntimeError(
                f"infer_die_size: ?P {before} -> {after} against probe "
                f"X{probe_x}Y{probe_y} does not divide exactly, so the answer "
                f"would be a guess (it computes X{size_x}Y{size_y}). The die "
                f"size is now the probe value X{probe_x}Y{probe_y} and cannot "
                "be restored automatically - set it from SET PRMTR, or move "
                "the chuck further from the origin on both axes and re-run.")

        self.set_die_size(size_x, size_y)
        back = self._parse_die_position(self.get_xy_position())
        if back != before:
            raise RuntimeError(
                f"infer_die_size: inferred X{size_x}Y{size_y}, but restoring it "
                f"made ?P read {back} instead of the original {before} - so that "
                "is NOT the size that was set. The die size is now "
                f"X{size_x}Y{size_y}; set the correct one from SET PRMTR.")
        return (size_x, size_y)

    def set_wafer_diameter(self, diameter):
        self.write(f"SP4D{int(diameter)}")

    def set_coordinate_quadrant(self, quadrant):
        self.write(f"SM11Q{int(quadrant)}")

    def set_count_pulse_width(self, width):
        self.write(f"SM32P{int(width)}")

    def set_current_cassette(self, cassette):
        self.write(f"SM70C{int(cassette)}")

    def set_date_time(self, when=None):
        when = when or datetime.datetime.now()
        self.write(f"TI{when.hour:02d}:{when.minute:02d}")

    def set_first_die(self):
        self.write("FD")

    def set_flat_orientation(self, orientation):
        self.write(f"SM3F{int(orientation)}")

    def set_probe_clean_count(self, count, w):
        self.write(f"SM12C{int(count)}W{int(w)}")

    def set_probe_quadrant(self, quadrant):
        self.write(f"SM2Q{int(quadrant)}")

    def set_profiler_retry_count(self, retries):
        self.write(f"SM42R{int(retries)}")

    def set_reference_die_coordinate(self, x, y):
        self.write(f"SP2X{int(x)}Y{int(y)}")

    def set_reprobe_count(self, count):
        self.write(f"SP14R{int(count)}")

    def set_starting_wafer_number(self, number):
        self.write(f"SM16N{int(number)}")

    def set_touchdown_counter(self, count):
        self.write(f"SP19C{int(count)}")

    def set_units(self, unit):
        self.write(f"SM1U{int(unit)}")

    def set_yield_to_pass_wafer(self, yield_pct):
        self.write(f"SP33Y{int(yield_pct)}")

    def set_z_autoalign_height(self, z):
        self.write(f"SP9Z{int(z)}")

    def set_z_travel_mode(self, mode):
        return self.send_command(f"SM5E{int(mode)}")

    def set_z_clearance(self, z):
        self.write(f"SP6Z{int(z)}")

    def set_z_down_limit(self, z):
        self.write(f"SP8Z{int(z)}")

    def set_z_overtravel(self, z):
        self.write(f"SP5Z{int(z)}")

    def set_z_undertravel(self, z):
        self.write(f"SP10Z{int(z)}")

    def set_z_up_limit(self, z):
        self.write(f"SP7Z{int(z)}")

    def set_zprofile_height(self):
        self.write("PH")

    def set_wafer_x_expansion(self, coefficient):
        self.write(f"SX4C{int(coefficient)}")

    def set_wafer_y_expansion(self, coefficient):
        self.write(f"SX5C{int(coefficient)}")

    def set_die_size_mm(self, x_mm, y_mm):
        self.set_die_size(round(x_mm * 1000), round(y_mm * 1000))

    def set_die_size_mil(self, x_mil, y_mil):
        self.set_die_size(round(x_mil * 10), round(y_mil * 10))
