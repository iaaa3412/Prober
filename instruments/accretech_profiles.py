"""Per-bench instrument profiles for the Accretech probers."""

import yaml

from instruments.gpib_base import get_machine_config_path

_PROFILES_FILE = "accretech_probers.yaml"

ACCR_KEYS = ("prober", "smu", "dmm", "switch_matrix", "wave_gen")

MANDATORY_KEYS = ("prober", "switch_matrix")

_KEY_LABELS = {
    "prober": "Prober", "smu": "SMU", "dmm": "DMM",
    "switch_matrix": "Switch Matrix", "wave_gen": "Wave Gen",
}

GENERIC_MODEL = "Generic (no driver yet)"

MODEL_CHOICES = {
    "prober": ("AccretechUF200R",),
    "smu": ("Keithley2636B", "Keithley2400"),
    "dmm": ("Keysight34461A",),
    "switch_matrix": ("Keithley707B",),
    "wave_gen": ("Keysight33512B",),
}

DEFAULT_MODEL = {
    "prober": "AccretechUF200R",
    "smu": "Keithley2636B",
    "dmm": "Keysight34461A",
    "switch_matrix": "Keithley707B",
    "wave_gen": "Keysight33512B",
}


def model_choices_for(key: str) -> tuple:
    if key in MODEL_CHOICES:
        return MODEL_CHOICES[key]
    seen = [GENERIC_MODEL]
    for models in MODEL_CHOICES.values():
        for m in models:
            if m not in seen:
                seen.append(m)
    return tuple(seen)


def _slugify_key(display_name: str) -> str:
    slug = "".join(c.lower() if c.isalnum() else "_" for c in display_name.strip())
    slug = "_".join(p for p in slug.split("_") if p)
    return slug or "instrument"


def _path() -> str:
    return get_machine_config_path(_PROFILES_FILE)


def load() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    with open(_path(), "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False,
                       allow_unicode=True, width=100)


def ensure_default_file() -> bool:
    path = _path()
    if __import__("os").path.exists(path):
        return False
    instruments = {}
    try:
        with open(get_machine_config_path("instruments.yaml"), "r", encoding="utf-8") as fh:
            live = (yaml.safe_load(fh) or {}).get("instruments") or {}
    except (OSError, ValueError):
        live = {}
    for key in ACCR_KEYS:
        existing = live.get(key) or {}
        instruments[key] = {
            "name": existing.get("name", _KEY_LABELS.get(key, key)),
            "address": existing.get("address", ""),
            "timeout_ms": int(existing.get("timeout_ms", 3000)),
            "model": DEFAULT_MODEL[key],
        }
    data = {"active": "probe08",
           "probers": {"probe08": {"label": "probe08", "instruments": instruments}}}
    _save(data)
    return True


def profile_names() -> list:
    return sorted((load().get("probers") or {}).keys())


def active_name() -> str:
    data = load()
    name = data.get("active")
    names = sorted((data.get("probers") or {}).keys())
    if name in names:
        return name
    return names[0] if names else ""


def get(name: str = None) -> dict:
    data = load()
    name = name or active_name()
    if not name:
        return {}
    profile = (data.get("probers") or {}).get(name)
    if profile is None:
        raise KeyError(f"no Accretech profile named {name!r} "
                       f"(known: {sorted((data.get('probers') or {}))})")
    return profile


def label(name: str = None) -> str:
    name = name or active_name()
    try:
        return get(name).get("label") or name
    except KeyError:
        return name


def instruments(name: str = None) -> dict:
    return get(name).get("instruments") or {}


def model_of(key: str, name: str = None) -> str:
    entry = instruments(name).get(key) or {}
    return entry.get("model") or DEFAULT_MODEL.get(key, GENERIC_MODEL)


def is_fitted(key: str, name: str = None) -> bool:
    entry = instruments(name).get(key)
    return bool(entry and entry.get("fitted", True))


def all_keys(name: str = None) -> list:
    inst = instruments(name)
    ordered = [k for k in ACCR_KEYS if k in inst]
    custom = sorted(k for k in inst if k not in ordered)
    return ordered + custom


def fitted_keys(name: str = None) -> list:
    inst = instruments(name)
    return [k for k in all_keys(name) if k in inst and inst[k].get("fitted", True)]


