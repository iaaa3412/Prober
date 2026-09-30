import json
import os
import platform
import sys

PRESETS = {
    "automationproject": "C:/automationproject",
    "proberautomation": r"\\prober\M\ETL\proberautomation",
}

def _exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_PREF_PATH = os.path.join(_exe_dir(), "working_dir_pref.json")

_FORCE_TEMPORARY_DEFAULT = None


def computer_name() -> str:
    return os.environ.get("COMPUTERNAME") or platform.node() or "UNKNOWN-PC"


def _load_pref() -> dict:
    try:
        with open(_PREF_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def saved_default_working_dir() -> "str | None":
    return _load_pref().get("working_dir")


def get_default_working_dir() -> str:
    if _FORCE_TEMPORARY_DEFAULT:
        return _FORCE_TEMPORARY_DEFAULT
    return saved_default_working_dir() or PRESETS["automationproject"]


def set_default_working_dir(path: str) -> None:
    data = _load_pref()
    data["working_dir"] = path
    try:
        with open(_PREF_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def _looks_like_project_root(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        names = os.listdir(path)
    except OSError:
        return False
    if "GUI System" in names:
        return True
    return any(n.lower().endswith("ata") and os.path.isdir(os.path.join(path, n))
              for n in names)


def _fallback_candidates() -> list:
    base = _exe_dir()
    return [base, os.path.dirname(base)]


_current = None


def get_current_working_dir() -> str:
    global _current
    if _current is None:
        _current = get_default_working_dir()
    if os.path.isdir(_current):
        return _current
    for candidate in _fallback_candidates():
        if _looks_like_project_root(candidate):
            return candidate
    return _current


def set_current_working_dir(path: str) -> None:
    global _current
    _current = path


def gui_system_dir() -> str:
    return os.path.join(get_current_working_dir(), "GUI System")
