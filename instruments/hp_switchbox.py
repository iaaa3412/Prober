"""HP/Agilent relay cards in the E1300A mainframe, addressed as switchboxes."""

from instruments.gpib_base import GPIBInstrument

CHANNELS = tuple(range(16))

FAMILY_MUX = "mux"
FAMILY_FORM_C = "formc"
FAMILY_UNKNOWN = "unknown"

_MUX_MODELS = ("E1343", "E1344", "E1345", "E1346", "E1347")
_FORM_C_MODELS = ("E1364", "E1365", "E1366", "E1367")


def card_family(card_type: str) -> str:
    text = (card_type or "").upper()
    if any(m in text for m in _MUX_MODELS):
        return FAMILY_MUX
    if any(m in text for m in _FORM_C_MODELS):
        return FAMILY_FORM_C
    return FAMILY_UNKNOWN


TREE_AT = 90
TREE_BT = 91
TREE_AT2 = 92
TREE_SWITCHES = (TREE_AT, TREE_BT, TREE_AT2)

TREE_LABELS = {TREE_AT: "AT  Bank 0 -> analog bus H/L/G (sense)",
               TREE_BT: "BT  Bank 1 -> analog bus I+/I-/IG (source)",
               TREE_AT2: "AT2 Bank 1 -> AT terminals"}

BANK0 = tuple(range(0, 8))
BANK1 = tuple(range(8, 16))


def bank_of(channel: int) -> int:
    return 0 if int(channel) in BANK0 else 1


def fres_partner(channel: int) -> int:
    ch = int(channel)
    return ch + 8 if ch in BANK0 else ch - 8

_LATCHING = True

MAX_VOLTAGE_DC = 250
MAX_CURRENT_A = 1.0
CHANNEL_SETTLE_S = 0.015


COAX_OF_CHANNEL = {0: 3, 1: 4, 2: 5, 3: 6, 8: 7, 9: 8, 10: 9, 11: 10}

NODE_OF_CHANNEL = {
    0: "IN_HI", 2: "IN_HI",
    1: "IN_LO", 3: "IN_LO",
    8: "CUR_HI", 10: "CUR_HI",
    9: "CUR_LO", 11: "CUR_LO",
}

NODE_LABELS = {
    "IN_HI": "Input HI (sense +)",
    "IN_LO": "Input LO (sense -)",
    "CUR_HI": "Current HI (source +)",
    "CUR_LO": "Current LO (source -)",
}

POLARITY_OF_CHANNEL = {ch: ("HI" if node.endswith("HI") else "LO")
                       for ch, node in NODE_OF_CHANNEL.items()}

DIE_SETS = {
    1: (0, 1),
    2: (2, 3),
    3: (8, 9),
    4: (10, 11),
}

CONFLICT_GROUPS = ((0, 2, 8, 10), (1, 3, 9, 11))

GROUND_TERMINAL_CHANNEL = 15

UNWIRED_CHANNELS = tuple(c for c in CHANNELS if c not in COAX_OF_CHANNEL)


def describe_channel(channel: int) -> str:
    channel = int(channel)
    if channel == GROUND_TERMINAL_CHANNEL:
        return "CH15 - NC terminal is the ground entry for the NC bus; not switched"
    coax = COAX_OF_CHANNEL.get(channel)
    if coax is None:
        return f"CH{channel:02d} - not wired on probe03"
    return (f"CH{channel:02d} - coax {coax} -> {NODE_LABELS[NODE_OF_CHANNEL[channel]]} "
            f"[die {die_of_channel(channel)} {POLARITY_OF_CHANNEL[channel]}] "
            f"(open = coax {coax} grounded)")


def die_of_channel(channel: int):
    for die, channels in DIE_SETS.items():
        if int(channel) in channels:
            return die
    return None


def conflicts_with(channel: int, already_closed) -> list:
    channel = int(channel)
    side = POLARITY_OF_CHANNEL.get(channel)
    if side is None:
        return []
    return [c for c in already_closed
            if int(c) != channel and POLARITY_OF_CHANNEL.get(int(c)) == side]


