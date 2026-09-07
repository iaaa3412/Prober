# Electroglas 2001X — working notes

Everything established by driving the machine at GPIB0::29 through an ADLINK
USB-3488A, July 2026. Measured unless marked otherwise. The authoritative
version of the protocol detail lives in the `instruments/electroglas_2001x.py`
module docstring; this is the orientation document.

---

## 1. Getting connected

**Chain:** `pyvisa` → `visa32.dll` → NI-VISA → `NiVisaTulip.dll` → ADLINK
`gpib-32.dll` → USB-3488A.

ADLINK ships no VISA of its own. NI-VISA reaches the adapter through the Tulip
passport, which is why `GPIB0::…` addresses work at all.

**Prober-side settings that must be right** (IO CONTROL MENU on the panel):

| Setting | Value |
|---|---|
| I/O PROTOCOL | ENHANCED |
| I/O PORT | GPIB-SP |
| GPIB ADDRESS | 29 |
| TERMINATOR | CR/LF |
| GPIB SRQ | DIS (works; enabling it is a possible future improvement) |
| TIMEOUT TIMER 1/2 | 5000 ms |

**`ON LINE` must be pressed after every power cycle.** It is not persistent.
Until then `X I/D` reads OFFLINE, the interface chip answers listener-detect
and serial polls, and every command byte is refused — which looks exactly like
a broken cable.

---

## 2. Protocol hazards

These caused most of the lost time. All are measured.

**Replies are asynchronous and unmatched.** An exchange is drain → write →
read; nothing binds a reply to the command that produced it. An uncollected
reply becomes the *next* command's answer, so readings come back one behind and
still look well-formed — `?P` returning `?S`'s status, `?E` returning `?P`'s
position.

**`SM15M111100000` makes every command acknowledge**, including `SP`/`SM`
config commands, and it is the *first* command in LaMP's init sequence. Leave
one ack uncollected and the prober refuses all further writes and shows
`EXTERNAL I/O TIMEOUT`. Recovery is a Selected Device Clear; draining alone
does not work.

**One conversation at a time.** `gui/app.py` polls prober status every 3
seconds on its own thread. A jog issued in that window lost its `MC` to the
poller and timed out *while the move completed*. Scripted runs doing identical
commands one-at-a-time were 36/36 clean. The driver now serialises all I/O on
an `RLock`; any new caller must go through it.

**A leaked VISA session cannot be cleared from inside.** If a crashed process
still holds the address, clearing and reopening will not help — kill the stale
process.

**Unsupported commands wedge the link** and latch error 35. Stay inside the
documented map.

**`?E` is read-and-clear.** The first read after a failure is the real answer;
a second reads `E0` regardless.

---

## 3. Verified commands

From Keysight IC-CAP's EG2001X driver documentation, all confirmed here:

| Command | Function | Reply |
|---|---|---|
| `MM` | move chuck (relative, microns) | `MC`/`MF` |
| `MA` | move absolute (microns) | `MC`/`MF` |
| `MD` | move relative (dies) | `MC`/`MF` |
| `ZU` / `ZD` | chuck up / down | `MC`/`MF` |
| `ZM` / `ZR` | Z absolute / relative (0.1 mil) | `MC`/`MF` |
| `MT` | rotate theta | `MC`/`MF` |
| `UL` / `LO` | chuck home (unload / load position) | `MC`/`MF` |
| `AA` / `PZ` / `IK` | auto align / auto profile / inker | `MC`/`MF` |

`MC` = Move Complete, `MF` = Move Failed. **Both are replies, not commands** —
the driver previously sent `MF` as a command. Reply case varies (`mc` and `MC`
both seen), so compare case-insensitively.

`HO` is the EG**1034X** home command, not this model's.

### Query map

| Query | Example | Meaning |
|---|---|---|
| `?S` | `SZDW1C0` | status: `ZD`/`ZU`, wafer no., cassette |
| `?P` | `X0Y0` | stage position, **die coordinates** |
| `?Z` | `Z2000` | Z position, 0.1-mil units |
| `?T` | `T-569` | theta, 1:1 with `MT`, unit unknown |
| `?E` | `E0` | error, read-and-clear |
| `?I` | `IX0Y0X84000Y51200D150` | wafer info, `D` = diameter mm |
| `?Y` | `G0B0U0` + 18×`D0` | good/bad/ugly counters, then bins |
| `?O` | `OM0A1P1B0S0W0H0T1D0K0` | options — M material handling, A auto
  align, P profiler, B wafer ID, S SECS, W wafer mapping, T thermal |
