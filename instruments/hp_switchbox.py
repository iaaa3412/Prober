"""HP/Agilent relay cards in the E1300A mainframe, addressed as switchboxes.

WHAT IS ACTUALLY ON THE BENCH
-----------------------------
There is ONE physical relay box: an HP 75000 Series B (E1300A) mainframe. It
holds three relay cards, and each card group is published on GPIB as its own
"switchbox" instrument at primary address 9 with a different secondary address.
Three GPIB addresses, one box - they are not three separate chassis:

    GPIB0::9::0    E1300A mainframe itself (the system instrument)
    GPIB0::9::15   SWITCHBOX, card 1: E1343A  16-ch HV relay MULTIPLEXER
    GPIB0::9::10   SWITCHBOX, card 1: E1364A  16-ch form C SWITCH
    GPIB0::9::14   SWITCHBOX, card 1: E1364A  16-ch form C SWITCH

Secondary address is the card's VXI logical address divided by 8, so SA 10/14/15
are logical addresses 80/112/120. (The E1326B manual's own example calls the
switchbox at "secondary address 14" logical address 112 - 112/8 = 14.)

Also in the mainframe, but NOT answering on GPIB: two E1326B 5.5-digit
multimeters, installed internally, broken out to banana terminals by E1326-80005
adapters. A bus scan finds no secondary address for either, so they are fitted
but not configured as instruments in the mainframe's instrument definition.

An E1343A multiplexes many channels onto one common bus; an E1364A is 16
independent form C (SPDT) relays. Do not assume a channel number means the same
thing on both.

CHANNEL NUMBERING is 00-15, and the SCPI channel spec is (@ccnn) - card number
then two-digit channel - per the HP E1343A/44A/45A/47A manual in references/:

    [ROUT:]CLOS  (@ccnn)     close channel nn on card cc
    [ROUT:]OPEN  (@ccnn)     open it
    [ROUT:]CLOS? (@ccnn)     query closure -> "1" closed, "0" open
    *RST                     opens ALL channels
    SYST:ERR?                error queue - use it to prove a channel spec is legal
"""

from instruments.gpib_base import GPIBInstrument

CHANNELS = tuple(range(16))          # E1364A channels are numbered 00-15

# ---------------------------------------------------------------------------
# Card families. Two very different animals answer *IDN? with the SAME string
# ("HEWLETT-PACKARD,SWITCHBOX,0,A.06.00"), so SYST:CTYP? is the only way to
# tell them apart - and the same secondary address holds a different card
# depending on which bench is plugged in.
# ---------------------------------------------------------------------------
FAMILY_MUX = "mux"          # E1343A/44A/45A/47A relay multiplexers
FAMILY_FORM_C = "formc"     # E1364A form C (SPDT) switch
FAMILY_UNKNOWN = "unknown"

_MUX_MODELS = ("E1343", "E1344", "E1345", "E1346", "E1347")
_FORM_C_MODELS = ("E1364", "E1365", "E1366", "E1367")


def card_family(card_type: str) -> str:
    """'HEWLETT-PACKARD,E1345A,0,A.06.00' -> FAMILY_MUX."""
    text = (card_type or "").upper()
    if any(m in text for m in _MUX_MODELS):
        return FAMILY_MUX
    if any(m in text for m in _FORM_C_MODELS):
        return FAMILY_FORM_C
    return FAMILY_UNKNOWN


# --- multiplexer topology (E1343A / E1345A), confirmed on the hardware ------
#
# Each channel carries High, Low and Guard. Channels split into two banks, and
# a closed channel reaches only its bank common - it gets to the outside world
# through a TREE SWITCH, which is itself an addressable channel. Close a
# channel without its tree switch and you have connected nothing, which looks
# exactly like a clean open circuit.
#
#   ch00..07  Bank 0 --[ AT  90 ]-- AT terminals + analog bus H / L / G   sense
#                          |
#                     [ AT2 92 ]
#                          |
#   ch08..15  Bank 1 --[ BT  91 ]-- BT terminals + analog bus I+ / I- / IG source
#
# Every common, tree and analog-bus line carries a 100 ohm series resistor for
# relay protection. It is in any 2-wire measurement - irrelevant at megohms,
# not at single ohms.
TREE_AT = 90            # Bank 0 -> AT terminals / analog bus H,L,G  (sense)
TREE_BT = 91            # Bank 1 -> BT terminals / analog bus I+,I-,IG (source)
TREE_AT2 = 92           # Bank 1 -> AT terminals
TREE_SWITCHES = (TREE_AT, TREE_BT, TREE_AT2)

