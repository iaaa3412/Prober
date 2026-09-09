import pyvisa
import yaml
import sys
import os

_OPEN_TIMEOUT_MS = 500

_COLD_OPEN_TIMEOUT_MS = 2500

_POLL_TIMEOUT_MS = 300

_VISA_BACKENDS = (None, "@py")
_rm_cache = {}


def get_resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
        return os.path.join(base_path, relative_path)
    except Exception:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        return os.path.join(project_root, relative_path)


import workdir


def _machine_config_dir():
    return workdir.gui_system_dir()


def get_machine_config_path(filename):
    return os.path.join(_machine_config_dir(), filename)


_ACCRETECH_KEYS = ("prober", "smu", "dmm", "switch_matrix", "wave_gen")
_EG_KEYS = ("prober_eg", "smu_eg", "dmm_eg", "dmm_vxi_eg",
           "relay1_eg", "relay2_eg", "relay3_eg", "power_supply_eg")


def create_default_instruments_yaml() -> bool:
    path = get_machine_config_path("instruments.yaml")
    if os.path.exists(path):
        return False
    os.makedirs(_machine_config_dir(), exist_ok=True)
    data = {"instruments": {
        key: {"name": "", "protocol": "GPIB", "address": "", "timeout_ms": 3000}
        for key in _ACCRETECH_KEYS + _EG_KEYS
    }}
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
    return True


def create_default_eg_probers_yaml() -> bool:
    path = get_machine_config_path("eg_probers.yaml")
    if os.path.exists(path):
        return False
    os.makedirs(_machine_config_dir(), exist_ok=True)
    data = {
        "active": "probe02",
        "probers": {
            "probe02": {
                "label": "probe02",
                "instruments": {
                    key: {"name": "", "address": "", "timeout_ms": 3000,
                         "fitted": True, "id_queries": []}
                    for key in _EG_KEYS
                },
            }
        },
    }
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False,
                       allow_unicode=True, width=100)
    return True


def _resource_manager_for(via):
    if via not in _rm_cache:
        _rm_cache[via] = pyvisa.ResourceManager() if via is None else pyvisa.ResourceManager(via)
    return _rm_cache[via]


def open_resource(address, open_timeout=_OPEN_TIMEOUT_MS):
    errors = []
    is_gpib = address.strip().upper().startswith("GPIB")
    default_ok = False
    for via in _VISA_BACKENDS:
        if via == "@py" and is_gpib and default_ok:
            continue
        try:
            rm = _resource_manager_for(via)
        except Exception as e:
            errors.append((via, f"backend unavailable: {e}"))
            continue
        if via is None:
            default_ok = True
        for timeout in (open_timeout, max(_COLD_OPEN_TIMEOUT_MS, open_timeout * 4)):
            try:
                inst = rm.open_resource(address, open_timeout=timeout)
                return inst, (via or "default")
            except Exception as e:
                last_error = str(e)
        errors.append((via, last_error))

    primary = next((msg for via, msg in errors if via is None and "backend unavailable" not in msg), None)
    if primary:
        raise RuntimeError(f"Could not open {address!r}: {primary}")
    raise RuntimeError(f"Could not open {address!r}. Tried: " +
                       "; ".join(f"{via or 'default'}: {msg}" for via, msg in errors))


class GPIBInstrument:
    def __init__(self, config_key):
        self.address = None
        self.timeout = 3000
        self.inst = None

        yaml_path = get_machine_config_path("instruments.yaml")
        try:
            with open(yaml_path, "r") as file:
                config = yaml.safe_load(file) or {}
            inst_data = (config.get("instruments") or {}).get(config_key)
            if not inst_data or not inst_data.get("address"):
                raise ValueError(f"Instrument '{config_key}' not configured yet.")
            self.address = inst_data["address"]
            self.timeout = inst_data.get("timeout_ms", 3000)
        except (OSError, ValueError) as e:
            print(f"[{config_key.upper()}] FAILED to connect: {e}")
            return

        try:
            self.inst, via = open_resource(self.address)
            self.inst.timeout = self.timeout
            self.inst.encoding = "latin-1"
            print(f"[{config_key.upper()}] Connected successfully at {self.address} (via {via})")
        except Exception as e:
            print(f"[{config_key.upper()}] FAILED to connect: {e}")
            self.inst = None

    def is_present(self) -> bool:
        if not self.inst:
            return False
        previous = self.inst.timeout
        try:
            self.inst.timeout = _POLL_TIMEOUT_MS
            self.inst.read_stb()
            return True
        except Exception:
            return False
        finally:
            try:
                self.inst.timeout = previous
            except Exception:
                pass

    def write(self, command):
        if self.inst:
            self.inst.write(command)

    def query(self, command):
        if self.inst:
            return self.inst.query(command).strip()
        return None

    def go_to_local(self) -> bool:
        if not self.inst:
            return False
        try:
            self.inst.control_ren(pyvisa.constants.VI_GPIB_REN_ADDRESS_GTL)
            return True
        except Exception:
            pass
        try:
            self.inst.write("SYSTem:LOCal")
            return True
        except Exception as e:
            print(f"[GPIB] go_to_local failed: {e}")
            return False

    def close(self):
        if self.inst:
            self.inst.close()