| `?C` `?F` `?H` `?N` `?Q` `?R` | | cassette, first die, home, bounds, —, run state |

`?X` and `?Y` are **not** position queries despite the names. Use `?P`.

`?A ?B ?K ?M ?V ?X` return nothing; `?G ?J` return `E-1` (not available).

---

## 4. Units

| Quantity | Unit | Example |
|---|---|---|
| `SP` Z parameters, `?Z`, `ZM`, `ZR` | **0.1 mil** | `SP9Z3500` = 350.0 mils |
| `.PMV` move coordinates, `MA`, `MM` | **microns** | `56336` = 56.336 mm |
| `MD`, `?P` | **die counts** | `X4Y1` |
| `MT`, `?T` | unknown, linear | |

---

## 5. Motion behaviour

- **The prober does not enforce travel limits on incremental moves.** Fifteen
  `MD +1 X` moves were each acknowledged and carried the chuck ~238 mm off the
  platen (error 38). Guarding is the host's job — `move_relative_die()` has a
  step cap and an optional die envelope.
- **Whether negative die indices work depends on the datum.** At the load
  position (bottom-right) they are refused; after `FIRST`/`FD` with the datum
  at wafer centre they are accepted.
- **Every XY move drives Z to the down limit first**, when Z is elevated.
- **A refused move still moves Z** — `MF` does not mean nothing happened.
- **After an out-of-platen error `?P` is not trustworthy** — the counter
  re-zeroes at a physical position that is not the origin. Re-home first.
- Typical timings: XY die move 0.4 s, Z move 0.3–0.7 s.

---

## 6. Touchdown — sensed, not commanded

**Z TRAVEL MODE must be EDGE SENSE (`SM5E`), not auto profile.**

The machine's original screen read `EDGE-SENSE.SEP`. LaMP's `SM5E2` (auto
profile) overwrote it, and that single setting made `ZU`, `ZD` **and** the
panel's `AUTO PROBE` all silent no-ops — they acknowledged `MC` and did
nothing, because auto profile waits on a profiler measurement that never
happens here. Switching to edge sense fixed all three at once.

How it works: the chuck rises until the **edge sensor** detects the needles
touching, then `SP5Z` overtravel is applied past that point. Overtravel is
therefore **needle pressure**, not a limit.

Measured with a card fitted:

```
ZD:  SZUW1C1 Z2990  ->  SZDW1C0 Z2000   needles clear
ZU:  SZDW1C0 Z2000  ->  SZUW1C1 Z2987   contact + overtravel
```

`ZU` found `Z2987` against a manual touchdown at `Z2990` — repeatable to
0.3 mil. **Contact height ≈ 299 mils.**

**Never use `ZM` to approach a probe card.** It is open-loop with no sensing.

---

## 7. Configuration: LaMP's values do not transfer

LaMP's 20-command init sequence was recovered from the real
`tblProberConfiguration`. Most of it is fine; the Z values are not — they suit
LaMP's probe card and optics.

| Parameter | LaMP | This machine | Reference prober |
|---|---|---|---|
| Z overtravel `SP5Z` | 3.70 mils | **1.50** | **1.00** |
| Z clearance `SP6Z` | 15.00 | 10.00 | 45.00 |
| Z up limit `SP7Z` | 420.00 | 300 → **400** | 380.00 |
| Z align `SP9Z` | 216.00 | 300 → **350** | 310.00 |
| Align scan vel `SP16V` | 2000 | **3000** | 3000 |

**`SP9Z` (Z ALIGN) is the alignment camera's focus height**, not just a limit.
LaMP's 216 left the wafer ~134 mils below focus and the video went featureless
grey. Measured sharp at **350 mils** here.

`MACHINE_INIT_SEQUENCE` in the driver applies LaMP's sequence with `SP7Z`/`SP9Z`
overridden, so the GUI's *Send LaMP Init* cannot undo the focus fix.
`PRE_LAMP_SETTINGS` records the originals — **there is no query that reads `SP`
parameters back**, so they survive only because they were photographed.

---

## 8. The wafer map lives on the PC

**The prober stores no die map.** LaMP kept it in `.PMA` file sets and drove
the stage to each coordinate.