TREE_LABELS = {TREE_AT: "AT  Bank 0 -> analog bus H/L/G (sense)",
               TREE_BT: "BT  Bank 1 -> analog bus I+/I-/IG (source)",
               TREE_AT2: "AT2 Bank 1 -> AT terminals"}

BANK0 = tuple(range(0, 8))
BANK1 = tuple(range(8, 16))


def bank_of(channel: int) -> int:
    return 0 if int(channel) in BANK0 else 1


def fres_partner(channel: int) -> int:
    """The 4-wire partner of a Bank 0 channel: sense N pairs with source N+8."""
    ch = int(channel)
    return ch + 8 if ch in BANK0 else ch - 8

# From references/hpe1364 manual.pdf, all confirmed against this bench:
#
#   "the Form C Switch consists of 16 channels (channels 00 through 15)"
#   "CLOSe ... to connect the normally open (NO) terminal to the common (C)"
#   "When the relay is open, the NC terminal is connected to the C terminal"
#   "(@ccnn) where cc = switch card number (01-99) and nn = channel numbers"
#   "the module with the lowest logical address is card number 01"
#
# THE RELAYS ARE LATCHING. "Since the relays are latching, the relay remains in
# the last state during power-up or power-down. When a reset occurs, all channel
# commons (C) are connected to the corresponding normally closed (NC) contacts."
#
# So the card powers up holding whatever it was left in - all-open is NOT the
# power-on state, only the post-reset state. Nothing may assume the guarded
# state until open_all() has actually been sent. Call it before trusting
# anything, which is what the GUI panel does on connect.
_LATCHING = True

# Contact ratings, per channel: 250 V dc or 250 V ac peak (177 V ac RMS),
# 1 A dc or ac RMS non-inductive, 30 W or 40 VA. Closure takes about 15 ms,
# so the ceiling on any scan is roughly 50 Hz.
MAX_VOLTAGE_DC = 250
MAX_CURRENT_A = 1.0
CHANNEL_SETTLE_S = 0.015


# ---------------------------------------------------------------------------
# probe03 wiring - transcribed from references/probe03mapping
# ---------------------------------------------------------------------------
# WHAT THE MEASUREMENT IS. The prober lands on a 2x2 shot and the relays then
# select each of the four dies in turn. Two pins per die, eight coax in total.
# The test forces a current and measures the resulting voltage, to verify
# electrical isolation - so a high reading is the pass.
#
# On a form C relay, CLOSE energises the coil and connects Common to NO; OPEN
# de-energises it and connects Common to NC. On this card every Common carries
# one coax to the probe card, every NO goes to a multimeter terminal, and every
# NC is daisy-chained into a single node that reaches ground via CH15 NC.
#
#   CH0 NC - CH1 NC - CH2 NC - CH3 NC - CH11 NC - CH10 NC - CH9 NC - CH8 NC
#            - CH15 NC -> ground (green wires, also soldered to the coax shields)
#
# So an OPEN channel grounds its probe pin. All-open is the guarded safe state,
# not a floating state, and that is what *RST gives you.
#
# The NO side lands on the bottom E1326B's adapter terminals, in four nodes:
#
#   CH0 NO  - CH2 NO  - Input HI     CH8 NO  - CH10 NO - Current HI
#   CH1 NO  - CH3 NO  - Input LO     CH9 NO  - CH11 NO - Current LO
#
# Line that up against the coax numbers and the pattern is exact: the coax run
# 3..10 alternates HI, LO, HI, LO, ... so consecutive coax form four HI/LO
# pairs, one per die. Two of the pairs land on the meter's Input (sense)
# terminals and two on its Current (source) terminals.
#
#   die 1  coax 3 (HI) + coax 4 (LO)    CH00 + CH01    on Input HI / Input LO
#   die 2  coax 5 (HI) + coax 6 (LO)    CH02 + CH03    on Input HI / Input LO
#   die 3  coax 7 (HI) + coax 8 (LO)    CH08 + CH09    on Current HI / Current LO
#   die 4  coax 9 (HI) + coax 10 (LO)   CH10 + CH11    on Current HI / Current LO
#
# OPEN QUESTION - VERIFY BEFORE MEASURING. Two pins per die means the current
# has to be forced down the same pair the voltage is read from, so Input HI and
# Current HI must be commoned, and Input LO with Current LO. Otherwise dies 1-2
# can only be sensed (no current path) and dies 3-4 can only be driven (no
# sense), and no die is measurable. probe03mapping does not record a strap
# between those terminals. Either it exists and was not noted, or the meter
# commons them internally, or the build is missing it. A continuity check at
# the adapter settles it in seconds: Input HI to Current HI, Input LO to
# Current LO.
#
# Note the E1326B has no current-measuring function at all - the Current
# terminals are the internal current SOURCE it uses for resistance. The manual
# is also explicit that measurements taken at the meter's own terminals "must be
# configured as 4-wire" (MEAS:FRES?), because a stand-alone E1326B has no 2-wire
# mode; the strap above is what makes it electrically 2-wire while the command
# stays FRES. Ceiling is 1.048 MOhm, which may not be enough headroom for an
# isolation test - LaMP used a Keithley 2400 SMU for this, not the E1326B.

