from __future__ import annotations

import csv
import json
import os
import re
from typing import Any, Dict, List, Optional

ATA_EXPORT_FORMATS_FILENAME = "ata_export_formats.json"

SQL_SOURCE_FIELDS = {
    "die_id":      "Device-ID string (e.g. 94-60/94-50/94-61/94-51) — from the "
                   "wafer map's own ID column, or Wafer Builder > Overlay; blank if neither",
    "switch":      "The recipe step's switch channel, blank for steps that set none",
    "set_voltage": "Commanded/set bias voltage",
    "voltage":     "Actual measured voltage (SMU readback, falls back to set_voltage)",
    "value":       "The measurement reading itself (current, resistance, etc.)",
    "unit":        "Unit of the reading (A, V, ohm)",
    "recipe":      "Active recipe name",
    "die":         "Die label shown on the Run tab (Accretech XY/die number)",
    "step":        "Recipe step name",
    "type":        "Step type (current, voltage, resistance)",
    "mode":        "Step mode (apply/measure)",
    "instrument":  "Instrument that took the reading (SMU/DMM)",
    "connection":  "Switch-matrix channel(s) closed for this reading",
    "timestamp":   "Reading timestamp",
    "test_serial": "Computed test serial — see compute_test_serial",
    "iteration":   "Always 1 (one row per die's final averaged reading)",
    "abs_row":     "Die's real absolute row index on the wafer map (blank if unknown)",
    "abs_col":     "Die's real absolute column index on the wafer map (blank if unknown)",
    "shot_row":    "Which reticle/shot row this die's shot is in (Minor Moves recipes "
                   "only — blank otherwise)",
    "shot_col":    "Which reticle/shot column this die's shot is in (Minor Moves "
                   "recipes only — blank otherwise)",
    "intra_row":   "Die's own row position WITHIN its shot (Minor Moves recipes only "
                   "— blank otherwise)",
    "intra_col":   "Die's own column position WITHIN its shot (Minor Moves recipes "
                   "only — blank otherwise)",
    "probe_card":  "Probe card that was active when this reading was taken",
}

CSV_SOURCE_FIELDS = {
    "lot_id":         "Lot ID entered on the Results tab",
    "wafer_id":       "Wafer ID entered on the Results tab",
    "chip_id":        "Overlay die ID if available, else a row+column label (e.g. 02I)",
    "die_id":         "Overlay die ID for this die, if one is loaded (blank otherwise)",
    "row_num":        "Die row number",
    "column_letter":  "Die column letter",
    "connection":     "All switch-matrix channels used for this die, merged",
    "current":        "Forced/measured current for this die",
    "voltage":        "SMU voltage readback for this die",
    "resistance":     "Resistance reading for this die",
    "voltage_dmm":    "Independent DMM voltage reading for this die",
    "compliance":     "Compliance-limit flag (currently always FALSE)",
    "time_stamp":     "Timestamp of the die's first reading",
    "test_serial":    "Computed test serial — see compute_test_serial",
    "abs_row":        "Die's real absolute row index on the wafer map (blank if unknown)",
    "abs_col":        "Die's real absolute column index on the wafer map (blank if unknown)",
    "shot_row":       "Which reticle/shot row this die's shot is in (Minor Moves recipes "
                      "only — blank otherwise; see the Wafer Builder Shot Map tab)",
    "shot_col":       "Which reticle/shot column this die's shot is in (Minor Moves "
                      "recipes only — blank otherwise)",
    "intra_row":      "Die's own row position WITHIN its shot (Minor Moves recipes only "
                      "— blank otherwise; see the Wafer Builder Shot tab)",
    "intra_col":      "Die's own column position WITHIN its shot (Minor Moves recipes "
                      "only — blank otherwise)",
    "probe_card":     "Probe card that was active when this die was measured",
}

SOURCE_FIELDS_BY_TYPE = {"sql": SQL_SOURCE_FIELDS, "csv": CSV_SOURCE_FIELDS}