def load_all_instrument_configs() -> dict:
    yaml_path = get_machine_config_path("instruments.yaml")
    with open(yaml_path, "r") as file:
        config = yaml.safe_load(file)
    return config["instruments"]


def set_instrument_address(config_key: str, address: str) -> None:
    yaml_path = get_machine_config_path("instruments.yaml")
    with open(yaml_path, "r") as file:
        config = yaml.safe_load(file)
    if config_key not in config["instruments"]:
        raise ValueError(f"Instrument '{config_key}' not found in YAML.")
    config["instruments"][config_key]["address"] = address
    with open(yaml_path, "w") as file:
        yaml.safe_dump(config, file, default_flow_style=False, sort_keys=False)


_DEFAULT_ID_QUERIES = ("*IDN?", "ID?")


def list_visa_resources() -> list:
    errors = []
    for via in _VISA_BACKENDS:
        try:
            rm = _resource_manager_for(via)
            return sorted(rm.list_resources("?*"))
        except Exception as e:
            errors.append(f"{via or 'default'}: {e}")
    raise RuntimeError("Could not list VISA resources. Tried: " + "; ".join(errors))


_NON_INSTRUMENT_RESOURCES = ("GPIB0::INTFC", "PXI0::MEMACC")


def _is_measurement_not_identity(text: str) -> bool:
    try:
        float(text.split(",")[0])
        return True
    except ValueError:
        return False


def discover_bus(timeout_ms: int = 600) -> list:
    found = []
    for address in list_visa_resources():
        if address in _NON_INSTRUMENT_RESOURCES or address.upper().startswith("ASRL"):
            continue
        try:
            inst, _ = open_resource(address)
        except Exception:
            continue
        try:
            inst.timeout = _POLL_TIMEOUT_MS
            try:
                inst.read_stb()
            except Exception:
                continue

            inst.timeout = timeout_ms
            identity = ""
            for query in _DEFAULT_ID_QUERIES:
                try:
                    response = (inst.query(query) or "").strip()
                except Exception:
                    continue
                if response and not _is_measurement_not_identity(response):
                    identity = response
                    break

            detail = []
            if "SWITCHBOX" in identity.upper():
                for slot in range(1, 5):
                    try:
                        card = (inst.query(f"SYST:CTYP? {slot}") or "").strip()
                    except Exception:
                        break
                    if card and not card.upper().startswith("NONE"):
                        detail.append(f"card {slot}: {card}")

            found.append({"address": address, "identity": identity, "detail": detail})
        finally:
            try:
                inst.close()
            except Exception:
                pass
    return found


def ping_address(address: str, timeout_ms: int = 1000,
                 id_queries=_DEFAULT_ID_QUERIES, write_probe=None) -> tuple:
    is_gpib = address.strip().upper().startswith("GPIB")
    try:
        inst, via = open_resource(address)
    except Exception as e:
        msg = str(e)
        if "VI_ERROR_RSRC_NFOUND" in msg:
            return False, "not present on the bus (VISA does not see this address)"
        return False, msg
    try:
        inst.timeout = min(timeout_ms, _POLL_TIMEOUT_MS)

        polled = False
        if hasattr(inst, "read_stb"):
            try:
                inst.read_stb()
                polled = True
            except Exception:
                if is_gpib:
                    return False, "no device at this address (no answer to serial poll)"

        inst.timeout = timeout_ms

        if write_probe is not None:
            try:
                inst.write(write_probe)
            except Exception:
                return False, ("answers a serial poll but refuses every command — "
                               "GPIB interface alive, instrument not servicing the "
                               "bus (host/remote control not enabled?)")

        for query in id_queries:
            try:
                resp = (inst.query(query) or "").strip()
            except Exception:
                continue
            if resp:
                return True, resp

        if polled:
            return True, f"present via {via} - responds to serial poll, no ID string"
        return True, f"opened via {via} - presence not verified"
    finally:
        try:
            inst.close()
        except Exception:
            pass


def send_raw_command(address: str, command: str, timeout_ms: int = 1000) -> str:
    inst, via = open_resource(address)
    try:
        inst.timeout = timeout_ms
        stripped = command.strip()
        if stripped.startswith("?") or stripped.endswith("?"):
            return inst.query(command).strip()
        inst.write(command)
        return f"Write sent via {via} - no response expected"
    finally:
        try:
            inst.close()
        except Exception:
            pass
