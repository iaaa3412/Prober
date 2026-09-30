"""Push results straight into an Access database (.mdb / .accdb)."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import export_formats as xfmt

try:
    import pyodbc
    _PYODBC_ERR = ""
except ImportError as _e:
    pyodbc = None
    _PYODBC_ERR = f"{type(_e).__name__}: {_e}"

MDB_PATH_FILENAME = "ata_mdb_path.json"


def save_mdb_path(folder: str, path: str) -> None:
    fpath = os.path.join(folder, MDB_PATH_FILENAME)
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump({"mdb_path": path}, f)
    except OSError:
        pass


def load_mdb_path(folder: str, default: str = "") -> str:
    fpath = os.path.join(folder, MDB_PATH_FILENAME)
    if not os.path.isfile(fpath):
        return default
    try:
        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("mdb_path", default)) or default
    except (OSError, ValueError, TypeError):
        return default

ACCESS_DRIVER_HINT = "Microsoft Access Driver"


def python_bits() -> int:
    import struct
    return struct.calcsize("P") * 8


def access_drivers() -> List[str]:
    if pyodbc is None:
        return []
    try:
        found = [d for d in pyodbc.drivers() if ACCESS_DRIVER_HINT in d]
    except Exception:
        return []
    return sorted(found, key=lambda d: ("accdb" not in d, d))


def available() -> bool:
    return pyodbc is not None and bool(access_drivers())


def connection_string(path: str, driver: Optional[str] = None) -> str:
    driver = driver or (access_drivers() or [""])[0]
    return f"DRIVER={{{driver}}};DBQ={os.path.abspath(path)};"


def preflight(path: str, table: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "problems": [], "warnings": [],
                           "driver": "", "columns": [], "row_count": None}
    if pyodbc is None:
        out["problems"].append(
            f"pyodbc is not installed ({_PYODBC_ERR}) — run: "
            ".venv\\Scripts\\pip install pyodbc")
        return out
    drivers = access_drivers()
    if not drivers:
        out["problems"].append(
            f"No Microsoft Access ODBC driver is installed for {python_bits()}-bit "
            "Python. Install the Microsoft Access Database Engine redistributable "
            f"in its {python_bits()}-bit build — a {128 - python_bits()}-bit driver "
            "cannot be loaded by this process.")
        return out
    out["driver"] = drivers[0]
    if not path:
        out["problems"].append("No database file chosen.")
        return out
    if not os.path.isfile(path):
        out["problems"].append(
            f"{path} does not exist or is not reachable from this machine. "
            "A network database must be given as a UNC path to the shared "
            "copy — pushing to a local copy changes nothing anyone else sees.")
        return out

    try:
        conn = pyodbc.connect(connection_string(path, out["driver"]), timeout=5)
    except Exception as exc:
        out["problems"].append(f"Could not open the database: {exc}")
        return out
    try:
        cur = conn.cursor()
        tables = {r.table_name.lower() for r in cur.tables(tableType="TABLE")}
        if table.lower() not in tables:
            out["problems"].append(
                f"The database has no table named '{table}'. Tables found: "
                + (", ".join(sorted(tables)[:12]) or "(none)"))
            return out
        out["columns"] = [r.column_name for r in cur.columns(table=table)]
        try:
            cur.execute(f"SELECT COUNT(*) FROM [{table}]")
            out["row_count"] = int(cur.fetchone()[0])
        except Exception:
            pass
        if os.access(path, os.W_OK) is False:
            out["problems"].append(f"{path} is read-only for this account.")
            return out
        out["ok"] = True
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def build_rows(fmt: Dict[str, Any], results_data: List[Dict[str, Any]],
               lot_id: str, wafer_id: str, folder: str = "") -> Tuple[List[str], List[tuple]]:
    rows = xfmt.rows_for_format(fmt, results_data)
    context = {"test_serial": xfmt.compute_test_serial(lot_id, wafer_id),
               "lot_id": lot_id, "wafer_id": wafer_id}
    cols = fmt["columns"]
    fields = [c["field"] for c in cols]
    out = []
    for r in rows:
        r = xfmt.apply_lookup(fmt, folder, r)
        vals = []
        for c in cols:
            raw = xfmt.resolve_column_value(c, r, context)
            if c.get("quote"):
                vals.append("" if raw is None else str(raw))
            else:
                try:
                    vals.append(float(xfmt.sql_num(raw)))
                except (TypeError, ValueError):
                    vals.append(0.0)
        out.append(tuple(vals))
    return fields, out


def push(path: str, fmt: Dict[str, Any], results_data: List[Dict[str, Any]],
         lot_id: str, wafer_id: str, driver: Optional[str] = None,
         folder: str = "") -> Dict[str, Any]:
    table = fmt["table"]
    fields, rows = build_rows(fmt, results_data, lot_id, wafer_id, folder)
    if not rows:
        return {"ok": False, "inserted": 0, "error": "No matching results to push."}
    placeholders = ",".join("?" for _ in fields)
    sql = (f"INSERT INTO [{table}] ("
           + ",".join(f"[{f}]" for f in fields)
           + f") VALUES ({placeholders})")
    try:
        conn = pyodbc.connect(connection_string(path, driver), timeout=10,
                              autocommit=False)
    except Exception as exc:
        return {"ok": False, "inserted": 0, "error": f"Could not open: {exc}"}
    try:
        cur = conn.cursor()
        cur.fast_executemany = False
        cur.executemany(sql, rows)
        conn.commit()
        return {"ok": True, "inserted": len(rows), "error": "", "table": table}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "inserted": 0,
                "error": f"{type(exc).__name__}: {exc}  (nothing was written)"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


LAMP_SQL_DUMP_DIR = r"C:\LampDump"
LAMP_MDB_PATH = r"\\fabserve\ProberStuff\LampElectricalProbeData.mdb"


def push_sql_dump_folder(mdb_path: str, dump_dir: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "files": [], "total_rows": 0, "error": None}
    if pyodbc is None:
        result["error"] = f"pyodbc is not installed ({_PYODBC_ERR})"
        return result
    if not os.path.isdir(dump_dir):
        result["error"] = f"Dump folder not found: {dump_dir}"
        return result
    sql_files = sorted(f for f in os.listdir(dump_dir)
                       if f.lower().endswith(".sql")
                       and os.path.isfile(os.path.join(dump_dir, f)))
    if not sql_files:
        result["ok"] = True
        result["error"] = f"No .sql files waiting in {dump_dir}."
        return result

    drivers = access_drivers()
    if not drivers:
        result["error"] = "No Microsoft Access ODBC driver found on this PC."
        return result

    try:
        conn = pyodbc.connect(connection_string(mdb_path, drivers[0]),
                              timeout=10, autocommit=False)
    except Exception as exc:
        result["error"] = f"Could not open {mdb_path}: {exc}"
        return result

    pushed_dir = os.path.join(dump_dir, "Pushed")
    try:
        cur = conn.cursor()
        for fname in sql_files:
            fpath = os.path.join(dump_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8-sig") as f:
                    statements = [ln.strip().rstrip(";")
                                 for ln in f if ln.strip()]
                n = 0
                for stmt in statements:
                    cur.execute(stmt)
                    n += 1
                conn.commit()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                result["files"].append(
                    {"file": fname, "ok": False, "rows": 0,
                     "error": f"{type(exc).__name__}: {exc}"})
                continue
            os.makedirs(pushed_dir, exist_ok=True)
            dest = os.path.join(pushed_dir, fname)
            if os.path.exists(dest):
                stem, ext = os.path.splitext(fname)
                dest = os.path.join(pushed_dir, f"{stem}_{int(time.time())}{ext}")
            os.replace(fpath, dest)
            result["files"].append({"file": fname, "ok": True, "rows": n, "error": None})
            result["total_rows"] += n
    finally:
        try:
            conn.close()
        except Exception:
            pass

    result["ok"] = all(f["ok"] for f in result["files"])
    return result