LAMP_FORMAT: Dict[str, Any] = {
    "name": "LaMP Electrical Quad (tblLampElectricalMeasurements)",
    "table": "tblLampElectricalMeasurements",
    "type": "sql",
    "requires_die_id": True,
    "quad_group_size": 4,
    "columns": [
        {"field": "fldTestSerial", "source": "test_serial",  "quote": False},
        {"field": "fldDieID",      "source": "quad_die_id",  "quote": True},
        {"field": "fldSwitch",     "source": "switch",       "quote": False},
        {"field": "fldIteration",  "source": "iteration",    "quote": False},
        {"field": "fldSetVoltage", "source": "set_voltage",  "quote": False},
        {"field": "fldVoltage",    "source": "voltage",      "quote": False},
        {"field": "fldCurrent",    "source": "value",        "quote": False},
    ],
}

MADX_FORMAT: Dict[str, Any] = {
    "name": "MAD-X Resistance CSV (LotID/WaferID/ChipID...)",
    "table": "madx_resistance",
    "type": "csv",
    "requires_die_id": False,
    "columns": [
        {"field": "LotID",       "source": "lot_id"},
        {"field": "WaferID",     "source": "wafer_id"},
        {"field": "ChipID",      "source": "chip_id"},
        {"field": "Row",         "source": "row_num"},
        {"field": "Column",      "source": "column_letter"},
        {"field": "Connection",  "source": "connection"},
        {"field": "Voltage",     "source": "voltage"},
        {"field": "Current",     "source": "current"},
        {"field": "Resistance",  "source": "resistance"},
        {"field": "Voltage_DMM", "source": "voltage_dmm", "multiply": -1},
        {"field": "Compliance",  "source": "compliance"},
        {"field": "Time_Stamp",  "source": "time_stamp"},
    ],
}


_lookup_cache: Dict[tuple, Dict[tuple, Dict[str, str]]] = {}
_lookup_cache_by_key: Dict[tuple, Dict[str, Dict[str, str]]] = {}


def load_lookup_table_by_field(folder: str, filename: str,
                               lookup_key_col: str) -> Dict[str, Dict[str, str]]:
    path = os.path.join(folder, filename)
    cache_key = (path, lookup_key_col)
    if cache_key in _lookup_cache_by_key:
        return _lookup_cache_by_key[cache_key]
    table: Dict[str, Dict[str, str]] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                key = (row.get(lookup_key_col) or "").strip()
                if key:
                    table[key] = row
    except OSError:
        pass
    _lookup_cache_by_key[cache_key] = table
    return table


def load_lookup_table(folder: str, filename: str,
                      lookup_row_col: str, lookup_col_col: str) -> Dict[tuple, Dict[str, str]]:
    path = os.path.join(folder, filename)
    cache_key = (path, lookup_row_col, lookup_col_col)
    if cache_key in _lookup_cache:
        return _lookup_cache[cache_key]
    table: Dict[tuple, Dict[str, str]] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    key = (int(float(row[lookup_row_col])), int(float(row[lookup_col_col])))
                except (KeyError, TypeError, ValueError):
                    continue
                table[key] = row
    except OSError:
        pass
    _lookup_cache[cache_key] = table
    return table


def apply_lookup(fmt: Dict[str, Any], folder: str, row: Dict[str, Any]) -> Dict[str, Any]:
    lookup = fmt.get("lookup")
    if not lookup or not folder:
        return row
    key_field = lookup.get("key_field")
    if key_field:
        key = (row.get(key_field) or "").strip()
        if not key:
            return row
        table = load_lookup_table_by_field(
            folder, lookup.get("file", ""), lookup.get("lookup_key_col", key_field))
        match = table.get(key)
        if not match:
            return row
        merged = dict(row)
        merged.update(match)
        return merged
    our_row = row.get(lookup.get("our_row_field", "abs_row"))
    our_col = row.get(lookup.get("our_col_field", "abs_col"))
    if our_row in (None, "") or our_col in (None, ""):
        return row
    try:
        key = (int(float(our_row)), int(float(our_col)))
    except (TypeError, ValueError):
        return row
    table = load_lookup_table(folder, lookup.get("file", ""),
                              lookup.get("lookup_row_col", "row"),
                              lookup.get("lookup_col_col", "col"))
    match = table.get(key)
    if not match:
        return row
    merged = dict(row)
    merged.update(match)
    return merged