COAX_OF_CHANNEL = {0: 3, 1: 4, 2: 5, 3: 6, 8: 7, 9: 8, 10: 9, 11: 10}

# channel -> which multimeter terminal its NO contact reaches
NODE_OF_CHANNEL = {
    0: "IN_HI", 2: "IN_HI",       # Input HI    - voltage sense +
    1: "IN_LO", 3: "IN_LO",       # Input LO    - voltage sense -
    8: "CUR_HI", 10: "CUR_HI",    # Current HI  - current source +
    9: "CUR_LO", 11: "CUR_LO",    # Current LO  - current source -
}

NODE_LABELS = {
    "IN_HI": "Input HI (sense +)",
    "IN_LO": "Input LO (sense -)",
    "CUR_HI": "Current HI (source +)",
    "CUR_LO": "Current LO (source -)",
}

# Which side of the die each channel drives. One HI and one LO closed at a time
# is the whole operating rule, and it holds whether or not the Input/Current
# terminals turn out to be strapped.
POLARITY_OF_CHANNEL = {ch: ("HI" if node.endswith("HI") else "LO")
                       for ch, node in NODE_OF_CHANNEL.items()}

# The four dies of the 2x2 shot. Each is exactly two channels: one HI, one LO.
DIE_SETS = {
    1: (0, 1),      # coax 3, 4
    2: (2, 3),      # coax 5, 6
    3: (8, 9),      # coax 7, 8
    4: (10, 11),    # coax 9, 10
}

# Channels that must never be closed together. Two HI channels closed at once
# shorts two probe pins through the HI terminal - same for two LO. This is the
# one way to damage something here, so it is checked rather than trusted.
CONFLICT_GROUPS = ((0, 2, 8, 10), (1, 3, 9, 11))

# CH15's NC terminal is the ground entry point for the whole NC chain. The
# ground wire lands on the terminal, not through the contact, so operating CH15
# does not break the ground bus - but there is no reason to touch it either.
GROUND_TERMINAL_CHANNEL = 15

UNWIRED_CHANNELS = tuple(c for c in CHANNELS if c not in COAX_OF_CHANNEL)


def describe_channel(channel: int) -> str:
    """One line saying what closing this channel actually connects."""
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
    """Which die of the 2x2 shot this channel belongs to, or None."""
    for die, channels in DIE_SETS.items():
        if int(channel) in channels:
            return die
    return None


def conflicts_with(channel: int, already_closed) -> list:
    """Which of `already_closed` clash with `channel`.

    Two channels clash when they drive the same side of the measurement - two
    HI or two LO - because closing both shorts their probe pins together.
    """
    channel = int(channel)
    side = POLARITY_OF_CHANNEL.get(channel)
    if side is None:
        return []
    return [c for c in already_closed
            if int(c) != channel and POLARITY_OF_CHANNEL.get(int(c)) == side]


