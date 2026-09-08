import json
import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from pma_wafer_panel import (read_main_menu_info as _pma_read_main_menu_info,
                             read_moves_grid as _pma_read_moves_grid)
from engineering_units import parse_engineering, format_engineering_compact
import switch_topology
try:
    import xlrd as _pma_xlrd
except ImportError:
    _pma_xlrd = None


# "move" only appears in the editor's Type dropdown when a recipe's Minor
# Moves checkbox (RecipePanel._minor_moves_var) is checked - see
# RecipePanel._refresh_type_values. It repositions the chuck to a specific
# die WITHIN the shot the current touchdown is already sitting on (Die #
# names which one, Wafer Builder Shot-tab order) - meaningless without
# Minor Moves' shot geometry, so it's hidden rather than just disabled.
_STEP_TYPES    = ("resistance", "ohmf", "voltage", "current", "wave", "passfail",
                  "delay", "open", "picture", "move")
_STEP_MODES    = ("measure", "apply")
_INSTRUMENTS   = ("DMM", "SMU", "WGEN")
_SMU_CHANNELS  = ("A", "B")
_WGEN_CHANNELS = ("CH1", "CH2")
_WAVE_SHAPES   = ("SIN", "SQU", "RAMP", "PULS", "DC")
# his/los are the 4-wire SENSE pins and are only ever populated on an "ohmf"
# step. Appended to the end so recipes written before 4-wire existed still
# parse - _parse_step matches on key name, not position.
_STEP_FIELDS   = ("name", "type", "mode", "instrument", "chan", "target", "hi", "lo",
                  "level", "limit", "shape", "freq", "conn", "min", "max",
                  "avg_count", "avg_delay", "nplc", "his", "los",
                  # How long to wait AFTER the bias/output is turned on but
                  # BEFORE the first reading is taken - separate from
                  # avg_delay (the gap BETWEEN averaged readings, or the
                  # SMU's own per-cycle source delay on a "current" step).
                  # Covers both "the instrument itself needs a moment to
                  # settle before its first reading is trustworthy" and "I'm
                  # biasing while measuring and want the bias to actually
                  # settle before the first sample" - see
                  # instrument_panel._exec_run_steps_once.
                  "settle_delay",
                  # Take abs() of the reading before it's compared to a
                  # target or recorded/painted on the map. "1" = on, blank
                  # = off (the default - most steps want the signed value).
                  # Only meaningful on a measurement step.
                  "abs_value",
                  # LaMP's MeterRange - the fixed measurement range. Appended
                  # rather than inserted so existing card CSVs, which are read
                  # by column NAME, keep loading unchanged; a file without the
                  # column just yields "" and the meter autoranges as before.
                  "mrange",
                  # Which die of the shot (Wafer Builder's Shot-tab order,
                  # 1-based) this step's measurement belongs to. Replaces the
                  # old "(Die N)" name-suffix convention as the source of
                  # truth for per-die results attribution - see
                  # instrument_panel._exec_run_steps_once. Blank/unparsable
                  # defaults to 1, so a single-die shot needs nothing set.
                  "die",
                  # "switch" (the default) or "direct". A direct step is
                  # cabled straight from the instrument to the probe card by
                  # hand, bypassing the switchbox entirely - so it closes no
                  # channels and needs no pin numbers, because the pins are
                  # not what routes it. See ROUTE_DIRECT.
                  "route",
                  # Which SPECIFIC fitted slot to use when more than one
                  # instrument of "instrument"'s family (SMU/DMM) is fitted
                  # at once (a second SMU added via Setup tab's + Add
                  # Instrument, or Electroglas's 3458A + E1326B VXI both
                  # fitted) - the controller.drivers key, e.g. "smu_2" or
                  # "dmm_vxi". Blank (the default, and every recipe saved
                  # before this field existed) means "whichever slot
                  # 'instrument' has always meant" - see
                  # instrument_panel._exec_run_steps_once's
                  # _exec_resolve_instrument.
                  "instrument_key",
                  # "" (default - send nothing, every recipe saved before
                  # this existed), "FRONT", or "REAR" - which physical
                  # terminals to source/measure from, for an instrument
                  # that supports switching (Keithley2400.set_terminals;
                  # see that driver's own comment - a switch-matrix-
                  # routed probe card is wired to REAR, a direct-wired
                  # one is typically hand-clipped to FRONT instead).
                  # Applied once at the top of this step, before it
                  # sources/measures anything - see
                  # instrument_panel._exec_run_steps_once.
                  "terminals")

# A step is either routed through the switch matrix (pins name the crosspoints
# / relay channels to close) or wired straight to the probe card by hand. The
# second case is real: a 4-wire resistance check on the 3458A is often cabled
# directly, and then a pin number describes nothing the GUI can act on - all
# it has to do is take the reading.
ROUTE_SWITCH = "switch"
ROUTE_DIRECT = "direct"
_ROUTES = (ROUTE_SWITCH, ROUTE_DIRECT)

# The one step type that takes four pins: source HI/LO carry the test current,
# sense HI/LO read the voltage right at the pad, so the probe/lead resistance
# in the source path drops out of the answer. Named for the 3458A's OHMF.
FOUR_WIRE_TYPE = "ohmf"
SENSE_FIELDS = ("his", "los")

STEP_FIELDS = _STEP_FIELDS

DEFAULT_RECIPE_FILENAME = "ata_default_recipe.json"


