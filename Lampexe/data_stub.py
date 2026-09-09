import json
import os
import sys
import winreg


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from instruments.electroglas_2001x import Electroglas2001X
from instruments.keithley2400 import Keithley2400
from instruments.hp_switchbox import HPSwitchbox, bench_wiring

REAL_RECIPE_DIR = r"C:\ProbeRecipe\LampElectrical"

_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "_settings_stub.json")

DEFAULT_PROBER_NAME = "IMTPRB02"

MASTER_DB_PATH = r"P:\ProberMaster.mdb"
LAMP_DB_PATH = r"P:\LampElectricalProbeData.mdb"


def scan_recipes():
    if not os.path.isdir(REAL_RECIPE_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(REAL_RECIPE_DIR)
        if f.lower().endswith(".pma")
    )


def recipe_file_path(name: str) -> str:
    return os.path.join(REAL_RECIPE_DIR, f"{name}.PMA")


def recipe_sibling_paths(name: str) -> list:
    suffixes = (
        "MovesMajorX.PMV", "MovesMajorY.PMV", "DeviceIDMajor.PMS",
        "MovesMinorX.PMV", "MovesMinorY.PMV", "DeviceIDMinor.PMS",
    )
    return [os.path.join(REAL_RECIPE_DIR, f"{name}{suffix}") for suffix in suffixes]


def missing_recipe_files(name: str) -> list:
    paths = [recipe_file_path(name)] + recipe_sibling_paths(name)
    return [p for p in paths if not os.path.isfile(p)]


AGT3494A_PROGID = "Agt3494ALib.Agt3494A"


def is_agt3494a_registered() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{AGT3494A_PROGID}\\CLSID")
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False


def check_database_connections() -> list:
    failures = []
    if not os.path.isfile(MASTER_DB_PATH):
        failures.append((
            "IMT LampElectrical Probing",
            "An attempt to make a connection to the Master Prober database failed! "
            "Please alert an engineer.\nWithout this connection the prober cannot run.",
        ))
    if not os.path.isfile(LAMP_DB_PATH):
        failures.append((
            "IMT LampElectrical Probing",
            "An attempt to make a connection to the LampElectrical database failed! "
            "Please alert an engineer.\nWithout this connection the prober cannot run.",
        ))
    return failures


_MOCK_PROBER_CONFIG = {
    "LampElectrical": [
        ("2001X", "GPIB0::29::INSTR"),
        ("Relay1", "GPIB0::9::15::INSTR"),
        ("Keithley2400", "GPIB0::24::INSTR"),
    ],
    "Default": [
        ("2001X", "GPIB0::29::INSTR"),
        ("Relay1", "GPIB0::9::15::INSTR"),
        ("Keithley2400", "GPIB0::24::INSTR"),
    ],
}


def load_prober_configuration(prober_name: str = "LampElectrical") -> list:
    rows = _MOCK_PROBER_CONFIG.get(prober_name)
    if not rows:
        rows = _MOCK_PROBER_CONFIG.get("Default", [])
    return list(rows)


PROBER_INIT_SEQUENCE = [
    (10, "MF/MC on XY MOTION", "on", ""),
    (20, "MF/MC on Z MOTION", "on", ""),
    (30, "MF/MC on Optional Devices", "on", ""),
    (40, "MF/MC on Rest of Commands", "on", "SM15M111100000"),
    (50, "Set Wafer Diameter", "150mm", "SP4D150"),
    (60, "Set Z Scale Factor", "3 steps per mil", "SP12S3"),
    (70, "Set Z Overtravel", "3.7mils", "SP5Z37"),
    (80, "Set Z Clearance", "15 mils", "SP6Z150"),
    (90, "Set Z UpLimit", "420 mils", "SP7Z4200"),
    (100, "Set Z DownLimit", "200 mils", "SP8Z2000"),
    (110, "Set Z Align Height", "216 mils", "SP9Z2160"),
    (120, "Set Z Scan Align Speed", "2000 mils per sec", "SP16V2000"),
    (130, "Set Metric XY Units", "", "SM1U1"),
    (140, "Set Initial Probing Direction", "Quadrant2", "SM2Q2"),
    (150, "Set Probe Mode", "1 = Edge", "SM4P1"),
    (160, "Set Z Travel Mode", "2 = Auto Profile", "SM5E2"),
    (170, "Set Ignore Vacuum", "1 = enabled", "SM22V1"),
    (180, "Set 30mil drop at load", "1 = enabled", "SM30L1"),
    (190, "Set Microprobing", "0 = disabled", "SM35B0"),
    (200, "Set Screen/Lamp Saver", "1= enabled", "SM40S1"),
    (210, "Material Handling", "off", ""),
    (220, "Auto Alignment", "on", ""),
    (230, "Wafer Profiler", "on", ""),
    (240, "Wafer ID reader", "off", ""),
    (250, "SECS protocol", "off", ""),
    (260, "Wafer Mapping", "off", ""),
    (270, "EG TC-2000 Thermal Chuck", "off", ""),
    (280, "Auto Temperature Compensation", "on", "SO01100001"),
    (290, "Temperature Compensation", "0 = disabled", "SX1B0"),
    (300, "Wafer mapping", "0 = disabled", "WM0"),
]