def compute_test_serial(lot_id: str, wafer_id: str) -> int:
    digits = "".join(ch for ch in f"{lot_id}{wafer_id}" if ch.isdigit())
    return int(digits) if digits else 0


def sql_num(value, default: float = 0.0) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = default
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    if f != 0 and abs(f) < 1e-9:
        return f"{f:.6E}"
    s = f"{f:.15f}".rstrip("0")
    return s if not s.endswith(".") else s + "0"


def sql_string(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _formats_filename(system: str) -> str:
    if system == "accretech":
        return ATA_EXPORT_FORMATS_FILENAME
    base, ext = os.path.splitext(ATA_EXPORT_FORMATS_FILENAME)
    return f"{base}_{system}{ext}"


def load_formats(folder: str, system: str = "accretech") -> List[Dict[str, Any]]:
    path = os.path.join(folder, _formats_filename(system))
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            formats = data.get("formats") or []
            if formats:
                return formats
        except (OSError, ValueError):
            pass
    seeded = [LAMP_FORMAT, MADX_FORMAT]
    save_formats(folder, seeded, system)
    return seeded


def save_formats(folder: str, formats: List[Dict[str, Any]], system: str = "accretech"):
    path = os.path.join(folder, _formats_filename(system))
    default = None
    export_path = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            default = existing.get("default")
            export_path = existing.get("export_path")
        except (OSError, ValueError):
            pass
    if default is not None and default not in {f["name"] for f in formats}:
        default = None
    out = {"formats": formats, "default": default}
    if export_path is not None:
        out["export_path"] = export_path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)


def add_format(folder: str, fmt: Dict[str, Any], system: str = "accretech") -> List[Dict[str, Any]]:
    formats = [f for f in load_formats(folder, system) if f["name"] != fmt["name"]]
    formats.append(fmt)
    save_formats(folder, formats, system)
    return formats


def delete_format(folder: str, name: str, system: str = "accretech") -> List[Dict[str, Any]]:
    formats = [f for f in load_formats(folder, system) if f["name"] != name]
    save_formats(folder, formats, system)
    return formats


def find_format(folder: str, name: str, system: str = "accretech") -> Optional[Dict[str, Any]]:
    return next((f for f in load_formats(folder, system) if f["name"] == name), None)


def get_default_format_name(folder: str, system: str = "accretech") -> Optional[str]:
    path = os.path.join(folder, _formats_filename(system))
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("default")
        except (OSError, ValueError):
            pass
    return None


def set_default_format_name(folder: str, name: Optional[str], system: str = "accretech") -> None:
    formats = load_formats(folder, system)
    if name is not None and name not in {f["name"] for f in formats}:
        return
    path = os.path.join(folder, _formats_filename(system))
    export_path = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                export_path = json.load(f).get("export_path")
        except (OSError, ValueError):
            pass
    out = {"formats": formats, "default": name}
    if export_path is not None:
        out["export_path"] = export_path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)


def get_default_export_path(folder: str, system: str = "accretech") -> Optional[str]:
    path = os.path.join(folder, _formats_filename(system))
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("export_path")
        except (OSError, ValueError):
            pass
    return None


def set_default_export_path(folder: str, export_path: str, system: str = "accretech") -> None:
    path = os.path.join(folder, _formats_filename(system))
    data = {"formats": [], "default": None}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            pass
    data["export_path"] = export_path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


class _BlankMissing(dict):
    def __missing__(self, key):
        return ""
    def __getitem__(self, key):
        val = dict.get(self, key)
        return "" if val in (None, "") else val


def resolve_source(source: str, row: Dict[str, Any], context: Dict[str, Any]):
    if source == "test_serial":
        return context.get("test_serial", 0)
    if source == "iteration":
        return 1
    if source == "abs_row" and "abs_row" not in row:
        return row.get("row", "")
    if source == "abs_col" and "abs_col" not in row:
        return row.get("col", "")
    if source in context:
        return context[source]
    return row.get(source, "")