BENCH_WIRING = {
    "probe03": {
        "driver_key": "relay2_eg",
        "card_type": "E1364A",
        "family": FAMILY_FORM_C,
        "wires_per_die": 2,
        "die_sets": DIE_SETS,
        "coax_of_channel": COAX_OF_CHANNEL,
        "node_of_channel": NODE_OF_CHANNEL,
        "conflict_groups": CONFLICT_GROUPS,
        "ground_channel": GROUND_TERMINAL_CHANNEL,
        "uses_analog_bus": False,
        "instrument": "HP 3458A / E1326B at the card's terminals",
        "summary": ("Form C SPDT, 8 wired channels: two per die (one HI, one LO) "
                    "for the four dies of a 2x2 shot. Open grounds the pin via "
                    "the chained NC bus."),
        "evidence": "transcribed from the physical wiring (references/probe03mapping)",
    },
    "probe02": {
        "driver_key": "relay1_eg",
        "card_type": "E1345A",
        "family": FAMILY_MUX,
        "wires_per_die": 1,
        "die_sets": {1: (0,), 2: (1,), 3: (2,), 4: (3,)},
        "coax_of_channel": {},
        "node_of_channel": {},
        "conflict_groups": (),
        "ground_channel": None,
        "uses_analog_bus": False,
        "instrument": ("Keithley 2400 SMU, rear IN/OUT HI/LO into the card's "
                       "DIRECT voltage-sense terminals (the bank common, not "
                       "the analog bus - which is why the tree switches make "
                       "no difference), 2-wire (SYST:RSEN OFF)"),
        "summary": ("16-channel relay multiplexer, 4 wired channels: CH00-CH03, "
                    "one per die of a 2x2 shot, each switching a HI/LO pair. "
                    "CH04-CH15 unwired. Tree switches unused."),
        "evidence": ("measured on the bench 2026-08-10 (see module header); "
                     "probe-card pin mapping and the 8-wire harness confirmed "
                     "with the operator 2026-08-12, see references/"
                     "SWITCHBOX_REPORT.txt section 8C"),
        "die_pins": {1: ("A32", "A33"), 2: ("A34", "A36"),
                     3: ("A13", "A12"), 4: ("A11", "A9")},
    },
    "Probe03New": {
        "driver_key": "relay1_eg",
        "card_type": "E1345A",
        "family": FAMILY_MUX,
        "wires_per_die": 1,
        "die_sets": {1: (0,), 2: (1,), 3: (2,), 4: (3,)},
        "coax_of_channel": {},
        "node_of_channel": {},
        "conflict_groups": (),
        "ground_channel": None,
        "uses_analog_bus": False,
        "instrument": ("Keithley 2400 SMU, rear IN/OUT HI/LO into the card's "
                       "DIRECT voltage-sense terminals, 2-wire "
                       "(SYST:RSEN OFF) - as probe02"),
        "summary": ("16-channel relay multiplexer, 4 wired channels: CH00-CH03, "
                    "one per die of a 2x2 shot, each switching a HI/LO pair. "
                    "CH04-CH15 unwired."),
        "evidence": ("instrument ADDRESSES measured on this bench 2026-09-07 "
                     "(*IDN? on every one, plus VXI:CONF:DLAD?); the WIRING is "
                     "assumed identical to probe02 on the operator's word, not "
                     "measured here. Card type per address is likewise carried "
                     "over from probe02 - *IDN? reports only 'SWITCHBOX' for "
                     "all three cards and SYST:CTYP? did not answer."),
        "die_pins": {1: ("A32", "A33"), 2: ("A34", "A36"),
                     3: ("A13", "A12"), 4: ("A11", "A9")},
    },
}

LAMP_SWITCH_OF_CHANNEL = {0: 1, 1: 2, 2: 3, 3: 4}


def bench_wiring(name: str) -> dict:
    return BENCH_WIRING.get(str(name), {
        "driver_key": "", "card_type": "", "family": FAMILY_UNKNOWN,
        "wires_per_die": 0, "die_sets": {}, "coax_of_channel": {},
        "node_of_channel": {}, "conflict_groups": (), "ground_channel": None,
        "uses_analog_bus": False, "instrument": "",
        "summary": "No wiring recorded for this bench yet.", "evidence": "",
    })


def wired_channels(name: str) -> tuple:
    wiring = bench_wiring(name)
    out = []
    for chans in wiring["die_sets"].values():
        out.extend(chans)
    return tuple(sorted(set(out)))


def wired_pin_labels(name: str) -> tuple:
    wiring = bench_wiring(name)
    pins = set()
    for pair in (wiring.get("die_pins") or {}).values():
        pins.update(str(p) for p in pair if p)
    return tuple(sorted(pins))


def die_of_channel_on(name: str, channel: int):
    for die, chans in bench_wiring(name)["die_sets"].items():
        if int(channel) in chans:
            return die
    return None


def describe_channel_on(name: str, channel: int) -> str:
    channel = int(channel)
    wiring = bench_wiring(name)
    if name == "probe03":
        return describe_channel(channel)
    die = die_of_channel_on(name, channel)
    if die is None:
        return f"CH{channel:02d} - not wired on {name}"
    sw = LAMP_SWITCH_OF_CHANNEL.get(channel)
    tail = f"  (LaMP switch {sw})" if sw else ""
    return (f"CH{channel:02d} - die {die} of the 2x2 shot, HI+LO pair "
            f"-> {wiring['instrument'].split(',')[0]}{tail}")


def _chan_spec(channel, card: int = 1) -> str:
    return f"(@{int(card):02d}{int(channel):02d})"


