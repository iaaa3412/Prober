import json
import os

import workdir


def _settings_dir() -> str:
    return workdir.gui_system_dir()


def _settings_path() -> str:
    return os.path.join(_settings_dir(), "app_settings.json")


def load_settings() -> dict:
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    os.makedirs(_settings_dir(), exist_ok=True)
    with open(_settings_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _this_machine(data: dict) -> dict:
    return data.setdefault("by_computer", {}).setdefault(workdir.computer_name(), {})


def machine_config_status() -> dict:
    from instruments import gpib_base
    import switch_topology
    return {
        "folder": os.path.isdir(_settings_dir()),
        "app_settings.json": os.path.isfile(_settings_path()),
        "instruments.yaml": os.path.isfile(
            gpib_base.get_machine_config_path("instruments.yaml")),
        "eg_probers.yaml": os.path.isfile(
            gpib_base.get_machine_config_path("eg_probers.yaml")),
        "accretech_probers.yaml": os.path.isfile(
            gpib_base.get_machine_config_path("accretech_probers.yaml")),
        "switch_topology.yaml": os.path.isfile(switch_topology.TOPOLOGY_PATH),
    }


def create_basic_machine_config() -> list:
    from instruments import gpib_base
    import switch_topology
    os.makedirs(_settings_dir(), exist_ok=True)
    created = []
    if not os.path.isfile(_settings_path()):
        save_settings({})
        created.append("app_settings.json")
    if gpib_base.create_default_instruments_yaml():
        created.append("instruments.yaml")
    if gpib_base.create_default_eg_probers_yaml():
        created.append("eg_probers.yaml")
    from instruments import accretech_profiles
    if accretech_profiles.ensure_default_file():
        created.append("accretech_probers.yaml")
    if switch_topology.ensure_default_file():
        created.append("switch_topology.yaml")
    return created


def get_default_ata_folder() -> "str | None":
    return _this_machine(load_settings()).get("default_ata_folder")


def set_default_ata_folder(folder: str) -> None:
    data = load_settings()
    _this_machine(data)["default_ata_folder"] = folder
    save_settings(data)


def get_default_prober() -> "tuple[str, str] | tuple[None, None]":
    entry = _this_machine(load_settings()).get("default_prober") or {}
    system, bench = entry.get("system"), entry.get("bench")
    return (system, bench) if system else (None, None)


def set_default_prober(system: str, bench: str) -> None:
    data = load_settings()
    _this_machine(data)["default_prober"] = {"system": system, "bench": bench}
    save_settings(data)


def clear_default_prober() -> None:
    data = load_settings()
    _this_machine(data).pop("default_prober", None)
    save_settings(data)


def get_default_gui_mode() -> str:
    return _this_machine(load_settings()).get("default_gui_mode") or "normal"


def set_default_gui_mode(mode: str) -> None:
    data = load_settings()
    _this_machine(data)["default_gui_mode"] = mode
    save_settings(data)


def get_channel_assignments(bench: str) -> dict:
    data = load_settings().get("channel_assignments", {})
    return dict(data.get(str(bench), {}))


def set_channel_assignments(bench: str, assignments: dict) -> None:
    data = load_settings()
    store = data.setdefault("channel_assignments", {})
    store[str(bench)] = {k: v.strip() for k, v in assignments.items()
                         if (v or "").strip()}
    save_settings(data)


def set_channel_assignment(bench: str, key: str, label: str) -> None:
    current = get_channel_assignments(bench)
    if (label or "").strip():
        current[key] = label.strip()
    else:
        current.pop(key, None)
    set_channel_assignments(bench, current)