def _load_settings() -> dict:
    if os.path.isfile(_SETTINGS_FILE):
        try:
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_settings(data: dict) -> None:
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def get_setting(section: str, key: str, default: str) -> str:
    return _load_settings().get(f"{section}/{key}", default)


def save_setting(section: str, key: str, value: str) -> None:
    data = _load_settings()
    data[f"{section}/{key}"] = value
    _save_settings(data)


def get_prober_name() -> str:
    return get_setting("Names", "ProberName", DEFAULT_PROBER_NAME)


def set_prober_name(name: str) -> None:
    save_setting("Names", "ProberName", name)


PROBE_DATA_DIR = r"C:\ProbeData"


def uncollected_data_files() -> list:
    if not os.path.isdir(PROBE_DATA_DIR):
        return []
    return sorted(f for f in os.listdir(PROBE_DATA_DIR) if f.lower().startswith("complete") and f.lower().endswith(".txt"))


DEMO_DIE_IDS = ["A1", "A2", "A3", "B1", "B2", "B3"]

DEMO_COMBO_DIES = [f"Die {i:02d}" for i in range(1, 21)]


def build_hpib_instruments() -> dict:
    return {
        "2001X": Electroglas2001X(),
        "Relay1": HPSwitchbox("relay1_eg"),
        "Keithley2400": Keithley2400(),
    }


def simulated_run_steps(die_ids=None):
    die_ids = die_ids or DEMO_DIE_IDS
    yield "Waiting final OK to begin probing", False
    yield "Ready", False
    yield "Moving to first site", True
    for die in die_ids:
        yield f"Probing {die}", True
        yield f"Measuring {die}", True
    yield "Probe Recipe completed normally", True
    yield "Moving to home", True
    yield "Ready", False


QUAD_DIE_IDS = ["Die 1", "Die 2", "Die 3", "Die 4"]


def real_run_steps(hpib: dict, die_ids=None):
    die_ids = die_ids or QUAD_DIE_IDS
    relay = hpib.get("Relay1")
    smu = hpib.get("Keithley2400")
    relay_live = relay is not None and relay.inst is not None
    smu_live = smu is not None and smu.inst is not None
    die_sets = bench_wiring("probe02").get("die_sets", {})

    yield "Waiting final OK to begin probing", False
    yield "Ready", False
    yield "Moving to first site", True
    for i, die in enumerate(die_ids, start=1):
        yield f"Probing {die}", True
        chans = die_sets.get(i)
        if relay_live and chans:
            try:
                relay.close_only(chans[0])
                yield f"  CH{chans[0]:02d} closed (die {i} of the quad)", True
            except Exception as e:
                yield f"  relay close failed: {e}", True
        elif not chans:
            yield f"  no relay channel mapped for die {i} - not switched", True
        else:
            yield "  (Relay1 not connected - not switched)", True

        yield f"Measuring {die}", True
        if smu_live:
            try:
                smu.set_voltage("", 10.0)
                smu.set_current_limit("", 1e-6)
                smu.turn_output_on("")
                current = smu.measure_current("")
                smu.turn_output_off("")
                resistance = (10.0 / current) if current else float("inf")
                yield f"  {die}: I={current:.3e} A  R={resistance:.3e} Ohm", True
            except Exception as e:
                yield f"  measurement failed: {e}", True
        else:
            yield "  (Keithley 2400 not connected - not measured)", True

    if relay_live:
        try:
            relay.open_all()
        except Exception:
            pass
    yield "Probe Recipe completed normally", True
    yield "Moving to home", True
    yield "Ready", False
