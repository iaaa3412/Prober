import pyvisa
import yaml
import sys
import os

# Opening a resource is a local driver call, not a round trip to the
# instrument, so it has no reason to take long. Presence is established
# afterwards by a serial poll.
_OPEN_TIMEOUT_MS = 500

# Fallback window for an address the driver has not opened before. See the
# retry in open_resource() for why a cold open needs more than the warm one.
_COLD_OPEN_TIMEOUT_MS = 2500

# How long a serial poll waits before calling an address empty. A serial poll
# is answered by the instrument's GPIB interface hardware, not its firmware, so
# anything powered and on the bus replies in microseconds even while it is busy
# executing a command - this only bounds what a *dead* address costs.
#
# Measured on the ADLINK USB-3488A: the driver rounds this up to its own 1s
# quantum, so 100 and 300 both produce a ~1.0s wait on a dead address and
# lowering it further buys nothing. It still bounds non-GPIB resources, where
# the value is honoured as written.
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


# Real per-machine setup - which instrument is at which GPIB address, which
# Electroglas bench is active, how the switch matrix is wired - lives outside
# the repo, next to app_settings.json (see gui/app_settings.py), not under
# instruments/. It describes THIS machine, not the program, so it has no
# business being committed/pushed/reverted along with the code.
#
# "GUI System" now lives inside whichever working directory is active (see
# gui/workdir.py) rather than at one fixed local path, since it's a shared
# network folder several computers can have open at once.
import workdir


def _machine_config_dir():
    return workdir.gui_system_dir()


def get_machine_config_path(filename):
    return os.path.join(_machine_config_dir(), filename)


# Duplicated from instruments.eg_profiles.EG_KEYS/gui.accretech_setup_panel's
# _ACCRETECH_KEYS rather than imported - eg_profiles imports FROM this module,
# so importing it back here would be circular, and accretech_setup_panel is a
# gui/ (Tk) module this low-level driver layer shouldn't need. Keep the three
# lists in step by hand if a key is ever added.
_ACCRETECH_KEYS = ("prober", "smu", "dmm", "switch_matrix", "wave_gen")
_EG_KEYS = ("prober_eg", "smu_eg", "dmm_eg", "dmm_vxi_eg",
           "relay1_eg", "relay2_eg", "relay3_eg", "power_supply_eg")


def create_default_instruments_yaml() -> bool:
    """First-run scaffold - every known instrument key present with a blank
    name/address, so GPIBInstrument always finds its key instead of crashing
    on a config file that was never written. No real address is guessed;
    that's what the Setup tab is for. Returns False if the file already
    existed (left untouched)."""
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
    """First-run scaffold - one blank starter bench (probe02) with every
    known EG instrument key present but unfilled, so the Setup tab and bench
    picker have something to show and duplicate from instead of an empty
    probers dict. Returns False if the file already existed."""
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
        # pyvisa-py cannot drive GPIB at all without gpib-ctypes. Once a vendor
        # VISA has loaded and given its verdict on a GPIB address, retrying
        # through pyvisa-py only costs seconds and ends in a misleading
        # "install gpib-ctypes" message. Non-GPIB resources still get the
        # fallback, and so does a machine with no vendor VISA at all.
        if via == "@py" and is_gpib and default_ok:
            continue
        try:
            rm = _resource_manager_for(via)
        except Exception as e:
            errors.append((via, f"backend unavailable: {e}"))
            continue
        if via is None:
            default_ok = True
        # The ADLINK driver needs longer for the FIRST open of an address than
        # for later ones, and when it does not get it the failure is an access
        # violation out of the DLL, not a clean VISA error. Measured on this
        # bench: GPIB0::23 failed twice at 500 ms, succeeded at 2000 ms, then
        # succeeded at 500 ms every time after - the address had been warmed.
        # So a failure is retried once with a generous window before the
        # address is written off as absent.
        for timeout in (open_timeout, max(_COLD_OPEN_TIMEOUT_MS, open_timeout * 4)):
            try:
                inst = rm.open_resource(address, open_timeout=timeout)
                return inst, (via or "default")
            except Exception as e:
                last_error = str(e)
        errors.append((via, last_error))

    # When the vendor VISA loaded and gave a verdict, that verdict is the whole
    # story - appending pyvisa-py's "install gpib-ctypes" advice on top of it
    # just buries the real reason on a bench where NI-VISA owns the GPIB board.
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

        # A missing/incomplete instruments.yaml (fresh machine, GUI System
        # folder declined at startup, key not filled in on Setup yet) is a
        # "not connected" instrument, same as one that's simply unplugged -
        # not a reason to crash the whole app.
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
        """Serial-poll the instrument to confirm hardware is really answering.

        The open_resource() in __init__ is not proof of a link: VISA hands back
        a session for any GPIB address in its resource table whether or not the
        instrument is powered on. A serial poll needs no command support, so it
        works even on pre-SCPI instruments.
        """
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
        """Release remote control so the instrument's own front-panel keys
        work again, without closing the VISA session (a later write/query
        just re-asserts remote automatically, same as any other command).

        Real GPIB resources support control_ren(); this sends GTL to just
        this address rather than deasserting REN on the whole bus, so other
        instruments on the same GPIB line stay under remote control. USB488
        instruments (the DMM) have no control_ren() at all in pyvisa, so
        fall back to the SCPI command every one of them accepts for this -
        SYSTem:LOCal is meaningless to the TSP-speaking SMU/switch and the
        prober's own ASCII protocol, so only try it as a fallback, not
        instead of control_ren.
        """
        if not self.inst:
            return False
        try:
            # ADDRESS_GTL (6), not DEASSERT_GTL (2). Both send Go To Local,
            # but DEASSERT_GTL then drops the REN line for the WHOLE BUS -
            # which is exactly what the docstring above and
            # instrument_panel._release_all_to_local both promise not to do.
            # On this bench that would pull the 3458A, the Keithley 2400 and
            # all three switchboxes out of remote alongside the prober,
            # including mid-run. ADDRESS_GTL addresses only this instrument.
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