# ---------------------------------------------------------------------------
# PER-BENCH WIRING
#
# The two benches are wired to different cards in different ways, so the panel
# needs to know which bench it is looking at rather than assuming probe03's
# form-C build. Everything above stays as it was - probe03's constants are the
# probe03 entry here, re-exported so existing callers keep working.
#
# probe02 (RELAY1, E1345A @ 9::15) was mapped ON THE BENCH, 2026-08-10, by
# closing each channel in turn against an all-open reference taken immediately
# before it, forward then reverse, with the Keithley 2400 sourcing 10 V into
# a 1 uA compliance. Channels 0-3 added +101..+155 pA (65-99 GOhm per die) in
# BOTH directions; every other channel stayed inside a +-40 pA noise band and
# six of them changed sign between passes. Tree switches 90/91/92 moved the
# reading by less than the noise, alone or combined with a channel - so the
# SMU is on the card's own terminals and the VXI analog bus is not in the
# path. That matches the original LaMP executable, whose database logs
# fldSwitch cycling 1,2,3,4 per quad against this one card.
# ---------------------------------------------------------------------------

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
        # A mux channel switches a HI/LO PAIR, so one die needs one channel -
        # unlike probe03's form-C card, where a die costs two.
        "wires_per_die": 1,
        "die_sets": {1: (0,), 2: (1,), 3: (2,), 4: (3,)},
        "coax_of_channel": {},
        "node_of_channel": {},
        # Nothing here can damage anything: closing two channels just puts two
        # dies in parallel, which spoils the reading rather than shorting pins.
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
        # HI/LO within a pair is inferred from pin ordering, not measured -
        # the pad pair is symmetric so it cannot be measured from here.
        "die_pins": {1: ("A32", "A33"), 2: ("A34", "A36"),
                     3: ("A13", "A12"), 4: ("A11", "A9")},
    },
    # probe02's measurement chain physically moved onto probe03. Confirmed on
    # the bus 2026-09-07: VXI:CONF:DLAD? returns +0,+24,+72,+112,+120 - byte
    # for byte probe02's recorded backplane - while probe03's own LADDR 80
    # (its wired E1364A) and 56 (its failed E1326B) are gone, and the Keithley
    # 2400 at GPIB 24 answers again. So the RELAY MODEL below is probe02's,
    # not probe03's form-C one: a mux channel switching a HI/LO pair, one
    # channel per die, NOT two.
    #
    # The harness itself was not re-measured on this bench - the operator
    # confirmed 2026-09-07 that it is wired the same as probe02, and that is
    # the whole basis for die_sets/die_pins here. If a needle is ever found on
    # the wrong die, this assumption is the first thing to re-check: nothing
    # downstream can detect it, because a wrong-but-valid channel closes
    # silently and measures a real die, just not the intended one.
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

# Same numbering the original LaMP executable logged in fldSwitch.
LAMP_SWITCH_OF_CHANNEL = {0: 1, 1: 2, 2: 3, 3: 4}


def bench_wiring(name: str) -> dict:
    """Wiring model for a bench, or an empty-but-valid one if unknown."""
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
    """The physical probe-card pins actually landed on this bench's relay
    card, e.g. probe02's ("A9","A11","A12","A13","A32","A33","A34","A36") -
    see BENCH_WIRING["probe02"]["die_pins"] and SWITCHBOX_REPORT.txt 8C.

    The card itself carries a B-side twin of each (B9, B32, ...) that the
    operator confirmed are spare needles, NOT landed on the relay - so they
    are deliberately excluded here rather than assumed usable. Only benches
    with a recorded die_pins table (currently just probe02) return anything;
    everywhere else this is empty, which callers should treat as "no
    restriction known" rather than "nothing is wired".
    """
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
    """What closing this channel connects, for the named bench."""
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
    """(@ccnn) - card number then two-digit channel, per the manual."""
    return f"(@{int(card):02d}{int(channel):02d})"