def resolve_column_value(col: Dict[str, Any], row: Dict[str, Any], context: Dict[str, Any]):
    if "constant" in col and col["constant"] not in (None, ""):
        return col["constant"]
    if col.get("template"):
        values = {**context, **row}
        try:
            return col["template"].format_map(_BlankMissing(values))
        except (KeyError, ValueError, IndexError):
            return col["template"]
    raw = resolve_source(col.get("source", ""), row, context)
    mult = col.get("multiply")
    rnd = col.get("round")
    has_mult = mult not in (None, "", 1, 1.0)
    has_round = rnd not in (None, "")
    if (has_mult or has_round) and raw not in (None, ""):
        try:
            val = float(raw)
            if has_mult:
                val *= float(mult)
            raw = f"{val:.{int(rnd)}f}" if has_round else f"{val:.6g}"
        except (TypeError, ValueError):
            return raw
    if col.get("abs") and raw not in (None, ""):
        try:
            val = abs(float(raw))
            return f"{val:.{int(rnd)}f}" if has_round else f"{val:.6g}"
        except (TypeError, ValueError):
            return raw
    return raw


def detect_reading_kinds(results_data: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for r in results_data:
        t, mode, instrument = r.get("type") or "", r.get("mode") or "", r.get("instrument") or ""
        key = (t, mode, instrument)
        if key == ("", "", "") or key in seen:
            continue
        seen.add(key)
        bits = [b for b in (t, mode, instrument) if b]
        out.append({"label": " / ".join(bits) if bits else "(reading)",
                    "type": t, "mode": mode, "instrument": instrument})
    return out


def rows_for_format(fmt: Dict[str, Any],
                    results_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if fmt.get("requires_die_id", True):
        return [r for r in results_data if r.get("die_id")]
    return list(results_data)


def _with_quad_die_id(rows: List[Dict[str, Any]], group_size: int) -> List[Dict[str, Any]]:
    out = []
    for i in range(0, len(rows), group_size):
        chunk = rows[i:i + group_size]
        joined = "/".join(r.get("die_id") or "" for r in chunk)
        for r in chunk:
            r2 = dict(r)
            r2["quad_die_id"] = joined
            out.append(r2)
    return out


def build_insert_statements(fmt: Dict[str, Any], results_data: List[Dict[str, Any]],
                            lot_id: str, wafer_id: str, folder: str = "") -> List[str]:
    rows = rows_for_format(fmt, results_data)
    group_size = fmt.get("quad_group_size")
    if group_size:
        rows = _with_quad_die_id(rows, group_size)
    context = {"test_serial": compute_test_serial(lot_id, wafer_id),
              "lot_id": lot_id, "wafer_id": wafer_id}
    cols = fmt["columns"]
    field_list = ", ".join(c["field"] for c in cols)
    out = []
    for r in rows:
        r = apply_lookup(fmt, folder, r)
        vals = []
        for c in cols:
            raw = resolve_column_value(c, r, context)
            vals.append(sql_string(raw) if c.get("quote") else sql_num(raw))
        out.append(f"INSERT INTO {fmt['table']} ({field_list}) VALUES ({','.join(vals)})")
    return out


_DIE_RC_RE = re.compile(r"R(\d+)C(\d+)")


def _parse_die_rc(die_label: str):
    m = _DIE_RC_RE.search(die_label or "")
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


_ID_ROW_COL_RE = re.compile(r"^(\d+)([A-Za-z]+)$")


def _parse_id_row_col(die_id: str):
    m = _ID_ROW_COL_RE.match(die_id or "")
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _col_letter(col: int) -> str:
    n = col + 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _combined_connection(rows: List[Dict[str, Any]]) -> str:
    seen: List[str] = []
    for r in rows:
        for ch in (r.get("connection") or "").split("_"):
            if ch and ch not in seen:
                seen.append(ch)
    seen.sort(key=lambda ch: ch[1] if len(ch) > 1 else ch)
    return "_".join(seen)


def _die_group_key(r: Dict[str, Any]):
    row, col = r.get("row"), r.get("col")
    if row is not None and col is not None:
        return ("rc", row, col)
    return ("label", r.get("die") or "")


def group_results_by_die(results_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    order: List[Any] = []
    rows_by_key: Dict[Any, List[Dict[str, Any]]] = {}
    for r in results_data:
        key = _die_group_key(r)
        if key not in rows_by_key:
            rows_by_key[key] = []
            order.append(key)
        rows_by_key[key].append(r)

    out = []
    for key in order:
        rows = rows_by_key[key]
        die = next((r.get("die") for r in rows if r.get("die")), rows[0].get("die") or "")
        row_num, col_num = ((key[1], key[2]) if key[0] == "rc" else _parse_die_rc(die))
        overlay_die_id = next((r.get("die_id") for r in rows if r.get("die_id")), "")
        id_row, id_col_letter = _parse_id_row_col(overlay_die_id) if overlay_die_id else (None, None)
        current_row = next((r for r in rows if r.get("type") == "current"), None)
        smu_voltage_row = next(
            (r for r in rows if r.get("type") == "voltage" and r.get("mode") == "measure"
             and r.get("instrument") == "SMU"), None)
        dmm_voltage_row = next(
            (r for r in rows if r.get("type") == "voltage" and r.get("mode") == "measure"
             and r.get("instrument") == "DMM"), None)
        resistance_row = next(
            (r for r in rows if r.get("type") == "resistance"
             or (r.get("unit") or "").strip().lower() in ("ohm", "ohms", "Ω".lower())), None)
        connection = _combined_connection(rows)

        current_val = current_row.get("value") if current_row else ""
        if current_row and current_row.get("voltage") not in (None, ""):
            voltage_val = current_row.get("voltage")
        elif smu_voltage_row:
            voltage_val = smu_voltage_row.get("value")
        else:
            voltage_val = ""

        if resistance_row:
            resistance_val = resistance_row.get("value")
        else:
            try:
                resistance_val = float(voltage_val) / float(current_val)
            except (TypeError, ValueError, ZeroDivisionError):
                resistance_val = ""

        out.append({
            "die": die,
            "chip_id": (overlay_die_id or
                       (f"{row_num:02d}{_col_letter(col_num)}"
                        if row_num is not None and col_num is not None else die)),
            "die_id": overlay_die_id,
            "row_num": (id_row if id_row is not None
                       else (f"{row_num:02d}" if row_num is not None else "")),
            "column_letter": (id_col_letter if id_col_letter is not None
                              else (_col_letter(col_num) if col_num is not None else "")),
            "connection": connection,
            "current": current_val,
            "voltage": voltage_val,
            "resistance": resistance_val,
            "voltage_dmm": dmm_voltage_row.get("value") if dmm_voltage_row else "",
            "compliance": "FALSE",
            "time_stamp": rows[0].get("timestamp", "") if rows else "",
            "abs_row": _first(rows, "row"),
            "abs_col": _first(rows, "col"),
            "shot_row": _first(rows, "shot_row"),
            "shot_col": _first(rows, "shot_col"),
            "intra_row": _first(rows, "intra_row"),
            "intra_col": _first(rows, "intra_col"),
            "probe_card": _first(rows, "probe_card"),
        })
    return out


def _first(rows: List[Dict[str, Any]], key: str):
    return next((r.get(key) for r in rows if r.get(key) not in (None, "")), "")


def build_csv_rows(fmt: Dict[str, Any], results_data: List[Dict[str, Any]],
                   lot_id: str, wafer_id: str, folder: str = "") -> List[Dict[str, Any]]:
    context = {"lot_id": lot_id, "wafer_id": wafer_id,
              "test_serial": compute_test_serial(lot_id, wafer_id)}
    if fmt.get("per_step"):
        out = []
        for r in rows_for_format(fmt, results_data):
            r = apply_lookup(fmt, folder, r)
            out.append({c["field"]: resolve_column_value(c, r, context) for c in fmt["columns"]})
        return out
    out = []
    for g in group_results_by_die(results_data):
        if not g["current"] and not g["resistance"]:
            continue
        g = apply_lookup(fmt, folder, g)
        out.append({c["field"]: resolve_column_value(c, g, context) for c in fmt["columns"]})
    return out


def has_data_for_format(fmt: Dict[str, Any], results_data: List[Dict[str, Any]]) -> bool:
    if fmt.get("type") == "csv":
        if fmt.get("per_step"):
            return bool(rows_for_format(fmt, results_data))
        return any(g["current"] or g["resistance"]
                  for g in group_results_by_die(results_data))
    return bool(rows_for_format(fmt, results_data))