```
recipe.PMA                  header: counts, paths, die size, electrical params
recipeMovesMajorX.PMV       absolute X, one per touchdown, microns
recipeMovesMajorY.PMV       absolute Y
recipeDeviceIDMajor.PMS     device IDs, one line per touchdown
recipeMoves/DeviceIDMinor.*  sub-site offsets; usually a single 0
```

### Die size is the step per touchdown, not the physical die

Proved across the recipe library. Two product families each ship a single-die
recipe *and* a quad recipe, and the quad's die size is exactly double in both
axes:

| Product | Recipe | DieSize | dies/shot |
|---|---|---|---|
| HP LaMP | `HP LaMP 21 PCMs` | 3521 × 1642 | 1 |
| HP LaMP | `HP LaMP electrical gauge` | **7042 × 3284** | 4 |
| LPLaMP | `LPLaMP 18 PCMs` | 2602 × 1642 | 1 |
| LPLaMP | `LPLaMP electrical whole wafer` | **5204 × 3284** | 4 |

Same product, same physical dies — only the touchdown footprint changed. So
setting the prober's die size to the quad makes one die-indexed move step a
whole shot, which is how a 4-die touchdown pattern is driven with `MD`.

Every `.PMV` coordinate is an exact integer multiple of that pitch.

Only 6 of 60 recipes surveyed are quad, all LaMP-family electrical ones;
everything else (CYRUS, Murphy, Sena, HOLLY, GIAL5) is single-die.

Corollary: the prober's SET PRMTR die size must match the recipe in use. A
reference prober showing `3.52100 MM` is the HP LaMP **single-die** value, not
a different product.

### The benches differ — profiles, not assumptions

`instruments/eg_probers.yaml` records each EG bench from a live bus scan;
`instruments/eg_profiles.py` selects between them and rewrites the `*_eg`
entries of `instruments.yaml` so every existing driver keeps working unchanged.
Switch benches from the **Instruments** tab; **Scan bus & match** says which
profile the hardware actually looks like. Adding probe01 = adding a YAML block.

| | probe02 (LaMP bench) | probe03 |
|---|---|---|
| 2001X | `29` | `29` |
| Keithley 2400 | **`24` ✓** | not fitted |
| HP 3458A | `23` (TERM? = REAR) | `23` (TERM? = FRONT) |
| E1326B | **`9::3`, LADDR 24, self-test passes ✓** | `9::7`, LADDR 56, **FAILED** |
| relay1 | `9::15` **E1345A** | `9::15` E1343A |
| relay2 | `9::9` E1343A | `9::10` E1364A *(wired)* |
| relay3 | `9::14` E1364A | `9::14` E1364A |

Same secondary address, different card — that is why nothing may be assumed.
probe02 matches LaMP's own instrument table on every address except RELAY2.

### Multiplexer architecture (E1343A / E1345A) — confirmed on hardware

Each channel carries **H, L and G**. Channels split into **Bank 0 (00–07)** and
**Bank 1 (08–15)**, and reach the outside world through *tree switches*, which
are themselves addressable channels:

| Channel | Tree switch | Connects |
|---|---|---|
| **90** | AT | Bank 0 → AT terminals **and analog bus H / L / G** (sense) |
| **91** | BT | Bank 1 → BT terminals **and analog bus I+ / I− / IG** (source) |
| **92** | AT2 | Bank 1 → AT terminals |

So a channel alone connects nothing — **the tree switch is what completes the
path.** Verified on both muxes: 90/91/92 close and read back, `SCAN:PORT ABUS`
is accepted, `*RST` opens everything.

- **2-wire on channel N:** close `(@190)` + `(@1NN)`, N in 00–07.
- **4-wire:** channel N (sense, Bank 0) **+ channel N+8** (source, Bank 1) with
  both 90 and 91 closed. That is the `SCAN:MODE FRES` n / n+8 pairing.

Every common, tree and analog-bus line has a **100 Ω series resistor** for relay
protection — it sits in any 2-wire measurement.

For the 10 V isolation test the natural wiring is the Keithley 2400 to the **AT
tree switch terminals**, then `(@190)` + one Bank 0 channel per die.

> **The analog bus is NOT cabled to the E1326B on probe02.** Readings appeared
> to track mux state until a control run showed the same state drifting
> 6516 → 8337 → 5834 Ω on its own, and interleaved A/B tests decayed straight
> through both states into *negative* resistance. That is a floating input
> settling, not switching. The first reading of a fresh reset was `9.9e37` —
> overload, i.e. open. Always run the repeat-the-same-state control before
> believing a switching effect.