class HPSwitchbox(GPIBInstrument):
    """One switchbox (a card group at a GPIB secondary address).

    `card` is the card number within the switchbox. Every unit in this mainframe
    reports a card in slot 1 and NONE in 2-4, so 1 is the default.
    """

    def __init__(self, config_key: str, card: int = 1):
        super().__init__(config_key)
        self.card = card

    def get_id(self) -> str:
        return self.query("*IDN?") or ""

    def card_type(self, slot: int = 1) -> str:
        """SYST:CTYP? - which relay card is actually in this slot."""
        return self.query(f"SYST:CTYP? {int(slot)}") or ""

    def cards(self, slots=range(1, 5)) -> list:
        """[(slot, card type)] for every slot that holds a card."""
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
        """SYST:ERR? - one entry off the error queue ('+0,"No error"' if clean)."""
        return self.query("SYST:ERR?") or ""

    def drain_errors(self, limit: int = 10) -> list:
        """Empty the error queue, returning everything that was in it."""
        found = []
        for _ in range(limit):
            entry = self.error()
            if not entry or entry.strip().startswith(("+0,", "0,")):
                break
            found.append(entry)
        return found

    # -- primitives ---------------------------------------------------------

    def close_channel(self, channel):
        self.write(f"CLOS {_chan_spec(channel, self.card)}")

    def open_channel(self, channel):
        self.write(f"OPEN {_chan_spec(channel, self.card)}")

    def open_all(self):
        """*RST - opens every channel.

        On probe03 that is also the guarded state: every open channel sits on
        its NC contact, which is tied to the grounded NC bus. Go here before
        changing routing and when finished.

        Send it at startup too. The relays latch, so the card comes up holding
        whatever it was left in - possibly with probe pins connected to the
        meter - and only a reset puts every common back on NC.
        """
        self.write("*RST")

    def read_channel(self, channel) -> bool:
        resp = self.query(f"CLOS? {_chan_spec(channel, self.card)}")
        return str(resp).strip() == "1"

    def closed_channels(self, channels=CHANNELS) -> list:
        """Which of `channels` currently read as closed. Read-only."""
        return [c for c in channels if self.read_channel(c)]

    def channel_states(self, channels=CHANNELS) -> dict:
        """{channel: is_closed} for the whole card in one pass."""
        return {c: self.read_channel(c) for c in channels}

    # -- guarded routing ----------------------------------------------------

    def close_only(self, channel, verify: bool = True) -> bool:
        """Open everything, then close exactly one channel.

        Opening first matters: closing on top of an existing closure leaves two
        coax lines on the same multimeter terminal, which shorts two probe pins
        and makes any reading meaningless.

        Returns whether the channel reads back closed (verify=False returns True
        without the readback).
        """
        self.open_all()
        self.close_channel(channel)
        if not verify:
            return True
        return self.read_channel(channel)

    def close_set(self, channels, verify: bool = True, guard: bool = True) -> dict:
        """Open everything, then close exactly `channels`.

        With guard=True (the default) a set containing two channels wired to the
        same multimeter terminal is refused before anything is switched.

        Returns {channel: reads_back_closed}.
        """
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

    # -- multiplexer routing ------------------------------------------------

    def family(self, slot: int = None) -> str:
        """Which card family is in this switchbox - mux or form C."""
        return card_family(self.card_type(slot or self.card))

    def scan_port(self) -> str:
        """ROUT:SCAN:PORT? -> 'ABUS' or 'NONE'."""
        return (self.query("ROUT:SCAN:PORT?") or "").strip()

    def set_scan_port(self, port: str = "ABUS"):
        """Route scanned channels to the analog bus (ABUS) or not (NONE).

        Only meaningful on a multiplexer, and only useful if the analog bus
        ribbon is physically fitted to the multimeter - on probe02 it is not.
        """
        self.write(f"ROUT:SCAN:PORT {port}")

    def tree_states(self) -> dict:
        """{90: bool, 91: bool, 92: bool} - which tree switches are closed."""
        return {t: self.read_channel(t) for t in TREE_SWITCHES}

    def mux_states(self) -> dict:
        """Every channel plus the tree switches, in one pass."""
        states = self.channel_states()
        states.update(self.tree_states())
        return states

    def close_2wire(self, channel: int, verify: bool = True) -> dict:
        """Select one device 2-wire: its channel plus the AT tree switch.

        Channel must be in Bank 0 - Bank 1 reaches the analog bus through BT,
        which carries the SOURCE pair, not the sense pair. Use AT2 if you
        really want a Bank 1 channel on the AT terminals.
        """
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
        """Select one device 4-wire: sense channel N, source channel N+8, and
        both tree switches. This is the pairing SCAN:MODE FRES uses."""
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
        """Select one die of the 2x2 shot - closes its HI and LO channels.

        Every other channel is opened, and therefore grounded, so the three
        dies that are not being measured stay guarded.
        """
        key = int(die)
        if key not in DIE_SETS:
            raise KeyError(f"die {die!r} is not on this shot (known: {sorted(DIE_SETS)})")
        return self.close_set(DIE_SETS[key], verify=verify)

    def verify_wiring_assumptions(self) -> list:
        """Cheap read-only sanity checks. Returns a list of complaints, empty if
        the card looks like what probe03mapping describes.

        Does not switch anything - CLOS? is a query.
        """
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