# Not instruments: the GPIB board itself and the PXI memory-access resource.
_NON_INSTRUMENT_RESOURCES = ("GPIB0::INTFC", "PXI0::MEMACC")


def _is_measurement_not_identity(text: str) -> bool:
    """True if `text` is a reading that leaked out of an instrument's buffer.

    A free-running 3458A answers a query it does not understand with whatever
    reading is sitting in its output buffer, so probing it with *IDN? yields
    something like '8.201751693E+00'. That is a measurement, not an identity,
    and listing it as one is worse than reporting no ID at all.
    """
    try:
        float(text.split(",")[0])
        return True
    except ValueError:
        return False


def discover_bus(timeout_ms: int = 600) -> list:
    """Identify every instrument currently answering on the bus.

    The Electroglas probers are not all fitted the same way, so the panel needs
    to report what is actually plugged in rather than assume the roster in
    instruments.yaml. Returns a list of dicts with 'address', 'identity' and
    'detail' keys.

    Read-only throughout: a serial poll, then ID queries, then - for an HP
    switchbox - SYST:CTYP? to name the relay cards it holds. Nothing here
    changes an instrument setting or moves a relay.
    """
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

            # Short timeout: this sweeps every address on the bus and most of
            # them will not answer, so a generous per-query wait turns the scan
            # into a coffee break. Any instrument that answers an ID query at
            # all answers it well inside this.
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
                # Each switchbox is a group of plug-in cards in the E1300A
                # mainframe, and the EG benches hold different ones - this is
                # what tells relay1 (an E1343A multiplexer) apart from relay2
                # and relay3 (E1364A form C switches).
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
    """Report whether a real instrument is answering at `address`.

    Opening the resource proves nothing on GPIB: VISA hands back a session for
    any address in its resource table whether or not hardware is listening.
    Confirmed on this bench - GPIB0::24::INSTR (Keithley 2400, powered off)
    opens cleanly, so the old open-plus-*IDN? check reported it connected.

    A GPIB serial poll is the real presence test. It needs no command support
    from the instrument, so it also covers pre-SCPI gear like the Electroglas
    2001X, which answers no ID query at all (ADLINK's own bus scan lists it as
    "Unknown instrument (PA:29)"). Pass id_queries=() for those instruments so
    ping never writes an unrecognised command at them.

    A serial poll is answered by the GPIB interface chip itself, though, so it
    can succeed on an instrument whose software is not servicing the bus at
    all - exactly what the 2001X does here. Pass write_probe with a harmless
    query string to also confirm the instrument accepts a command byte.
    """
    is_gpib = address.strip().upper().startswith("GPIB")
    try:
        inst, via = open_resource(address)
    except Exception as e:
        msg = str(e)
        # VISA's wording for this is a paragraph long; in a status label next to
        # the address, "not on the bus" is the part that matters.
        if "VI_ERROR_RSRC_NFOUND" in msg:
            return False, "not present on the bus (VISA does not see this address)"
        return False, msg
    try:
        # A live instrument answers a serial poll almost instantly; only a dead
        # address burns the whole window, so keep that window short and save
        # the caller's full timeout for the ID query that follows.
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
