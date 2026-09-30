from __future__ import annotations

import re

_PARSE_SUFFIXES = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6,
    "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6,
}

_FORMAT_PREFIXES = [
    ("M", 1e6), ("k", 1e3), ("", 1.0),
    ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12),
]

_NUM_RE = re.compile(r'^\s*([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*([a-zA-Zµ]*)\s*$')


def parse_engineering(text: str) -> float:
    m = _NUM_RE.match(text or "")
    if not m:
        raise ValueError(f"Not a number: {text!r}")
    value = float(m.group(1))
    suffix = m.group(2)
    if not suffix:
        return value
    prefix_char = suffix[0]
    if prefix_char in _PARSE_SUFFIXES:
        return value * _PARSE_SUFFIXES[prefix_char]
    if prefix_char.lower() in ("v", "a", "w", "s", "h", "f", "o"):
        return value
    raise ValueError(f"Unrecognized unit prefix: {suffix!r}")


def _pick_prefix(value: float) -> tuple[str, float]:
    if value == 0:
        return "", 1.0
    av = abs(value)
    for prefix, scale in _FORMAT_PREFIXES:
        if av >= scale:
            return prefix, scale
    return _FORMAT_PREFIXES[-1]


def format_engineering(value, unit: str, decimals: int = 3) -> str:
    if value is None:
        return "--"
    prefix, scale = _pick_prefix(value)
    return f"{value / scale:.{decimals}f} {prefix}{unit}"


def format_engineering_compact(value, decimals: int = 3) -> str:
    if value is None or value == "":
        return ""
    value = float(value)
    prefix, scale = _pick_prefix(value)
    return f"{value / scale:.{decimals}f}{prefix}"
