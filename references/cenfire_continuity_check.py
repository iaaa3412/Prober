"""Cenfire probe-card continuity check."""
import sys, os, time, itertools

ROOT = r"c:\automationproject\Probe08"
for p in (ROOT, os.path.join(ROOT, "gui")):
    if p not in sys.path:
        sys.path.insert(0, p)

from instruments.keithley_707b import Keithley707B
from instruments.keysight_34461a import Keysight34461A

OPEN_OHM = 1e5
SAME_PAD_MAX_OHM = 20
SETTLE_S = 0.4

SAME_PAD_PAIRS = {frozenset((12, 13)), frozenset((1, 24)), frozenset((11, 2))}

PINS = {
    1:  ("2", "01", "J1",  "Sense-"),
    2:  ("2", "02", "J3",  ""),
    11: ("2", "11", "J21", "Force+"),
    12: ("2", "12", "J23", "Sense+"),
    13: ("4", "01", "J25", ""),
    23: ("4", "11", "J45", "Force-"),
    24: ("4", "12", "J47", ""),
}


def tag(p):
    slot, col, j, label = PINS[p]
    return f"pin{p}({j}{'/' + label if label else ''})"


def main():
    print("=== Connecting (DMM + switch only, SMU untouched) ===")
    switch = Keithley707B(config_key="switch_matrix")
    dmm = Keysight34461A(config_key="dmm")

    switch.write('channel.open("allslots")')
    time.sleep(0.3)

    pin_ids = list(PINS.keys())
    pairs = list(itertools.combinations(pin_ids, 2))
    print(f"Sweeping {len(pairs)} pin pairs (DMM 2-wire, zero SMU bias)\n")

    opens, bad, ok = [], [], []

    try:
        for a, b in pairs:
            slot_a, col_a, _, _ = PINS[a]
            slot_b, col_b, _, _ = PINS[b]
            same_pad = frozenset((a, b)) in SAME_PAD_PAIRS
            switch.close_crosspoint(f"{slot_a}E", col_a)
            switch.close_crosspoint(f"{slot_b}F", col_b)
            time.sleep(SETTLE_S)
            r = dmm.measure_resistance(wire_mode=2)
            switch.open_crosspoint(f"{slot_a}E", col_a)
            switch.open_crosspoint(f"{slot_b}F", col_b)
            time.sleep(0.15)

            label = f"{tag(a):22s} <-> {tag(b):22s}"
            pad_note = "  [same pad - expect short]" if same_pad else ""
            is_open = not (r == r) or r >= OPEN_OHM

            if same_pad:
                if is_open or r > SAME_PAD_MAX_OHM:
                    shown = "OPEN" if is_open else f"{r:.4g} ohm"
                    print(f"  {label}  {shown:>10}   <-- NOT SHORTED (expected short){pad_note}")
                    bad.append((a, b, r, "expected short but isn't"))
                else:
                    print(f"  {label}  {r:10.4g} ohm   ok (shorted as expected){pad_note}")
                    ok.append((a, b, r))
            else:
                if is_open:
                    print(f"  {label}  OPEN")
                    opens.append((a, b))
                elif r <= SAME_PAD_MAX_OHM:
                    print(f"  {label}  {r:10.4g} ohm   <-- SUSPICIOUSLY LOW (same-pad-level short on a pair that shouldn't be)")
                    bad.append((a, b, r, "unexpectedly shorted"))
                else:
                    print(f"  {label}  {r:10.4g} ohm   ok")
                    ok.append((a, b, r))
    finally:
        switch.write('channel.open("allslots")')

    print("\n=== Summary ===")
    print(f"OK ({len(ok)}):")
    for a, b, r in ok:
        print(f"  pin{a} <-> pin{b}: {r:.4g} ohm")

    print(f"\nUNEXPECTED ({len(bad)}):")
    for a, b, r, why in bad:
        print(f"  pin{a} <-> pin{b}: {r:.4g} ohm  ({why})")

    print(f"\nOPEN / not connected ({len(opens)}):")
    for a, b in opens:
        print(f"  pin{a} <-> pin{b}")


if __name__ == "__main__":
    main()