class HPSwitchbox(GPIBInstrument):

    def __init__(self, config_key: str, card: int = 1):
        super().__init__(config_key)
        self.card = card

    def get_id(self) -> str:
        return self.query("*IDN?") or ""

    def card_type(self, slot: int = 1) -> str:
        return self.query(f"SYST:CTYP? {int(slot)}") or ""

    def cards(self, slots=range(1, 5)) -> list:
        out = []
        for slot in slots:
            try:
                card = (self.card_type(slot) or "").strip()
            except Exception:
                break
            if card and not card.upper().startswith("NONE"):
                out.append((slot, card))
        return out

    def error(self) -> str:
        return self.query("SYST:ERR?") or ""

    def drain_errors(self, limit: int = 10) -> list:
        found = []
        for _ in range(limit):
            entry = self.error()
            if not entry or entry.strip().startswith(("+0,", "0,")):
                break
            found.append(entry)
        return found


    def close_channel(self, channel):
        self.write(f"CLOS {_chan_spec(channel, self.card)}")

    def open_channel(self, channel):
        self.write(f"OPEN {_chan_spec(channel, self.card)}")

    def open_all(self):
        self.write("*RST")

    def read_channel(self, channel) -> bool:
        resp = self.query(f"CLOS? {_chan_spec(channel, self.card)}")
        return str(resp).strip() == "1"

    def closed_channels(self, channels=CHANNELS) -> list:
        return [c for c in channels if self.read_channel(c)]

    def channel_states(self, channels=CHANNELS) -> dict:
        return {c: self.read_channel(c) for c in channels}


    def close_only(self, channel, verify: bool = True) -> bool:
        self.open_all()
        self.close_channel(channel)
        if not verify:
            return True
        return self.read_channel(channel)

    def close_set(self, channels, verify: bool = True, guard: bool = True) -> dict:
        channels = [int(c) for c in channels]
        if guard:
            for i, ch in enumerate(channels):
                clash = conflicts_with(ch, channels[i + 1:])
                if clash:
                    raise ValueError(
                        f"CH{ch:02d} and CH{clash[0]:02d} are both "
                        f"{POLARITY_OF_CHANNEL[ch]} side; closing both shorts "
                        f"coax {COAX_OF_CHANNEL[ch]} to coax {COAX_OF_CHANNEL[clash[0]]}")
        self.open_all()
        for ch in channels:
            self.close_channel(ch)
        if not verify:
            return {c: True for c in channels}
        return {c: self.read_channel(c) for c in channels}


    def family(self, slot: int = None) -> str:
        return card_family(self.card_type(slot or self.card))

    def scan_port(self) -> str:
        return (self.query("ROUT:SCAN:PORT?") or "").strip()

    def set_scan_port(self, port: str = "ABUS"):
        self.write(f"ROUT:SCAN:PORT {port}")

    def tree_states(self) -> dict:
        return {t: self.read_channel(t) for t in TREE_SWITCHES}

    def mux_states(self) -> dict:
        states = self.channel_states()
        states.update(self.tree_states())
        return states

    def close_2wire(self, channel: int, verify: bool = True) -> dict:
        ch = int(channel)
        if ch not in BANK0:
            raise ValueError(
                f"channel {ch:02d} is in Bank 1; 2-wire selection goes through "
                f"the AT tree switch, which serves Bank 0 (00-07). Close "
                f"AT2 ({TREE_AT2}) explicitly if you meant to bring Bank 1 to "
                "the AT terminals.")
        self.open_all()
        self.close_channel(TREE_AT)
        self.close_channel(ch)
        if not verify:
            return {TREE_AT: True, ch: True}
        return {TREE_AT: self.read_channel(TREE_AT), ch: self.read_channel(ch)}

    def close_4wire(self, channel: int, verify: bool = True) -> dict:
        sense = int(channel)
        if sense not in BANK0:
            sense = fres_partner(sense)
        source = fres_partner(sense)
        wanted = (TREE_AT, TREE_BT, sense, source)
        self.open_all()
        for c in wanted:
            self.close_channel(c)
        if not verify:
            return {c: True for c in wanted}
        return {c: self.read_channel(c) for c in wanted}

    def route_die(self, die: int, verify: bool = True) -> dict:
        key = int(die)
        if key not in DIE_SETS:
            raise KeyError(f"die {die!r} is not on this shot (known: {sorted(DIE_SETS)})")
        return self.close_set(DIE_SETS[key], verify=verify)

    def verify_wiring_assumptions(self) -> list:
        problems = []
        card = (self.card_type(self.card) or "").strip()
        if card and "E1364" not in card.upper():
            problems.append(f"slot {self.card} holds {card}, not an E1364A - "
                            "probe03mapping describes an E1364A form C switch")
        self.drain_errors()
        for probe in (0, 15):
            try:
                self.read_channel(probe)
            except Exception as e:
                problems.append(f"CLOS? on CH{probe:02d} failed: {e}")
                continue
            err = self.error()
            if err and not err.strip().startswith(("+0,", "0,")):
                problems.append(f"CH{probe:02d} rejected by the card: {err}")
        return problems