def apply_to_instruments_yaml(name: str = None) -> list:
    name = name or active_name()
    inst = instruments(name)
    yaml_path = get_machine_config_path("instruments.yaml")
    with open(yaml_path, "r", encoding="utf-8") as fh:
        live = yaml.safe_load(fh) or {}
    live.setdefault("instruments", {})

    changed = []
    for key in all_keys(name):
        entry = inst.get(key)
        if not entry:
            continue
        want = {"name": entry.get("name", key),
                "protocol": "GPIB",
                "address": entry.get("address", ""),
                "timeout_ms": int(entry.get("timeout_ms", 3000))}
        if live["instruments"].get(key) != want:
            live["instruments"][key] = want
            changed.append(key)

    if changed:
        with open(yaml_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(live, fh, default_flow_style=False, sort_keys=False)
    return changed


def add_profile(new_name: str, based_on: str = None) -> None:
    import copy
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("prober name cannot be blank")
    data = load()
    probers = data.setdefault("probers", {})
    if new_name in probers:
        raise ValueError(f"a profile named {new_name!r} already exists")
    source = based_on or active_name()
    if source not in probers:
        raise KeyError(f"no Accretech profile named {source!r}")
    probers[new_name] = copy.deepcopy(probers[source])
    probers[new_name]["label"] = new_name
    _save(data)


def rename_profile(old_name: str, new_name: str) -> None:
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not old_name or not new_name:
        raise ValueError("prober name cannot be blank")
    if old_name == new_name:
        return
    data = load()
    probers = data.get("probers") or {}
    if old_name not in probers:
        raise KeyError(f"no Accretech profile named {old_name!r}")
    entry = probers.pop(old_name)
    entry["label"] = new_name
    probers[new_name] = entry
    if data.get("active") == old_name:
        data["active"] = new_name
    _save(data)

    try:
        import switch_topology
        switch_topology.rename_bench(old_name, new_name)
    except Exception:
        pass
    try:
        from wafer_map_view import retag_bench_recipes
        retag_bench_recipes(old_name, new_name)
    except Exception:
        pass


def set_instrument(bench: str, key: str, *, name: str = None,
                   address: str = None, timeout_ms: int = None,
                   model: str = None, fitted: bool = None) -> None:
    data = load()
    probers = data.get("probers") or {}
    if bench not in probers:
        raise KeyError(f"no Accretech profile named {bench!r}")
    inst = probers[bench].setdefault("instruments", {})
    if key not in inst:
        raise KeyError(f"{key!r} is not a slot on Accretech bench {bench!r} - "
                       "use add_instrument for a new one")
    if model is not None and model not in model_choices_for(key):
        raise ValueError(f"{model!r} is not a valid model for {key!r} "
                         f"(expected one of {model_choices_for(key)})")
    entry = inst[key]
    if name is not None:
        entry["name"] = name
    if address is not None:
        entry["address"] = address
    if timeout_ms is not None:
        entry["timeout_ms"] = int(timeout_ms)
    if model is not None:
        entry["model"] = model
    if fitted is not None:
        entry["fitted"] = bool(fitted)
    _save(data)


def add_instrument(bench: str, display_name: str, *, address: str = "",
                   timeout_ms: int = 3000, fitted: bool = True) -> str:
    display_name = (display_name or "").strip()
    if not display_name:
        raise ValueError("instrument name cannot be blank")
    data = load()
    probers = data.get("probers") or {}
    if bench not in probers:
        raise KeyError(f"no Accretech profile named {bench!r}")
    inst = probers[bench].setdefault("instruments", {})
    base_key = _slugify_key(display_name)
    key = base_key
    n = 2
    while key in inst:
        key = f"{base_key}_{n}"
        n += 1
    inst[key] = {"name": display_name, "address": address,
                "timeout_ms": int(timeout_ms), "model": GENERIC_MODEL,
                "fitted": bool(fitted)}
    _save(data)
    return key


def remove_instrument(bench: str, key: str) -> None:
    if key in MANDATORY_KEYS:
        raise ValueError(f"{key!r} is a mandatory Accretech slot and can't "
                         "be removed - mark it not fitted instead.")
    data = load()
    probers = data.get("probers") or {}
    if bench not in probers:
        raise KeyError(f"no Accretech profile named {bench!r}")
    (probers[bench].get("instruments") or {}).pop(key, None)
    _save(data)


def set_active(name: str) -> list:
    data = load()
    if name not in (data.get("probers") or {}):
        raise KeyError(f"no Accretech profile named {name!r}")
    data["active"] = name
    _save(data)
    return apply_to_instruments_yaml(name)


def summary(name: str = None) -> str:
    name = name or active_name()
    lines = [f"{name}: {label(name)}"]
    inst = instruments(name)
    for key in all_keys(name):
        entry = inst.get(key)
        if not entry:
            continue
        fitted_note = "" if entry.get("fitted", True) else "  (not fitted)"
        role = _KEY_LABELS.get(key) or entry.get("name") or key
        lines.append(f"   {role:<14} "
                     f"{entry.get('model', ''):<16} {entry.get('address', '')}{fitted_note}")
    return "\n".join(lines)