def load_default_recipe(folder: str, system: str = None):
    """(card, recipe) last marked default for this ATA folder, or (None, None).

    Scoped per system (accretech/electroglas) - the same ATA folder's probe
    card list is shared between benches (one probe_cards\\ folder), so a
    default set while running Accretech names a card wired for Accretech's
    pin format. Loading that same entry while on Electroglas would switch
    to a mismatched card and select that recipe's touchdowns (in Accretech's
    grid) on the Electroglas map. `system=None` reads the legacy flat
    top-level entry, for callers that predate this split.
    """
    if not folder:
        return None, None
    path = os.path.join(folder, DEFAULT_RECIPE_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, None
    if system:
        entry = data.get(system) or {}
        return entry.get("card"), entry.get("recipe")
    return data.get("card"), data.get("recipe")


def save_default_recipe(folder: str, card: str, recipe: str, system: str = None) -> bool:
    if not folder:
        return False
    path = os.path.join(folder, DEFAULT_RECIPE_FILENAME)
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if system:
        data[system] = {"card": card, "recipe": recipe}
    else:
        data["card"] = card
        data["recipe"] = recipe
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError:
        return False


def _normalize_numeric_field(text: str) -> str:
    try:
        float(text)
        return text
    except ValueError:
        pass
    return repr(parse_engineering(text))


def _is_measurement_step(step: dict) -> bool:
    t = step.get("type")
    if t in ("resistance", FOUR_WIRE_TYPE):
        return True
    if t in ("voltage", "current"):
        return step.get("mode") == "measure"
    return False


def _step_unit(step: dict) -> str:
    """The physical unit a step's own value carries - "" for step types
    that don't produce/force a plain V/A/ohm quantity (delay, open,
    passfail, picture, wave)."""
    t = step.get("type")
    if t in ("resistance", FOUR_WIRE_TYPE):
        return "ohm"
    if t == "voltage":
        return "V"
    if t == "current":
        return "A"
    return ""


def describe_target_calc(measured_step: dict, applied_step: dict) -> str:
    """Grey hint text: what combining a measure step's own reading with its
    Target step's forced quantity will compute - e.g. force current
    elsewhere, measure voltage here, and the two combine into a resistance;
    or (Ohm's law) measure resistance here, force V or I elsewhere, and get
    the other quantity out. "" means there's no known calculation for this
    pair of units - the Target is still kept as a reference (so a passfail
    after it can still name it), but the step's raw reading is what gets
    reported/checked."""
    a_unit = _step_unit(applied_step)
    m_unit = _step_unit(measured_step)
    if not (a_unit and m_unit):
        return ""
    applied_name = applied_step.get("name") or "target"
    if {a_unit, m_unit} == {"V", "A"}:
        if m_unit == "V":
            return f"→ resistance: R = this reading (V) ÷ '{applied_name}' (I)  →  Ω"
        return f"→ resistance: R = '{applied_name}' (V) ÷ this reading (I)  →  Ω"
    if m_unit == "ohm" and a_unit == "V":
        return f"→ Ohm's law: I = '{applied_name}' (V) ÷ this reading (Ω)  →  A"
    if m_unit == "ohm" and a_unit == "A":
        return f"→ Ohm's law: V = '{applied_name}' (I) × this reading (Ω)  →  V"
    return ""


def compute_target_derived(measured_value: float, measured_unit: str,
                           applied_value: float, applied_unit: str):
    """(value, unit) derived from a measure step's own reading plus its
    Target step's value, or None when no known calculation applies to this
    pair of units - the caller should keep the raw measured value in that
    case (blank divide-by-zero also returns None, for the same reason).

    Two known pairings: a measured V + an applied I (or vice versa) combine
    into resistance; a measured resistance + an applied V or I combine via
    Ohm's law into the other quantity (I = V/R, or V = I*R)."""
    if {measured_unit, applied_unit} == {"V", "A"}:
        v = measured_value if measured_unit == "V" else applied_value
        i = measured_value if measured_unit == "A" else applied_value
        if not i:
            return None
        return v / i, "ohm"
    if measured_unit == "ohm" and applied_unit == "V":
        if not measured_value:
            return None
        return applied_value / measured_value, "A"
    if measured_unit == "ohm" and applied_unit == "A":
        return applied_value * measured_value, "V"
    return None


def _instrument_options(step_type: str, mode: str) -> tuple:
    if step_type == "resistance":
        return ("DMM", "SMU")
    if step_type == FOUR_WIRE_TYPE:
        # 4-wire ohms is a DMM function (3458A OHMF); the SMU has no
        # equivalent in this rig, so offering it would only mislead.
        return ("DMM",)
    if step_type in ("voltage", "current"):
        return ("SMU",) if mode == "apply" else ("SMU", "DMM")
    if step_type == "wave":
        return ("WGEN",)
    return ()


def _default_instrument(step_type: str, mode: str) -> str:
    if step_type == "wave":
        return "WGEN"
    if step_type in ("resistance", FOUR_WIRE_TYPE):
        return "DMM"
    if step_type == "voltage":
        return "SMU" if mode == "apply" else "DMM"
    if step_type == "current":
        return "SMU"
    return ""


def _limit_applicable(step_type: str, mode: str, instrument: str) -> bool:
    if step_type == "wave":
        return True
    if instrument != "SMU":
        return False
    return step_type == "current" or (step_type == "voltage" and mode == "apply")


_DEFAULT_SMU_CURRENT_LIMIT = "0.000001"


def _limit_is_current_compliance(step_type: str, mode: str, instrument: str) -> bool:
    if not _limit_applicable(step_type, mode, instrument):
        return False
    if step_type == "voltage":
        return True
    return step_type == "current" and mode != "apply"


def _avg_display(step: dict) -> str:
    try:
        n = int(step.get("avg_count") or 1)
    except ValueError:
        n = 1
    nplc = (step.get("nplc") or "").strip()
    parts = []
    if n > 1:
        parts.append(f"{n}×{step.get('avg_delay') or 0}ms")
    if nplc:
        parts.append(f"NPLC={nplc}")
    settle = (step.get("settle_delay") or "").strip()
    if settle and settle != "0":
        parts.append(f"settle={settle}ms")
    if (step.get("abs_value") or "").strip():
        parts.append("abs")
    return ", ".join(parts)


def _serialize_step(step: dict) -> str:
    return " | ".join(f"{k}={step.get(k, '')}" for k in _STEP_FIELDS)


def _normalize_step(step: dict) -> dict:
    t = step["type"]
    # Blank/unparsable (old recipes, or a hand-edited CSV with no "die"
    # column at all) defaults to 1 - the common single-die-per-shot case
    # then needs nothing set. Passfail still carries its own die number (it
    # tracks a per-die verdict for shot coloring - see
    # instrument_panel._exec_run_steps_once's _exec_slot_verdicts), so it
    # defaults the same way. Delay/open/picture touch no die at all - a wait,
    # a channel release, and a not-yet-implemented photo aren't "of" any
    # die - so they get no number rather than a misleading "1".
    if t in ("delay", "open", "picture"):
        step["die"] = ""
    else:
        try:
            step["die"] = str(max(1, int(float(step.get("die") or "1"))))
        except (TypeError, ValueError):
            step["die"] = "1"
    # Blank means switch-routed, NOT "work out the default from the
    # instrument". Every recipe written before this field existed was
    # switch-routed, and quietly re-reading one as direct would stop it
    # closing the channels it has always closed. The 3458A-defaults-to-direct
    # rule is an EDITOR default for steps being built (see
    # _default_route_for), applied when you pick the instrument.
    if step.get("route") not in _ROUTES:
        step["route"] = ROUTE_SWITCH
    if step["route"] == ROUTE_DIRECT:
        # Nothing to close. Pins stay whatever they were - they are simply
        # not required or used - so flipping back to switch does not lose
        # what was already typed.
        step["conn"] = ""
    # Enforced here rather than only in the editor, so a recipe hand-edited or
    # imported with sense pins on a 2-wire step cannot smuggle them through.
    if t != FOUR_WIRE_TYPE:
        step["his"] = step["los"] = ""
    if t == "delay":
        step["mode"] = step["chan"] = step["target"] = step["instrument"] = ""
        step["instrument_key"] = ""
        step["hi"] = step["lo"] = step["conn"] = ""
        step["limit"] = step["shape"] = step["freq"] = ""
        step["min"] = step["max"] = ""
        step["avg_count"] = step["avg_delay"] = step["nplc"] = step["settle_delay"] = ""
        step["abs_value"] = ""
        return step
    if t == "open":
        step["mode"] = step["chan"] = step["instrument"] = ""
        step["instrument_key"] = ""
        step["hi"] = step["lo"] = step["level"] = ""
        step["limit"] = step["shape"] = step["freq"] = ""
        step["min"] = step["max"] = ""
        step["avg_count"] = step["avg_delay"] = step["nplc"] = step["settle_delay"] = ""
        step["abs_value"] = ""
        return step
    if t == "passfail":
        step["mode"] = step["chan"] = step["instrument"] = ""
        step["instrument_key"] = ""
        step["hi"] = step["lo"] = step["conn"] = ""
        step["level"] = step["limit"] = step["shape"] = step["freq"] = ""
        step["avg_count"] = step["avg_delay"] = step["nplc"] = step["settle_delay"] = ""
        step["abs_value"] = ""
        return step
    if t == "picture":
        step["mode"] = step["chan"] = step["target"] = step["instrument"] = ""
        step["instrument_key"] = ""
        step["hi"] = step["lo"] = step["conn"] = step["level"] = ""
        step["limit"] = step["shape"] = step["freq"] = ""
        step["min"] = step["max"] = ""
        step["avg_count"] = step["avg_delay"] = step["nplc"] = step["settle_delay"] = ""
        step["abs_value"] = ""
        return step
    if t == "move":
        # Die # is the one field that matters - see _STEP_TYPES - everything
        # else here routes/measures nothing.
        step["mode"] = step["chan"] = step["target"] = step["instrument"] = ""
        step["instrument_key"] = ""
        step["hi"] = step["lo"] = step["conn"] = step["level"] = ""
        step["limit"] = step["shape"] = step["freq"] = ""
        step["min"] = step["max"] = ""
        step["avg_count"] = step["avg_delay"] = step["nplc"] = step["settle_delay"] = ""
        step["abs_value"] = ""
        try:
            step["die"] = str(max(1, int(float(step.get("die") or "1"))))
        except (TypeError, ValueError):
            step["die"] = "1"
        return step

    saved_target = step.get("target", "")
    step["target"] = ""
    step["min"] = step["max"] = ""
    if t in ("resistance", FOUR_WIRE_TYPE):
        step["mode"] = "measure"
    elif t == "wave":
        step["mode"] = "apply"
    elif step["mode"] not in _STEP_MODES:
        step["mode"] = "measure"
    if step["mode"] == "measure" and t != FOUR_WIRE_TYPE:
        # A measure step's Target names an earlier APPLY step whose forced
        # quantity combines with this step's own reading into a derived
        # value (resistance, or - for a plain "resistance" step - the other
        # quantity via Ohm's law; see compute_target_derived /
        # instrument_panel._exec_run_steps_once). Blank is legitimate -
        # most measurements need nothing applied elsewhere to already mean
        # what they say. 4-wire ohms never combines with a Target at all -
        # see _on_type_change's own comment on why it's excluded here too.
        step["target"] = saved_target

    options = _instrument_options(t, step["mode"])
    # Blank is now a legitimate state, not just "not yet set" - see
    # pma_params_to_steps, which leaves it blank on purpose for a step
    # that needs an instrument the active bench does not have fitted at
    # all (Bias Voltage/apply needs an SMU; probe03 has none). Only a
    # genuinely WRONG (non-blank, invalid) instrument gets corrected to
    # the type/mode's default here - forcing blank back to a default
    # would silently reintroduce the unavailable instrument.
    if step["instrument"] and step["instrument"] not in options:
        step["instrument"] = _default_instrument(t, step["mode"])
        # A specific-slot pick only ever makes sense for the family it
        # was picked under - once that family itself was invalid and got
        # reset, a leftover instrument_key would silently point at the
        # WRONG family's slot instead of just falling back cleanly.
        step["instrument_key"] = ""
    instrument = step["instrument"]

    if t == "wave":
        if step["shape"] not in _WAVE_SHAPES:
            step["shape"] = "SIN"
        if not step["freq"]:
            step["freq"] = "1000"
        if step["chan"] not in _WGEN_CHANNELS:
            step["chan"] = "CH1"
    else:
        step["shape"] = step["freq"] = ""
        if instrument == "SMU":
            if step["chan"] not in _SMU_CHANNELS:
                step["chan"] = "A"
        else:
            step["chan"] = ""

    if not _limit_applicable(t, step["mode"], instrument):
        step["limit"] = ""
    elif (_limit_is_current_compliance(t, step["mode"], instrument)
          and not (step.get("limit") or "").strip()):
        step["limit"] = _DEFAULT_SMU_CURRENT_LIMIT

    if _is_measurement_step(step):
        if not (step.get("avg_count") or "").strip():
            step["avg_count"] = "1"
        if not (step.get("avg_delay") or "").strip():
            step["avg_delay"] = "0"
        if not (step.get("nplc") or "").strip():
            step["nplc"] = "1"
        if not (step.get("settle_delay") or "").strip():
            step["settle_delay"] = "0"
    else:
        step["avg_count"] = step["avg_delay"] = step["nplc"] = step["settle_delay"] = ""
        step["abs_value"] = ""
    return step


def _parse_step(text: str) -> dict:
    step = {k: "" for k in _STEP_FIELDS}
    for part in text.split("|"):
        key, _, val = part.partition("=")
        key = key.strip().lower()
        if key in step:
            step[key] = val.strip()
    if step["type"] not in _STEP_TYPES:
        step["type"] = "resistance"
    return _normalize_step(step)


def _safe_filename(name: str) -> str:
    return "".join(c for c in name.strip() if c.isalnum() or c in " _-").strip() or "recipe"


def parse_recipe_file(path: str) -> dict:
    name = os.path.splitext(os.path.basename(path))[0]
    step_items = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(("#", ";", "[")):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key.lower().startswith("step") and key[4:].isdigit():
                    step_items.append((int(key[4:]), val))
    steps = [_parse_step(val) for _n, val in sorted(step_items)]
    return {name: {"steps": steps}}


def write_recipe_file(path: str, recipe: dict):
    lines = [f"# ATA recipe — {os.path.splitext(os.path.basename(path))[0]}", ""]
    steps = recipe.get("steps", [])
    for i, step in enumerate(steps, 1):
        lines.append(f"Step{i}={_serialize_step(step)}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


_PMA_MAPPED_KEYS   = {"Voltage", "MeterCurrentLimit", "Averages", "MeterDelay",
                      "Delay1", "Delay2", "Delay3", "NPLC", "MeterRange"}
# Iterations is the only one left with nowhere to go: it repeats the whole
# measurement block, which is a run-level concern rather than a step field.
_PMA_UNMAPPED_KEYS = {"Iterations"}
_PMA_USEFUL_KEYS   = _PMA_MAPPED_KEYS | _PMA_UNMAPPED_KEYS


def parse_pma_params(path: str) -> dict:
    useful = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(("#", ";", "[")) or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key in _PMA_USEFUL_KEYS and val:
                useful[key] = val
    return useful


def _pma_blank_step() -> dict:
    return {k: "" for k in _STEP_FIELDS}


def _pma_num(params: dict, key: str, default: str = "0") -> str:
    val = (params.get(key) or "").strip()
    if not val:
        return default
    try:
        float(val)
    except ValueError:
        return default
    return val


def pma_params_to_steps(params: dict, available: tuple = _INSTRUMENTS) -> list:
    """`available`: which instruments the active bench actually has fitted
    (see RecipePanel._bench_instruments) - probe03 has no SMU, only the
    3458A. A step whose type/mode has no instrument in common with
    `available` at all (Bias Voltage/apply needs an SMU to source - no DMM
    can do that) is built with instrument="" rather than a hardcoded "SMU"
    that does not exist on that bench and would silently measure nothing.
    A step that CAN run on what IS available (Leakage Measurement/measure
    accepts either SMU or DMM) picks whichever of those is actually
    fitted, preferring SMU when both are - so a whole-wafer .PMA imported
    on probe03 still comes back mostly usable, not entirely blank.
    """
    steps = []

    def _pick_instrument(step_type: str, mode: str, preferred: str) -> str:
        options = [i for i in _instrument_options(step_type, mode) if i in available]
        if preferred in options:
            return preferred
        return options[0] if options else ""

    d1 = _pma_num(params, "Delay1", "100")
    if float(d1) > 0:
        steps.append(_normalize_step({**_pma_blank_step(), "type": "delay",
                                      "name": "Settle before bias (Delay1)",
                                      "level": d1}))

    voltage = (params.get("Voltage") or "").strip()
    limit   = (params.get("MeterCurrentLimit") or "").strip()
    apply_name = "Bias Voltage"
    steps.append(_normalize_step({
        **_pma_blank_step(), "type": "voltage", "mode": "apply",
        "instrument": _pick_instrument("voltage", "apply", "SMU"),
        "chan": "A", "name": apply_name,
        "level": voltage, "limit": limit,
    }))

    d2 = _pma_num(params, "Delay2", "100")
    if float(d2) > 0:
        steps.append(_normalize_step({**_pma_blank_step(), "type": "delay",
                                      "name": "Settle between bias and measure (Delay2)",
                                      "level": d2}))

    avg_count = _pma_num(params, "Averages", "1")
    meter_delay_s = (params.get("MeterDelay") or "").strip()
    avg_delay_ms = "0"
    if meter_delay_s:
        try:
            ms = float(meter_delay_s) * 1000
            avg_delay_ms = str(int(ms)) if ms.is_integer() else str(ms)
        except ValueError:
            pass
    nplc = _pma_num(params, "NPLC", "1")

    meas_name = "Leakage Measurement"
    steps.append(_normalize_step({
        **_pma_blank_step(), "type": "current", "mode": "measure",
        "instrument": _pick_instrument("current", "measure", "SMU"),
        "chan": "A", "name": meas_name,
        "avg_count": avg_count, "avg_delay": avg_delay_ms, "nplc": nplc,
        # MeterRange used to be dropped, so the SMU autoranged where LaMP had
        # pinned it. Carrying it means a different .PMA reconfigures the meter
        # on LOAD ALL rather than silently inheriting the last recipe's range.
        "mrange": (params.get("MeterRange") or "").strip(),
    }))

    if limit:
        # The threshold must sit WELL BELOW the compliance limit, not at it.
        # A source held in compliance reads a hair under its limit - 999.99 nA
        # against a 1 uA setting - so "max = limit" can never be exceeded and
        # a dead short passes by a fraction of a nanoamp. Measured on the
        # wafer: a TARGET, which is solid metal, passed a Leakage Check
        # written that way.
        #
        # A tenth of the compliance separates the two populations by a wide
        # margin in both directions. In the LaMP reference data a short sits
        # at the clamp and an isolated die three orders below it, so anything
        # between is safe; a tenth is far from both.
        #
        # Bounded on BOTH sides, because a large negative current is just as
        # much a failure and an upper bound alone lets it through.
        try:
            edge = abs(float(limit)) / 10.0
            lo, hi = f"{-edge:.12g}", f"{edge:.12g}"
        except (TypeError, ValueError):
            lo, hi = "", limit
        steps.append(_normalize_step({
            **_pma_blank_step(), "type": "passfail", "name": "Leakage Check",
            "target": meas_name, "min": lo, "max": hi}))

    steps.append(_normalize_step({
        **_pma_blank_step(), "type": "open", "name": "Release", "target": apply_name}))

    d3 = _pma_num(params, "Delay3", "100")
    if float(d3) > 0:
        steps.append(_normalize_step({**_pma_blank_step(), "type": "delay",
                                      "name": "Settle after release (Delay3)",
                                      "level": d3}))
    return steps


def repeat_steps_per_die(steps: list, dies_per_shot: int, channels=None,
                        pins=None) -> list:
    """One block of steps per die under the shot.

    The prober lands ONCE on a shot; the relay card then connects each of the
    co-touched dies in turn and the block is measured against each. So the
    repetition belongs here, in the steps, and not in the touchdown list -
    listing the shot four times would move the chuck to the same place four
    times.

    `channels` is the relay channel per die, in die order. Without it the
    blocks are identical and every die is measured through whatever routing
    the previous one left closed, which silently measures die 1 four times.

    `pins` is the (HI, LO) probe-card pin pair per die. It changes nothing
    electrically - the relay decides what is connected - but it records which
    needles the reading came from, and it is what the recipe validator checks
    against the loaded card.
    """
    if dies_per_shot <= 1:
        return steps
    names_in_block = {s["name"] for s in steps if s.get("name")}
    out = []
    for i in range(1, dies_per_shot + 1):
        suffix = f" (Die {i})"
        chan = ""
        if channels and i <= len(channels):
            chan = channels[i - 1]
        hi = lo = ""
        if pins and i <= len(pins):
            hi, lo = pins[i - 1]
        # Open everything before connecting this die: the mux would otherwise
        # hold the previous die closed as well, putting two in parallel.
        if chan:
            out.append({"kind": "STEP", "name": f"Isolate{suffix}",
                        "type": "open", "target": "all", "conn": "all",
                        # Left unset until now, which _normalize_step then
                        # defaulted to "1" on load/display - so "Isolate
                        # (Die 3)"/"Isolate (Die 4)" showed Die # = 1 in the
                        # Recipe tab, contradicting their own name.
                        "die": str(i)})
        for s in steps:
            s2 = dict(s)
            if s2.get("name"):
                s2["name"] = s2["name"] + suffix
            if s2.get("target") in names_in_block:
                s2["target"] = s2["target"] + suffix
            # Every step in this block belongs to die i, whether or not it
            # touches the wafer itself (delay/open/passfail included) - this
            # is what _exec_slot_identity reads to file a measurement
            # against the right square. Previously left unset here, so
            # every step kept whatever "die" the single-die source steps
            # had (normalized to "1"), and a whole multi-die shot's worth
            # of results all filed against die 1 - see the reference
            # HPLaMP_WHOLE_WAFER recipe in LaMP_HP.csv, which sets this
            # correctly per block.
            s2["die"] = str(i)
            # Route only the steps that actually touch the wafer. A delay has
            # no connection, and giving one a channel would close a relay at
            # a point the original sequence had it open.
            if s2.get("type") in ("voltage", "current", "resistance"):
                if chan:
                    s2["conn"] = chan
                if hi and lo:
                    s2["hi"], s2["lo"] = hi, lo
            out.append(s2)
    return out


def _pma_dies_per_shot(path: str) -> int:
    """How many devices a touchdown of this .PMA co-touches.

    Taken from the widest device-ID string in the recipe, not from the first:
    a wafer-edge shot is written "NA/86-14/NA/NA" and still costs four
    switch positions, while a shot that happens to be full would read the
    same as a genuine single-die recipe.
    """
    try:
        import electroglas_pma as egpma
        fields = egpma.parse_pma_file(path)
        touchdowns = egpma.load_touchdowns(path, fields)
    except Exception:
        return 1
    widths = [len((t.get("device_id") or "").split("/")) for t in touchdowns]
    return max(widths) if widths else 1


def die_pins_from_card(wiring: list, dies_per_shot: int,
                       rows: int = 0, cols: int = 0,
                       die_pins: dict = None) -> list:
    """[(hi_pin, lo_pin)] per die, read off the probe card's own pin table.

    Derived, never assumed. A pad is matched to a die only when its label
    starts with that die's quad corner (TL/BL/TR/BR) and ends U or D, which
    is how the LaMP_HP card is labelled; the U pad becomes HI and the D pad
    LO. A card labelled any other way simply yields nothing and the steps
    keep their blank pins, which is what they had before - a wrong guess here
    would put a needle on the wrong die and still look plausible.

    Where a pad carries more than one pin the first is taken. On LaMP_HP the
    second is a manufacturer's spare that is not connected to anything, and
    that is specific to that card - it is not a pattern to rely on elsewhere,
    which is exactly why this picks one rather than trying to use both.
    """
    # An explicit slot -> (hi, lo) PIN table on the card wins outright. Pins
    # are the durable identifier: they are the physical contacts, they are what
    # lands on the relay, and they still mean something on the next probe card.
    # Pad names like "BRU" are one project's drawing convention, so deriving
    # dies from them only works by luck.
    if die_pins:
        out = []
        for slot in range(1, dies_per_shot + 1):
            pair = die_pins.get(slot) or die_pins.get(str(slot))
            if not pair or not (pair[0] and pair[1]):
                out = []
                break
            out.append((str(pair[0]), str(pair[1])))
        if out:
            return out

    try:
        from electroglas_pma import shot_geometry, slot_names
        order = slot_names(*shot_geometry(dies_per_shot, rows, cols))
    except Exception:
        order = ("TL", "BL", "TR", "BR")
    pin_of_pad = {}
    for row in wiring or []:
        pad = (row.get("pad") or "").strip().upper()
        pin = (row.get("pin") or "").strip()
        if pad and pin:
            pin_of_pad.setdefault(pad, pin)
    out = []
    for corner in order[:dies_per_shot]:
        hi = pin_of_pad.get(f"{corner}U")
        lo = pin_of_pad.get(f"{corner}D")
        if not (hi and lo):
            return []
        out.append((hi, lo))
    return out


def die_channels_for_bench(dies_per_shot: int) -> list:
    """Relay channel per die for the active Electroglas bench, if known.

    Read from hp_switchbox.BENCH_WIRING rather than assumed, because the two
    benches differ: probe02's mux switches a HI/LO pair per die (one channel),
    probe03's form-C card needs two channels for the same job.
    """
    try:
        # 'from instruments import', not a bare import - eg_profiles lives in
        # the instruments package, so the bare form raised ModuleNotFoundError
        # into the except below and every recipe came out with no channels.
        from instruments import eg_profiles
        from instruments.hp_switchbox import bench_wiring
        die_sets = bench_wiring(eg_profiles.active_name()).get("die_sets") or {}
    except Exception:
        return []
    out = []
    for die in range(1, dies_per_shot + 1):
        chans = die_sets.get(die)
        if not chans:
            return []
        out.append(",".join(f"{int(c):02d}" for c in chans))
    return out


def site_key(site: dict) -> tuple:
    """(row, col) of a touchdown, or None when it carries only a die ID."""
    try:
        return int(site["row"]), int(site["col"])
    except (KeyError, TypeError, ValueError):
        return None


def recipes_to_rows(recipes: dict) -> list:
    rows = []
    for name, rec in recipes.items():
        # bench: which Electroglas prober (probe02/probe03) this recipe was
        # built for - blank for Accretech (one fixed bench, no ambiguity)
        # and for recipes saved before this existed. See RecipePanel's
        # _active_bench_tag/_visible_recipe_names - a probe card is shared
        # hardware between benches, but a recipe built around one bench's
        # fitted instruments is not automatically usable on the other, so
        # the picker only shows a bench-tagged recipe on its own bench.
        origin = rec.get("shot_origin")
        rows.append({"kind": "RECIPE", "recipe": name, "bench": rec.get("bench", ""),
                     # Minor moves: see RecipePanel._on_minor_moves_toggle /
                     # _set_shot_origin. shot_origin is the die-index
                     # coordinate the chuck was sitting at (shot row0/col0,
                     # die #1 - the Wafer Builder Shot tab's own numbering,
                     # not necessarily grid cell row0/col0) when the
                     # operator last pressed Set Shot Origin for THIS
                     # recipe - re-captured live before each run, but saved
                     # here so a reload does not lose it.
                     "minor_moves": "1" if rec.get("minor_moves") else "",
                     # Shortcut: see RecipePanel._on_shortcut_toggle /
                     # instrument_panel._exec_should_configure. Off by
                     # default (a recipe saved before this existed comes
                     # back with shortcut=False, same as a fresh one) -
                     # opt IN per recipe, never assumed safe.
                     "shortcut": "1" if rec.get("shortcut") else "",
                     # Skip auto-clear on Force Current: see
                     # RecipePanel._on_fast_current_settle_toggle /
                     # instruments.keithley2400.set_source_clear_auto. Off by
                     # default, opt IN per recipe, same as shortcut above.
                     "fast_current_settle": "1" if rec.get("fast_current_settle") else "",
                     # Manual mode: see RecipePanel._on_manual_mode_toggle /
                     # instruments.keithley2400.measure_resistance. Off by
                     # default, opt IN per recipe, same as shortcut/
                     # fast_current_settle above.
                     "manual_mode": "1" if rec.get("manual_mode") else "",
                     # Align die: purely a convenience - it preselects the
                     # Run tab's "Chuck is on" dropdown when this recipe
                     # loads and the chuck is not already set, so the
                     # operator does not scroll a list of thousands to
                     # find the die they always start at. It has no effect
                     # on the run itself. See RecipePanel._on_align_die_pick
                     # and instrument_panel._exec_preselect_align_die.
                     "align_die": rec.get("align_die") or "",
                     # Wafer Map: which of this ATA folder's saved Wafer
                     # Builder maps this recipe goes with - see
                     # RecipePanel._on_wafer_map_pick and
                     # instrument_panel._exec_autoload_recipe_wafer_map.
                     "wafer_map": rec.get("wafer_map") or "",
                     "shot_origin_x": "" if origin is None else str(origin[0]),
                     "shot_origin_y": "" if origin is None else str(origin[1])})
        for i, step in enumerate(rec.get("steps", []), 1):
            row = {"kind": "STEP", "recipe": name, "seq": str(i)}
            for k in _STEP_FIELDS:
                row[k] = step.get(k, "")
            rows.append(row)
        # SITE rows ride in the same CSV as the steps, reusing existing
        # columns (name = die ID, hi/lo = row/col) so a probe card written by
        # this version still loads in one that predates touchdown lists - an
        # unknown "kind" is skipped, and the recipe just comes back without
        # its sites rather than failing to parse.
        for i, site in enumerate(rec.get("sites", []), 1):
            rows.append({"kind": "SITE", "recipe": name, "seq": str(i),
                         "name": site.get("die_id", ""),
                         "hi": str(site.get("row", "")),
                         "lo": str(site.get("col", ""))})
    return rows


def rows_to_recipes(rows: list) -> dict:
    recipes: dict = {}
    step_rows: dict = {}
    site_rows: dict = {}
    for row in rows:
        kind = (row.get("kind") or "").strip().upper()
        if kind not in ("RECIPE", "STEP", "SITE"):
            continue
        name = (row.get("recipe") or "").strip()
        if not name:
            continue
        recipes.setdefault(name, {"steps": [], "sites": [], "bench": "",
                                  "minor_moves": False, "shortcut": False,
                                  "fast_current_settle": False,
                                  "manual_mode": False,
                                  "align_die": "",
                                  "wafer_map": "",
                                  "shot_origin": None})
        if kind == "RECIPE":
            bench = (row.get("bench") or "").strip()
            if bench:
                recipes[name]["bench"] = bench
            recipes[name]["minor_moves"] = (row.get("minor_moves") or "").strip() == "1"
            recipes[name]["shortcut"] = (row.get("shortcut") or "").strip() == "1"
            recipes[name]["fast_current_settle"] = (
                row.get("fast_current_settle") or "").strip() == "1"
            recipes[name]["manual_mode"] = (row.get("manual_mode") or "").strip() == "1"
            recipes[name]["align_die"] = (row.get("align_die") or "").strip()
            recipes[name]["wafer_map"] = (row.get("wafer_map") or "").strip()
            ox = (row.get("shot_origin_x") or "").strip()
            oy = (row.get("shot_origin_y") or "").strip()
            if ox and oy:
                try:
                    recipes[name]["shot_origin"] = [float(ox), float(oy)]
                except ValueError:
                    pass
            continue
        try:
            seq = int(row.get("seq") or 0)
        except ValueError:
            seq = 0
        if kind == "SITE":
            def _int(v):
                try:
                    return int(str(v).strip())
                except (TypeError, ValueError):
                    return None
            site = {"die_id": (row.get("name") or "").strip(),
                    "row": _int(row.get("hi")), "col": _int(row.get("lo"))}
            # A site with no row/col cannot be walked to, so drop it rather
            # than let the run silently skip it later.
            if site["row"] is not None and site["col"] is not None:
                site_rows.setdefault(name, []).append((seq, site))
            continue
        step = {k: row.get(k, "") for k in _STEP_FIELDS}
        if step.get("type") not in _STEP_TYPES:
            step["type"] = "resistance"
        step_rows.setdefault(name, []).append((seq, step))
    for name, items in step_rows.items():
        items.sort(key=lambda t: t[0])
        recipes[name]["steps"] = [_normalize_step(s) for _seq, s in items]
    for name, items in site_rows.items():
        items.sort(key=lambda t: t[0])
        recipes[name]["sites"] = [s for _seq, s in items]
    for rec in recipes.values():
        rec.setdefault("sites", [])
    return recipes


class RecipePanel(ttk.Frame):
    def __init__(self, parent, controller, get_pins=None, get_wiring=None,
                 get_active_card=None, save_recipes=None, system: str = "accretech",
                 switch_card=None, get_card_names=None, get_ata_folder=None,
                 get_die_pins=None, on_save=None, get_wafer_map_names=None):
        super().__init__(parent)
        self.controller = controller
        self._get_pins = get_pins or (lambda: [])
        self._get_wiring = get_wiring or (lambda: [])
        # slot -> (hi pin, lo pin) for the active card, if it declares one.
        self._get_die_pins = get_die_pins or (lambda: {})
        self._get_active_card = get_active_card or (lambda: "")
        self._save_recipes = save_recipes or (lambda _card, _recipes: False)
        self._switch_card_cb = switch_card or (lambda _name: None)
        self._get_card_names = get_card_names or (lambda: [])
        self._get_ata_folder = get_ata_folder or (lambda: None)
        # Names of the ATA folder's saved Wafer Builder maps, for the
        # Wafer Map: dropdown - see _on_wafer_map_pick/get_wafer_map and
        # instrument_panel._exec_autoload_recipe_wafer_map.
        self._get_wafer_map_names = get_wafer_map_names or (lambda: [])
        # Save-also-loads-into-Run-tab redundancy for the ⟳-less Recipe
        # dropdown on the Run tab - see _save() below.
        self._on_save = on_save or (lambda _name: None)
        self._conn_viewer = None
        # Which instrument the editor's Direct/Switch box was last defaulted
        # for, so picking a new instrument re-seeds it (3458A -> direct) but
        # merely re-running _on_type_change does not clobber a manual choice.
        self._route_defaulted_for = None
        self._system = system
        if system == "electroglas":
            self._smu_channel_choices = ("A",)
            self._step_type_choices = tuple(t for t in _STEP_TYPES if t != "wave")
        else:
            self._smu_channel_choices = self._smu_channels_for_active_bench()
            self._step_type_choices = _STEP_TYPES
        self._instrument_choices = self._bench_instruments()
        self._conn_report = "— no steps —"

        self._recipes: dict = {"(unsaved)": {"steps": [], "sites": []}}
        self._current: str = "(unsaved)"
        self._active_card: str = ""

        self._steps: list[dict] = self._recipes[self._current]["steps"]
        self._sites: list[dict] = self._recipes[self._current]["sites"]

        # Minor moves: a wafer-map square is a SHOT (several real dies,
        # e.g. a 7x9 reticle) rather than one die - single-die probe
        # cards (Accretech's today; Electroglas's on a future project)
        # physically reposition to whichever die # a step calls for
        # instead of contacting the whole shot at once. Off by default -
        # see the recipe's own "minor_moves"/"shot_origin" fields above.
        self._minor_moves_var = tk.BooleanVar(value=False)
        self._shot_origin_status_var = tk.StringVar(value="")

        # Shortcut: skip resending a step's SMU/DMM configuration (level,
        # limit, NPLC, range, source delay) on a touchdown where it would
        # be identical to what was already sent - see
        # instrument_panel._exec_should_configure. Off by default and
        # opt-in PER RECIPE, not global - a real-hardware test surfaced a
        # case (Maddy TL, Keithley 2400) where skipping a resend left
        # voltage compliance at the instrument's own default instead of
        # the recipe's configured limit, so this is not assumed safe for
        # every recipe/instrument combination without being verified on
        # the bench first.
        self._shortcut_var = tk.BooleanVar(value=False)

        # Skip auto-clear on Force Current: the Keithley 2400's
        # sour:clear:auto (on by default, _FIXED_SETUP) drops the output
        # and re-applies the bias fresh on every :READ? - fine for a
        # reading that's actually used, disruptive when a Force Current
        # step's own readback is purely for logging and a dependent sense
        # step reads the pad a moment later (confirmed on the bench,
        # Cenfire: a marginal contact's sense reading collapsed to
        # near-zero specifically because of this transient). Off by
        # default and opt-in PER RECIPE, same reasoning as Shortcut above -
        # not assumed safe for every recipe/instrument combination (LaMP's
        # own current-measure step genuinely needs auto-clear ON) without
        # being verified on the bench first.
        self._fast_current_settle_var = tk.BooleanVar(value=False)

        # Manual mode: which resistance-measurement mode the Keithley 2400's
        # measure_resistance() uses - see instruments.keithley2400's own
        # comment. AUTO (this box unchecked, the default) is correct for
        # every recipe that has no preceding Force Current step reusing the
        # same pins (10340/10341, any standalone resistance check) - MANUAL
        # reuses whatever current was last forced, which is 0 with nothing
        # forced first, and that was the direct-wiring 0-ohm bug. Off by
        # default and opt-in PER RECIPE, same reasoning as Shortcut/Skip
        # auto-clear above - only Maddy TL's Kelvin Resistance step (forces
        # a current, then reads ohms off the same pins) actually needs it
        # checked.
        self._manual_mode_var = tk.BooleanVar(value=False)

        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_toolbar()
        self._build_body()
        self._refresh_picker()
        self._update_connections()
        self._update_validity_label()


    # Which recipe "instrument" each profile key can stand in for. The recipe
    # stays generic - a step says DMM, not "3458A" - so the same recipe runs on
    # any bench that has some DMM fitted.
    _EG_INSTRUMENT_KEYS = {
        "DMM": ("dmm_eg", "dmm_vxi_eg"),
        "SMU": ("smu_eg",),
    }

    def _active_bench_tag(self) -> str:
        """Which prober bench a recipe created right now should be tagged
        with, so it only shows on that bench later - see
        _visible_recipe_names().

        Electroglas: reads instruments.eg_profiles.active_name() directly,
        NOT AtomicaDashboard._active_bench() - that reflects the bench for
        whichever system is CURRENTLY DISPLAYED (controller.active_system),
        which is still "accretech" for a chunk of app startup (both
        systems' ATA folders autoload before _apply_default_prober ever
        switches the displayed system - see app.py's __init__ ordering).
        Going through the controller during that window made every
        Electroglas recipe tagged for a real bench (e.g. probe03) look
        invisible - _active_bench() falls through to the Accretech branch
        and returns "probe08", which never matches - so a default recipe
        autoload silently found nothing to load.

        Accretech: unchanged, still via the controller (single fixed bench
        today - probe08 - so nothing is filtered out in practice; a second
        Accretech prober would already be scoped correctly here).
        """
        if self._system == "electroglas":
            try:
                from instruments import eg_profiles
                return eg_profiles.active_name() or ""
            except Exception:
                return ""
        try:
            return self.controller._active_bench() or ""
        except Exception:
            return ""

    def _visible_recipe_names(self) -> list:
        """Recipe names the picker/dropdown should show on the CURRENT
        bench - a probe card is shared hardware between probe02/probe03,
        but a recipe built around one bench's fitted instruments (e.g. an
        SMU step) is not automatically usable on the other, so a
        bench-tagged recipe only shows on its own bench. Untagged recipes
        (Accretech, or anything saved before this existed) show
        everywhere - nothing that already worked silently disappears.
        Storage itself is untouched either way; _save_recipes still writes
        every recipe on the card, just not all of them are offered here.
        """
        tag = self._active_bench_tag()
        if not tag:
            return list(self._recipes.keys())
        return [n for n, rec in self._recipes.items()
               if not rec.get("bench") or rec.get("bench") == tag]

    def _smu_channels_for_active_bench(self) -> tuple:
        """Which SMU channel letters ("A", "B") the ACTIVE Accretech bench's
        switch topology actually has a relay row wired for - not a fixed
        ("A", "B") regardless of hardware. A single-channel SMU (Keithley
        2400) only ever has channel A's rows (HI/LO) assigned; picking "B"
        in the editor used to be possible on every Accretech bench
        regardless, and switch_topology.rows_for_fields() would silently
        come back with NO rows for a channel nothing is wired to - no
        error, just an empty/broken connection the moment "Recompute
        Connections" ran (see CENFIRE-INLINE's real force1/force2 steps,
        which used to route channel B through rows C/D - correct on
        probe08old's 2636B, dead air on probe08's 2400).

        Electroglas doesn't call this - see __init__, it stays hardcoded
        to ("A",) same as always. Falls back to just ("A",) (the safe
        minimum every bench needs for its own SMU to work at all) rather
        than the full set on any lookup failure - offering an EXTRA
        channel that might not exist is the actual failure mode this
        exists to prevent; offering too few just limits the editor.
        """
        try:
            roles = switch_topology.row_roles()
            chans = sorted({role.get("channel") for role in roles.values()
                           if role and role.get("instrument") == "SMU"
                           and role.get("channel")})
        except Exception:
            chans = []
        return tuple(chans) or ("A",)

    def _bench_instruments(self) -> tuple:
        """Instruments the ACTIVE prober actually has fitted.

        probe03 has only the 3458A, so offering SMU there would let someone
        build a recipe that cannot run. Accretech is a single fixed bench and
        keeps the full list.
        """
        if self._system != "electroglas":
            return _INSTRUMENTS
        try:
            from instruments import eg_profiles
            fitted = set(eg_profiles.fitted_keys())
        except Exception:
            return ("DMM", "SMU")
        avail = tuple(name for name, keys in self._EG_INSTRUMENT_KEYS.items()
                      if fitted.intersection(keys))
        return avail or ("DMM",)

    def _bench_instrument_note(self) -> str:
        if self._system != "electroglas":
            return ""
        try:
            from instruments import eg_profiles
            inst = eg_profiles.instruments()
            bench = eg_profiles.active_name()
        except Exception:
            return ""
        parts = []
        for name, keys in self._EG_INSTRUMENT_KEYS.items():
            if name not in self._instrument_choices:
                continue
            key = next((k for k in keys
                        if k in inst and inst[k].get("fitted", True)), None)
            if key:
                parts.append(f"{name} = {inst[key].get('name', key)}")
        if not parts:
            return f"{bench}: no measurement instrument fitted"
        return f"{bench}:   " + "    ".join(parts)

    def refresh_bench_instruments(self):
        """Re-read the active prober - call after switching benches."""
        self._instrument_choices = self._bench_instruments()
        if self._system != "electroglas":
            self._smu_channel_choices = self._smu_channels_for_active_bench()
        if hasattr(self, "_bench_note_lbl"):
            self._bench_note_lbl.config(text=self._bench_instrument_note())
        if hasattr(self, "_instr_cb"):
            self._on_type_change()
        self._update_validity_label()
        # A recipe tagged for the OTHER bench should stop showing the
        # moment the bench actually switches, not just next time something
        # else happens to refresh the picker.
        if hasattr(self, "_picker"):
            self._refresh_picker()

    def _log_unbuildable_steps(self, steps: list):
        """pma_params_to_steps left instrument="" on any step whose type/
        mode has no instrument the active bench actually has fitted (Bias
        Voltage/apply needs an SMU to source, and probe03 has none) - say
        so plainly instead of leaving the operator to notice a blank
        dropdown on their own. The touchdown list and the rest of the
        recipe still come through; only these specific steps need a human
        to either pick an instrument by hand or accept they can't run here.
        """
        blank = [s.get("name") or f"step {i}" for i, s in enumerate(steps, 1)
                 if not s.get("instrument")
                 and s.get("type") not in ("delay", "open", "passfail", "picture", "move")]
        if not blank:
            return
        self.controller.log(
            f"[RECIPE] ⚠ {len(blank)} step(s) left with no instrument - the "
            f"active bench has nothing that can do them "
            f"({', '.join(self._instrument_choices) or 'none fitted'} only): "
            + ", ".join(blank) + ". Pick one by hand on the Recipe tab if "
            "there's a usable substitute, or accept they can't run on this "
            "bench.")

    def _build_toolbar(self):
        bar = tk.Frame(self, bg="#e2e8f0", relief="flat", bd=1)
        bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))

        tk.Label(bar, text="Recipe:", bg="#e2e8f0",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(8, 2), pady=4)
        self._picker_var = tk.StringVar(value=self._current)
        self._picker = ttk.Combobox(bar, textvariable=self._picker_var,
                                    state="readonly", width=26)
        self._picker.pack(side="left", padx=(0, 8), pady=4)
        self._picker.bind("<<ComboboxSelected>>", lambda _e: self._switch_recipe())

        self._validity_lbl = tk.Label(bar, text="", bg="#e2e8f0",
                                      font=("Segoe UI", 9, "bold"))
        self._validity_lbl.pack(side="left", padx=(0, 8), pady=4)

        self._btn_set_default = ttk.Button(bar, text="Set Default", width=15,
                                           command=self._set_default_recipe)
        self._btn_set_default.pack(side="left", padx=2, pady=4)

        self._btn_new = ttk.Button(bar, text="＋ New", width=9,
                                   command=self._new_recipe)
        self._btn_new.pack(side="left", padx=2, pady=4)
        self._btn_rename = ttk.Button(bar, text="✎ Rename", width=11,
                                      command=self._rename_recipe)
        self._btn_rename.pack(side="left", padx=2, pady=4)
        self._btn_delete = ttk.Button(bar, text="Delete", width=11,
                                      command=self._delete_recipe)
        self._btn_delete.pack(side="left", padx=2, pady=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6, pady=4)

        # The Import Legacy buttons are gone: the PMA Process tab's LOAD ALL
        # is the one way in, and it drives the same import_legacy_from_path /
        # import_legacy_workbook_from_path underneath. Two entry points meant a
        # recipe could be imported here from one PMA while the run adopted
        # another, with nothing to flag the mismatch.
        self._btn_save = ttk.Button(bar, text="Save", command=self._save)
        self._btn_save.pack(side="left", padx=2, pady=4)

        self._locked_lbl = tk.Label(bar, text="", bg="#e2e8f0", fg="#b45309",
                                    font=("Segoe UI", 8, "italic"))
        self._locked_lbl.pack(side="left", padx=(4, 8))

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6, pady=4)

        tk.Label(bar, text="Probe Card:", bg="#e2e8f0",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(4, 2), pady=4)
        self._card_picker_var = tk.StringVar(value="")
        self._card_picker = ttk.Combobox(bar, textvariable=self._card_picker_var,
                                         state="readonly", width=16)
        self._card_picker.pack(side="left", padx=(0, 8), pady=4)
        self._card_picker.bind("<<ComboboxSelected>>",
                               lambda _e: self._on_card_picker_selected())

        # Wafer Map: which of this ATA folder's saved Wafer Builder maps
        # THIS recipe goes with - saved with the recipe, same spirit as
        # Align die below. Loading the recipe (any entry point - the Run
        # tab's own dropdown, autoload on folder open, picking it here)
        # checks this against whichever map is currently active and
        # switches to the right one if they disagree - see
        # instrument_panel._exec_autoload_recipe_wafer_map. Both systems
        # get this (unlike Align die): Wafer Builder maps are shared
        # infrastructure, not an Electroglas-only concept.
        tk.Label(bar, text="Wafer Map:", bg="#e2e8f0",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(4, 2), pady=4)
        self._wafer_map_var = tk.StringVar(value="")
        self._wafer_map_cb = ttk.Combobox(
            bar, textvariable=self._wafer_map_var, state="readonly", width=16,
            postcommand=lambda: self._wafer_map_cb.config(
                values=self._get_wafer_map_names()))
        self._wafer_map_cb.pack(side="left", padx=(0, 8), pady=4)
        self._wafer_map_cb.bind("<<ComboboxSelected>>",
                                lambda _e: self._on_wafer_map_pick())

        self._file_lbl = tk.Label(bar, text="No probe card selected",
                                  bg="#e2e8f0", fg="#6b7280",
                                  font=("Segoe UI", 8), anchor="w")
        self._file_lbl.pack(side="left", padx=8)

        # Not shown - cluttered the bar down to just the recipe count and
        # probe card picker. Both widgets kept alive (unpacked) since other
        # code still calls .config() on them.
        self._default_lbl = tk.Label(bar, text="", bg="#e2e8f0", fg="#374151",
                                     font=("Segoe UI", 8, "italic"))
        self._bench_note_lbl = tk.Label(bar, text=self._bench_instrument_note(),
                                        bg="#e2e8f0", fg="#1d4ed8",
                                        font=("Segoe UI", 8))

    def _build_shot_origin_controls(self, parent):
        """Accretech gets its shot origin from Wafer Builder's own Overlay
        sub-tab (its confirmed row/col offset IS the translation between
        Wafer Builder's logical die grid and real absolute die coordinates
        - nothing to capture or refresh here, the status label just shows
        whatever Overlay's own Confirm button already set, live). Electroglas
        has no Overlay yet, so it still needs the manual capture button -
        self._shot_origin_btn stays None on Accretech, since there is no
        action for it to take; every other reference to it is guarded
        accordingly.

        Was its own bar above the step list - moved down here, next to
        Validate, so every button on this tab lives in one row instead of
        split across two."""
        if self._system == "accretech":
            self._shot_origin_btn = None
        else:
            self._shot_origin_btn = ttk.Button(
                parent, text="📍 Set Shot Origin", state="disabled",
                command=self._set_shot_origin)
            self._shot_origin_btn.pack(side="left", padx=(10, 2))

        # ttk.Label (not tk.Label) - this now sits in the ttk.Frame button
        # bar, not the old bar's own tk.Frame(bg="#e2e8f0"); a plain
        # tk.Label's hardcoded bg would fight the themed frame behind it.
        ttk.Label(parent, textvariable=self._shot_origin_status_var,
                 foreground="#6b7280", font=("Segoe UI", 8, "italic")).pack(
                 side="left", padx=(2, 8))

    def _on_minor_moves_toggle(self):
        rec = self._recipes.get(self._current)
        if rec is not None:
            rec["minor_moves"] = self._minor_moves_var.get()
            card = self._get_active_card()
            if card:
                self._save_recipes(card, self._recipes)
        if self._shot_origin_btn is not None:
            self._shot_origin_btn.config(
                state="normal" if self._minor_moves_var.get() else "disabled")
        self._refresh_shot_origin_label()

    def _on_shortcut_toggle(self):
        rec = self._recipes.get(self._current)
        if rec is not None:
            rec["shortcut"] = self._shortcut_var.get()
            card = self._get_active_card()
            if card:
                self._save_recipes(card, self._recipes)

    def is_shortcut(self) -> bool:
        """Whether the CURRENTLY LOADED recipe has Shortcut checked - read
        by instrument_panel._exec_should_configure's own caller to decide
        whether a repeat-config send can be skipped at all. False (the
        safe default) for any recipe that has never had this box checked."""
        return self._shortcut_var.get()

    def _on_manual_mode_toggle(self):
        rec = self._recipes.get(self._current)
        if rec is not None:
            rec["manual_mode"] = self._manual_mode_var.get()
            card = self._get_active_card()
            if card:
                self._save_recipes(card, self._recipes)

    def is_manual_mode(self) -> bool:
        """Whether the CURRENTLY LOADED recipe has Manual mode checked - read
        by instrument_panel's resistance-step handler to decide whether the
        Keithley 2400's measure_resistance() reuses the last-forced current
        (MANUAL) instead of the instrument's own auto-ranged current source
        (AUTO). False (the safe default) for any recipe that has never had
        this box checked - see instruments.keithley2400.measure_resistance's
        own comment for why AUTO, not MANUAL, is the correct default."""
        return self._manual_mode_var.get()

    def _on_fast_current_settle_toggle(self):
        rec = self._recipes.get(self._current)
        if rec is not None:
            rec["fast_current_settle"] = self._fast_current_settle_var.get()
            card = self._get_active_card()
            if card:
                self._save_recipes(card, self._recipes)

    def is_fast_current_settle(self) -> bool:
        """Whether the CURRENTLY LOADED recipe has Skip auto-clear on Force
        Current checked - read by instrument_panel's Force Current ("current"
        + apply) step handler to decide whether to turn the Keithley 2400's
        sour:clear:auto off before its own readback. False (the safe,
        unchanged-behavior default) for any recipe that has never had this
        box checked."""
        return self._fast_current_settle_var.get()

    def _refresh_shot_origin_label(self):
        if not self._minor_moves_var.get():
            self._shot_origin_status_var.set("")
            return
        if self._system == "accretech":
            ui = self.controller._by_system.get("accretech", {}).get("ui")
            confirmed = bool(getattr(ui, "_exec_overlay_offset_confirmed", False))
            if confirmed:
                ro = getattr(ui, "_exec_overlay_row_offset", 0)
                co = getattr(ui, "_exec_overlay_col_offset", 0)
                self._shot_origin_status_var.set(
                    f"using Overlay alignment (row {ro:+d}, col {co:+d})")
            else:
                self._shot_origin_status_var.set(
                    "no confirmed Overlay alignment — go to Wafer Builder > "
                    "Overlay first")
            return
        rec = self._recipes.get(self._current) or {}
        origin = rec.get("shot_origin")
        if origin:
            self._shot_origin_status_var.set(
                f"origin set: die X={origin[0]:.0f} Y={origin[1]:.0f}")
        else:
            self._shot_origin_status_var.set(
                "origin not set — 📍 Set Shot Origin before running")

    def _set_shot_origin(self):
        """Capture the chuck's CURRENT die coordinate as this recipe's
        minor-moves origin - the operator must have it sitting on shot
        (row 0, col 0)'s die #1 first (the Wafer Builder Shot tab's own
        die-1, NOT necessarily grid cell (0,0) - present_slots()'s "order"
        can put die #1 anywhere in the shot), same manual alignment step
        every other run mode already requires before starting. Mirrors
        eg_pma_run_panel's _set_anchor in spirit, without the quad/
        align-site disambiguation that has no equivalent here."""
        if self._current not in self._recipes:
            return
        drv = self.controller.drivers.get("prober")
        if not (drv and drv.inst):
            messagebox.showerror("Not Connected", "The prober is not connected.")
            return
        try:
            x, y = drv.get_die_position()
        except Exception as exc:
            messagebox.showerror(
                "Set Shot Origin", f"Could not read the current die position: {exc}")
            return
        if not messagebox.askyesno(
                "Set Shot Origin",
                f"Set this recipe's shot origin to the chuck's CURRENT "
                f"position (die X={x:.0f} Y={y:.0f})?\n\n"
                "The chuck must be sitting on shot (row 0, col 0)'s die #1 "
                "(per the Wafer Builder Shot tab's own numbering) right "
                "now - every minor move a run makes is computed relative "
                "to this point."):
            return
        self._recipes[self._current]["shot_origin"] = [x, y]
        self._refresh_shot_origin_label()
        self.controller.log(
            f"[RECIPE] '{self._current}': shot origin set to die "
            f"X={x:.0f} Y={y:.0f}")

    def _build_body(self):
        # A PanedWindow (drag sash) rather than fixed grid-row weights, so the
        # Steps/Touchdowns split is something the user can resize by hand -
        # not just something that happens to grow proportionally when the
        # window does.
        body = ttk.PanedWindow(self, orient="vertical")
        body.grid(row=2, column=0, sticky="nsew", padx=6, pady=4)
        self._build_steps(body)
        self._build_sites(body)


    # -- touchdown list -----------------------------------------------------
    #
    # Which dies a recipe probes used to live outside the recipe: Accretech
    # kept one ata_wafer_map_selected.csv per ATA FOLDER, so every recipe in
    # that folder shared a single selection and switching recipe silently kept
    # the previous one's sites. Here the list belongs to the recipe, travels
    # with the probe card, and carries the die ID beside the row/col so a
    # saved list can be checked against the map it was taken from.

    def _build_sites(self, parent):
        sf = ttk.LabelFrame(parent, text="Touchdown List", padding=6)
        parent.add(sf, weight=2)
        sf.rowconfigure(1, weight=1)
        sf.columnconfigure(0, weight=1)

        # Still tracked (see _refresh_sites) even with no label showing it -
        # only the grey description text was removed, not the underlying
        # touchdown-count bookkeeping.
        self._sites_var = tk.StringVar(value="No touchdowns — the run walks every die")

        bar = ttk.Frame(sf)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(4, 4))
        ttk.Button(bar, text="⬅ Take from map selection",
                   command=self._sites_from_map).pack(side="left")
        ttk.Button(bar, text="Take die IDs",
                   command=self._sites_from_die_ids).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="➡ Push to map",
                   command=self._sites_to_map).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="✕ Remove selected",
                   command=self._site_remove).pack(side="left", padx=(16, 0))
        ttk.Button(bar, text="Clear all",
                   command=self._sites_clear).pack(side="left", padx=(6, 0))

        # Basic map-only builders: fill the table below and nothing else -
        # no map highlighting, no auto-save. ➡ Push to map / 💾 Save (both
        # already exist) are the separate, explicit next steps, same as a
        # hand-picked list from ⬅ Take from map selection.
        ttk.Button(bar, text="Pull shots",
                   command=self._sites_pull_shots).pack(side="left", padx=(16, 0))
        ttk.Button(bar, text="🔎 Find all",
                   command=self._sites_find_all).pack(side="left", padx=(6, 0))
        self._find_all_var = tk.StringVar(value="")
        ttk.Entry(bar, textvariable=self._find_all_var, width=14).pack(
            side="left", padx=(4, 0))

        # Align die. Saved with the recipe and otherwise inert: it does not
        # anchor anything, does not move the chuck and takes no part in the
        # run. All it does is preselect the Run tab's "Chuck is on"
        # dropdown when this recipe loads and the chuck is not already set,
        # so the operator does not scroll thousands of entries to reach the
        # die they always start from. See _on_align_die_pick and
        # instrument_panel._exec_preselect_align_die.
        #
        # Electroglas only, because the box it preselects is: Accretech has
        # no "Chuck is on" dropdown to point at, so the control would be
        # inert there. The var is still created either way, so the save/
        # load path does not have to test for it.
        self._align_die_var = tk.StringVar(value="")
        self._align_die_cb = None
        if self._system == "electroglas":
            ttk.Label(bar, text="Align die:").pack(side="left", padx=(16, 0))
            self._align_die_cb = ttk.Combobox(
                bar, textvariable=self._align_die_var, width=22)
            self._align_die_cb.pack(side="left", padx=(4, 0))
            # Editable and filtered as you type, for the same reason the
            # Run tab's own anchor box is (EgPmaRunPanel._build_controls):
            # a whole-wafer map has thousands of dies and scrolling to one
            # is hopeless.
            self._align_die_cb.bind("<KeyRelease>", self._on_align_die_typed)
            self._align_die_cb.bind("<<ComboboxSelected>>", self._on_align_die_pick)
            self._align_die_cb.bind("<FocusOut>", self._on_align_die_pick)
            self._align_die_cb.bind("<Button-1>", self._fill_align_die_choices,
                                    add="+")

        cols = ("n", "die_id", "row", "col")
        self._site_tree = ttk.Treeview(sf, columns=cols, show="headings", height=6)
        for cid, text, width, anchor in (("n", "#", 40, "center"),
                                         ("die_id", "Die ID", 260, "w"),
                                         ("row", "Row", 60, "center"),
                                         ("col", "Col", 60, "center")):
            self._site_tree.heading(cid, text=text)
            self._site_tree.column(cid, width=width, anchor=anchor,
                                   stretch=(cid == "die_id"))
        self._site_tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(sf, orient="vertical", command=self._site_tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self._site_tree.configure(yscrollcommand=sb.set)

    def _run_panel(self):
        """The Run tab that owns the wafer map, or None if not built yet."""
        return getattr(self.controller, "ui", None)

    def _refresh_sites(self):
        self._site_tree.delete(*self._site_tree.get_children())
        for i, s in enumerate(self._sites, 1):
            self._site_tree.insert("", "end", values=(
                i, s.get("die_id", "") or "—", s.get("row", ""), s.get("col", "")))
        n = len(self._sites)
        if not n:
            self._sites_var.set(
                "No touchdowns — the run walks every die on the wafer map. "
                "Select dies on the Run tab's map, then ⬅ Take from map selection.")
        else:
            named = sum(1 for s in self._sites if s.get("die_id"))
            self._sites_var.set(
                f"{n} touchdown{'' if n == 1 else 's'} — the run probes only these, "
                f"in this order. {named} carry a die ID.")

    def _sites_from_map(self):
        ui = self._run_panel()
        wm = getattr(ui, "_exec_wafer_map", None)
        if wm is None:
            messagebox.showinfo("Touchdowns", "The Run tab's wafer map is not available.")
            return
        picks = list(wm.get_picked())
        if not picks:
            messagebox.showinfo(
                "Touchdowns",
                "No dies are selected on the Run tab's map.")
            return
        # On Electroglas a square is a die and a shot owns several of them -
        # the chuck lands once per shot, so four picked dies of one shot
        # must collapse to ONE touchdown, not four. Accretech: a no-op
        # (a square already is a touchdown there) - see
        # instrument_panel._exec_picks_as_touchdowns's own docstring.
        collapse = getattr(ui, "_exec_picks_as_touchdowns", None)
        if collapse:
            picks = collapse(picks)
        overlay = getattr(ui, "_exec_overlay_die_ids", None) or {}
        sites = []
        for rc in picks:
            rc = (int(rc[0]), int(rc[1]))
            die_id = overlay.get(rc) or wm.die_ids.get(rc, "")
            sites.append({"die_id": die_id, "row": rc[0], "col": rc[1]})
        self._sites[:] = sites
        self._store_form()
        self._refresh_sites()
        # Save immediately, not deferred until some later 💾 Save - this is
        # the only place a map selection becomes a touchdown list now (the
        # Run tab's own Save Selected Map was removed as a duplicate of
        # this button), so it must not silently be lost on a restart.
        card = self._get_active_card()
        saved = bool(card) and bool(self._save_recipes(card, self._recipes))
        self.controller.log(
            f"[RECIPE] '{self._current}': touchdown list set to {len(sites)} "
            f"die(s) from the map selection"
            + (f" and saved to probe card '{card}'." if saved
               else " — NOT saved (no probe card); press 💾 Save."))

    def _sites_from_die_ids(self):
        ui = self._run_panel()
        wm = getattr(ui, "_exec_wafer_map", None)
        if wm is None:
            messagebox.showinfo("Touchdowns", "The Run tab's wafer map is not available.")
            return
        overlay = getattr(ui, "_exec_overlay_die_ids", None) or {}
        ided = {}
        for rc in wm.dies:
            die_id = overlay.get(rc) or wm.die_ids.get(rc, "")
            if die_id:
                ided[rc] = die_id
        if not ided:
            messagebox.showinfo(
                "Touchdowns",
                "No dies on the loaded map carry a die ID.\n\n"
                "Overlay the map (Run tab) or load a map that has real IDs.")
            return
        picks = sorted(ided.keys())
        wm.set_picked(picks)
        sites = [{"die_id": ided[rc], "row": rc[0], "col": rc[1]} for rc in picks]
        self._sites[:] = sites
        self._store_form()
        self._refresh_sites()
        card = self._get_active_card()
        saved = bool(card) and bool(self._save_recipes(card, self._recipes))
        self.controller.log(
            f"[RECIPE] '{self._current}': touchdown list set to {len(sites)} "
            f"die(s) with an ID from the map"
            + (f" and saved to probe card '{card}'." if saved
               else " — NOT saved (no probe card); press 💾 Save."))

    # -- align die (a Run tab convenience, saved with the recipe) ----------

    def _align_die_choices(self) -> list:
        """The same entries the Run tab's "Chuck is on" box offers.

        Taken from that box's own list wherever it exists, so the two can
        never drift apart or format an entry differently - the whole point
        is that what is picked here is what gets preselected there. Falls
        back to the published map's die IDs when the Run tab has not built
        its list yet (a recipe can be edited before a map is loaded).
        """
        run = getattr(self._run_panel(), "eg_pma_run", None)
        choices = list(getattr(run, "_anchor_choices", None) or [])
        if choices:
            return choices
        wm = getattr(self._run_panel(), "_exec_wafer_map", None)
        dies = getattr(wm, "_last_dies", None) or []
        return [d["die_id"] for d in dies if (d.get("die_id") or "").strip()]

    def _fill_align_die_choices(self, _event=None):
        cb = getattr(self, "_align_die_cb", None)
        if cb is not None:
            cb.config(values=self._align_die_choices())

    def _on_align_die_typed(self, _event=None):
        """Filter the list to what has been typed, same as the Run tab's
        anchor box - then persist, so typing a die ID straight in counts
        as picking it."""
        text = (self._align_die_var.get() or "").strip().lower()
        allc = self._align_die_choices()
        shown = [c for c in allc if text in c.lower()] if text else allc
        cb = getattr(self, "_align_die_cb", None)
        if cb is not None:
            cb.config(values=shown[:400])
        self._on_align_die_pick()

    def _on_align_die_pick(self, _event=None):
        rec = self._recipes.get(self._current)
        if rec is None:
            return
        value = (self._align_die_var.get() or "").strip()
        if value == (rec.get("align_die") or ""):
            return
        rec["align_die"] = value
        card = self._get_active_card()
        if card:
            self._save_recipes(card, self._recipes)

    def get_align_die(self) -> str:
        """The loaded recipe's align die, or "" - read by
        instrument_panel._exec_preselect_align_die."""
        rec = self._recipes.get(self._current) or {}
        return (rec.get("align_die") or "").strip()

    def _on_wafer_map_pick(self, _event=None):
        rec = self._recipes.get(self._current)
        if rec is None:
            return
        value = (self._wafer_map_var.get() or "").strip()
        if value == (rec.get("wafer_map") or ""):
            return
        rec["wafer_map"] = value
        card = self._get_active_card()
        if card:
            self._save_recipes(card, self._recipes)

    def get_wafer_map(self) -> str:
        """The loaded recipe's saved wafer map name, or "" - read by
        instrument_panel._exec_autoload_recipe_wafer_map."""
        rec = self._recipes.get(self._current) or {}
        return (rec.get("wafer_map") or "").strip()

    def _sites_pull_shots_eg(self, ui, wm):
        """Pull Shots, Electroglas: die #1 of every shot on the published map.

        Same intent as the Accretech path below - one pick per shot, at the
        shot's first die - but it needs none of that path's machinery. The
        Accretech version derives which shot a square is in by
        floor-dividing (row, col) by the Shot tab's live dims and a
        confirmed Overlay offset. On Electroglas the published Wafer
        Builder map states it outright: every die row carries the seq of
        its shot and the slot it occupies. So there is no Overlay to
        confirm, no Tk entry box to have open, and no arithmetic that can
        drift out of alignment with the map.

        Die #1 is the shot's first canonical slot (slot_names()[0] - the
        top-left for every layout), and only falls past it when the shot
        genuinely has no cell there.

        It does NOT look at the label. An earlier version skipped a slot
        whose die ID was "NA" or blank, which made the picks jump around
        the shot for no reason an operator could see - "NA" is simply what
        LaMP calls some of its dies, and on a map with no IDs at all
        (Cenfire: 12505 of 12915 dies unlabelled) every slot would have
        been skipped. A die is a cell the map marks enabled; what it is
        called has nothing to do with it.

        Selection ONLY. This fills the touchdown table and nothing else -
        it never writes to the wafer map, which is the source of truth.
        """
        run = getattr(ui, "eg_pma_run", None)
        slots_fn = getattr(run, "_builder_shot_slots", None)
        if slots_fn is None:
            messagebox.showinfo("Touchdowns", "The Electroglas Run tab is "
                                              "not available.")
            return
        dies = getattr(wm, "_last_dies", None) or []
        if not dies:
            messagebox.showinfo("Touchdowns", "No wafer map is published yet "
                                              "- build and save one on the "
                                              "Wafer Builder tab first.")
            return
        by_shot, _rc_to_shot = slots_fn()
        if not by_shot:
            messagebox.showinfo("Touchdowns", "The published map does not say "
                                              "which shot each die belongs to.")
            return
        die_id_by_rc = {(d["row"], d["col"]): (d.get("die_id") or "").strip()
                        for d in dies
                        if d.get("row") is not None and d.get("col") is not None}
        from electroglas_pma import slot_names as _slot_names
        order = _slot_names(*run.shot_layout())
        picks = []
        for _seq, slots in sorted(by_shot.items()):
            for q in order:
                rc = slots.get(q)
                if rc is None:
                    continue
                # Whatever the map calls it, including "NA" and nothing at
                # all - see the docstring. An unlabelled die is named by
                # its position, the same fallback the Run tab's Die list
                # uses (eg_pma_run_panel.adopt_from_wafer_builder).
                die_id = die_id_by_rc.get(rc) or f"({rc[0]},{rc[1]})"
                picks.append((rc, die_id))
                break
        if not picks:
            messagebox.showinfo("Touchdowns", "No shot on the published map "
                                              "has any dies in it.")
            return
        picks.sort(key=lambda p: p[0])
        self._sites[:] = [{"die_id": die_id, "row": rc[0], "col": rc[1]}
                          for rc, die_id in picks]
        self._store_form()
        self._refresh_sites()

    def _sites_pull_shots(self):
        """Basic touchdown-list builder: one pick per shot on the loaded
        map - the die Wafer Builder's Shot tab numbers #1 in each. Only
        fills the table below (➡ Push to map / 💾 Save stay separate,
        explicit steps) - matches the "any die within the shot" touchdown
        style (see instrument_panel._exec_prepare_shot_geometry), just
        picking THE specific one that is #1."""
        ui = self._run_panel()
        wm = getattr(ui, "_exec_wafer_map", None)
        if wm is None:
            messagebox.showinfo("Touchdowns", "The Run tab's wafer map is not available.")
            return
        if self._system == "electroglas":
            self._sites_pull_shots_eg(ui, wm)
            return
        gen = getattr(ui, "recipe_gen", None)
        if gen is None:
            messagebox.showinfo("Touchdowns", "The Wafer Builder tab is not available.")
            return
        shot_rows, shot_cols = gen._shot_dims()
        if shot_rows * shot_cols <= 1:
            messagebox.showinfo(
                "Touchdowns",
                "The Wafer Builder Shot template is a single die.")
            return
        if not getattr(ui, "_exec_overlay_offset_confirmed", False):
            messagebox.showinfo(
                "Touchdowns",
                "No confirmed Overlay alignment - go to Wafer Builder > Overlay "
                "and press 🖌 Overlay on Map first, so a real (row, col) on the "
                "map can be resolved into a shot.")
            return
        from recipe_gen_panel import shot_die_rc as _shot_die_rc  # deferred: avoids
        # a module-load-time circular import (recipe_gen_panel -> wafer_map_view
        # -> recipe_panel), same reason other cross-panel imports in this file
        # are done lazily inside the function that needs them.
        die1_rc = _shot_die_rc(dict(gen._shot_cells), shot_rows, shot_cols, 1)
        if die1_rc is None:
            messagebox.showinfo("Touchdowns",
                                "The Wafer Builder Shot template has no die #1 defined.")
            return
        slot_r1, slot_c1 = die1_rc
        row_offset = ui._exec_overlay_row_offset
        col_offset = ui._exec_overlay_col_offset
        overlay = getattr(ui, "_exec_overlay_die_ids", None) or {}
        picks = []
        for row, col in wm.dies:
            wb_row, wb_col = row - row_offset, col - col_offset
            shot_row, shot_col = wb_row // shot_rows, wb_col // shot_cols
            slot_row = wb_row - shot_row * shot_rows
            slot_col = wb_col - shot_col * shot_cols
            if (slot_row, slot_col) != (slot_r1, slot_c1):
                continue
            # wm.dies is every square on the Accretech map, which is NOT the
            # same grid as the Wafer Builder shot map it's being aligned
            # against here - the two only line up inside the real wafer, so
            # a square near the edge can satisfy the slot-position check
            # above by pure modular arithmetic while corresponding to no
            # real Wafer Builder shot at all. Its die ID is exactly what
            # tells the two apart (a real shot always has one; a phantom
            # edge square never does), so requiring one keeps this pull
            # scoped to actual shots. Electroglas's map has no such
            # mismatch (there is no separate Accretech-style grid), so this
            # only ever filters anything out here.
            if not (overlay.get((row, col)) or wm.die_ids.get((row, col), "")):
                continue
            picks.append((row, col))
        if not picks:
            messagebox.showinfo(
                "Touchdowns",
                "No die #1 positions found on the loaded map - check the "
                "Overlay alignment and Shot template.")
            return
        picks.sort()
        sites = [{"die_id": overlay.get(rc) or wm.die_ids.get(rc, ""),
                 "row": rc[0], "col": rc[1]} for rc in picks]
        self._sites[:] = sites
        self._store_form()
        self._refresh_sites()
        self.controller.log(
            f"[RECIPE] '{self._current}': touchdown list set to {len(sites)} "
            "shot(s) - die #1 of each - from the loaded map. Not saved yet - "
            "press 💾 Save when ready.")

    def _sites_find_all(self):
        """Basic touchdown-list builder: every die on the loaded map whose
        ID exactly matches the typed text. Only fills the table below,
        same as 🎯 Pull shots - no map highlighting, no auto-save."""
        ui = self._run_panel()
        wm = getattr(ui, "_exec_wafer_map", None)
        if wm is None:
            messagebox.showinfo("Touchdowns", "The Run tab's wafer map is not available.")
            return
        target = (self._find_all_var.get() or "").strip()
        if not target:
            messagebox.showinfo("Touchdowns", "Type a die ID to search for first.")
            return
        overlay = getattr(ui, "_exec_overlay_die_ids", None) or {}
        picks = []
        for rc in wm.dies:
            die_id = overlay.get(rc) or wm.die_ids.get(rc, "")
            if die_id == target:
                picks.append(rc)
        if not picks:
            messagebox.showinfo("Touchdowns",
                                f"No dies on the loaded map are labeled '{target}'.")
            return
        picks.sort()
        sites = [{"die_id": target, "row": rc[0], "col": rc[1]} for rc in picks]
        self._sites[:] = sites
        self._store_form()
        self._refresh_sites()
        self.controller.log(
            f"[RECIPE] '{self._current}': touchdown list set to {len(sites)} "
            f"die(s) labeled '{target}' from the loaded map. Not saved yet - "
            "press 💾 Save when ready.")

    def _sites_to_map(self):
        ui = self._run_panel()
        wm = getattr(ui, "_exec_wafer_map", None)
        if wm is None:
            messagebox.showinfo("Touchdowns", "The Run tab's wafer map is not available.")
            return
        if not self._sites:
            messagebox.showinfo("Touchdowns", "This recipe has no touchdowns yet.")
            return
        # Electroglas: resolve each site's die_id against the loaded map
        # (ground truth) rather than trusting the recipe's own (row, col) -
        # same reasoning/helper as the Run tab's own recipe-load path (see
        # instrument_panel._exec_resolve_site_cells). Accretech falls
        # straight through to the site's own row/col, unchanged.
        resolve = getattr(ui, "_exec_resolve_site_cells", None)
        picks = resolve(self._sites) if resolve else [
            (s["row"], s["col"]) for s in self._sites]
        missing = [rc for rc in picks if rc not in wm.dies]
        wm.set_picked(picks)
        if hasattr(ui, "_exec_on_sites_changed"):
            ui._exec_on_sites_changed(picks)
        note = (f" ({len(missing)} not on the loaded map — wrong wafer map for "
                "this recipe?)" if missing else "")
        self.controller.log(f"[RECIPE] '{self._current}': highlighted "
                            f"{len(picks)} touchdown(s) on the Run map.{note}")

    def _site_remove(self):
        sel = self._site_tree.selection()
        if not sel:
            return
        for idx in sorted((self._site_tree.index(i) for i in sel), reverse=True):
            if 0 <= idx < len(self._sites):
                del self._sites[idx]
        self._store_form()
        self._refresh_sites()

    def _sites_clear(self):
        if not self._sites:
            return
        if not messagebox.askokcancel(
                "Clear touchdowns",
                f"Remove all {len(self._sites)} touchdown(s) from "
                f"'{self._current}'?\n\nThe run will then touch every die."):
            return
        self._sites.clear()
        self._store_form()
        self._refresh_sites()

    def set_sites(self, recipe: str, sites: list) -> bool:
        """Replace a recipe's touchdown list. Used by the PMA tab's LOAD ALL.

        Saves to the probe card straight away: the list arrives as part of a
        chain the operator did not step through, so leaving it unsaved would
        mean a restart silently drops it.
        """
        rec = self._recipes.get(recipe)
        if rec is None:
            return False
        clean = []
        for s in sites:
            try:
                clean.append({"die_id": str(s.get("die_id", "") or ""),
                              "row": int(s["row"]), "col": int(s["col"])})
            except (KeyError, TypeError, ValueError):
                continue
        rec["sites"] = clean
        if recipe == self._current:
            self._sites = rec["sites"]
            self._refresh_sites()
        card = self._get_active_card()
        if card:
            self._save_recipes(card, self._recipes)
        return True

    def get_sites(self) -> list:
        """The active recipe's touchdown list, as [(row, col), ...]."""
        return [(s["row"], s["col"]) for s in self._sites]

    def get_site_records(self) -> list:
        return list(self._sites)

    def _build_steps(self, parent):
        sf = ttk.LabelFrame(parent, text="Measurement Steps (per shot)",
                            padding=6)
        parent.add(sf, weight=3)
        sf.rowconfigure(0, weight=1)
        sf.columnconfigure(0, weight=1)

        cols = ("n", "name", "type", "instrument", "mode", "chan", "target", "die",
                "route", "hi", "lo", "his", "los",
                "level", "limit", "avg", "min", "max", "shape", "freq", "conn")
        self._step_tree = ttk.Treeview(sf, columns=cols, show="headings",
                                       height=5, selectmode="browse")
        heads = [("n", "#", 28), ("name", "Name", 90), ("type", "Type", 75),
                 ("instrument", "Instr", 50), ("mode", "Mode", 55), ("chan", "Chan", 40),
                 ("target", "Target", 62), ("die", "Die #", 40),
                 ("route", "Route", 52),
                 ("hi", "HI pin", 55), ("lo", "LO pin", 55),
                 ("his", "SnsHI", 52), ("los", "SnsLO", 52),
                 ("level", "Level", 52), ("limit", "Limit", 50),
                 ("avg", "Avg", 68),
                 ("min", "Min", 46), ("max", "Max", 46),
                 ("shape", "Shape", 48), ("freq", "Freq(Hz)", 58),
                 ("conn", "Switch conn", 110)]
        for cid, text, width in heads:
            self._step_tree.heading(cid, text=text)
            self._step_tree.column(
                cid, width=width,
                anchor="center" if cid in ("n", "type", "instrument", "mode", "chan",
                                           "shape") else "w")
        # All 20 fields stay in `columns`/get inserted (and saved) in full -
        # `displaycolumns` just narrows what's shown so the table reads at a
        # glance. Selecting a row still pulls every field into the editor
        # below from self._steps, not from what's on screen.
        self._step_tree["displaycolumns"] = (
            "n", "name", "type", "die", "hi", "lo", "level")
        self._step_tree.grid(row=0, column=0, sticky="nsew")
        ssb = ttk.Scrollbar(sf, orient="vertical", command=self._step_tree.yview)
        ssb.grid(row=0, column=1, sticky="ns")
        self._step_tree.configure(yscrollcommand=ssb.set)
        self._step_tree.bind("<<TreeviewSelect>>", lambda _e: self._step_to_editor())

        # A compact label:widget grid (2 fields per row-slot) instead of one
        # long pack()ed row per group - keeps the editor readable without
        # needing to widen the window, now that the per-field hint labels
        # (which used to force the old rows wide) are gone.
        editor = ttk.Frame(sf)
        editor.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for c in range(8):
            editor.columnconfigure(c, weight=0)

        self._ed_vars = {k: tk.StringVar() for k in _STEP_FIELDS}
        self._ed_vars["type"].set("resistance")
        self._ed_vars["mode"].set("measure")
        self._ed_vars["die"].set("1")
        self._ed_vars["route"].set(ROUTE_SWITCH)
        # A checkbox is the control; the step field stays the "switch"/
        # "direct" string, so a recipe CSV reads as words rather than 0/1.
        self._direct_var = tk.BooleanVar(value=False)

        def _lbl(r, c, text):
            ttk.Label(editor, text=text).grid(row=r, column=c, sticky="e", padx=(6, 2), pady=2)

        _lbl(0, 0, "Name:")
        ttk.Entry(editor, textvariable=self._ed_vars["name"], width=12).grid(
            row=0, column=1, sticky="w")
        _lbl(0, 2, "Type:")
        self._type_cb = ttk.Combobox(editor, textvariable=self._ed_vars["type"],
                                     values=self._step_type_choices, state="readonly", width=10,
                                     postcommand=self._refresh_type_values)
        self._type_cb.grid(row=0, column=3, sticky="w")
        self._type_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_type_change())
        _lbl(0, 4, "Mode:")
        self._mode_cb = ttk.Combobox(editor, textvariable=self._ed_vars["mode"],
                                     values=_STEP_MODES, state="readonly", width=8)
        self._mode_cb.grid(row=0, column=5, sticky="w")
        self._mode_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_type_change())
        _lbl(0, 6, "Instr:")
        # Displays "SMU (Keithley2636B)" etc, and - only when more than one
        # instrument of a family is actually fitted at once - one entry per
        # slot so a step can target a specific one. Purely a display layer:
        # the combobox is NOT bound to _ed_vars["instrument"] directly, that
        # stays the real stored family string exactly as before (every
        # other reader of _ed_vars["instrument"]/step["instrument"] is
        # unaffected) - see _refresh_instrument_dropdown/
        # _on_instrument_display_selected.
        self._instr_label_map: dict = {}
        self._instr_display_var = tk.StringVar()
        self._instr_cb = ttk.Combobox(editor, textvariable=self._instr_display_var,
                                      values=(), state="readonly", width=26)
        self._instr_cb.grid(row=0, column=7, sticky="w")
        self._instr_cb.bind(
            "<<ComboboxSelected>>", lambda _e: self._on_instrument_display_selected())

        _lbl(1, 0, "Chan:")
        self._chan_cb = ttk.Combobox(editor, textvariable=self._ed_vars["chan"],
                                     values=self._smu_channel_choices, state="readonly", width=5)
        self._chan_cb.grid(row=1, column=1, sticky="w")
        _lbl(1, 2, "Target:")
        self._target_cb = ttk.Combobox(editor, textvariable=self._ed_vars["target"],
                                       values=("all",), width=13,
                                       postcommand=self._refresh_target_values)
        self._target_cb.grid(row=1, column=3, sticky="w")
        self._target_cb.bind("<<ComboboxSelected>>", lambda _e: self._update_target_calc_hint())
        self._target_cb.bind("<KeyRelease>", lambda _e: self._update_target_calc_hint())
        # Grey, computed-on-the-fly explanation of what a measure step's
        # Target will combine into (e.g. force current elsewhere + measure
        # voltage here -> resistance) - see describe_target_calc. The Label
        # itself is built down by term_row, next to Terminals - only the
        # StringVar is declared here, next to Target, since every other
        # reader of _target_calc_var is a .set() call, not layout.
        self._target_calc_var = tk.StringVar(value="")
        _lbl(1, 4, "HI:")
        # readonly - a step can only pick a pin actually on the active probe
        # card, which itself can now only carry pins the active bench really
        # has wired (see ProbeCardWiringFrame._valid_pins). A free-typed pin
        # here would look plausible but close nothing on real hardware.
        self._hi_cb = ttk.Combobox(editor, textvariable=self._ed_vars["hi"], width=8,
                                   state="readonly",
                                   postcommand=lambda: self._refresh_pin_values(self._hi_cb))
        self._hi_cb.grid(row=1, column=5, sticky="w")
        _lbl(1, 6, "LO:")
        self._lo_cb = ttk.Combobox(editor, textvariable=self._ed_vars["lo"], width=8,
                                   state="readonly",
                                   postcommand=lambda: self._refresh_pin_values(self._lo_cb))
        self._lo_cb.grid(row=1, column=7, sticky="w")
        self._pin_widgets = [self._hi_cb, self._lo_cb]

        # The 4-wire sense pair. Present on every step for a stable layout but
        # only ever enabled for "ohmf" - see _on_type_change.
        _lbl(5, 0, "Sense HI:")
        self._his_cb = ttk.Combobox(editor, textvariable=self._ed_vars["his"], width=8,
                                    state="readonly",
                                    postcommand=lambda: self._refresh_pin_values(self._his_cb))
        self._his_cb.grid(row=5, column=1, sticky="w")
        _lbl(5, 2, "Sense LO:")
        self._los_cb = ttk.Combobox(editor, textvariable=self._ed_vars["los"], width=8,
                                    state="readonly",
                                    postcommand=lambda: self._refresh_pin_values(self._los_cb))
        self._los_cb.grid(row=5, column=3, sticky="w")
        self._sense_widgets = [self._his_cb, self._los_cb]

        _lbl(2, 0, "Level:")
        self._level_ent = ttk.Entry(editor, textvariable=self._ed_vars["level"], width=9)
        self._level_ent.grid(row=2, column=1, sticky="w")
        _lbl(2, 2, "Limit:")
        self._limit_ent = ttk.Entry(editor, textvariable=self._ed_vars["limit"], width=9)
        self._limit_ent.grid(row=2, column=3, sticky="w")
        _lbl(2, 4, "Min:")
        self._pf_min_ent = ttk.Entry(editor, textvariable=self._ed_vars["min"], width=9)
        self._pf_min_ent.grid(row=2, column=5, sticky="w")
        _lbl(2, 6, "Max:")
        self._pf_max_ent = ttk.Entry(editor, textvariable=self._ed_vars["max"], width=9)
        self._pf_max_ent.grid(row=2, column=7, sticky="w")

        _lbl(3, 0, "Shape:")
        self._shape_cb = ttk.Combobox(editor, textvariable=self._ed_vars["shape"],
                                      values=_WAVE_SHAPES, state="readonly", width=6)
        self._shape_cb.grid(row=3, column=1, sticky="w")
        _lbl(3, 2, "Freq (Hz):")
        self._freq_ent = ttk.Entry(editor, textvariable=self._ed_vars["freq"], width=9)
        self._freq_ent.grid(row=3, column=3, sticky="w")
        _lbl(3, 4, "Avg Count:")
        self._avg_count_ent = ttk.Entry(editor, textvariable=self._ed_vars["avg_count"], width=5)
        self._avg_count_ent.grid(row=3, column=5, sticky="w")
        _lbl(3, 6, "Avg Delay (ms):")
        self._avg_delay_ent = ttk.Entry(editor, textvariable=self._ed_vars["avg_delay"], width=7)
        self._avg_delay_ent.grid(row=3, column=7, sticky="w")

        _lbl(5, 4, "Settle Delay (ms):")
        self._settle_delay_ent = ttk.Entry(
            editor, textvariable=self._ed_vars["settle_delay"], width=7)
        self._settle_delay_ent.grid(row=5, column=5, sticky="w")

        # abs() the reading before target/pass-fail compares against it and
        # before it's recorded/painted - see _STEP_FIELDS' own comment.
        self._abs_chk = ttk.Checkbutton(
            editor, text="Absolute Value",
            variable=self._ed_vars["abs_value"], onvalue="1", offvalue="")
        self._abs_chk.grid(row=5, column=6, columnspan=2, sticky="w",
                           padx=(6, 2), pady=2)

        _lbl(4, 0, "NPLC:")
        self._nplc_ent = ttk.Entry(editor, textvariable=self._ed_vars["nplc"], width=6)
        self._nplc_ent.grid(row=4, column=1, sticky="w")
        _lbl(4, 2, "Conn:")
        conn_ent = ttk.Entry(editor, textvariable=self._ed_vars["conn"], width=16)
        conn_ent.grid(row=4, column=3, columnspan=3, sticky="w")
        # ⚙ button used to sit right here, unlabeled - moved down to the
        # button bar, next to ✓ Validate, as "Compute Connection".
        self._conn_widgets = [conn_ent]
        # Direct: the instrument is cabled straight to the probe card by
        # hand, so no channel is closed and the pin fields below stop
        # applying (they grey out). Ticked by default for the 3458A, which
        # is usually leaded up directly for a 4-wire check - see
        # _default_route_for.
        self._direct_chk = ttk.Checkbutton(
            editor, text="Direct wiring",
            variable=self._direct_var, command=self._on_route_toggle)
        self._direct_chk.grid(row=6, column=0, columnspan=3, sticky="w",
                              padx=(6, 2), pady=(2, 0))
        # Front/Rear terminal select - blank (the default) sends nothing,
        # same as every recipe saved before this existed. Only meaningful
        # for an instrument whose driver actually supports switching
        # (Keithley2400.set_terminals) - harmless no-op logged, not an
        # error, if the step's resolved driver doesn't have one (see
        # instrument_panel._exec_run_steps_once).
        term_row = ttk.Frame(editor)
        term_row.grid(row=7, column=0, columnspan=3, sticky="w",
                      padx=(6, 2), pady=(2, 0))
        ttk.Label(term_row, text="Terminals:").pack(side="left")
        self._terminals_cb = ttk.Combobox(
            term_row, textvariable=self._ed_vars["terminals"],
            values=("", "FRONT", "REAR"), state="readonly", width=7)
        self._terminals_cb.pack(side="left", padx=(4, 0))
        ttk.Label(term_row, textvariable=self._target_calc_var, foreground="#6b7280",
                 font=("Arial", 8, "italic"), wraplength=380, justify="left").pack(
                 side="left", padx=(12, 0))
        # Per-RECIPE toggles (not per-step, unlike everything else in this
        # editor) - moved down here next to Direct Wiring, on the same row,
        # so every checkbox on the tab lives in one place at the bottom
        # instead of split between here and a separate bar above the step
        # list. Columns run past the 8 the rest of the editor uses (fine -
        # an ungoverned grid column just takes its natural width from
        # whatever's in it, same as any other).
        self._shortcut_chk = ttk.Checkbutton(
            editor, text="Don't resend configs",
            variable=self._shortcut_var, command=self._on_shortcut_toggle)
        self._shortcut_chk.grid(row=6, column=3, columnspan=4, sticky="w",
                                padx=(6, 2), pady=(2, 0))
        self._minor_moves_chk = ttk.Checkbutton(
            editor, text="Minor moves (multi-die shot)",
            variable=self._minor_moves_var, command=self._on_minor_moves_toggle)
        self._minor_moves_chk.grid(row=6, column=7, columnspan=7, sticky="w",
                                   padx=(6, 2), pady=(2, 0))
        self._fast_current_settle_chk = ttk.Checkbutton(
            editor, text="Skip auto-clear on Force Current (Keithley 2400 only)",
            variable=self._fast_current_settle_var,
            command=self._on_fast_current_settle_toggle)
        self._fast_current_settle_chk.grid(row=8, column=3, columnspan=4, sticky="w",
                                           padx=(6, 2), pady=(2, 0))
        # Manual mode: see this class's own _manual_mode_var comment /
        # instruments.keithley2400.measure_resistance. Unchecked (AUTO) is
        # correct for almost every recipe - only check this for one that
        # forces a specific current then reads ohms off the same pins in
        # the same step (Maddy TL's Kelvin Resistance).
        self._manual_mode_chk = ttk.Checkbutton(
            editor, text="Manual mode (reuse forced current for Ω, Keithley 2400)",
            variable=self._manual_mode_var, command=self._on_manual_mode_toggle)
        self._manual_mode_chk.grid(row=8, column=7, columnspan=7, sticky="w",
                                   padx=(6, 2), pady=(2, 0))
        _lbl(4, 6, "Die #:")
        # Which die of the shot this measurement belongs to (Wafer Builder
        # Shot tab's die order, 1-based) - what the Results tab uses to
        # paint the right square. "1" covers the common single-die-per-shot
        # case with nothing to set.
        self._die_ent = ttk.Spinbox(editor, textvariable=self._ed_vars["die"],
                                    from_=1, to=64, width=4)
        self._die_ent.grid(row=4, column=7, sticky="w")

        self._on_type_change()

        btns = ttk.Frame(sf)
        btns.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self._btn_add_step = ttk.Button(btns, text="＋ Add Step", command=self._step_add)
        self._btn_add_step.pack(side="left", padx=2)
        self._btn_update_step = ttk.Button(btns, text="Update Selected",
                                           command=self._step_update)
        self._btn_update_step.pack(side="left", padx=2)
        self._btn_remove_step = ttk.Button(btns, text="Remove", command=self._step_remove)
        self._btn_remove_step.pack(side="left", padx=2)
        self._btn_move_up = ttk.Button(btns, text="▲", width=3,
                                       command=lambda: self._step_move(-1))
        self._btn_move_up.pack(side="left", padx=(10, 2))
        self._btn_move_down = ttk.Button(btns, text="▼", width=3,
                                         command=lambda: self._step_move(+1))
        self._btn_move_down.pack(side="left", padx=2)
        # Electroglas connections depend on per-bench relay wiring that
        # isn't reliable/present for every project (unlike Accretech's
        # pin-table approach - see BENCH_WIRING's own per-bench notes), and
        # HI/LO pins can't be inferred from a .PMA at all. A button that
        # looks like it computes the right answer but can't for most
        # projects is worse than no button - both stay Accretech-only.
        if self._system != "electroglas":
            self._btn_conn = ttk.Button(btns, text="Compute Connection",
                                        command=self._conn_from_editor)
            self._btn_conn.pack(side="left", padx=(10, 2))
            self._btn_recompute = ttk.Button(btns, text="↻ Compute All",
                                             command=self._recompute_all)
            self._btn_recompute.pack(side="left", padx=2)
        ttk.Button(btns, text="✓ Validate",
                   command=self._validate_clicked).pack(side="left", padx=(10, 2))
        self._build_shot_origin_controls(btns)

    def _refresh_pin_values(self, cb):
        tokens = []
        for r in self._get_wiring():
            pin = (r.get("pin") or "").strip()
            pad = (r.get("pad") or "").strip()
            if pin:
                tokens.append(f"{pin}:{pad}" if pad else pin)
        if not tokens:
            tokens = [v for v, _label in self._get_pins()]
        cb.config(values=tokens)

    def _refresh_type_values(self):
        # "move" needs Minor Moves' shot geometry to mean anything - see
        # _STEP_TYPES - so it's only offered while that recipe's checkbox is
        # on. A step already saved as "move" still shows/loads fine if the
        # box later gets unchecked; it just can't be freshly picked again
        # until it's back on.
        choices = self._step_type_choices
        if not self._minor_moves_var.get():
            choices = tuple(t for t in choices if t != "move")
        self._type_cb.config(values=choices)

    def _refresh_target_values(self):
        t = self._ed_vars["type"].get()
        mode = self._ed_vars["mode"].get()
        if t == "passfail":
            names = [s.get("name", "") for s in self._steps
                     if _is_measurement_step(s) and s.get("name")]
        elif mode == "measure":
            # A measure step's Target is an earlier APPLY step (force
            # current/voltage), not "all"/any-step like open's - it's naming
            # what to divide this reading by, not what to release. Blank
            # (first, so it's the easy pick) clears a Target set by mistake -
            # the step then just reports its own raw reading, same as if
            # Target had never been touched.
            names = [""] + [s.get("name", "") for s in self._steps
                            if s.get("mode") == "apply" and s.get("type") in ("voltage", "current")
                            and s.get("name")]
        else:
            names = ["all"] + [s.get("name", "") for s in self._steps
                               if s.get("type") not in ("delay", "open", "passfail", "picture", "move")
                               and s.get("name")]
        self._target_cb.config(values=names)

    def _update_target_calc_hint(self):
        if not hasattr(self, "_target_calc_var"):
            return
        t = self._ed_vars["type"].get()
        mode = self._ed_vars["mode"].get()
        tgt = self._ed_vars["target"].get().strip()
        if mode != "measure" or not tgt or tgt.lower() == "all":
            self._target_calc_var.set("")
            return
        applied = self._find_step(tgt)
        if applied is None:
            self._target_calc_var.set(f"target '{tgt}' not found among the earlier steps")
            return
        desc = describe_target_calc({"type": t, "mode": mode}, applied)
        self._target_calc_var.set(
            desc or f"no known calculation for a {t} reading + "
                    f"'{applied.get('name')}' ({applied.get('type')}) — will "
                    "record the raw measurement unchanged")

    def _on_type_change(self):
        t = self._ed_vars["type"].get()

        def _set(widgets, state):
            for w in widgets:
                w.config(state=state)

        _set(self._pin_widgets + self._conn_widgets + [self._level_ent], "normal")
        # Four pins are exclusive to the 4-wire step; every other branch below
        # leaves these disabled, and the values are cleared so a type change
        # cannot leave orphaned sense pins behind.
        if t == FOUR_WIRE_TYPE:
            _set(self._sense_widgets, "normal")
        else:
            _set(self._sense_widgets, "disabled")
            self._ed_vars["his"].set("")
            self._ed_vars["los"].set("")
        self._target_cb.config(state="disabled")
        self._limit_ent.config(state="disabled")
        self._shape_cb.config(state="disabled")
        self._freq_ent.config(state="disabled")
        self._instr_cb.config(state="disabled")
        self._chan_cb.config(state="disabled")
        self._pf_min_ent.config(state="disabled")
        self._pf_max_ent.config(state="disabled")
        self._avg_count_ent.config(state="disabled")
        self._avg_delay_ent.config(state="disabled")
        self._nplc_ent.config(state="disabled")
        self._settle_delay_ent.config(state="disabled")
        self._abs_chk.config(state="disabled")
        # Die # means nothing for a wait or a channel release - see
        # _normalize_step - so it's greyed out and cleared for those, same
        # as every other field that type doesn't use.
        if t in ("delay", "open", "picture"):
            self._ed_vars["die"].set("")
            self._die_ent.config(state="disabled")
        else:
            self._die_ent.config(state="normal")
        # delay/open/passfail/picture route nothing and return early below,
        # so settle the Direct box here rather than in each branch. Only
        # "open" can carry channels, and those are the target step's, not
        # its own - none of the four is a thing you cable up by hand.
        if t in ("delay", "open", "passfail", "picture", "move"):
            self._ed_vars["route"].set(ROUTE_SWITCH)
            self._route_defaulted_for = None
            self._direct_var.set(False)
            self._direct_chk.config(state="disabled")
        else:
            self._direct_chk.config(state="normal")

        if t == "move":
            self._ed_vars["mode"].set("")
            self._ed_vars["chan"].set("")
            self._ed_vars["instrument"].set("")
            self._ed_vars["instrument_key"].set("")
            self._instr_display_var.set("")
            self._mode_cb.config(state="disabled")
            _set(self._pin_widgets + self._conn_widgets + [self._level_ent], "disabled")
            self._update_target_calc_hint()
            return
        if t == "delay":
            self._ed_vars["mode"].set("")
            self._ed_vars["chan"].set("")
            self._ed_vars["instrument"].set("")
            self._ed_vars["instrument_key"].set("")
            self._instr_display_var.set("")
            self._mode_cb.config(state="disabled")
            _set(self._pin_widgets + self._conn_widgets, "disabled")
            if not self._ed_vars["level"].get():
                self._ed_vars["level"].set("200")
            self._update_target_calc_hint()
            return
        if t == "picture":
            self._ed_vars["mode"].set("")
            self._ed_vars["chan"].set("")
            self._ed_vars["instrument"].set("")
            self._ed_vars["instrument_key"].set("")
            self._instr_display_var.set("")
            self._mode_cb.config(state="disabled")
            _set(self._pin_widgets + self._conn_widgets + [self._level_ent], "disabled")
            self._update_target_calc_hint()
            return
        if t == "open":
            self._ed_vars["mode"].set("")
            self._ed_vars["chan"].set("")
            self._ed_vars["instrument"].set("")
            self._ed_vars["instrument_key"].set("")
            self._instr_display_var.set("")
            self._mode_cb.config(state="disabled")
            self._target_cb.config(state="normal")
            self._refresh_target_values()
            _set(self._pin_widgets + [self._level_ent], "disabled")
            self._update_target_calc_hint()
            return
        if t == "passfail":
            self._ed_vars["mode"].set("")
            self._ed_vars["chan"].set("")
            self._ed_vars["instrument"].set("")
            self._ed_vars["instrument_key"].set("")
            self._instr_display_var.set("")
            self._mode_cb.config(state="disabled")
            self._target_cb.config(state="normal")
            self._refresh_target_values()
            _set(self._pin_widgets + self._conn_widgets + [self._level_ent], "disabled")
            self._pf_min_ent.config(state="normal")
            self._pf_max_ent.config(state="normal")
            self._update_target_calc_hint()
            return

        if t in ("resistance", FOUR_WIRE_TYPE):
            self._ed_vars["mode"].set("measure")
            self._mode_cb.config(state="disabled")
            # Neither type sources anything - the instrument just reads
            # whatever's across the pins - so there is no level to set.
            self._ed_vars["level"].set("")
            self._level_ent.config(state="disabled")
        elif t == "wave":
            self._ed_vars["mode"].set("apply")
            self._mode_cb.config(state="disabled")
        else:
            if self._ed_vars["mode"].get() not in _STEP_MODES:
                self._ed_vars["mode"].set("measure")
            self._mode_cb.config(state="readonly")
        mode = self._ed_vars["mode"].get()

        options = tuple(o for o in _instrument_options(t, mode) if o in self._instrument_choices)
        if t == "wave":
            self._ed_vars["instrument"].set("WGEN")
            self._instr_cb.config(state="readonly")
            self._refresh_instrument_dropdown(("WGEN",))
        else:
            self._instr_cb.config(state="readonly")
            if self._ed_vars["instrument"].get() not in options:
                self._ed_vars["instrument"].set(_default_instrument(t, mode))
                self._ed_vars["instrument_key"].set("")
            self._refresh_instrument_dropdown(options)
        instrument = self._ed_vars["instrument"].get()

        if t == "wave":
            self._chan_cb.config(state="readonly", values=_WGEN_CHANNELS)
            if self._ed_vars["chan"].get() not in _WGEN_CHANNELS:
                self._ed_vars["chan"].set("CH1")
            self._shape_cb.config(state="readonly")
            if self._ed_vars["shape"].get() not in _WAVE_SHAPES:
                self._ed_vars["shape"].set("SIN")
            self._freq_ent.config(state="normal")
            if not self._ed_vars["freq"].get():
                self._ed_vars["freq"].set("1000")
        elif instrument == "SMU":
            self._chan_cb.config(state="readonly", values=self._smu_channel_choices)
            if self._ed_vars["chan"].get() not in self._smu_channel_choices:
                self._ed_vars["chan"].set("A")
        else:
            self._ed_vars["chan"].set("")

        if _limit_applicable(t, mode, instrument):
            self._limit_ent.config(state="normal")
            if (_limit_is_current_compliance(t, mode, instrument)
                    and not self._ed_vars["limit"].get()):
                self._ed_vars["limit"].set(_DEFAULT_SMU_CURRENT_LIMIT)

        if _is_measurement_step({"type": t, "mode": mode}):
            self._avg_count_ent.config(state="normal")
            self._nplc_ent.config(state="normal")
            self._settle_delay_ent.config(state="normal")
            self._abs_chk.config(state="normal")
            # The 2636B (Accretech's SMU) has no source-delay mechanism at
            # all - it averages internally via its own repeat-average filter
            # (set_averages), so a per-reading delay here would be a no-op.
            # The 2400 (Electroglas's SMU) does implement it and genuinely
            # needs it - see instrument_panel.py's set_source_delay comment
            # about the charging-transient read on LaMP data - so leave it
            # live there.
            if instrument == "SMU" and self._system == "accretech":
                self._avg_delay_ent.config(state="disabled")
                self._ed_vars["avg_delay"].set("0")
            else:
                self._avg_delay_ent.config(state="normal")
                if not self._ed_vars["avg_delay"].get():
                    self._ed_vars["avg_delay"].set("0")
            if not self._ed_vars["avg_count"].get():
                self._ed_vars["avg_count"].set("1")
            if not self._ed_vars["nplc"].get():
                self._ed_vars["nplc"].set("1")
            if not self._ed_vars["settle_delay"].get():
                self._ed_vars["settle_delay"].set("0")

        # A measure step's Target names an earlier apply step to combine
        # with (see _normalize_step / describe_target_calc) - open/passfail
        # already enabled it for their own, different, meaning above.
        # 4-wire ohms is excluded: it's a DMM function with its own dedicated
        # sense pins, never combined with a separate apply step in practice,
        # and offering a Target here that quietly never did anything was
        # more confusing than useful - a plain "resistance" step keeps
        # Target (see the Ohm's law pairing in describe_target_calc).
        if mode == "measure" and t != FOUR_WIRE_TYPE:
            self._target_cb.config(state="normal")
            self._refresh_target_values()
        else:
            self._ed_vars["target"].set("")

        # Last, so it can override the pin/conn states the branches above
        # just set: a direct step disables them whatever its type wanted.
        if instrument != self._route_defaulted_for:
            self._ed_vars["route"].set(self._default_route_for(instrument))
            self._route_defaulted_for = instrument
        self._apply_route_state()
        self._update_target_calc_hint()

    def _refresh_instrument_dropdown(self, options):
        """Rebuild self._instr_cb's displayed choices from `options` (the
        valid instrument FAMILIES for the step's current type/mode, e.g.
        ("SMU", "DMM")) - each shown with its model name, and, only when a
        family has more than one instrument fitted at once (a second SMU
        added via Setup tab's + Add Instrument, or Electroglas's 3458A +
        E1326B VXI both fitted), one entry per slot so a step can target a
        specific one.

        Purely a display layer - self._ed_vars["instrument"] (the family)
        is unaffected and every other reader of it still sees exactly what
        it always has. self._ed_vars["instrument_key"] only ever gets a
        non-blank value here when the operator actually picks one of the
        per-slot entries below; a family with a single fitted instrument
        never sets it, so every recipe saved on a single-instrument bench
        looks identical to one saved before this feature existed. See
        controller.slots_for_family (app.py) for what builds `options`'
        per-family (key, model, display) list."""
        self._instr_label_map = {}
        labels = []
        for family in options:
            try:
                slots = self.controller.slots_for_family(family)
            except Exception:
                slots = []
            if not slots:
                label = family
                self._instr_label_map[label] = (family, "")
                labels.append(label)
            elif len(slots) == 1:
                _drv_key, model, _display = slots[0]
                label = f"{family} ({model})"
                self._instr_label_map[label] = (family, "")
                labels.append(label)
            else:
                for drv_key, model, _display in slots:
                    label = f"{family} ({model}) — {drv_key}"
                    self._instr_label_map[label] = (family, drv_key)
                    labels.append(label)
        self._instr_cb.config(values=labels)

        want_family = self._ed_vars["instrument"].get()
        want_key = self._ed_vars["instrument_key"].get()
        match = next((lbl for lbl, (fam, key) in self._instr_label_map.items()
                     if fam == want_family and key == want_key), None)
        if match is None:
            # A saved instrument_key that no longer matches any currently-
            # fitted slot (bench reconfigured since) - fall back to any
            # label for the same family rather than showing nothing.
            match = next((lbl for lbl, (fam, _key) in self._instr_label_map.items()
                         if fam == want_family), None)
        self._instr_display_var.set(match or "")

    def _on_instrument_display_selected(self):
        label = self._instr_display_var.get()
        family, key = self._instr_label_map.get(label, ("", ""))
        if family:
            self._ed_vars["instrument"].set(family)
        self._ed_vars["instrument_key"].set(key)
        self._on_type_change()

    # ------------------------------------------------------------------
    # DIRECT vs SWITCH
    #
    # Most steps route through the switch matrix, and their HI/LO pins are
    # what say which crosspoints (Accretech) or relay channels (Electroglas)
    # to close. Some are cabled by hand straight from the instrument to the
    # probe card - a 4-wire resistance check on the 3458A usually is - and
    # then a pin number describes nothing the GUI can act on: the operator
    # has already made the connection, and all the run has to do is take the
    # reading.
    # ------------------------------------------------------------------
    def _instrument_model(self, instrument: str) -> str:
        """The model name behind a step's DMM/SMU/WGEN choice on this bench."""
        if not instrument:
            return ""
        try:
            if self._system == "electroglas":
                from instruments import eg_profiles
                inst = eg_profiles.instruments()
                fitted = set(eg_profiles.fitted_keys())
            else:
                from instruments.gpib_base import load_all_instrument_configs
                inst = load_all_instrument_configs()
                fitted = set(inst)
        except Exception:
            return ""
        keys = (self._EG_INSTRUMENT_KEYS.get(instrument)
                if self._system == "electroglas"
                else {"DMM": ("dmm",), "SMU": ("smu",),
                      "WGEN": ("wave_gen",)}.get(instrument)) or ()
        for k in keys:
            if k in inst and k in fitted:
                return str(inst[k].get("name") or "")
        return ""

    def _default_route_for(self, instrument: str) -> str:
        """Direct for the 3458A, switch for everything else.

        Keyed off the MODEL rather than the "DMM" slot: the same slot is a
        34461A on Accretech, which is bench-wired through the matrix like
        anything else. Only ever seeds a step being built - a saved recipe
        keeps whatever it was saved with (see _normalize_step).
        """
        return (ROUTE_DIRECT if "3458" in self._instrument_model(instrument)
                else ROUTE_SWITCH)

    def _apply_route_state(self):
        """Grey the pin/Conn fields when the step is directly wired."""
        direct = self._ed_vars["route"].get() == ROUTE_DIRECT
        self._direct_var.set(direct)
        if direct:
            for w in self._pin_widgets + self._sense_widgets + self._conn_widgets:
                try:
                    w.config(state="disabled")
                except tk.TclError:
                    pass
        for btn in (getattr(self, "_btn_conn", None),):
            if btn is not None:
                btn.config(state="disabled" if direct else "normal")
        # Terminals (Front/Rear) only makes sense for a direct-wired
        # step - a switch-routed one's physical connection point is
        # whatever the switch matrix wiring is, not a front/rear panel
        # choice. Cleared (not just disabled) going back to switch, so a
        # step doesn't carry a stale FRONT/REAR into a mode it no longer
        # applies to.
        term_cb = getattr(self, "_terminals_cb", None)
        if term_cb is not None:
            term_cb.config(state="readonly" if direct else "disabled")
            if not direct:
                self._ed_vars["terminals"].set("")

    def _on_route_toggle(self):
        self._ed_vars["route"].set(
            ROUTE_DIRECT if self._direct_var.get() else ROUTE_SWITCH)
        if not self._direct_var.get():
            # Back to switch: re-run the type logic so the pin and Conn
            # fields come back in whatever state this step type wants them.
            self._on_type_change()
        else:
            self._ed_vars["conn"].set("")
            self._apply_route_state()

    def _conn_from_editor(self):
        step = self._editor_step()
        _channels, _detail, unresolved = self.step_connections(step)
        self._ed_vars["conn"].set(self._computed_conn_string(step))
        if unresolved:
            messagebox.showwarning(
                "Unresolved",
                "Not found in wiring / steps: " + ", ".join(unresolved))


    def _resolve_pin(self, token: str):
        token = token.strip()
        if not token:
            return None
        head = token.split(":", 1)[0].strip()
        if head.isdigit():
            return int(head)
        for r in self._get_wiring():
            if token.lower() == (r.get("pad") or "").strip().lower():
                pin = (r.get("pin") or "").strip()
                return int(pin) if pin.isdigit() else None
        return None

    def _step_index(self, ref: str):
        ref = ref.strip()
        if not ref:
            return None
        if ref.isdigit():
            i = int(ref) - 1
            return i if 0 <= i < len(self._steps) else None
        for j, s in enumerate(self._steps):
            if s.get("name", "").strip().lower() == ref.lower():
                return j
        return None

    def _find_step(self, ref: str):
        idx = self._step_index(ref)
        return self._steps[idx] if idx is not None else None

    def _computed_conn_string(self, step: dict) -> str:
        if step.get("type") == "open" \
                and (step.get("target") or "").strip().lower() == "all":
            return "all"
        return ",".join(self.step_connections(step)[0])

    def step_connections(self, step: dict):
        t = step.get("type")
        # Before every type check: a directly-cabled step closes nothing
        # whatever it measures, because the operator has already made the
        # connection by hand at the probe card.
        if step.get("route") == ROUTE_DIRECT and t not in ("delay", "picture", "move"):
            return [], ["direct wiring — no switchbox, nothing to close"], []
        if t == "delay":
            return [], ["no switching — wait"], []
        if t == "picture":
            return [], ["no switching — take picture (not yet implemented)"], []
        if t == "move":
            return [], [f"no switching — move chuck to die {step.get('die') or '?'} "
                        "of this shot"], []
        if t == "passfail":
            tgt = (step.get("target") or "").strip()
            return [], [f"no switching — checks '{tgt}' against Min/Max" if tgt
                        else "no switching — checks the previous measurement"], []
        if t == "open":
            tgt = (step.get("target") or "").strip()
            if tgt.lower() == "all":
                return [], ["open ALL channels (channel.open('allslots')) "
                            "+ reset all instrument outputs"], []
            ref = self._find_step(tgt)
            if ref is None or ref.get("type") in ("delay", "open", "passfail", "picture", "move"):
                return [], [], [tgt or "(no target)"]
            channels = [c for c in (ref.get("conn") or "").replace(" ", "").split(",")
                        if c]
            detail = [f"open closures of step '{ref.get('name')}'"]
            if ref.get("type") == "wave":
                detail.append(f"reset WGEN {ref.get('chan') or 'CH1'} output")
            elif ref.get("mode") == "apply":
                detail.append(f"reset SMU {ref.get('chan') or 'A'} output")
            return channels, detail, []

        if self._system == "electroglas":
            return self._step_connections_eg(step)

        by_field = switch_topology.rows_for_fields(t, step.get("chan") or "",
                                                   step.get("instrument") or "")
        roles = switch_topology.row_roles()
        max_pin = switch_topology.total_pins()
        instrument = step.get("instrument") or ""
        chan = step.get("chan") or "A"
        # "hi"/"lo" are whichever instrument the step actually names
        # (SMU or DMM) - "his"/"los" are always the DMM's own 4-wire
        # sense legs (rows_for_fields' own docstring). Used to say "DMM
        # SHI/SLO" unconditionally, which was flat wrong for an SMU hi/lo
        # field with no row wired for its channel (e.g. picking channel B
        # on a bench whose SMU only has channel A wired) - named the
        # actual instrument/channel instead so the message says what to
        # go fix in Switch Settings.
        field_labels = {
            "hi": f"{instrument} {f'channel {chan} ' if instrument == 'SMU' else ''}HI",
            "lo": f"{instrument} {f'channel {chan} ' if instrument == 'SMU' else ''}LO",
            "his": "DMM SHI",
            "los": "DMM SLO",
        }
        channels, detail, unresolved = [], [], []
        for field in ("hi", "lo", "his", "los"):
            rows = by_field.get(field, ())
            if not rows and (step.get(field) or "").strip():
                detail.append(
                    f"{field.upper()} pin(s) named but no switch row is assigned "
                    f"{field_labels[field]} — set one in Switch Settings")
            for token in (p for p in step.get(field, "").split(",") if p.strip()):
                pin = self._resolve_pin(token)
                if pin is None or not (1 <= pin <= max_pin):
                    unresolved.append(token.strip())
                    continue
                slot, col = switch_topology.slot_and_col_for_pin(pin)
                for row in rows:
                    ch = switch_topology.pin_channel(pin, row)
                    channels.append(ch)
                    detail.append(
                        f"{ch} = {switch_topology.role_label(roles.get(row))} × pin {pin} "
                        f"(slot {slot} col {col:02d})")
        return channels, detail, unresolved

    def _step_connections_eg(self, step: dict):
        """Electroglas equivalent of the block above.

        There is no per-pin crosspoint here - the relay card selects a whole
        die of the 2x2 shot at once (see hp_switchbox.BENCH_WIRING), so the
        step's `die` field is what decides the channel(s), not its HI/LO pin
        names. HI/LO are still checked against the card's wiring below (in
        validate_recipe), just not used to compute the connection.
        """
        try:
            from instruments import eg_profiles
            from instruments.hp_switchbox import bench_wiring
        except Exception as e:
            return [], [], [f"(hp_switchbox unavailable: {e})"]
        bench = eg_profiles.active_name()
        wiring = bench_wiring(bench)
        die_sets = wiring.get("die_sets") or {}
        if not die_sets:
            return [], [], [f"no relay wiring recorded for bench '{bench}'"]
        try:
            die = int(step.get("die") or "1")
        except ValueError:
            die = 1
        chans = die_sets.get(die)
        if not chans:
            return [], [], [f"die {die} (bench '{bench}' has no channel mapped "
                            f"for it — known dies: {sorted(die_sets)})"]
        channels = [f"{int(c):02d}" for c in chans]
        detail = [f"bench '{bench}': die {die} -> CH" + "/".join(channels)]
        return channels, detail, []

    def _update_connections(self):
        lines = []
        for i, step in enumerate(self._steps, 1):
            _channels, detail, unresolved = self.step_connections(step)
            computed = self._computed_conn_string(step)
            stored   = (step.get("conn") or "").replace(" ", "")
            tag = step.get("mode") or step.get("chan") or ""
            label = f"{i}. {step.get('name') or '(unnamed)'} " \
                    f"[{step.get('type')}{('/' + tag) if tag else ''}]"
            if step.get("type") == "delay":
                lines.append(f"{label}  wait {step.get('level') or '?'} ms — no switching")
                continue
            if step.get("type") == "passfail":
                tgt = step.get("target") or "(most recent measurement)"
                mn, mx = step.get("min") or "—", step.get("max") or "—"
                lines.append(f"{label}  check '{tgt}' in [{mn}, {mx}] — no switching")
                continue
            if step.get("type") == "move":
                lines.append(f"{label}  move to die {step.get('die') or '?'} "
                             "of this shot — no switching")
                continue
            verb = "open" if step.get("type") == "open" else "close"
            body = f"{verb} {stored}" if stored else "no closures stored"
            if stored != computed:
                body += f"   ✎ edited (auto: {computed or '—'})"
            elif detail:
                body += f"   ({'; '.join(detail)})"
            if unresolved:
                body += f"   ⚠ unresolved: {', '.join(unresolved)}"
            lines.append(f"{label}  {body}")
        self._conn_report = "\n".join(lines) if lines else "— no steps —"
        if self._conn_viewer:
            try:
                self._conn_viewer(f"[{self._current}]\n{self._conn_report}")
            except Exception:
                pass

    def set_connections_viewer(self, fn):
        self._conn_viewer = fn
        self._update_connections()


    _CHAN_RE = re.compile(r"^[24][A-H](0[1-9]|1[0-2])$")
    # Electroglas conn strings are relay channel numbers ("00".."15"), zero
    # padded by die_channels_for_bench/_step_connections_eg - a different
    # shape entirely from Accretech's crosspoint channel spec above.
    _CHAN_RE_EG = re.compile(r"^(0[0-9]|1[0-5])$")

    def validate_recipe(self) -> list:
        issues = []
        closed = {}
        outputs_on = {}
        wiring_pins = {(r.get("pin") or "").strip()
                       for r in self._get_wiring() if (r.get("pin") or "").strip()}
        for i, s in enumerate(self._steps, 1):
            t    = s.get("type")
            name = s.get("name") or f"step {i}"
            tag  = f"{i}. {name}"
            if t == "delay":
                try:
                    float(s.get("level") or "")
                except ValueError:
                    issues.append(f"ERROR {tag}: delay time (Level) is not a number")
                continue

            if t == "picture":
                continue

            if t == "move":
                if not self._minor_moves_var.get():
                    issues.append(f"ERROR {tag}: 'move' steps need this recipe's "
                                  "Minor Moves checkbox on")
                try:
                    if int(float(s.get("die") or "")) < 1:
                        raise ValueError
                except ValueError:
                    issues.append(f"ERROR {tag}: Die # must be a whole number ≥ 1")
                continue

            if t == "passfail":
                tgt = (s.get("target") or "").strip()
                if tgt:
                    idx = self._step_index(tgt)
                    if idx is None:
                        issues.append(f"ERROR {tag}: target '{tgt}' not found")
                    elif idx >= i - 1:
                        issues.append(f"ERROR {tag}: target '{tgt}' comes at/after this "
                                      "passfail step — the measurement must come first")
                    elif not _is_measurement_step(self._steps[idx]):
                        issues.append(f"ERROR {tag}: target '{tgt}' is a "
                                      f"{self._steps[idx].get('type')} step "
                                      "(not a measurement — nothing to check)")
                elif not any(_is_measurement_step(s2) for s2 in self._steps[:i - 1]):
                    issues.append(f"ERROR {tag}: no target set and no measurement "
                                  "step precedes this passfail step")
                mn, mx = (s.get("min") or "").strip(), (s.get("max") or "").strip()
                if not mn and not mx:
                    issues.append(f"ERROR {tag}: set at least one of Min/Max")
                for label, val in (("Min", mn), ("Max", mx)):
                    if val:
                        try:
                            float(val)
                        except ValueError:
                            issues.append(f"ERROR {tag}: {label} is not a number")
                if mn and mx:
                    try:
                        if float(mn) > float(mx):
                            issues.append(f"ERROR {tag}: Min is greater than Max")
                    except ValueError:
                        pass
                continue

            if t == "open":
                tgt = (s.get("target") or "").strip()
                if tgt.lower() == "all":
                    closed.clear()
                    outputs_on.clear()
                    continue
                idx = self._step_index(tgt)
                if idx is None:
                    issues.append(f"ERROR {tag}: target '{tgt}' not found")
                elif idx >= i - 1:
                    issues.append(f"ERROR {tag}: target '{tgt}' comes at/after this "
                                  "open step — open must follow the step it opens")
                elif self._steps[idx].get("type") in ("delay", "open", "passfail", "picture", "move"):
                    issues.append(f"ERROR {tag}: target '{tgt}' is a "
                                  f"{self._steps[idx].get('type')} step (nothing to open)")
                else:
                    outputs_on.pop(idx, None)
                    for ch in (self._steps[idx].get("conn") or "").replace(" ", "").split(","):
                        closed.pop(ch, None)
                continue

            mode = s.get("mode") or ""
            instrument = s.get("instrument") or ""
            valid_instruments = _instrument_options(t, mode)
            if instrument not in valid_instruments:
                issues.append(f"ERROR {tag}: instrument '{instrument or '(none)'}' is "
                              f"not valid for {t}{'/' + mode if mode else ''} "
                              f"(expected {' or '.join(valid_instruments)})")

            if mode == "measure":
                tgt = (s.get("target") or "").strip()
                if tgt:
                    idx = self._step_index(tgt)
                    if idx is None:
                        issues.append(f"ERROR {tag}: target '{tgt}' not found")
                    elif idx >= i - 1:
                        issues.append(f"ERROR {tag}: target '{tgt}' comes at/after this "
                                      "step — the applied step must come first")
                    elif (self._steps[idx].get("mode") != "apply"
                          or self._steps[idx].get("type") not in ("voltage", "current")):
                        issues.append(f"ERROR {tag}: target '{tgt}' is a "
                                      f"{self._steps[idx].get('type')} step (not a "
                                      "voltage/current APPLY step to combine with)")

            # A directly-cabled step routes through no switch, so its pins
            # name nothing the GUI can act on. They are not required, and a
            # blank one is not an error - the operator made the connection at
            # the probe card by hand. Anything that IS filled in still gets
            # the consistency checks below, so a half-remembered pin cannot
            # sit there contradicting the wiring unnoticed.
            direct = s.get("route") == ROUTE_DIRECT
            hi, lo = s.get("hi", "").strip(), s.get("lo", "").strip()
            if hi and lo and hi == lo:
                issues.append(f"ERROR {tag}: HI and LO are the same pin ({hi})")
            pin_tokens = [hi, lo]
            if t == FOUR_WIRE_TYPE:
                his, los = s.get("his", "").strip(), s.get("los", "").strip()
                pin_tokens += [his, los]
                missing = [n for n, v in (("Sense HI", his), ("Sense LO", los)) if not v]
                if missing and not direct:
                    issues.append(f"ERROR {tag}: 4-wire needs all four pins — "
                                  f"missing {', '.join(missing)}")
                # Four legs on one pin is a 2-wire measurement wearing a
                # 4-wire label, and would read lead resistance as device.
                named = [(n, v) for n, v in (("HI", hi), ("LO", lo),
                                             ("Sense HI", his), ("Sense LO", los)) if v]
                seen = {}
                for name, val in named:
                    seen.setdefault(val, []).append(name)
                for val, names in seen.items():
                    if len(names) > 1:
                        issues.append(f"ERROR {tag}: {' and '.join(names)} are the "
                                      f"same pin ({val}) — 4-wire needs four "
                                      f"separate pins")
                if self._system != "electroglas" and not direct:
                    # Accretech's crosspoint needs a row explicitly assigned
                    # to SHI/SLO in Switch Settings - Electroglas's relay
                    # wiring has no such per-role assignment to check, and a
                    # direct step does not go through either.
                    rows = switch_topology.rows_for_fields(t, s.get("chan") or "", instrument)
                    for field, role in (("his", "SHI"), ("los", "SLO")):
                        if s.get(field, "").strip() and not rows.get(field):
                            issues.append(f"ERROR {tag}: no switch row is assigned "
                                          f"DMM {role} — add one in Switch Settings "
                                          f"or the sense leg will not be connected")
            _ch, _det, unresolved = self.step_connections(s)
            if unresolved:
                issues.append(f"ERROR {tag}: pins not resolvable / out of range: "
                              + ", ".join(unresolved))
            for token in pin_tokens:
                if not token:
                    continue
                if self._system == "electroglas":
                    # Electroglas HI/LO already store the physical pin label
                    # (e.g. "A32"), not something to resolve first.
                    if wiring_pins and token not in wiring_pins:
                        issues.append(f"WARN {tag}: pin '{token}' is not defined "
                                      "in the probe card wiring")
                    continue
                pin = self._resolve_pin(token)
                if pin is not None and wiring_pins and str(pin) not in wiring_pins:
                    issues.append(f"WARN {tag}: pin {pin} ('{token}') is not defined "
                                  "in the probe card wiring")
            if t == "wave" or mode == "apply":
                try:
                    float(s.get("level") or "")
                except ValueError:
                    issues.append(f"ERROR {tag}: "
                                  f"{'amplitude' if t == 'wave' else 'source level'}"
                                  " (Level) is not a number")
                outputs_on[i - 1] = s

            if t == "wave":
                try:
                    float(s.get("freq") or "")
                except ValueError:
                    issues.append(f"ERROR {tag}: frequency (Freq) is not a number")
                if s.get("shape") not in _WAVE_SHAPES:
                    issues.append(f"ERROR {tag}: waveform shape "
                                  f"'{s.get('shape')}' is invalid")

            limit = s.get("limit") or ""
            if limit:
                try:
                    float(limit)
                except ValueError:
                    issues.append(f"ERROR {tag}: limit value is not a number")
                if not _limit_applicable(t, mode, instrument):
                    issues.append(f"WARN {tag}: limit set but not applicable to this "
                                  "step (needs SMU sourcing, or wave) — it will be ignored")

            if _is_measurement_step(s):
                try:
                    if int(s.get("avg_count") or 1) < 1:
                        issues.append(f"ERROR {tag}: Avg Count must be a whole number ≥ 1")
                except ValueError:
                    issues.append(f"ERROR {tag}: Avg Count is not a whole number")
                try:
                    if float(s.get("avg_delay") or 0) < 0:
                        issues.append(f"ERROR {tag}: Avg Delay must be a number ≥ 0")
                except ValueError:
                    issues.append(f"ERROR {tag}: Avg Delay is not a number")
                try:
                    if float(s.get("nplc") or 1) <= 0:
                        issues.append(f"ERROR {tag}: NPLC must be a number > 0")
                except ValueError:
                    issues.append(f"ERROR {tag}: NPLC is not a number")
                try:
                    if float(s.get("settle_delay") or 0) < 0:
                        issues.append(f"ERROR {tag}: Settle Delay must be a number ≥ 0")
                except ValueError:
                    issues.append(f"ERROR {tag}: Settle Delay is not a number")

            conn = (s.get("conn") or "").replace(" ", "")
            if direct:
                # Storing no closures is the whole point of a direct step,
                # not a missing-configuration error.
                if conn:
                    issues.append(f"WARN {tag}: marked direct but still carries "
                                  f"switch closures ({conn}) — they will not be "
                                  "closed; press Compute Connection to clear them")
                continue
            if not conn:
                issues.append(f"ERROR {tag}: no switch closures stored")
                continue
            chan_re = self._CHAN_RE_EG if self._system == "electroglas" else self._CHAN_RE
            bad = [c for c in conn.split(",") if not chan_re.match(c)]
            if bad:
                issues.append(f"ERROR {tag}: invalid channel(s): {', '.join(bad)}")
                continue
            if self._system == "electroglas":
                # No row/role concept on the relay card to check for a
                # second-HI-on-the-same-pin conflict - just track what is
                # closed, for the "still closed at the end" check below.
                for ch in conn.split(","):
                    closed[ch] = tag
            else:
                _HI_ROWS = set("ACFGH")
                for ch in conn.split(","):
                    pin_key = (ch[0], ch[2:])
                    bus_key = ch[:2]
                    for other, other_tag in closed.items():
                        if ((other[0], other[2:]) == pin_key and other[1] != ch[1]
                                and ch[1] in _HI_ROWS and other[1] in _HI_ROWS):
                            issues.append(
                                f"WARN {tag}: {ch} puts a second instrument HI row on "
                                f"the same pin as {other} (closed by {other_tag}) — "
                                "intended bias, or missing open step?")
                        # A row is one shared electrical bus (e.g. row A =
                        # SMU-A HI). Closing two channels on that SAME row
                        # but DIFFERENT pins ties those two pins directly
                        # together for as long as both stay closed - an
                        # actual short, not just a bias question, whichever
                        # instrument/steps put them there.
                        if other[:2] == bus_key and other[2:] != ch[2:]:
                            issues.append(
                                f"ERROR {tag}: {ch} shares a row (bus) with "
                                f"{other} (closed by {other_tag}) but a "
                                f"different pin — this shorts pin {ch[2:]} to "
                                f"pin {other[2:]} together until one is opened")
                    closed[ch] = tag

        for idx in sorted(outputs_on):
            s = outputs_on[idx]
            what = ("WGEN " + (s.get("chan") or "CH1")) if s.get("type") == "wave" \
                   else f"SMU {s.get('chan') or 'A'}"
            issues.append(f"WARN {idx + 1}. {s.get('name') or 'step'}: {what} output "
                          "is never opened/reset — add an open step")
        if closed:
            issues.append(f"WARN: {len(closed)} closure(s) still closed at the end "
                          "— consider finishing with an open (target=all) step")
        return issues

    def _validate_clicked(self):
        issues = self.validate_recipe()
        if not self._steps:
            messagebox.showinfo("Validate Recipe", "No steps to check.")
            return
        for msg in issues:
            self.controller.log(f"[RECIPE] {msg}")
        self._store_validity(self._current, issues)
        if not issues:
            self.controller.log(f"[RECIPE] '{self._current}' validated — "
                                f"{len(self._steps)} step(s) OK")
            messagebox.showinfo("Recipe OK",
                                f"'{self._current}' — {len(self._steps)} step(s), "
                                "no issues found.")
        else:
            shown = "\n".join(issues[:15])
            if len(issues) > 15:
                shown += f"\n… and {len(issues) - 15} more (see log)"
            messagebox.showwarning(
                "Recipe Issues",
                f"'{self._current}' — {len(issues)} issue(s):\n\n{shown}")


    def _store_validity(self, name: str, issues: list):
        rec = self._recipes.get(name)
        if rec is None:
            return
        rec["valid"] = bool(rec.get("steps")) and not any(
            m.startswith("ERROR") for m in issues)
        if name == self._current:
            self._update_validity_label()

    def validate_all_recipes(self) -> dict:
        # validate_recipe() reads self._minor_moves_var (a single shared
        # checkbox var, not per-recipe) for its 'move' step check - swap it
        # to each recipe's OWN saved minor_moves while validating, or every
        # recipe but whichever one currently matches the checkbox's on-
        # screen value comes back invalid regardless of what was saved.
        saved_steps = self._steps
        saved_minor_moves = self._minor_moves_var.get()
        results = {}
        try:
            for name, rec in self._recipes.items():
                self._steps = rec.get("steps", [])
                self._minor_moves_var.set(bool(rec.get("minor_moves")))
                issues = self.validate_recipe()
                self._store_validity(name, issues)
                results[name] = rec.get("valid", False)
        finally:
            self._steps = saved_steps
            self._minor_moves_var.set(saved_minor_moves)
        return results

    def _update_validity_label(self):
        rec = self._recipes.get(self._current, {})
        valid = rec.get("valid")
        if valid is True:
            self._validity_lbl.config(text="✓ Valid", fg="#15803d")
        elif valid is False:
            self._validity_lbl.config(text="✗ Invalid", fg="#dc2626")
        else:
            self._validity_lbl.config(text="— Not validated", fg="#6b7280")

    def _lockable_buttons(self) -> tuple:
        names = ["_btn_new", "_btn_rename", "_btn_delete"]
        names += ["_btn_save", "_btn_add_step", "_btn_update_step",
                 "_btn_remove_step", "_btn_move_up", "_btn_move_down",
                 "_btn_recompute"]
        return tuple(names)

    def set_locked(self, locked: bool):
        state = "disabled" if locked else "normal"
        for attr in self._lockable_buttons():
            getattr(self, attr).config(state=state)
        self._picker.config(state="disabled" if locked else "readonly")
        self._locked_lbl.config(
            text="🔒 Locked while a run is in progress" if locked else "")

    def _recompute_all(self):
        if not self._steps:
            return
        if not messagebox.askyesno(
                "Recompute Connections",
                "Overwrite the stored switch connections on ALL steps with\n"
                "values computed from the probe card wiring?\n"
                "Hand-edited connections will be replaced."):
            return
        for step in self._steps:
            if step.get("type") not in ("delay", "open", "passfail", "picture", "move"):
                step["conn"] = self._computed_conn_string(step)
        for step in self._steps:
            if step.get("type") == "open":
                step["conn"] = self._computed_conn_string(step)
        self._refresh_steps()

    def _editor_step(self) -> dict:
        step = {k: self._ed_vars[k].get().strip() for k in _STEP_FIELDS}
        if step["type"] not in _STEP_TYPES:
            step["type"] = "resistance"
        return _normalize_step(step)

    _PREFIXABLE_FIELDS = ("level", "limit", "freq", "min", "max")

    def _step_to_editor(self):
        sel = self._step_tree.selection()
        if not sel:
            return
        idx = self._step_tree.index(sel[0])
        if 0 <= idx < len(self._steps):
            stored = self._steps[idx]
            for k in _STEP_FIELDS:
                raw = stored.get(k, "")
                if k in self._PREFIXABLE_FIELDS and raw:
                    try:
                        self._ed_vars[k].set(format_engineering_compact(float(raw)))
                        continue
                    except ValueError:
                        pass
                self._ed_vars[k].set(raw)
            # Claim the instrument as already-defaulted BEFORE _on_type_change
            # runs, or it would treat this step's instrument as a fresh pick
            # and overwrite the route the step was actually saved with.
            self._route_defaulted_for = stored.get("instrument", "")
            self._on_type_change()

    def _finalize_step(self, step: dict) -> bool:
        if step["type"] == "delay":
            try:
                step["level"] = _normalize_numeric_field(step["level"])
            except ValueError:
                messagebox.showerror("Invalid Step", "Delay steps need a time in ms (Level).")
                return False
            return True
        if step["type"] == "picture":
            return True
        if step["type"] == "move":
            if not self._minor_moves_var.get():
                messagebox.showerror(
                    "Invalid Step",
                    "'move' steps need this recipe's Minor Moves checkbox "
                    "on first.")
                return False
            try:
                if int(float(step.get("die") or "")) < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Step", "Die # must be a whole number ≥ 1.")
                return False
            return True
        if step["type"] == "open":
            tgt = step["target"].strip()
            if tgt.lower() != "all":
                ref = self._find_step(tgt)
                if ref is None or ref.get("type") in ("delay", "open", "passfail", "picture", "move"):
                    messagebox.showerror(
                        "Invalid Step",
                        "Open steps need a Target: a previous measurement/wave\n"
                        "step (by name or number), or 'all'.")
                    return False
            if not step["conn"]:
                step["conn"] = self._computed_conn_string(step)
            return True
        if step["type"] == "passfail":
            tgt = step["target"].strip()
            if tgt:
                ref = self._find_step(tgt)
                if ref is None or not _is_measurement_step(ref):
                    messagebox.showerror(
                        "Invalid Step",
                        "Passfail Target must be a previous resistance / voltage"
                        "(measure) / current(measure) step, or blank to use the "
                        "most recent measurement.")
                    return False
            elif not any(_is_measurement_step(s) for s in self._steps):
                messagebox.showerror(
                    "Invalid Step",
                    "No measurement step exists yet for this passfail step to check.")
                return False
            if not (step["min"] or step["max"]):
                messagebox.showerror("Invalid Step", "Set at least one of Min/Max.")
                return False
            for label, key in (("Min", "min"), ("Max", "max")):
                if step[key]:
                    try:
                        step[key] = _normalize_numeric_field(step[key])
                    except ValueError:
                        messagebox.showerror("Invalid Step", f"{label} must be a number.")
                        return False
            return True
        # A direct-wired step is cabled straight to the probe card by hand -
        # its HI/LO pins name nothing the GUI can act on (same exemption
        # validate_recipe() already gives it), so requiring them here just
        # blocks saving a step that is correctly configured.
        if step.get("route") != ROUTE_DIRECT and not (step["hi"] or step["lo"]):
            messagebox.showerror("Invalid Step", "Specify at least one HI or LO pin.")
            return False
        if _is_measurement_step(step):
            try:
                if int(step["avg_count"]) < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Step", "Avg Count must be a whole number ≥ 1.")
                return False
            try:
                if float(step["avg_delay"]) < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Step", "Avg Delay must be a number ≥ 0.")
                return False
            try:
                if float(step["nplc"]) <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Step", "NPLC must be a number > 0.")
                return False
            try:
                if float(step["settle_delay"]) < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Step", "Settle Delay must be a number ≥ 0.")
                return False
        for label, key in (("Level", "level"), ("Limit", "limit"), ("Freq", "freq")):
            if step[key]:
                try:
                    step[key] = _normalize_numeric_field(step[key])
                except ValueError:
                    messagebox.showerror(
                        "Invalid Step",
                        f"{label} must be a number (optionally with a unit "
                        "prefix like m/µ/n/k, e.g. \"5m\" or \"2u\").")
                    return False
        if not step["conn"]:
            step["conn"] = self._computed_conn_string(step)
        return True

    def _step_add(self):
        step = self._editor_step()
        if not step["name"]:
            step["name"] = f"Step {len(self._steps) + 1}"
        if not self._finalize_step(step):
            return
        self._steps.append(step)
        self._refresh_steps()

    def _step_update(self):
        sel = self._step_tree.selection()
        if not sel:
            messagebox.showinfo("No Selection", "Select a step to update.")
            return
        idx = self._step_tree.index(sel[0])
        if 0 <= idx < len(self._steps):
            step = self._editor_step()
            if not self._finalize_step(step):
                return
            self._steps[idx] = step
            self._refresh_steps(select=idx)

    def _step_remove(self):
        sel = self._step_tree.selection()
        if not sel:
            return
        idx = self._step_tree.index(sel[0])
        if 0 <= idx < len(self._steps):
            del self._steps[idx]
            self._refresh_steps()

    def _step_move(self, delta: int):
        sel = self._step_tree.selection()
        if not sel:
            return
        idx = self._step_tree.index(sel[0])
        new = idx + delta
        if 0 <= idx < len(self._steps) and 0 <= new < len(self._steps):
            self._steps[idx], self._steps[new] = self._steps[new], self._steps[idx]
            self._refresh_steps(select=new)

    def _refresh_steps(self, select: int = -1):
        self._step_tree.delete(*self._step_tree.get_children())
        for i, step in enumerate(self._steps, 1):
            self._step_tree.insert("", "end", values=(
                i, step.get("name", ""), step.get("type", ""),
                step.get("instrument", ""), step.get("mode", ""),
                step.get("chan", ""), step.get("target", ""), step.get("die", "1"),
                step.get("route", ROUTE_SWITCH),
                step.get("hi", ""), step.get("lo", ""),
                step.get("his", ""), step.get("los", ""),
                step.get("level", ""), step.get("limit", ""),
                _avg_display(step),
                step.get("min", ""), step.get("max", ""),
                step.get("shape", ""), step.get("freq", ""), step.get("conn", "")))
        kids = self._step_tree.get_children()
        if 0 <= select < len(kids):
            self._step_tree.selection_set(kids[select])
        self._refresh_target_values()
        self._update_connections()


    def _store_form(self):
        rec = self._recipes.get(self._current)
        if rec is None:
            return
        rec["steps"] = self._steps
        rec["sites"] = self._sites

    def _load_form(self, name: str):
        rec = self._recipes[name]
        self._current = name
        self._picker_var.set(name)
        self._steps = rec.setdefault("steps", [])
        self._sites = rec.setdefault("sites", [])
        self._refresh_steps()
        self._refresh_sites()
        self._update_validity_label()
        self._minor_moves_var.set(bool(rec.get("minor_moves")))
        self._shortcut_var.set(bool(rec.get("shortcut")))
        self._fast_current_settle_var.set(bool(rec.get("fast_current_settle")))
        self._manual_mode_var.set(bool(rec.get("manual_mode")))
        if getattr(self, "_align_die_var", None) is not None:
            self._align_die_var.set(rec.get("align_die") or "")
            self._fill_align_die_choices()
        self._wafer_map_var.set(rec.get("wafer_map") or "")
        if self._shot_origin_btn is not None:
            self._shot_origin_btn.config(
                state="normal" if self._minor_moves_var.get() else "disabled")
        self._refresh_shot_origin_label()

    def _switch_recipe(self):
        name = self._picker_var.get()
        if name == self._current or name not in self._recipes:
            return
        self._store_form()
        self._load_form(name)
        self.controller.log(f"[RECIPE] Active recipe: {name}")
        self._update_default_label()

    def _refresh_picker(self):
        names = self._visible_recipe_names()
        self._picker.config(values=names)
        if self._current in names:
            self._picker_var.set(self._current)
        elif names:
            self._load_form(names[0])
        else:
            # Nothing visible on this bench (e.g. every recipe on the card
            # is tagged for a different one) - clear the display instead
            # of leaving the previous bench's recipe/steps stuck on
            # screen with a dropdown that no longer lists it. The recipe
            # itself is untouched in self._recipes, just not shown here.
            self._current = ""
            self._picker_var.set("")
            self._steps = []
            self._sites = []
            self._refresh_steps()
            self._refresh_sites()
            self._update_validity_label()
            self._minor_moves_var.set(False)
            self._shortcut_var.set(False)
            self._fast_current_settle_var.set(False)
            self._manual_mode_var.set(False)
            if getattr(self, "_align_die_var", None) is not None:
                self._align_die_var.set("")
            self._wafer_map_var.set("")
            if self._shot_origin_btn is not None:
                self._shot_origin_btn.config(state="disabled")
            self._shot_origin_status_var.set("")
        self._update_default_label()

    def _set_default_recipe(self):
        folder = self._get_ata_folder()
        if not folder:
            messagebox.showerror("No ATA Folder",
                                 "Load an ATA folder first — the default recipe is "
                                 "remembered per ATA folder.")
            return
        card = self._active_card
        if not card:
            messagebox.showerror("No Probe Card", "Select a probe card first.")
            return
        name = self._current
        if name == "(unsaved)":
            messagebox.showerror("Unsaved Recipe",
                                 "Save this recipe (💾 Save) before setting it as default.")
            return
        if save_default_recipe(folder, card, name, system=self._system):
            self.controller.log(
                f"[RECIPE] '{name}' (probe card '{card}') set as default — will "
                "auto-load on the Run tab whenever this ATA folder is opened.")
            self._update_default_label()
        else:
            messagebox.showerror("Save Failed", "Could not write ata_default_recipe.json.")

    def _update_default_label(self):
        folder = self._get_ata_folder()
        card, name = load_default_recipe(folder, system=self._system) if folder else (None, None)
        if card and name:
            is_current = (card == self._active_card and name == self._current)
            self._default_lbl.config(
                text=("⭐ default: this recipe" if is_current
                      else f"⭐ default: '{name}' ({card})"))
        else:
            self._default_lbl.config(text="")

    def _new_recipe(self):
        card = self._get_active_card()
        if not card:
            messagebox.showerror(
                "No Probe Card",
                "Select or create a probe card first — on the Probe Card "
                "tab. Recipes belong to exactly one probe card and are "
                "stored inside its .csv file.")
            return
        name = simpledialog.askstring("New Recipe", "Recipe name:",
                                      parent=self)
        if not name:
            return
        name = _safe_filename(name)
        if not name:
            messagebox.showerror("Invalid Name", "Use letters, digits, space, - or _.")
            return
        if name in self._recipes:
            messagebox.showerror("Duplicate", f"Recipe '{name}' already exists.")
            return
        self._store_form()
        # No recipe to copy from once the card's last one was deleted -
        # start blank rather than KeyError on a self._current that no
        # longer points at anything.
        cur = self._recipes.get(self._current, {"steps": [], "sites": []})
        rec = {"steps": [dict(s) for s in cur["steps"]],
               "sites": [dict(s) for s in cur.get("sites", [])],
               "bench": self._active_bench_tag(),
               # Copies whether the sibling recipe used minor moves, but
               # NOT its captured shot_origin - that was a physical chuck
               # position read live for that run, and blindly trusting it
               # for a new recipe (possibly a different shot layout) would
               # be exactly the kind of stale-state mistake Set Shot Origin
               # exists to prevent. The new recipe has to capture its own.
               "minor_moves": bool(cur.get("minor_moves")),
               "shot_origin": None}
        self._recipes[name] = rec
        if ("(unsaved)" in self._recipes and "(unsaved)" != name
                and len(self._recipes) > 1
                and not self._recipes["(unsaved)"]["steps"]
                and not self._recipes["(unsaved)"].get("sites")):
            del self._recipes["(unsaved)"]
        self._load_form(name)
        self._refresh_picker()
        if self._save_recipes(card, self._recipes):
            self.controller.log(f"[RECIPE] Created '{name}' in probe card '{card}' "
                                f"(copy of previous recipe)")
        else:
            self.controller.log(f"[RECIPE] Created '{name}' — save to probe card "
                                f"'{card}' failed")

    def _rename_recipe(self):
        old_name = self._current
        new_name = simpledialog.askstring("Rename Recipe", "New recipe name:",
                                          initialvalue=old_name, parent=self)
        if not new_name or new_name == old_name:
            return
        new_name = _safe_filename(new_name)
        if not new_name:
            messagebox.showerror("Invalid Name", "Use letters, digits, space, - or _.")
            return
        if new_name in self._recipes:
            messagebox.showerror("Duplicate", f"Recipe '{new_name}' already exists.")
            return
        self._store_form()
        self._recipes[new_name] = self._recipes.pop(old_name)
        self._current = new_name
        self._load_form(new_name)
        self._refresh_picker()

        card = self._get_active_card()
        if card and self._save_recipes(card, self._recipes):
            self.controller.log(f"[RECIPE] Renamed '{old_name}' -> '{new_name}' "
                                f"in probe card '{card}'")
        elif card:
            self.controller.log(f"[RECIPE] Renamed '{old_name}' -> '{new_name}' — "
                                f"save to probe card '{card}' failed")
        else:
            self.controller.log(f"[RECIPE] Renamed '{old_name}' -> '{new_name}' "
                                "(in-memory only — no probe card active)")

    def _delete_recipe(self):
        name = self._current
        if name not in self._recipes:
            return
        if not messagebox.askyesno("Delete Recipe", f"Delete recipe '{name}'?"):
            return

        del self._recipes[name]
        if self._recipes:
            self._current = next(iter(self._recipes))
            self._load_form(self._current)
        else:
            # The last recipe on this card is gone - leave the dropdown
            # blank rather than conjuring up a placeholder "(unsaved)"
            # recipe nobody asked for. _new_recipe/import can start one.
            self._current = ""
            self._steps = []
            self._sites = []
            self._refresh_steps()
            self._refresh_sites()
            self._update_validity_label()
        self._refresh_picker()

        card = self._get_active_card()
        if card and self._save_recipes(card, self._recipes):
            self.controller.log(f"[RECIPE] Deleted '{name}' from probe card '{card}'")
        elif card:
            self.controller.log(f"[RECIPE] Deleted '{name}' — save to probe card "
                                f"'{card}' failed")
        else:
            self.controller.log(f"[RECIPE] Deleted '{name}' (in-memory only — "
                                "no probe card active)")


    def _import_legacy(self):
        if not self._get_active_card():
            messagebox.showerror(
                "No Probe Card",
                "Select or create a probe card first — on the Probe Card "
                "tab. An imported recipe is registered under the active card.")
            return
        path = filedialog.askopenfilename(
            title="Import Legacy Recipe (.pma / .PMS)",
            filetypes=[("Legacy recipe files", "*.pma *.PMS *.ini *.txt *.cfg"),
                      ("All files", "*.*")],
        )
        if not path:
            return
        self.import_legacy_from_path(path)

    def import_legacy_from_path(self, path: str) -> bool:
        card = self._get_active_card()
        if not card:
            messagebox.showerror(
                "No Probe Card",
                "Select or create a probe card first — on the Probe Card "
                "tab. An imported recipe is registered under the active card.")
            return False
        try:
            useful = parse_pma_params(path)
        except Exception as exc:
            self.controller.log(f"[RECIPE] Import error: {exc}")
            return False
        if not useful:
            messagebox.showwarning(
                "Nothing to Import",
                "No recognized measurement parameters (Voltage, delays, "
                "averaging, current limit) were found in that file.")
            return False
        steps = pma_params_to_steps(useful, available=self._bench_instruments())
        self._log_unbuildable_steps(steps)
        # A .PMA whose touchdowns name several devices is a multi-die shot,
        # and the block has to run once per die. This path never did that -
        # only the workbook import did - so a LOAD ALL of a quad recipe built
        # a recipe that measured one die and called the shot done.
        dies_per_shot = _pma_dies_per_shot(path)
        if dies_per_shot > 1:
            try:
                wiring = self._get_wiring()
            except Exception:
                wiring = []
            get_dp = getattr(self, "_get_die_pins", None)
            card_die_pins = {}
            if callable(get_dp):
                try:
                    card_die_pins = get_dp() or {}
                except Exception:
                    card_die_pins = {}
            channels = die_channels_for_bench(dies_per_shot)
            pins = die_pins_from_card(wiring, dies_per_shot,
                                      die_pins=card_die_pins)
            # Say so when the bench cannot reach every die. Without a channel
            # per die the steps all measure whichever path is already closed,
            # so N dies come back as N copies of one reading - which looks
            # like a working multi-die recipe right up until the data is used.
            if dies_per_shot > 1 and not channels:
                self.controller.log(
                    f"[RECIPE] ⚠ This shot has {dies_per_shot} dies but the "
                    f"active bench has no relay channel mapping for that many "
                    f"— the steps carry no channel, so every die would measure "
                    f"the same path. Wire the extra channels and add them to "
                    f"BENCH_WIRING before trusting a run.")
            if dies_per_shot > 1 and not pins:
                self.controller.log(
                    f"[RECIPE] ⚠ No probe-card pin mapping for {dies_per_shot} "
                    f"dies — add a DIEPIN table to the card so each die names "
                    f"its own HI/LO pins.")
            steps = repeat_steps_per_die(steps, dies_per_shot, channels, pins)

        name = os.path.splitext(os.path.basename(path))[0]
        # Silently uniquifying was wrong for the main caller. LOAD ALL exists to
        # regenerate a recipe from its .PMA, so a name clash is the normal case,
        # not an accident - and minting "name (2)" meant every regeneration went
        # somewhere the user was not looking. Seven stale copies of the gauge
        # recipe accumulated that way while the loaded one kept its old steps.
        if name in self._recipes:
            choice = messagebox.askyesnocancel(
                "Recipe Already Exists",
                f"A recipe named '{name}' is already saved under this probe "
                f"card.\n\n"
                f"Yes — replace it with the one built from this .PMA\n"
                f"No — keep both, saving this as '{name} (2)'\n"
                f"Cancel — leave everything as it is\n\n"
                f"Replacing overwrites its measurement steps and touchdown "
                f"list.")
            if choice is None:
                self.controller.log(
                    f"[RECIPE] Import cancelled — '{name}' left unchanged.")
                return False
            if choice is False:
                orig_name, n = name, 2
                while name in self._recipes:
                    name = f"{orig_name} ({n})"
                    n += 1
            else:
                self.controller.log(f"[RECIPE] Replacing existing recipe '{name}'.")
        self._store_form()
        # NOT auto-computing conn here on purpose. Electroglas connections
        # come from per-bench relay wiring (hp_switchbox.BENCH_WIRING) that
        # does not exist - or cannot be assumed correct - for every project,
        # unlike Accretech's pin-table approach; silently "computing" a
        # value from possibly-wrong/missing wiring is worse than leaving it
        # blank and making that fact visible. HI/LO pins can't be inferred
        # from the .PMA either - checked a real file (LAMPATA's own "HP LaMP
        # electrical gauge.PMA"): it carries only touchdown-move references
        # and generic measurement parameters (voltage/delays/averages/NPLC/
        # current limit), no electrical pin/device field at all. Both stay
        # exactly what pma_params_to_steps set (blank) until the operator
        # fills them in by hand.
        self._recipes[name] = {"steps": steps, "sites": [],
                               "bench": self._active_bench_tag()}
        if ("(unsaved)" in self._recipes and "(unsaved)" != name
                and len(self._recipes) > 1
                and not self._recipes["(unsaved)"]["steps"]
                and not self._recipes["(unsaved)"].get("sites")):
            del self._recipes["(unsaved)"]
        self._load_form(name)
        self._refresh_picker()
        # Without this the just-imported recipe's "valid" flag is whatever
        # it happened to be before (stale, or never set for a brand new
        # recipe) - not a reflection of the steps actually just built. A
        # recipe with no pins/connections set yet should show red
        # (unresolved) or at least accurately "not validated", not a
        # leftover green from some earlier recipe.
        self.validate_all_recipes()

        mapped = ", ".join(f"{k}={useful[k]}" for k in _PMA_MAPPED_KEYS if k in useful)
        unmapped = ", ".join(f"{k}={useful[k]}" for k in _PMA_UNMAPPED_KEYS if k in useful)
        msg = (f"[RECIPE] Imported recipe '{name}' — "
              f"{len(steps)} step(s) generated from: {mapped or '(nothing recognized)'}")
        if unmapped:
            msg += f" — no step field for: {unmapped} (set on the instrument directly if needed)"
        self.controller.log(msg)
        if self._save_recipes(card, self._recipes):
            self._file_lbl.config(text=f"Imported recipe '{name}'", fg="#374151")
            self.controller.log(f"[RECIPE] Saved '{name}' to probe card '{card}'")
        else:
            self.controller.log(
                f"[RECIPE] Imported '{name}' — save to probe card '{card}' failed")

        messagebox.showinfo(
            "Legacy Recipe Imported",
            f"Created recipe '{name}' with {len(steps)} step(s) from the legacy "
            "file's measurement defaults.\n\n"
            "HI/LO pins could not be inferred from the file — set them on the "
            "measurement step, then ✓ Validate before running.")
        return True

    def _import_legacy_workbook(self):
        if _pma_xlrd is None:
            messagebox.showerror(
                "xlrd Not Installed",
                "Reading legacy .xls workbooks needs the xlrd package.\n\n"
                "Run:  .venv\\Scripts\\pip install xlrd")
            return
        path = filedialog.askopenfilename(
            title="Import Legacy Recipe Workbook (.xls)",
            filetypes=[("Excel 97-2003 Workbook", "*.xls"), ("All files", "*.*")],
        )
        if not path:
            return
        self.import_legacy_workbook_from_path(path)

    def import_legacy_workbook_from_path(self, path: str) -> bool:
        if _pma_xlrd is None:
            messagebox.showerror(
                "xlrd Not Installed",
                "Reading legacy .xls workbooks needs the xlrd package.\n\n"
                "Run:  .venv\\Scripts\\pip install xlrd")
            return False
        card = self._get_active_card()
        if not card:
            messagebox.showerror(
                "No Probe Card",
                "Select or create a probe card first — on the Probe Card "
                "tab. An imported recipe is registered under the active card.")
            return False
        try:
            book = _pma_xlrd.open_workbook(path, formatting_info=True)
            info = _pma_read_main_menu_info(book)
            useful = info["params"]
        except Exception as exc:
            self.controller.log(f"[RECIPE] Workbook import error: {exc}")
            messagebox.showerror("Import Failed", f"Could not read that workbook:\n{exc}")
            return False
        if not useful:
            messagebox.showwarning(
                "Nothing to Import",
                "No Name/Value measurement fields (Voltage, delays, "
                "averaging, current limit) were found on that workbook's "
                "MainMenu tab.")
            return False
        steps = pma_params_to_steps(useful, available=self._bench_instruments())
        self._log_unbuildable_steps(steps)

        dies_per_shot = 1
        try:
            grid = _pma_read_moves_grid(book, "MajorMoves")
            widths = [len(s["dies"]) for s in grid["shots"] if s["included"]]
            if widths:
                dies_per_shot = max(widths)
        except Exception as exc:
            self.controller.log(f"[RECIPE] Could not read MajorMoves for dies-per-shot "
                                f"(defaulting to 1): {exc}")
        steps = repeat_steps_per_die(steps, dies_per_shot)

        name = info["recipe_name"] or os.path.splitext(os.path.basename(path))[0]
        orig_name, n = name, 2
        while name in self._recipes:
            name = f"{orig_name} ({n})"
            n += 1
        self._store_form()
        # See import_legacy_from_path's identical comment - not auto-
        # computing conn or pins here on purpose. Neither is reliably
        # inferrable for Electroglas across every project; left for the
        # operator to fill in by hand.
        self._recipes[name] = {"steps": steps, "sites": [],
                               "bench": self._active_bench_tag()}
        if ("(unsaved)" in self._recipes and "(unsaved)" != name
                and len(self._recipes) > 1
                and not self._recipes["(unsaved)"]["steps"]
                and not self._recipes["(unsaved)"].get("sites")):
            del self._recipes["(unsaved)"]
        self._load_form(name)
        self._refresh_picker()
        self.validate_all_recipes()

        mapped = ", ".join(f"{k}={useful[k]}" for k in _PMA_MAPPED_KEYS if k in useful)
        unmapped = ", ".join(f"{k}={useful[k]}" for k in _PMA_UNMAPPED_KEYS if k in useful)
        msg = (f"[RECIPE] Imported recipe '{name}' from workbook — "
              f"{len(steps)} step(s) generated from: {mapped or '(nothing recognized)'}")
        if dies_per_shot > 1:
            msg += f" — repeated {dies_per_shot}x (this probe card's shots co-touch {dies_per_shot} dies)"
        if unmapped:
            msg += f" — no step field for: {unmapped} (set on the instrument directly if needed)"
        self.controller.log(msg)
        if self._save_recipes(card, self._recipes):
            self._file_lbl.config(text=f"Imported recipe '{name}'", fg="#374151")
            self.controller.log(f"[RECIPE] Saved '{name}' to probe card '{card}'")
        else:
            self.controller.log(
                f"[RECIPE] Imported '{name}' — save to probe card '{card}' failed")

        repeat_note = (
            f"This probe card's shots co-touch {dies_per_shot} dies, so the "
            f"sequence was repeated {dies_per_shot}x (\"(Die 1)\", \"(Die 2)\", "
            "...) — assign each repetition's HI/LO pins separately.\n\n"
            if dies_per_shot > 1 else "")
        messagebox.showinfo(
            "Legacy Recipe Imported",
            f"Created recipe '{name}' with {len(steps)} step(s) from the "
            "workbook's MainMenu measurement defaults.\n\n"
            f"{repeat_note}"
            "HI/LO pins could not be inferred from the file — set them on the "
            "measurement step, then ✓ Validate before running.")
        return True

    def _save(self):
        if not self._recipes:
            return
        self._store_form()
        self.validate_all_recipes()
        card = self._get_active_card()
        if not card:
            messagebox.showerror(
                "No Probe Card",
                "Select or create a probe card first — recipes are stored "
                "inside its .csv file.")
            return
        if self._save_recipes(card, self._recipes):
            self._file_lbl.config(
                text=f"Saved {len(self._recipes)} recipe(s) to probe card '{card}'",
                fg="#374151")
            self.controller.log(
                f"[RECIPE] Saved {len(self._recipes)} recipe(s) to probe card '{card}'")
            self._on_save(self._current)
        else:
            self.controller.log(f"[RECIPE] Save failed for probe card '{card}'")


    def load_recipes(self, card: str, recipes: dict):
        self._active_card = card
        if recipes:
            # bench dropped here previously - every recipe reloaded from
            # disk came back untagged, so _visible_recipe_names() could
            # never actually filter anything and every recipe showed on
            # every bench regardless of what it was saved with.
            # {**rec, ...}: see WaferMapPanel.get_recipes for why this
            # copies the whole recipe instead of naming fields.
            self._recipes = {name: {**rec,
                                    "steps": [dict(s) for s in rec.get("steps", [])],
                                    "sites": [dict(s) for s in rec.get("sites", [])],
                                    "bench": rec.get("bench", ""),
                                    "minor_moves": bool(rec.get("minor_moves")),
                                    # shortcut/fast_current_settle used to be
                                    # dropped here - this dict only copied a
                                    # hand-picked list of fields, so either
                                    # checkbox silently reset to unchecked on
                                    # every probe-card (re)load regardless of
                                    # what was actually saved.
                                    "shortcut": bool(rec.get("shortcut")),
                                    "fast_current_settle": bool(rec.get("fast_current_settle")),
                                    "manual_mode": bool(rec.get("manual_mode")),
                                    "shot_origin": rec.get("shot_origin")}
                              for name, rec in recipes.items()}
            visible = self._visible_recipe_names()
            self._current = visible[0] if visible else next(iter(self._recipes))
        else:
            self._recipes = {"(unsaved)": {"steps": [], "sites": []}}
            self._current = "(unsaved)"
        self.validate_all_recipes()
        self._load_form(self._current)
        self._refresh_picker()

        self._card_picker.config(values=[""] + sorted(self._get_card_names()))
        self._card_picker_var.set(card)
        if card:
            self._file_lbl.config(
                text=f"{len(recipes)} recipe(s)", fg="#374151")
            self.controller.log(
                f"[RECIPE] Probe card '{card}': {len(recipes)} recipe(s)"
                + (f": {', '.join(recipes)}" if recipes else ""))
        else:
            self._file_lbl.config(text="No probe card selected", fg="#6b7280")
            self.controller.log("[RECIPE] No probe card active — no recipes to show.")


    def get_active_card(self) -> str:
        return self._active_card

    def _on_card_picker_selected(self):
        name = self._card_picker_var.get()
        if name != self._active_card:
            self._switch_card_cb(name)

    def get_steps(self) -> list:
        return [dict(s) for s in self._steps]

    def refresh_connections(self):
        self._update_connections()

    def get_recipe_names(self) -> list:
        return self._visible_recipe_names()

    def get_active_recipe(self) -> str:
        return self._current

    def is_minor_moves(self) -> bool:
        """Whether the loaded recipe wants shot-aware single-die stepping
        - see the Recipe tab's Minor Moves checkbox / _build_minor_moves_bar."""
        rec = self._recipes.get(self._current)
        return bool(rec and rec.get("minor_moves"))

    def get_shot_origin(self):
        """The (die_x, die_y) captured by 📍 Set Shot Origin for the loaded
        recipe, or None if it has not been set yet this session."""
        rec = self._recipes.get(self._current)
        return rec.get("shot_origin") if rec else None

    def select_recipe(self, name: str) -> bool:
        if name not in self._recipes:
            return False
        self._store_form()
        self._load_form(name)
        self._refresh_picker()
        return True
