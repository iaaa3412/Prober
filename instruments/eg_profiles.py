"""Per-bench instrument profiles for the Electroglas probers."""

import yaml

from instruments.gpib_base import get_machine_config_path

_PROFILES_FILE = "eg_probers.yaml"

EG_KEYS = ("prober_eg", "smu_eg", "dmm_eg", "dmm_vxi_eg",
           "relay1_eg", "relay2_eg", "relay3_eg", "power_supply_eg")


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
        raise KeyError(f"no Electroglas profile named {name!r} "
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


def is_fitted(key: str, name: str = None) -> bool:
    entry = instruments(name).get(key)
    return bool(entry and entry.get("fitted", True))


def fitted_keys(name: str = None) -> list:
    inst = instruments(name)
    return [k for k in EG_KEYS if k in inst and inst[k].get("fitted", True)]


def roster(name: str = None) -> list:
    inst = instruments(name)
    out = []
    for key in EG_KEYS:
        entry = inst.get(key)
        if not entry:
            continue
        out.append((entry.get("name", key),
                    key,
                    tuple(entry.get("id_queries") or ()),
                    bool(entry.get("fitted", True)),
                    entry.get("write_probe")))
    return out


def apply_to_instruments_yaml(name: str = None) -> list:
    name = name or active_name()
    inst = instruments(name)
    yaml_path = get_machine_config_path("instruments.yaml")
    with open(yaml_path, "r", encoding="utf-8") as fh:
        live = yaml.safe_load(fh) or {}
    live.setdefault("instruments", {})

    changed = []
    for key in EG_KEYS:
        entry = inst.get(key)
        if not entry:
            continue
        want = {"name": entry.get("name", key),
                "protocol": "GPIB",
                "address": entry["address"],
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
        raise KeyError(f"no Electroglas profile named {source!r}")
    probers[new_name] = copy.deepcopy(probers[source])
    probers[new_name]["label"] = new_name
    _save(data)


def set_instrument(bench: str, key: str, *, name: str = None,
                   address: str = None, timeout_ms: int = None,
                   fitted: bool = None) -> None:
    if key not in EG_KEYS:
        raise ValueError(f"{key!r} is not a known instrument key "
                         f"(expected one of {EG_KEYS})")
    data = load()
    probers = data.get("probers") or {}
    if bench not in probers:
        raise KeyError(f"no Electroglas profile named {bench!r}")
    inst = probers[bench].setdefault("instruments", {})
    entry = inst.setdefault(key, {"id_queries": []})
    if name is not None:
        entry["name"] = name
    if address is not None:
        entry["address"] = address
    if timeout_ms is not None:
        entry["timeout_ms"] = int(timeout_ms)
    if fitted is not None:
        entry["fitted"] = bool(fitted)
    _save(data)


def remove_instrument(bench: str, key: str) -> None:
    data = load()
    probers = data.get("probers") or {}
    if bench not in probers:
        raise KeyError(f"no Electroglas profile named {bench!r}")
    (probers[bench].get("instruments") or {}).pop(key, None)
    _save(data)


def set_active(name: str) -> list:
    data = load()
    if name not in (data.get("probers") or {}):
        raise KeyError(f"no Electroglas profile named {name!r}")
    data["active"] = name
    _save(data)
    return apply_to_instruments_yaml(name)


def summary(name: str = None) -> str:
    name = name or active_name()
    lines = [f"{name}: {label(name)}"]
    for display, key, _queries, fitted, _probe in roster(name):
        addr = instruments(name)[key]["address"]
        lines.append(f"   {'ok ' if fitted else '-- '} {display:<34} {addr}")
    return "\n".join(lines)