### Driving a recipe — verified on the bench

**The LaMP recipes are not in `pma/`.** The real ones live in
`C:\_local\data\debug\LaMPElectrical\` — `HPLaMP_WHOLE_WAFER.PMA` (634
touchdowns), `HP LaMP electrical gauge.PMA`, `HPLaMP_TRACE_PROBE.PMA`. All
three use **DieSize 7042 × 3284 µm**, and all three carry the electrical test
inline: `Voltage=10`, `MeterRange=0.0001`, `MeterCurrentLimit=1e-6`, `NPLC=1`.
The three recipes in `pma/` are other products and do *not* match this wafer.

Confirmed by moving the machine, 2026-07-30:

| Fact | How |
|---|---|
| `MD +1 X` moves **right**, `MD +1 Y` moves **DOWN** | measured 2026-08-21: from `54-00`, `MD +1,0` landed `54-01` and `MD 0,+1` landed `44-71` — TL→TR→BR of the quad. An earlier note here said "up"; that was wrong. |
| Recipe +X/+Y match `MD +1` — no sign flip | same |
| Prober die size **is** 7.042 × 3.284 mm | `MD +1` from `54-00` landed exactly on `54-02` |
| `?P` tracks every step exactly, both axes | ±1 out-and-back, exact return |
| Move time | 0.5–0.8 s per die step |

**Align die = `54-00 / 44-70 / 54-01 / 44-71`**, touchdown #323, quad (10, 20).
`54-00` occurs exactly once in the recipe. Three things agree on that spot: the
`...FromAlignSite` fields, all four `TARGET` markers sitting on that same row,
and the operator recognising it.

Do **not** use absolute moves. The `.PMV` frame's origin has never been
confirmed against the machine — only differences between consecutive touchdowns
are trustworthy, and those are exact integer quad steps.

`gui/eg_pma_run_panel.py` drives this from the **Run** tab (Electroglas only;
the Accretech pane is untouched). It takes the recipe from the PMA Process tab,
asks the operator which die the chuck is on, then walks the list as relative
steps — chunking anything over `max_die_step`, and stopping if the `?P` delta
ever disagrees with what was commanded.

One trap worth knowing: the 2001X intermittently wedges the link mid-run, and
it is the *position read* that fails, not the move. A run that aborts there has
usually still moved. The panel retries once via `recover()` and stops if it
still cannot read `?P` — never continue without knowing where the chuck is.

### Alignment comes first

The `.PMA` carries `PreAlignMessage=Align` and `PostAlignMessage=Make sure the
plate and the alligator clip are clamped down`, and
`XMoveFirstFromAlignSite`/`YMoveFirstFromAlignSite` give the offset **from the
align site to the first touchdown**. `.PMS` files contain literal `TARGET`
entries marking alignment targets rather than devices.

So the run is: align at the align site, offset to the first touchdown, then
walk the move list. Which panel operation performed that alignment is not
established here.

`DeviceIDMajor.PMS` carries **four IDs per line**, one per die in the quad:

```
93-01/83-71/93-02/83-72
NA/86-14/NA/NA            NA     = no die at that position
TARGET/41-71/TARGET/41-72 TARGET = alignment target
```

That matches the physical model — one touchdown contacts 4 dies through 8 pins,
and the relay multiplexes between them because a single SMU can only measure one
at a time.

`XMoveFirstFromAlignSite` / `YMoveFirstFromAlignSite` offset from the align site
to the first touchdown.

`gui/electroglas_pma.py` parses all of this; `split_quad_devices()` breaks out
the per-die IDs. Verified against real recipes including a 3125-touchdown map.

---

## 9. Panel vs GPIB

| Panel | GPIB |
|---|---|
| SET PRMTR / SET MODE / SET OPTION | `SP` / `SM` / `SO` |
| FIRST | `FD` |
| LOAD | `LO` — **this machine's home** |
| Z | `ZU` / `ZD` |
| X / Y green keys | `MA` / `MO` |
| **Joystick, DISK, LEARN, DIAG, STORE, PRINT, FIND TARG, DIG VID** | **none** |

There is no HOME key — LOAD is the datum, which is why the chuck powers up at
the load position.

`DISK` has no remote equivalent, so prober programs cannot be loaded over GPIB.
Combined with §8, that confirms the architecture: **the PC owns the map.**

Operator-only, per the operator: `AUTO PROBE`, `AUTO ALIGN`, `FIND TARG` are
not used on this setup. `ALIGN SCAN` sweeps right-to-left repeatedly for
correcting theta by eye.

Alignment sequence observed: `FIND TARG`, then `PAUSE/CONT` for the
illuminator; `LAMP` toggles light, `CAMR` toggles camera.

---

## 10. Diagnostics

In `references/`, all read-only unless noted:

| Script | Purpose |
|---|---|
| `adlink_gpib_scan.ps1` | raw 488 bus scan — **no Python needed** |
| `visa_probe.ps1` | what VISA can see |
| `ping_eg_test.py` | scan + ping every EG instrument |
| `probe_2001x.py` | full prober link diagnosis |
| `check_eg_state.py` | status / position / error snapshot |
| `test_eg_telemetry.py` | decoded telemetry |
| `unwedge_2001x.py` | escalating link recovery |
| `test_poller_vs_jog.py` | concurrency regression test |
| `restore_needle_pressure.py` | **writes** `SP5Z15` + `SP6Z100` |

---

## 11. The instrument side (probe03)

### The GPIB addresses are fewer boxes than they look

Physically there are four things: the prober, the HP 3458A, one HP 75000
Series B (E1300A) mainframe, and the PC. The mainframe publishes each of its
card groups as a separate GPIB *instrument* at primary address 9, so one box
accounts for four addresses:

| Address | What it is |
|---|---|
| `GPIB0::29` | Electroglas 2001X — the prober |
| `GPIB0::23` | HP 3458A — standalone bench DMM |
| `GPIB0::9::0` | E1300A mainframe's own system instrument |
| `GPIB0::9::15` | switchbox, card 1: **E1343A** 16-ch HV multiplexer |
| `GPIB0::9::10` | switchbox, card 1: **E1364A** 16-ch form C switch |
| `GPIB0::9::14` | switchbox, card 1: **E1364A** 16-ch form C switch |

Secondary address = VXI logical address ÷ 8, so SA 10/14/15 are logical
addresses 80/112/120. (The E1326B manual's own example calls the switchbox at
"secondary address 14" logical address 112.)

Three cards, one wired, two spare — which matches the box.

Also fitted but **not on GPIB**: two E1326B 5½-digit multimeters installed
internally, broken out to banana terminals by E1326-80005 adapters. No
secondary address answers for either, so they are present in hardware but not
configured as instruments in the mainframe. That has to be fixed before
anything can read them.

### The wired card selects one of four dies on a 2×2 shot

The prober lands on a 2×2 shot; the relays then measure the four dies one at a
time. **Two pins per die, eight coax.** The test forces a current and reads the
voltage to verify electrical isolation, so a *high* reading is the pass.

Transcribed in `references/probe03mapping`, encoded in
`instruments/hp_switchbox.py`. On a form C relay, CLOSE ties Common→NO and
OPEN ties Common→NC.

- Every **Common** carries one coax toward the probe card (coax 3–10).
- Every **NC** is daisy-chained into one node that reaches ground via CH15 NC.
  So **open = grounded**, and all-open (`*RST`) is a guarded state, not a
  floating one.
- Every **NO** lands on the bottom E1326B adapter.

The coax run 3–10 alternates HI, LO, HI, LO…, so consecutive coax form four
HI/LO pairs — one per die. Two pairs land on the meter's Input (sense)
terminals, two on its Current (source) terminals:

| Die | Channels | Coax | E1326B terminals |
|---|---|---|---|
| 1 | CH00 / CH01 | 3 / 4 | Input HI / Input LO |
| 2 | CH02 / CH03 | 5 / 6 | Input HI / Input LO |
| 3 | CH08 / CH09 | 7 / 8 | Current HI / Current LO |
| 4 | CH10 / CH11 | 9 / 10 | Current HI / Current LO |

HI channels are 00, 02, 08, 10 (coax 3, 5, 7, 9); LO are 01, 03, 09, 11
(coax 4, 6, 8, 10). Channels 4–7 and 12–14 are unwired spares.

**Operating rule: one HI and one LO closed at a time.** Two HI closed at once
shorts two probe pins through the HI terminal — same for two LO. That is the
one way to damage something here, so `close_set()` and the GUI both refuse it.

> **Unverified, and it blocks measurement.** Two pins per die means the current
> must be forced down the same pair the voltage is read from — so Input HI has
> to be commoned with Current HI, and Input LO with Current LO. Otherwise dies
> 1–2 can only be sensed (no current path) and dies 3–4 only driven (no sense),
> and *no* die is measurable. `probe03mapping` records no such strap. Meter
> between those adapter terminals before trusting any reading.

The E1326B has **no current-measuring function** — the Current terminals are
its internal current *source*, used for resistance. The manual is explicit that
measurements at the meter's own terminals **must be configured as 4-wire**
(`MEAS:FRES?`); a stand-alone E1326B has no 2-wire mode at all, so the strap
above is what makes it electrically 2-wire while the command stays `FRES`.
Its ceiling is 1.048 MΩ, which may not be enough headroom for an isolation
test — LaMP used a Keithley 2400 SMU for this, not the E1326B.

### Which of the two E1364As is the wired one is not yet known

The wiring is on the **top** E1364-66201 (that part number is the card
assembly; `SYST:CTYP?` reports it as E1364A). The other E1364 and the E1343
mux are in the box but unconnected.

GPIB cannot answer this: VXI slot position and logical address are set
independently, so nothing queryable maps "top" to an address. Two ways to
settle it:

- **Read the logical address switch on the wired card.** Fastest, no meter.
  80 → `9::10`, 112 → `9::14`.
- **Continuity walk** (Switch Debug tab): nothing powered, coax free of the
  probe card, close one channel at a time and meter from that coax to the named
  adapter terminal. The wired card shows short-to-terminal when closed and
  short-to-ground when open; the spares show neither.

### The E1326B is unassigned — that is the blocker

Ask the mainframe itself and the gap is obvious. The system instrument at
`9::0` answers `VXI:CONF:NUMB?` and `VXI:CONF:DLAD?`, which report the VXI
backplane rather than what GPIB happens to reach:

```
VXI:CONF:NUMB? -> +5     VXI:CONF:DLAD? -> +0,+56,+80,+112,+120
```

**Five devices, four instruments.** LADDR 0/80/112/120 are the command module
and the three switchboxes. **LADDR 56 has no GPIB instrument** — it is seated
and on the backplane, but nothing is published for it, and VISA will not even
open `GPIB0::9::7::INSTR` (`VI_ERROR_RSRC_NFOUND`, not a timeout). That is
almost certainly the E1326B the relay wiring lands on.

**LADDR 56 is an E1326B, and it has FAILED.** Both halves are proven, not
inferred:

- Its registers, read through the command module with `VXI:READ? 56,<offset>`:
  ID (`base+00`) = `0xFFFF`, Device Type (`base+02`) = **`0xFF40`**. The manual
  states outright: *"FF40₁₆ — HP E1326B 5½ Digit Multimeter"*. Controls agree —
  LADDR 80 reads `0xFF20` (E1364A), LADDR 120 reads `0xFF01` (E1343A).
- The mainframe's own error queue says
  **`+2101,"Config error 1, Failed device"`** for it, and the status register
  (`base+04`) reads `0xFF03` with bit 2 ("Passed") clear.

The manual explains the mechanism: *"The multimeter drives the SYSFAIL line
during a self-test, and the line remains asserted if the self-test fails. If
the multimeter fails its power on self-test, the Resource Manager de-asserts
SYSFAIL and resets the multimeter to take the device off-line."* That is
exactly the observed state, and it is why no instrument is published.

A register-level reset does not recover it. Self-test codes 1–4 all carry the
same instruction in the manual: return the multimeter for repair. **Treat this
E1326B as dead.**

Note also that only **one** extra device is on the backplane, though there are
two E1326-80005 adapters. Two adapters is not two modules.

> **Do not write 0 to its Control Register (`base+04`).** Bit 1 controls
> whether the card may drive SYSFAIL; the Resource Manager de-asserts it
> deliberately to hold a failed card off-line. Writing 0 restores the failed
> card's SYSFAIL drive and hangs the whole mainframe — which, being mid-chain
> on GPIB, takes the prober and 3458A down with it. Recovery is a power cycle.

`references/find_vxi_instruments.py` does this comparison automatically. Do not
diagnose this with a blind address sweep — an unassigned module's address fails
to *open*, so a sweep skips it silently and reports nothing wrong.

The E1326B is a complete 5½-digit DMM, not a breakout — DCV, true-RMS ACV,
4-wire ohms, temperature — and it works stand-alone with signals on its own
faceplate terminals. The **E1326-80005 adapter** is the breakout: HI, LO, COM
and I on banana plugs, no electronics.

Its VXI logical address switch decides everything. The address must be a free
multiple of 8 or the mainframe leaves the module unassigned — powered, seated,
no GPIB address. Factory setting is **24 → secondary address 03**, and the
manual warns that with more than one multimeter you must move the others, "as
there can only be one instrument per secondary address". **Two modules both
left at 24 collide and neither is assigned** — consistent with what the sweep
sees. Fix, in order:

1. E1300A front panel — it lists the configured instruments.
2. The logical address switch on each E1326B. Move the second to 32, 40, 48…
   Any free multiple of 8; 80, 112 and 120 are taken by the switchboxes.
3. Power-cycle the mainframe — logical addresses are read at boot.

Then set `dmm_vxi_eg` in `instruments.yaml` to the address it lands on.

### The 3458A is the measurement instrument

With the E1326B dead, the 3458A is not a fallback — it is the better tool.
**Its ohms functions source a known current and read the resulting voltage**,
which *is* the isolation test. From the manual: *"The multimeter measures
resistance by supplying a known current through the unknown resistance being
measured… measures this voltage and calculates the unknown resistance."*

| Range | Current sourced | | Range | Current sourced |
|---|---|---|---|---|
| 10 Ω | 10 mA | | 1 MΩ | 5 µA |
| 100 Ω | 1 mA | | 10 MΩ | 500 nA |
| 1 kΩ | 1 mA | | 100 MΩ | 500 nA |
| 10 kΩ | 100 µA | | **1 GΩ** | 500 nA |
| 100 kΩ | 50 µA | | | |

**1.2 GΩ full scale** against the E1326B's 1.048 MΩ — about a thousand times
the headroom, which is the difference between a real isolation number and an
over-range.

**Use 2-wire (`OHM`), not 4-wire (`OHMF`).** probe03 lands two pins per die, so
sense leads could only terminate back at the relay and would measure nothing
extra. At isolation-test values a few ohms of lead resistance is irrelevant.
`OHMF` with the sense pair unconnected gives a meaningless reading, not merely
an imprecise one.

**Wiring it also removes the strap question.** Land 3458A **HI** on *both* the
Input HI and Current HI terminals, and **LO** on both Input LO and Current LO.
Then all four HI channels (00, 02, 08, 10) reach HI and all four LO channels
(01, 03, 09, 11) reach LO, and any HI+LO pair is a 2-wire path — four dies, no
strap needed at the adapter.

The front/rear **Terminals switch is mechanical** and cannot be set over GPIB;
the rear set is the one to wire in. Each set carries HI, LO, Sense HI, Sense
LO, Guard and a fused I terminal (current only — not used for ohms).

For the record, LaMP's table marked TRUE: the 2001X, `RELAY1` (`9::15`, the
E1343A) and the **Keithley 2400**, with the 3458A FALSE. probe03's wiring is on
an E1364A instead, so this is a new build rather than a restoration of LaMP's
path, and the instrument choice does not have to follow LaMP's.

---

## 12. Open items

- **`SP5Z` is still on LaMP's 3.70 mils.** Two independent sources say ~1–1.5.
  With a card fitted this is live needle pressure.
- `MT` theta unit unknown — check SET PRMTR page 2.
- `MO` (absolute die) never tested; `goto_die()` deliberately steps with `MD`.
- `move_to_start_die()` sends `MF`, which is a *reply* code — suspect.
- `DEFAULT_Z_LIMITS` is a hand-maintained mirror of `SP7Z`/`SP8Z`; update it
  when those change.
- Keithley 2400 and Agilent 6634B are not fitted, so the measurement half of
  the LaMP workflow is untested.
- **Which E1364A is wired** (`9::10` or `9::14`) — settle by continuity walk.
- **Neither E1326B answers on GPIB**, so nothing can read the wired path yet.
  This is the blocker.
- **Is Input HI strapped to Current HI, and Input LO to Current LO** at the
  adapter? Without it no die is measurable. One continuity check settles it.
- **Coax 3–10 are not yet landed on the probe card**, so which physical die of
  the 2×2 shot is die 1 is still unknown. The relay side is mapped; the
  probe-card side is not.
- The E1326B tops out at 1.048 MΩ. An isolation test usually wants far more
  headroom — check that before committing to it as the measurement instrument.
  LaMP used a Keithley 2400 SMU, which is not fitted here.
