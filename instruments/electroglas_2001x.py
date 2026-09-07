"""Electroglas 2001X / 2001CX(E) prober.

VERIFIED COMMAND SET - Keysight IC-CAP's "EG2001X" prober driver documentation
(people.ece.ubc.ca ICCAP-2008-doc/icref/icref022.html). This is the only
independent source found for this prober's GPIB commands; everything else in
this file predates it and is unverified guesswork:

    MM      Move Chuck        -> reply "MC" or "MF"
    ZU      Chuck Up
    ZD      Chuck Down
    UL/LO   Chuck Home        -> reply "MC" or "MF"
    ?S      Chuck Status      -> reply "SZ..."
    IK      Trigger Inker     -> reply "MC" or "MF"
    PZ      Auto Profile      -> reply "MC" or "MF"
    AA      Auto Align        -> reply "MC" or "MF"

"MC" is Move Complete and "MF" Move Failed - both are REPLIES, not commands.
The same doc lists "HO" as Chuck Home for the EG1034X, a different model.

MT IS A REAL ROTATION COMMAND, verified here even though IC-CAP does not list
it. MT<n> rotates the chuck and ?T reports the angle, tracking one-for-one:
from T-569, 'MT100' gave T-469 and 'MT-100' returned it to T-569, both
acknowledged MC. ?P and ?Z are unaffected - rotation is independent of the die
grid and Z. (?Q also has a "T" field; it stayed 0 throughout, so that one is
NOT theta.)

The UNIT is unknown - the readout is linear in the command value, but nothing
establishes what one count is in degrees. Rotation about a centre away from the
current stage position shows up as a small XY displacement of the viewed point,
which is what it looks like through the microscope.

Normally theta is not driven by hand: AA (Auto Align) corrects wafer rotation
as part of alignment, and that is the intended path.

QUERY MAP - measured directly from this prober once it was brought ON LINE,
each query asked in isolation after draining, and confirmed stable across
repeats. Entries marked (*) are cross-checked against the prober's own screen:

    ?C  'CP0L0S0R0W/-2'                 cassette status
    ?D  'DT941'
    ?E  'E0'                            error code, 0 = none
    ?F  'X0Y0S0'                        first die
    ?H  'HX0Y0'                         home position
    ?I  'IX0Y0X84000Y51200D150'      (*) wafer info, D = diameter mm
    ?L  'L0'
    ?N  'BX0Y0LX0Y0RX0Y0'
    ?O  'OM0A1P1B0S0W0H0T0D0K0'         option settings
    ?P  'X0Y0'                       (*) stage position
    ?Q  'QC0T0P0Z0C0'
    ?R  'C0S0F0R0W1P0E0A0'           (*) W field = wafer number
    ?S  'SZDW1C0'                    (*) status: ZD = Z down, W1 = wafer 1
    ?T  'T-569'                      (*) THETA position - see MT below
    ?U  'U0:16'
    ?W  '2'
    ?Y  'G0B0U0' + 18x'D0'           (*) Good/Bad/Ugly counters, then per-bin
    ?Z  'Z0'                         (*) Z position
    ?A ?B          no reply
    ?K ?M ?V ?X    empty reply
    ?G ?J          template ending 'E-1', i.e. not available on this machine

'?X' and '?Y' are NOT position queries on this prober despite the axis names -
?X returns nothing and ?Y returns the die counters. Use ?P for position.

REPLIES ARE ASYNCHRONOUS. A reply nobody reads stays queued and surfaces as the
answer to the NEXT query, so everything reads one behind and still looks
well-formed. query() below drains before every command; do not bypass it.

ONE CONVERSATION AT A TIME - THIS LINK IS NOT CONCURRENCY-SAFE. Every exchange
is drain -> write -> read, and nothing binds a reply to the command that caused
it. Two threads sharing the session interleave, each collects the other's
acknowledgement, and the result is moves that execute late, acks that match the
wrong command, and timeouts.

That is what made the jog pad appear to "queue": each button press spawned its
own thread. It now locks its buttons until the MC returns and DROPS presses
made meanwhile. Any future caller must serialise the same way.

Measured for contrast: scripted single commands are fast - three MD moves plus
their ?P reads complete in a couple of seconds, and a fifteen-move walk ran
without a hitch. Slowness has so far only appeared with overlapping callers.
Whether the prober ALSO queues internally is unverified; do not assume it does.

A TIMEOUT IS NOT A REJECTION. The reply may be late rather than absent, and if
it arrives afterwards it will be misread as the next command's acknowledgement
- which is why _motion() clears the link before raising.

THE PROBER DOES NOT ENFORCE TRAVEL LIMITS ON INCREMENTAL MOVES. Fifteen
consecutive 'MD +1 X' moves were each acknowledged 'MC' and carried the chuck
~238mm off the platen (prober error 38, "out of platen"); only the single large
move back was refused with 'MF'. Wafer mapping is disabled on this machine (?O
reports W0), so there is no die map bounding anything, and the chuck's power-on
position is the LOAD position, not wafer centre - so "a few dies from here" is
not a safe assumption. move_relative_die() carries the guard instead.

SM15M111100000 MAKES EVERY COMMAND ACKNOWLEDGE, AND THE ACK IS NOT OPTIONAL.
It is "MF/MC on Rest of Commands = on", and it is the FIRST command in
LAMP_INIT_SEQUENCE - so from that point on even SP/SM config commands reply
'MC'. Measured: SP4D150 replies 'MC\r\n'. Leave one uncollected and the prober
stops accepting writes ENTIRELY, queries included, and the panel shows
"EXTERNAL I/O TIMEOUT" - it is waiting on the host that never read its reply.

Recovery is clear_interface() (Selected Device Clear), which resets the GPIB
I/O state without moving the machine or changing its configuration. Draining
alone does NOT recover it: the prober is not holding a queued reply at that
point, it has stopped listening.

Use send_command() / send_init_sequence(), which collect the acknowledgement.
Do not use bare write() for anything once SM15 is in effect.

THE DIE GRID DEPENDS ENTIRELY ON WHERE THE DATUM WAS SET - AND FIRST/FD MOVES
IT. Two measured states, and they behave differently:

  Before FIRST, datum at the LOAD position (bottom-right corner of travel):
    ?P reads X0Y0 there, 'MD -1 X' is refused with 'MF' because negative
    indices are rejected, and 'MD +1 X' was accepted fifteen times running,
    straight off the platen. One-sided, and only the lower bound guarded.

  After pressing FIRST with a wafer loaded, datum at WAFER CENTRE:
    ?P reads X0Y0 at the centre and negative indices ARE accepted, bounded
    instead by the edge of the probing area.

So "negative die indices are refused" is a property of the datum, not of the
prober. Do not hard-code either behaviour. Until FIRST/FD has been used against
a real wafer, X0Y0 is just where the chuck happens to sit rather than a die,
which is why die coordinates mean nothing before alignment.

NOTE: gui/eg_prober_debug_panel.py's _JOG_LEFT/_JOG_RIGHT constants were chosen
on the assumption of a bottom-right origin. That reasoning no longer holds with
a centre datum, and the physical direction of +X was never directly confirmed -
re-check which way the chuck actually moves before trusting the arrow labels.

THIS MACHINE USES EDGE SENSE, NOT AUTO PROFILE - AND LAMP'S SM5E2 BROKE THAT.
The very first screenshot taken of this prober, before any config was sent,
read "EDGE-SENSE.SEP". LAMP_INIT_SEQUENCE's SM5E2 ("Z Travel Mode, 2 = Auto
Profile") then switched it away, which is the most likely reason ZU/ZD became
silent no-ops: auto profile wants a profiler measurement that never happens
here, so there is no contact height to move to.

The two are different mechanisms:
  edge sense   - the sensor detects the needles touching DURING Z-up, and
                 overtravel is applied from that point. Closed loop on contact.
  auto profile - a separate profiler measures the wafer surface height in
                 ADVANCE, and Z then moves to the computed height.

Edge sense is what this setup is built around, confirmed independently by
another prober of the same family. The correct SM5E value for it is NOT known -
LaMP's table only captions 2, and there is no read-back query. Read it off the
prober's SET MODE page rather than guessing.

An earlier version of this file advised leaving SM5E at 2. That was wrong: it
took LaMP's configuration as this machine's intent, when the machine's own
screen said edge sense.

EDGE SENSE CONFIRMED WORKING (2026-07). With Z TRAVEL MODE switched to edge
sense, ZU and ZD both work and are repeatable:

    ZD:  ?S SZUW1C1 Z2990  ->  SZDW1C0 Z2000   (needles clear)
    ZU:  ?S SZDW1C0 Z2000  ->  SZUW1C1 Z2987   (contact + overtravel)

ZU landed at Z2987 against a manual touchdown at Z2990 - 0.3 mil apart, so the
sensor finds the same contact height repeatably. ?P did not move during either,
so Z motion leaves XY alone. This machine's contact height with the fitted card
is therefore about 299 mils.

A LEAKED VISA SESSION CANNOT BE CLEARED FROM INSIDE. If a crashed process still
holds a session on this address, clear_interface() and even reopening will not
help - queries come back intermittently and then fail again. Kill the stale
process; that is what actually fixes it.

TOUCHDOWN IS SENSED, NOT COMMANDED - DO NOT DRIVE Z TO CONTACT WITH ZM.
There is an edge/touch sensor box on the machine (its connector panel carries
2 EDGE SENSORS and 4 INKERS). Contact height is found by the prober driving Z
up until that sensor triggers; SP5Z overtravel is then applied ON TOP of the
sensed height to set needle pressure. That is what Z TRAVEL MODE = auto profile
(SM5E2) means, and it is why PZ exists.

So the chain is:  PZ profiles -> contact height known -> ZU goes to contact +
overtravel. ZU being a no-op without a profile is the interlock doing its job,
not a defect to route around. Setting SM5E to something non-profiling to "make
ZU work" would remove the sensing, and ZM is open-loop by definition - with a
probe card fitted either one drives needles by dead reckoning.

LaMP's operator prompt is the same interlock seen from the other side: "You
MUST now check the 'Z' height on the prober lower key pad. The Green light on
the edge sensor box should then be on."

Consequence for SP5Z: it is not a limit, it is the pressure applied past
sensed contact. This machine's own value is 1.50 mils (SP5Z15); LaMP's
SP5Z37 is 2.5x that, and applies to LaMP's probe card, not necessarily one
fitted here.

EVERY XY MOVE DRIVES Z TO THE DOWN LIMIT FIRST. Observed: any MD lowers Z to
2000 (200.0 mils, the Z DOWN LIMIT) before travelling. So an XY move is never
just an XY move - it is a Z move, then XY, and with Z TRAVEL MODE = auto
profile (SM5E2) most likely a profiling step afterwards too.

That matters for timing. Pure Z moves (ZM/ZR) are consistently fast - measured
0.3-0.4s each, twelve for twelve. XY moves are the ones that intermittently
take tens of seconds and occasionally exceed a 30s acknowledgement window. The
obvious suspect is the profiler hunting for a wafer surface that is not there
and retrying (see SM42R, profiler retry count) - unconfirmed, but it fits: the
slow path is exactly the one that profiles, and the fast path is the one that
does not.

A REFUSED MOVE STILL MOVES Z. 'MD' lowers Z before attempting XY, and that
happens even when the XY target is rejected - measured, ?Z went 300 -> 0 on a
move that returned 'MF'. Never read 'MF' as "nothing happened".

AFTER AN OUT-OF-PLATEN ERROR, ?P IS NOT TRUSTWORTHY. The die counter appears to
re-zero, so ?P reads X0Y0 at a physical location that is not the original
origin. Re-home the prober from its front panel before believing coordinates
again. Note also that ?E is read-and-clear, so the error code is gone after one
read even though the condition happened.

DO NOT SEND COMMANDS OUTSIDE THIS MAP. An unsupported query (?A was the one
measured) is not merely ignored: the prober stops answering entirely until the
link is drained, and it latches error 35. Since unsupported queries also return
no reply at all, they are what skews the reply pipeline in the first place.

'?E' IS READ-AND-CLEAR. It reports the most recent latched error and resets to
E0, so reading it twice looks like "no error" even when there was one. Treat
the first read after any failure as the real answer.

PANEL KEYS THE OPERATOR REPORTS AS NOT-FOR-USE ON THIS SETUP (2026-07):
    AUTO PROBE   does nothing - same cause as ZU/ZD, no contact height while
                 SM5E is on auto profile rather than edge sense
    AUTO ALIGN   not the procedure wanted here
    FIND TARG    likewise
    ALIGN SCAN   sweeps the stage right-to-left repeatedly; used to correct
                 theta by eye, not an automatic alignment
Treat these as off-limits unless something establishes otherwise. AA and PZ are
in the verified command set, but "documented" is not "appropriate for this
machine" - SM5E2 was documented too.

ALIGNMENT IS AN OPERATOR PROCEDURE, AND Z ALIGN IS THE CAMERA FOCUS HEIGHT.
Observed sequence on this machine: press FIND TARG, then PAUSE/CONT to bring
the illuminator up; LAMP toggles the light and CAMR toggles the camera. None of
those have GPIB equivalents.

SP9Z (Z ALIGN) sets how high the chuck rises for alignment, which is what puts
the wafer surface in the microscope's focal plane - it is an OPTICAL setting,
not just a safety limit. This machine focuses at 300.00 mils (SP9Z3000), where
Z ALIGN equals its Z UP LIMIT. LaMP's SP9Z2160 leaves the wafer ~84 mils too
low and the video goes featureless grey with no visible change as the stage
moves. LaMP's Z values are tied to LaMP's probe card AND its optics; treat
PRE_LAMP_SETTINGS as this machine's truth.

OPERATOR PANEL <-> GPIB. From photographs of this machine's two keypads. Most
panel functions have a GPIB equivalent; the ones that do not are the reason
some steps stay manual.

  Main keypad
    SET PRMTR (A)   -> SP<n>... commands        SET MODE (B)  -> SM<n>...
    SET OPTION (C)  -> SO...                    FIRST (D)     -> FD
    AUTO ALIGN (E)  -> AA                       ON LINE (H)   -> enables GPIB
    X / Y (green)   -> absolute coordinate entry, cf. MA / MO
    DIAG (F), DISK (G), LEARN (J), PROG (K), STORE (L), PRINT (M),
    FIND TARG (O), DELE (P), DIG VID (Q), RUN ID (I)
                    -> NO GPIB EQUIVALENT FOUND. DISK is how prober programs
                       (the .DB on screen) are loaded - not remotely loadable.

  Lower keypad (probe station)
    LOAD            -> LO   chuck to load position = THIS MACHINE'S HOME
                            (IC-CAP calls UL/LO "Chuck Home"; UL is unload)
    Z               -> ZU / ZD
    VAC.            -> chuck vacuum
    ALIGN SCAN, AUTO PROBE, TEST CYCLE, PAUSE/CONT, INK TEST, INK ENBL,
    LAMP, CAMR      -> not mapped
    joystick        -> manual XY jog, no GPIB equivalent

There is no key labelled HOME: LOAD is the home/datum reference, which is why
the chuck sits at the load position at power-up.

IO CONTROL MENU, as read off this machine - and SM15M111100000 decoded.
The ENHANCED EXTERNAL I/O MODE menu has 4 MF/MC flags then 5 message flags,
which is exactly the command's "M" + 1111 + 00000 payload:
    01 MF/MC ON X-Y MOTION  ENB  \
    02 MF/MC ON Z MOTION    ENB   |  the "1111"
    03 MF/MC ON OPT DEVICES ENB   |
    04 MF/MC ON REST OF CMDS ENB /
    05..09 TEST START / TEST COMPLETE / PATTERN COMPLETE / PAUSE-CONTINUE /
           ALARM messages   DIS      the "00000"
So that one command sets this whole menu, and it is why Z motion acknowledges.

    01 I/O PROTOCOL   ENHANCED   <- required
    03 I/O PORT       GPIB-SP    <- required
    05 GPIB ADDRESS   29
    06 TERMINATOR     CR/LF      <- matches the '\r\n' every reply carries
    07 GPIB SRQ       DIS        <- IC-CAP wants this ENABLED; left disabled
                                    because the MC/MF reply protocol works
                                    without it. Enabling it would allow polling
                                    for completion instead of blocking on a
                                    read, which is the proper fix for the
                                    wedging described above - worth revisiting.
    09/10 TIMEOUT TIMER 1/2  5000 ms
           ^ this is the timer behind the panel's "EXTERNAL I/O TIMEOUT": fail
             to collect an acknowledgement within 5s and it fires.

REQUIRED PROBER-SIDE SETTINGS for any of this to work (same source):
    I/O PROTOCOL = ENHANCED
    I/O PORT     = GPIB-SP
    SRQ switch   = enabled
Until the prober's I/O port is configured and brought ON LINE, its software
does not service the GPIB bus at all: the interface chip still answers
listener-detect and serial polls, but every command byte is refused at the
handshake.
"""

import datetime
import functools
import re
import threading
import time

from instruments.gpib_base import GPIBInstrument, open_resource


def _fmt6(value) -> str:
    return f"{float(value):07.3f}"


def _serialised(method):
    """Hold the driver's I/O lock for the whole of `method`.

    Nothing in this protocol binds a reply to the command that caused it: an
    exchange is drain -> write -> read, and whoever reads next gets whatever
    arrived. Two threads sharing one session therefore collect each other's
    acknowledgements.

    This is not hypothetical. gui/app.py polls prober status every 3 seconds on
    its own thread, so a jog issued at the wrong moment loses its MC to the
    poller and times out while the move itself completes - which is exactly the
    intermittent failure seen in the GUI, and exactly why scripted runs doing
    the identical commands never reproduced it (36/36 clean, no poller).

    The lock lives on the driver because the session is what needs protecting;
    a lock in any one panel cannot cover the other callers. RLock, so the
    compound operations can call the primitives without deadlocking.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._io_lock:
            return method(self, *args, **kwargs)
    return wrapper


# This machine's SET PRMTR page as photographed BEFORE LAMP_INIT_SEQUENCE was
# ever sent, with the equivalent command to put each value back. There is no
# GPIB query that reads most SP parameters back, so without this list the
# previous setup would only exist on that photograph. Die size (SP1) is the
# one exception - see infer_die_size() below for an INDIRECT read via ?P,
# found and verified by direct experiment. Nothing else here has an
# equivalent trick known yet.
#
# Z values are in 0.1-mil units (command = mils x 10), derived from LaMP's own
# command/comment pairs and consistent across all five of them.
PRE_LAMP_SETTINGS = [
    ("Align scan velocity", "3000",       "SP16V3000"),
    ("Z overtravel",        "1.50 mils",  "SP5Z15"),
    ("Z clearance",         "10.00 mils", "SP6Z100"),
    ("Z up limit",          "300.00 mils", "SP7Z3000"),
    ("Z down limit",        "200.00 mils", "SP8Z2000"),
    ("Z align height",      "300.00 mils", "SP9Z3000"),
    ("Z under travel",      "0.00 mils",  "SP10Z0"),
    ("Wafer diameter",      "150 mm",     "SP4D150"),
]


# The prober configuration LaMP (prjLampElectrical.exe) sent at startup, taken
# from the real tblProberConfiguration rows recovered in
# Lampexe/GPIB_COMMANDS.txt section 0.7. Rows whose command is blank were
# documentation in the original table, not something sent.
#
# CONFIGURATION ONLY - every one of these is an SP/SM/SO/SX/WM "set" command.
# None of them move the chuck, the stage or the wafer handler.
#
# Three of these are independently corroborated by the prober's own screen:
# SP4D150 (150mm) shows as DIA......150 MM, SM4P1 (probe mode = edge) as
# PROBE..-> EDGE, and SM1U1 (metric) as the mm-valued DIE X/Y readout - i.e.
# the machine is already sitting in this configuration.
LAMP_INIT_SEQUENCE = [
    ("MF/MC on Rest of Commands", "on", "SM15M111100000"),
    ("Set Wafer Diameter", "150mm", "SP4D150"),
    ("Set Z Scale Factor", "3 steps per mil", "SP12S3"),
    ("Set Z Overtravel", "3.7 mils", "SP5Z37"),
    ("Set Z Clearance", "15 mils", "SP6Z150"),
    ("Set Z UpLimit", "420 mils", "SP7Z4200"),
    ("Set Z DownLimit", "200 mils", "SP8Z2000"),
    ("Set Z Align Height", "216 mils", "SP9Z2160"),
    ("Set Z Scan Align Speed", "2000 mils/sec", "SP16V2000"),
    ("Set Metric XY Units", "", "SM1U1"),
    ("Set Initial Probing Direction", "Quadrant 2", "SM2Q2"),
    ("Set Probe Mode", "1 = Edge", "SM4P1"),
    ("Set Z Travel Mode", "2 = Auto Profile", "SM5E2"),
    ("Set Ignore Vacuum", "1 = enabled", "SM22V1"),
    ("Set 30mil drop at load", "1 = enabled", "SM30L1"),
    ("Set Microprobing", "0 = disabled", "SM35B0"),
    ("Set Screen/Lamp Saver", "1 = enabled", "SM40S1"),
    ("Auto Temperature Compensation", "on", "SO01100001"),
    ("Temperature Compensation", "0 = disabled", "SX1B0"),
    ("Wafer Mapping", "0 = disabled", "WM0"),
]

# What to actually send to THIS prober: LaMP's sequence with the two Z values
# that do not transfer replaced by ones measured on this machine.
#
# LaMP's SP9Z2160 puts the wafer ~134 mils below focus - the alignment video
# goes featureless grey. The operator confirmed the image is sharp with the
# chuck at Z3500, so Z ALIGN is 350.0 mils here, and Z UP LIMIT has to be at
# least that for the align height to be reachable. 3500 rather than LaMP's
# 4200 keeps the ceiling as low as focus allows and preserves this machine's
# original Z ALIGN == Z UP LIMIT pairing.
#
# LAMP_INIT_SEQUENCE above is kept verbatim as the record of what LaMP did.
# This is what send_init_sequence() sends.
# SET PRMTR page photographed on ANOTHER working prober of the same family
# (2026-07), kept as a sanity reference. It is NOT a configuration to copy:
# that machine runs a different product (die 3.521 x 1.642 mm against 15.86 x
# 7.08 here) and its Z values suit its own probe card and wafer stack.
#
# What it is good for is judging whether a number is the right ORDER of
# magnitude, and on two points it lines up with this machine's own original
# settings and against LaMP's:
#
#   Z OVERTRAVEL     1.00 mils  <- vs this machine's own 1.50, vs LaMP's 3.70.
#                                  Two independent sources put needle pressure
#                                  near 1 mil. LaMP's 3.7 looks wrong for any
#                                  card here.
#   ALIGN SCAN VEL   3000       <- matches this machine's original 3000, not
#                                  LaMP's 2000.
#
# And one where it differs from everything:
#   Z CLEARANCE     45.00 mils  <- vs 10.00 here originally and LaMP's 15.00.
#                                  Clearance is card-height dependent, so this
#                                  is the least transferable of the three.
#
# Z UP LIMIT 380 / Z ALIGN 310 sit between this machine's originals (300/300)
# and what is currently set (400/350).
REFERENCE_PROBER_SETTINGS = [
    ("Die X",           "3.52100 mm   (different product - do not copy)"),
    ("Die Y",           "1.64200 mm   (different product - do not copy)"),
    ("Preset",          "X0 Y0 die"),
    ("Wafer diameter",  "150 mm"),
    ("Align scan vel",  "3000"),
    ("Z overtravel",    "1.00 mils"),
    ("Z clearance",     "45.00 mils"),
    ("Z up limit",      "380.00 mils"),
    ("Z down limit",    "200.00 mils"),
    ("Z align",         "310.00 mils"),
    ("Z under travel",  "0.00 mils"),
]

_MACHINE_Z_OVERRIDES = {
    "SP7Z4200": ("Set Z UpLimit", "350 mils (this machine, not LaMP's 420)",
                 "SP7Z3500"),
    "SP9Z2160": ("Set Z Align Height", "350 mils (measured sharp; LaMP's 216 "
                 "is out of focus here)", "SP9Z3500"),
}

MACHINE_INIT_SEQUENCE = [
    _MACHINE_Z_OVERRIDES.get(command, (what, value, command))
    for what, value, command in LAMP_INIT_SEQUENCE
]


class Electroglas2001X(GPIBInstrument):
    # Largest single MD move allowed by default. Stepping die to die is what MD
    # is for; a large jump is where a sign error or a stale datum does real
    # damage, and the prober will not refuse it (see move_relative_die).
    DEFAULT_MAX_DIE_STEP = 5

    # Z travel limits in 0.1-mil units, mirroring SP8Z (down) and SP7Z (up) as
    # currently set on the machine: 200.0 and 400.0 mils.
    #
    # THERE IS NO QUERY THAT READS THESE BACK, so this constant is a mirror
    # maintained by hand - update it whenever SP7Z/SP8Z change on the prober
    # (up limit was raised 350 -> 400 mils for probe-card touchdown work).
    # Being stale is not dangerous: the prober enforces its own limits and
    # answers "CANNOT MOVE Z OUTSIDE OF LIMITS", so this guard only exists to
    # turn that into a message that says which limit and by how much.
    #
    # ?Z can read OUTSIDE this range - Z0 is the parked position, below the
    # down limit - and a relative move from there targets somewhere still
    # outside, which is why move_z_relative() checks the target not the origin.
    DEFAULT_Z_LIMITS = (2000, 4000)

    def __init__(self):
        super().__init__('prober_eg')
        self.z_is_up = None
        self.max_die_step = self.DEFAULT_MAX_DIE_STEP
        self.z_limits = self.DEFAULT_Z_LIMITS
        self._die_envelope = None
        # Guards every exchange on this session - see _serialised().
        self._io_lock = threading.RLock()

    @_serialised
    def _drain(self, settle_ms: int = 150) -> int:
        """Discard any replies the prober still has queued.

        The 2001X answers asynchronously and a reply nobody collected stays
        queued, so the next read returns the PREVIOUS query's answer. Measured
        on this bench before this existed: ?P returned ?S's status string, ?E
        returned ?P's position, ?Y returned ?E's error code - every reading one
        behind, all of them well-formed enough to look right. On a prober that
        is the difference between "the chuck is down" and something else.
        """
        if not self.inst:
            return 0
        previous = self.inst.timeout
        dropped = 0
        try:
            self.inst.timeout = settle_ms
            while True:
                try:
                    self.inst.read_raw()
                    dropped += 1
                except Exception:
                    return dropped
        finally:
            try:
                self.inst.timeout = previous
            except Exception:
                pass

    @_serialised
    def query(self, command, _retry=True):
        """Query, draining first, and recovering from a transient refusal.

        Never query this prober without clearing what is already queued. Even
        then a write can be refused outright: observed repeatedly that ?S, ?P
        and ?Z succeed and the very next write times out, because a straggling
        reply arrived after _drain()'s window closed and the prober is still
        holding the bus. A Selected Device Clear resets that, so one retry
        turns a hard failure into a hiccup.
        """
        if not self.inst:
            return None
        self._drain()
        try:
            return super().query(command)
        except Exception:
            if not _retry:
                raise
            try:
                self.clear_interface()
            except Exception:
                pass
            self._drain()
            return self.query(command, _retry=False)

    @_serialised
    def accepts_commands(self) -> bool:
        """True if the prober will actually take a command byte.

        A serial poll is answered by the GPIB interface chip on its own, so it
        keeps succeeding even when the prober's software is not servicing the
        bus. Measured on this bench: address 29 answers listener-detect and
        serial polls, yet every write is refused at the handshake, while a
        switchbox on the same adapter accepts data in the same instant. Writing
        a harmless query is what separates the two.

        '?S' is a query - it cannot move the chuck, stage or wafer handler.
        """
        if not self.inst:
            return False
        try:
            self.inst.write("?S")
        except Exception:
            return False
        # Collect the reply we just provoked. Leaving it queued would make the
        # next query return this status string instead of its own answer.
        self._drain()
        return True

    def get_id(self) -> str:
        # The 2001X predates *IDN? and parses no ID query at all - an ADLINK bus
        # scan lists it as "Unknown instrument (PA:29)". So don't write one at
        # it; serial-poll instead. That also stops this returning a truthy
        # string for a prober that is switched off.
        if not self.is_present():
            return ""
        if not self.accepts_commands():
            raise RuntimeError(
                "GPIB interface responds to serial poll but refuses every "
                "command - the prober is not servicing the bus. Check that "
                "host/remote GPIB control is enabled on the prober itself.")
        return "Electroglas 2001X (serial poll OK, no ID string)"

    def _not_implemented(self, name):
        raise NotImplementedError(
            f"Electroglas 2001X: '{name}' has no real command mapping yet. "
            f"Add it to instruments/electroglas_2001x.py once the Electroglas "
            f"command reference is available.")

    def get_prober_id(self) -> str:
        return self.get_id()

    def get_error_code(self) -> str:
        """?E -> 'E0' when there is no error."""
        return self.query("?E") or ""

    def get_error_message(self) -> str:
        self._not_implemented("get_error_message")

    def get_prober_status(self) -> str:
        return self.query("?S") or ""

    def _wait_until_not_moving(self, timeout_s: float = 30.0) -> str:
        """Read the mc/mf reply a motion command sends when the move finishes.

        Motion commands answer 'mc' (Move Complete) or 'mf' (Move Failed) -
        LOWERCASE on this machine, confirmed live: MM with X1 Y0 replied 'mc'.

        This used to poll ?S waiting for the word "moving" to disappear, but
        this prober never emits that word in any reply. What it actually
        returned was the mc reply that happened to arrive during the poll,
        while the real ?S answer was left queued for the next caller to
        misread. Read the reply directly instead.
        """
        if not self.inst:
            return ""
        previous = self.inst.timeout
        try:
            self.inst.timeout = int(timeout_s * 1000)
            reply = (self.inst.read() or "").strip()
        except Exception as e:
            raise TimeoutError(
                f"Electroglas 2001X: no mc/mf reply within {timeout_s}s ({e})")
        finally:
            try:
                self.inst.timeout = previous
            except Exception:
                pass

        low = reply.lower()
        if low.startswith("mf"):
            raise RuntimeError(
                f"Electroglas 2001X reported MOVE FAILED ({reply!r}) - the XY "
                f"target was rejected, most likely outside the probing area. "
                f"NOTE: XY did not move, but Z may still have changed - a move "
                f"lowers Z before attempting XY, and that part happens even "
                f"when the XY move is refused (measured: ?Z went 300 -> 0 on a "
                f"refused MD). Re-read ?Z rather than assuming nothing moved.")
        if not low.startswith("mc"):
            raise RuntimeError(
                f"Electroglas 2001X: expected 'mc' or 'mf', got {reply!r}")
        return reply

    def get_xy_position(self) -> str:
        """?P -> 'X0Y0'.

        Was built from '?X' and '?Y', which this prober answers with nothing at
        all - and because replies lag, those two empty reads used to shift every
        later query's answer by one. '?P' is the real position query, confirmed
        against the prober's own display reading POS X....0 Y....0.
        """
        return self.query("?P") or ""

    def get_die_position(self) -> tuple:
        """Current die coordinate as (x, y), parsed from ?P ("X0Y0").

        Thin wrapper around get_xy_position() + _parse_die_position() -
        goto_die() already does this internally on every step; this just
        exposes the same read as a single call for a caller (e.g. the
        Recipe tab's minor-moves origin capture) that only wants the
        current position, not a move.
        """
        pos = self._parse_die_position(self.get_xy_position())
        if pos is None:
            raise ValueError("Cannot parse ?P response for current die position")
        return pos

    def get_die_counts(self) -> str:
        """?Y -> 'G0B0U0' + one 'D<n>' per bin.

        G/B/U are the Good, Bad and Ugly die counters shown on the prober's
        display; the 18 trailing D fields are the per-bin counts.
        """
        return self.query("?Y") or ""

    def get_cassette_status(self) -> str:
        """?C -> 'CP0L0S0R0W/-2'."""
        return self.query("?C") or ""

    @staticmethod
    def decode_status(raw: str) -> str:
        """Translate a ?S reply such as 'SZDW1C0' into readable text.

        Field meanings are those confirmed against the prober's own screen -
        ZD/ZU against the chuck position, W against WAFER #. Anything not
        recognised is passed through verbatim rather than guessed at, so an
        unfamiliar field shows up instead of being silently dropped.
        """
        text = (raw or "").strip()
        if not text:
            return "no status"
        body = text[1:] if text[:1].upper() == "S" else text

        parts, seen = [], 0
        match = re.match(r"Z([UD])", body, re.IGNORECASE)
        if match:
            seen = match.end()
            parts.append("Z UP - wafer CONTACTING the probe card"
                         if match.group(1).upper() == "U"
                         else "Z DOWN - wafer clear of the probe card")

        rest = body[seen:]
        for letter, label in (("W", "wafer"), ("C", "cassette")):
            found = re.search(letter + r"(\d+)", rest, re.IGNORECASE)
            if found:
                parts.append(f"{label} {int(found.group(1))}")

        known = re.sub(r"Z[UD]|[WC]\d+", "", body, flags=re.IGNORECASE)
        if known:
            parts.append(f"unrecognised: {known!r}")
        return "  |  ".join(parts) if parts else text

    @staticmethod
    def decode_error(raw: str) -> str:
        """'E0' means no error. Anything else is a latched fault code.

        Remember ?E is read-and-clear: the first read after a failure is the
        real answer, and a second read will say E0 either way.
        """
        text = (raw or "").strip()
        if not text:
            return "no reply"
        if text.upper() in ("E0", "E00"):
            return "no error"
        code = text[1:] if text[:1].upper() == "E" else text
        if code == "35":
            return "error 35 - unsupported/invalid command"
        return f"ERROR {code}"

    @_serialised
    def read_telemetry(self) -> dict:
        """Read every verified status query in one pass.

        Returns {label: value}. Raw reply strings are always included so a field
        this code cannot decode is still visible rather than silently dropped.
        Read-only - every command here is a '?' query.
        """
        out = {}
        for label, cmd in (("status", "?S"), ("position", "?P"), ("z", "?Z"),
                           ("theta", "?T"), ("error", "?E"),
                           ("wafer_info", "?I"), ("die_counts", "?Y"),
                           ("cassette", "?C"), ("run_state", "?R")):
            try:
                out[label] = self.query(cmd) or ""
            except Exception as e:
                out[label] = f"<{type(e).__name__}>"

        status = out.get("status", "")
        if status.startswith("S"):
            # 'SZDW1C0' -> Z Down, wafer 1. Only ZU/ZD are decoded; anything
            # else is left raw rather than guessed at.
            if "ZD" in status:
                out["z_state"] = "DOWN (wafer clear of the probe card)"
            elif "ZU" in status:
                out["z_state"] = "UP (wafer CONTACTING the probe card)"
            wafer = status.split("W", 1)[-1].split("C", 1)[0] if "W" in status else ""
            if wafer:
                out["wafer_number"] = wafer

        counts = out.get("die_counts", "")
        if counts.startswith("G"):
            try:
                good = counts.split("G", 1)[1].split("B", 1)[0]
                bad = counts.split("B", 1)[1].split("U", 1)[0]
                ugly = counts.split("U", 1)[1].split("D", 1)[0]
                out["die_tally"] = f"good {good}, bad {bad}, ugly {ugly}"
            except (IndexError, ValueError):
                pass

        info = out.get("wafer_info", "")
        if "D" in info:
            diameter = info.rsplit("D", 1)[-1]
            if diameter.isdigit():
                out["wafer_diameter_mm"] = diameter

        return out

    @_serialised
    def recover(self) -> str:
        """Get the link talking again after a timeout has wedged it.

        A late or uncollected acknowledgement leaves the prober refusing every
        write, including queries, and nothing recovers on its own - which is
        why a VI_ERROR_TMO tends to be followed by the GUI being unable to talk
        to the prober at all.

        Escalates least-invasive first, stopping at the first thing that works:
          1. drain, in case it is only holding a queued reply
          2. Selected Device Clear, which resets its GPIB I/O state
          3. close and reopen the VISA session, for when the session itself has
             gone bad rather than the instrument

        None of these move the machine or change its configuration. Returns a
        description of what it took.
        """
        def _talks(checks=2):
            """Require several CONSECUTIVE clean queries, not just one.

            A single successful read is not proof: after a wedge the link comes
            back intermittently, and recover() previously declared success on
            one good ?S while the very next query still timed out. That is
            worse than reporting failure, because the caller believes it is
            fine and carries on.
            """
            for _ in range(checks):
                try:
                    if not (self.inst and self.query("?S", _retry=False)):
                        return False
                except Exception:
                    return False
            return True

        if not self.inst:
            return "no session open - use Refresh Connections"

        dropped = self._drain()
        if _talks():
            return f"recovered by draining ({dropped} stale reply(s))"

        try:
            self.clear_interface()
            self._drain()
        except Exception as e:
            return f"device clear failed: {e}"
        if _talks():
            return "recovered by device clear"

        try:
            self.inst.close()
        except Exception:
            pass
        try:
            self.inst, via = open_resource(self.address)
            self.inst.timeout = self.timeout
            self.inst.encoding = "latin-1"
        except Exception as e:
            self.inst = None
            return f"could not reopen {self.address}: {e}"
        if _talks():
            return f"recovered by reopening the session (via {via})"
        return ("still not responding. If another process holds a VISA session "
                "on this address, no amount of clearing here will fix it - "
                "that session has to die first. Otherwise check the prober is "
                "ON LINE.")

    @_serialised
    def write(self, command):
        # Inherited from GPIBInstrument, overridden only to take the I/O lock -
        # the set_* helpers use bare writes and must not interleave either.
        return super().write(command)

    @_serialised
    def is_present(self) -> bool:
        return super().is_present()

    @_serialised
    def clear_interface(self):
        """Selected Device Clear - reset the prober's GPIB I/O state.

        This is the recovery for the wedged condition an uncollected
        acknowledgement causes. Interface-level only: it does not move the
        machine and does not change its configuration.
        """
        if self.inst:
            self.inst.clear()

    @_serialised
    def send_command(self, command: str, ack_timeout_s: float = 10.0):
        """Write a command and collect its MC/MF acknowledgement.

        Once SM15M111100000 ("MF/MC on Rest of Commands") is in effect - and
        LAMP_INIT_SEQUENCE turns it on as its very FIRST command - every
        subsequent command replies MC or MF. Those replies are not optional to
        read: leaving one uncollected makes the prober refuse all further
        writes, including queries, and recovering needs clear_interface().

        Returns the acknowledgement, or None if the command did not send one
        (not every command does, and SM15 may not be enabled yet).
        """
        if not self.inst:
            return None
        self._drain()
        try:
            self.inst.write(command)
        except Exception:
            # Same transient refusal query() handles - clear and try once more
            # rather than abandoning a half-sent configuration sequence.
            self.clear_interface()
            self._drain()
            self.inst.write(command)

        previous = self.inst.timeout
        try:
            self.inst.timeout = int(ack_timeout_s * 1000)
            ack = (self.inst.read() or "").strip()
        except Exception:
            return None
        finally:
            try:
                self.inst.timeout = previous
            except Exception:
                pass

        if ack.lower().startswith("mf"):
            raise RuntimeError(f"prober rejected {command!r} (replied {ack!r})")
        return ack

    @_serialised
    def send_init_sequence(self, log=None) -> int:
        """Send MACHINE_INIT_SEQUENCE - LaMP's configuration, Z values corrected.

        Sends MACHINE_INIT_SEQUENCE, not LAMP_INIT_SEQUENCE: LaMP's SP9Z2160
        leaves this prober's alignment camera out of focus, so sending the
        verbatim LaMP sequence would undo a correction that was measured on the
        machine. See _MACHINE_Z_OVERRIDES.

        Configuration only; nothing here moves the machine. Each command's
        acknowledgement is collected, because the first command switches
        acknowledgement on for all the rest. Returns how many were sent.
        """
        sent = 0
        for what, value, command in MACHINE_INIT_SEQUENCE:
            ack = self.send_command(command)
            sent += 1
            if log:
                suffix = f" ({value})" if value else ""
                log(f"{command:<16} {what}{suffix}"
                    + (f"   [{ack}]" if ack else ""))
            time.sleep(0.05)
        return sent

    @_serialised
    def send_settings(self, rows, log=None) -> int:
        """Send any (what, value, command) list the same way."""
        sent = 0
        for what, value, command in rows:
            ack = self.send_command(command)
            sent += 1
            if log:
                suffix = f" ({value})" if value else ""
                log(f"{command:<16} {what}{suffix}"
                    + (f"   [{ack}]" if ack else ""))
            time.sleep(0.05)
        return sent

    def get_wafer_info(self) -> str:
        """?I -> 'IX0Y0X84000Y51200D150' - the D field is wafer diameter in mm,
        matching DIA......150 MM on the prober's display."""
        return self.query("?I") or ""

    def get_xy_absolute(self) -> str:
        self._not_implemented("get_xy_absolute")

    def get_on_wafer_info(self) -> str:
        self._not_implemented("get_on_wafer_info")

    def get_lot_number(self) -> str:
        self._not_implemented("get_lot_number")

    def get_wafer_number(self) -> str:
        self._not_implemented("get_wafer_number")

    def get_wafer_id(self) -> str:
        self._not_implemented("get_wafer_id")

    def get_pass_fail_counts(self) -> str:
        self._not_implemented("get_pass_fail_counts")

    def get_gross_value(self) -> str:
        self._not_implemented("get_gross_value")

    def get_wafer_status(self) -> str:
        self._not_implemented("get_wafer_status")

    def get_yield_data(self) -> str:
        return self.get_die_counts()

    def get_hot_chuck_status(self) -> str:
        self._not_implemented("get_hot_chuck_status")

    def get_chuck_temperature(self) -> str:
        self._not_implemented("get_chuck_temperature")

    def get_start_die_coords(self) -> str:
        self._not_implemented("get_start_die_coords")

    def get_multisite_info(self) -> str:
        self._not_implemented("get_multisite_info")

    def buzzer_clear(self) -> str:
        self._not_implemented("buzzer_clear")

    def send_es(self):
        self._not_implemented("send_es")

    def confirm_and_clear_alarm(self) -> bool:
        self._not_implemented("confirm_and_clear_alarm")

    def read_stb_decoded(self) -> tuple:
        return 0, (self.get_prober_status() or "unknown")

    @_serialised
    def _motion(self, command: str, timeout_s: float = 30.0) -> str:
        """Send a motion command and collect its MC/MF acknowledgement.

        Use this rather than write() + _wait_until_not_moving(). The bare-write
        form has no drain, so a stale reply gets consumed as THIS command's
        acknowledgement while the real one is left queued - which then wedges
        the next command. That is why the Z up/down buttons stopped working
        once MF/MC ON Z MOTION was enabled.

        send_command() drains first, collects the ack, and raises on MF.
        """
        ack = self.send_command(command, ack_timeout_s=timeout_s)
        if ack is None:
            # The acknowledgement is late, not absent - the prober queues work
            # and replies when it gets there. Giving up without resyncing lets
            # that straggler arrive later and be read as the NEXT command's
            # ack, desyncing everything after it. Clear the link so the next
            # command starts from a known state.
            try:
                self.clear_interface()
                self._drain()
            except Exception:
                pass
            raise TimeoutError(
                f"no mc/mf acknowledgement to {command!r} within {timeout_s}s. "
                f"This is NOT necessarily a rejection - the move may have "
                f"executed with only its acknowledgement lost. Link resynced; "
                f"re-read ?P and ?Z to see where the stage actually is.")
        return ack

    @_serialised
    def _z_move_verified(self, command: str, expect: str) -> str:
        """ZU/ZD, verified against ?S rather than trusted.

        These two acknowledge 'MC' and then do nothing at all when Z TRAVEL
        MODE is "auto profile" (SM5E2, which LAMP_INIT_SEQUENCE sets): with no
        profiled wafer there is no target to move to. Measured directly - ZU
        and ZD both returned MC while ?Z sat at Z2000 and ?S never left ZD,
        whereas ZM2500 moved the axis and flipped ?S to ZU in the same session.

        A silent no-op on a Z axis is worse than an error, so check the status
        flag actually reached the state that was asked for.
        """
        status = self._motion(command)
        after = (self.get_prober_status() or "").upper()
        if expect not in after:
            raise RuntimeError(
                f"{command} acknowledged ({status!r}) but Z did not reach "
                f"{expect} - ?S still reads {after!r}. Z TRAVEL MODE is set to "
                f"auto profile (SM5E2), which needs a profiled wafer to know "
                f"where 'up' is. Use move_z_absolute() for a direct height, or "
                f"profile a wafer first.")
        return status

    def z_up(self):
        status = self._z_move_verified("ZU", "ZU")
        self.z_is_up = True
        return status

    def z_down(self):
        status = self._z_move_verified("ZD", "ZD")
        self.z_is_up = False
        return status

    def move_z_absolute(self, z):
        status = self._motion(f"ZM{int(z)}")
        self.z_is_up = None
        return status

    @_serialised
    def move_z_relative(self, dz):
        """ZR - relative Z move, with the target checked against the limits.

        ?Z can sit OUTSIDE the Z limits: Z0 is the parked position, well below
        the 2000 (200.0 mil) down limit. A relative move from there whose
        target is still outside gets refused by the prober - "CANNOT MOVE Z
        OUTSIDE OF LIMITS" on its screen - which over GPIB just looks like a
        failed move. Checking here says why, and points at the way out, which
        is an absolute move into range.
        """
        dz = int(dz)
        low, high = self.z_limits
        here = self._parse_z(self.query("?Z"))
        if here is not None:
            target = here + dz
            if not low <= target <= high:
                where = ("below" if target < low else "above")
                extra = ""
                if not low <= here <= high:
                    extra = (f" Z is currently parked at {here}, itself outside "
                             f"the limits - use move_z_absolute() to get back "
                             f"into range first.")
                raise ValueError(
                    f"ZR{dz:+d} from Z{here} targets Z{target}, {where} the Z "
                    f"limits [{low}..{high}] (0.1-mil units). The prober would "
                    f"refuse this.{extra}")
        status = self._motion(f"ZR{dz}")
        self.z_is_up = None
        return status

    @staticmethod
    def _parse_z(reply):
        """'Z2400' -> 2400, or None if it cannot be read."""
        try:
            return int(str(reply).strip().lstrip("Zz"))
        except (AttributeError, ValueError):
            return None

    def move_theta_relative(self, dtheta):
        return self._motion(f"MT{int(dtheta)}")

    def emergency_stop(self):
        self._not_implemented("emergency_stop")

    def unload_wafer(self):
        status = self._motion("U")
        self.z_is_up = False
        return status

    def load_wafer(self):
        status = self._motion("L")
        self.z_is_up = False
        return status

    def cassette_wait_for_wafer_ready(self, timeout_s=None):
        self._not_implemented("cassette_wait_for_wafer_ready")

    def cassette_next_die(self, timeout_s=None):
        self._not_implemented("cassette_next_die")

    def cassette_unload_and_load_next(self, timeout_s=None):
        self._not_implemented("cassette_unload_and_load_next")

    def next_die(self):
        status = self._motion("J")
        self.z_is_up = False
        return status

    def index_die_alt(self):
        status = self._motion("I")
        self.z_is_up = False
        return status

    def set_index_size(self, x_um: float, y_um: float):
        self._not_implemented("set_index_size")

    def move_xy_absolute(self, dx_um: float, dy_um: float):
        self._not_implemented("move_xy_absolute")

    def move_to_start_die(self):
        # SUSPECT: "MF" is documented as a REPLY (Move Failed), not a command -
        # see the verified command set in this module's docstring. Left as-is
        # because it cannot be tested while the prober's I/O port is OFFLINE,
        # but it should be checked before this is trusted to move anything.
        status = self._motion("MF")
        self.z_is_up = False
        return status

    def move_to_home(self):
        # SUSPECT: IC-CAP lists "HO" as Chuck Home for the EG1034X; for the
        # EG2001X it documents "UL"/"LO" instead. Unverifiable until the prober
        # is ON LINE - verify before relying on this.
        status = self._motion("HO")
        self.z_is_up = False
        return status

    def trigger_inker(self):
        """IK - fires the inker. Display shows INKER..DIS when it is disabled."""
        return self._motion("IK")

    def auto_profile(self):
        """PZ - runs the automatic Z profile."""
        return self._motion("PZ")

    def auto_align(self):
        """AA - runs automatic wafer alignment."""
        return self._motion("AA")

    def move_to_die_xy(self, x_die: int, y_die: int):
        self._not_implemented("move_to_die_xy")

    def move_absolute_die(self, x_die, y_die):
        status = self._motion(f"MOX{int(x_die)}Y{int(y_die)}")
        self.z_is_up = False
        return status

    @_serialised
    def goto_die(self, x_die: int = 0, y_die: int = 0, max_moves: int = 60) -> str:
        """Travel to an absolute die coordinate, stepping there with MD.

        Deliberately built from relative moves rather than MO. MD is verified
        on this machine and ?P tracks it exactly; MO is inherited guesswork
        that has never been tested. A large single MD is also refused with MF
        even when the same distance walks fine in steps, so this goes in
        chunks of at most max_die_step.

        ?P is re-read before every step, so a refusal, an unexpected position
        or a stalled counter stops the walk rather than letting it grind on.
        """
        target = (int(x_die), int(y_die))
        for _ in range(max_moves):
            here = self._parse_die_position(self.get_xy_position())
            if here is None:
                raise RuntimeError(
                    "cannot read a usable die position from ?P - refusing to "
                    "walk blind")
            if here == target:
                return f"X{target[0]}Y{target[1]}"

            cap = self.max_die_step
            dx = max(-cap, min(cap, target[0] - here[0]))
            dy = max(-cap, min(cap, target[1] - here[1]))
            before = here
            self.move_relative_die(dx, dy)

            after = self._parse_die_position(self.get_xy_position())
            if after == before:
                raise RuntimeError(
                    f"MD {dx:+d},{dy:+d} was accepted but ?P did not change "
                    f"(still X{before[0]}Y{before[1]}) - stopping rather than "
                    f"looping. The stage may be against a bound.")
        raise RuntimeError(
            f"did not reach X{target[0]}Y{target[1]} within {max_moves} moves")

    @_serialised
    def move_relative_die(self, dx_die, dy_die):
        """Relative move in die counts, with a software travel guard.

        THE PROBER DOES NOT RELIABLY STOP YOU. Measured on this machine:
        fifteen consecutive MD +1 moves in X were all acknowledged with 'MC'
        and took the chuck about 238mm, straight off the platen (prober error
        38, "out of platen"); only the single large move back was refused with
        'MF'. Wafer mapping is disabled here (?O reports W0), so the prober has
        no die map to bound moves against, and with ignore-vacuum enabled and
        no wafer loaded nothing else gates it either.

        So the guard has to live on this side. Two layers:
          - a cap on any single move, since a large jump is the more damaging
            mistake and MD is meant for stepping die to die
          - an optional absolute envelope, enforced against ?P, once the
            caller has established a trustworthy datum via set_die_envelope()
        """
        dx, dy = int(dx_die), int(dy_die)

        if max(abs(dx), abs(dy)) > self.max_die_step:
            raise ValueError(
                f"MD move of ({dx},{dy}) dies exceeds max_die_step="
                f"{self.max_die_step}. Step in smaller increments, or raise "
                f"max_die_step deliberately if this is really intended.")

        if self._die_envelope is not None:
            here = self._parse_die_position(self.get_xy_position())
            if here is None:
                raise RuntimeError(
                    "cannot read a usable die position from ?P, so the travel "
                    "envelope cannot be enforced - refusing to move")
            x_min, x_max, y_min, y_max = self._die_envelope
            target = (here[0] + dx, here[1] + dy)
            if not (x_min <= target[0] <= x_max and y_min <= target[1] <= y_max):
                raise ValueError(
                    f"MD move would land at {target}, outside the configured "
                    f"envelope X[{x_min}..{x_max}] Y[{y_min}..{y_max}]. "
                    f"Refusing - the prober will NOT catch this for you.")

        status = self._motion(f"MDX{dx}Y{dy}")
        self.z_is_up = False
        return status

    @staticmethod
    def _parse_die_position(pos):
        """'X1Y-2' -> (1, -2), or None if it cannot be parsed."""
        try:
            return (int(pos.split("X", 1)[1].split("Y", 1)[0]),
                    int(pos.split("Y", 1)[1]))
        except (AttributeError, IndexError, ValueError):
            return None

    def set_die_envelope(self, x_min, x_max, y_min, y_max):
        """Constrain MD moves to an absolute die-coordinate box.

        Only meaningful once ?P is trustworthy - i.e. after the prober has been
        homed and a datum established. Pass None to clear.
        """
        self._die_envelope = (int(x_min), int(x_max), int(y_min), int(y_max))

    def clear_die_envelope(self):
        self._die_envelope = None

    # MM IS A FINE POSITIONAL MOVE, BUT ITS COUNT IS NOT ONE MICRON.
    #
    # Measured 2026-08-11 on a real recipe. From touchdown #228 of
    # HPLaMP_WHOLE_WAFER (TL die 65-23, x = 98588 um) a MMX7042Y0 was sent,
    # intending one 7042 um quad step to #229. The chuck landed with TL die
    # 66-23, which that recipe places at x = 116193 um:
    #
    #     travelled 116193 - 98588 = 17605 um  for a commanded 7042
    #     scale = 17605 / 7042 = 2.50
    #
    # 17605 um is also exactly this product's SHOT pitch (5 physical dies of
    # 3521 um), which is why it looked like a tidy one-shot step.
    #
    # CONFIRMED 2.5 um, NOT 0.1 mil. 0.1 mil (2.54) was the tidier guess -
    # this prober's Z axis is in 0.1-mil units - but it is wrong. The two
    # differ by 1.6%, invisible in one step, so it was settled by multiplying
    # the error up: 17 consecutive quad steps along row y=49260 of
    # HPLaMP_WHOLE_WAFER, sent as 2772 counts each (7042/2.54). Predicted
    # end-of-row drift was 19 um if the unit were 2.54 and 1904 um if it were
    # 2.50; the operator measured about half a die (1760 um). Solving for the
    # unit from that drift gives 2.503.
    #
    # WHY it is 2.5 is NOT established, and that matters: if the factor comes
    # from a prober configuration setting rather than being intrinsic (this
    # machine has scale-factor parameters - cf. SP12S "3 steps per mil" for Z,
    # and SX4C/SX5C wafer expansion), then changing prober setup silently
    # changes it, and micron moves would go wrong with nothing to flag it.
    # Die moves (MD) have no such dependency and are the safer default.
    MM_UNIT_UM = 2.50

    # Same guard as move_relative_die, in microns. These commands had NO bounds
    # check at all, which is how a value meant as microns went out as counts
    # and travelled 2.5x too far with nothing to stop it.
    DEFAULT_MAX_UM_STEP = 200000

    def _check_um(self, dx_um, dy_um):
        limit = getattr(self, "max_um_step", self.DEFAULT_MAX_UM_STEP)
        if max(abs(dx_um), abs(dy_um)) > limit:
            raise ValueError(
                f"Electroglas 2001X: micron move of ({dx_um:.0f},{dy_um:.0f}) um "
                f"exceeds max_um_step={limit}. Raise it deliberately if this is "
                f"really intended.")

    def _um_to_counts(self, um):
        return int(round(float(um) / self.MM_UNIT_UM))

    def move_relative_um(self, dx_um, dy_um):
        """Relative move in MICRONS, converted to MM's own counts."""
        self._check_um(dx_um, dy_um)
        status = self._motion(
            f"MMX{self._um_to_counts(dx_um)}Y{self._um_to_counts(dy_um)}")
        self.z_is_up = False
        return status

    def move_relative_counts(self, dx, dy):
        """Raw MM counts, for calibrating MM_UNIT_UM. No unit conversion."""
        status = self._motion(f"MMX{int(dx)}Y{int(dy)}")
        self.z_is_up = False
        return status

    def move_relative_m(self, dx, dy):
        """Deprecated alias - 'm' meant microns but sent raw counts, which is
        the bug above. Kept pointing at the raw form so nothing silently
        changes meaning; new callers should pick move_relative_um explicitly."""
        return self.move_relative_counts(dx, dy)

    def move_absolute_m(self, x, y):
        status = self._motion(f"MAX{int(x)}Y{int(y)}")
        self.z_is_up = False
        return status

    def move_micro(self, dx, dy):
        status = self._motion(f"FMX{int(dx)}Y{int(dy)}")
        self.z_is_up = False
        return status

    def move_xy_relative(self, dx_index: int, dy_index: int):
        self._not_implemented("move_xy_relative")

    def mark_current_die(self, category: str = ""):
        self._not_implemented("mark_current_die")

    def set_die_size(self, x, y):
        self.write(f"SP1X{int(x)}Y{int(y)}")

    def set_die_size_precise_mm(self, x_mm, y_mm):
        self.write(f"SP29X{_fmt6(x_mm)}Y{_fmt6(y_mm)}")

    @_serialised
    def infer_die_size(self, probe_x: int = 7042, probe_y: int = 3284) -> tuple:
        """The current SP1 die size, WITHOUT a direct query - there is none
        (see PRE_LAMP_SETTINGS's own note above). '?I' looks like it should
        answer this but does not - confirmed by direct experiment: it
        always reports a fixed 84000x51200 machine constant, unrelated to
        whatever SP1 is actually set to.

        Method (found and verified by direct experiment on this machine):
        SP1X<x>Y<y> changes the die size in microns WITHOUT moving the
        stage - the physical (micron) position stays fixed while the
        die-COUNT ?P reports rescales to match it. So:

            P_old * size_old == P_new * size_new

        Reading ?P before and after writing a KNOWN probe size solves for
        the size that was actually set before this ran:

            size_old = probe_size * P_new / P_old

        Verified example: ?P read X-10Y-4, then SP1X7042Y3284 (probe) was
        sent (acked MC), then ?P read X-5Y-2 - giving
        7042*5/10=3521 and 3284*2/4=1642, a clean whole-micron result (the
        sanity check; a wrong reading gives a ragged fraction instead).

        Restores the die size to the inferred value before returning, so
        the net effect on the machine's actual configuration is nothing -
        only the temporary probe write in between is not a no-op. Raises
        if position cannot be read either time, or if either axis' before-
        reading is 0 (division by zero - re-run once the chuck has moved
        off that axis).
        """
        before = self._parse_die_position(self.get_xy_position())
        if before is None:
            raise RuntimeError("infer_die_size: cannot read ?P before probing - "
                               "refusing to write a temporary die size blind")
        if before[0] == 0 or before[1] == 0:
            raise RuntimeError(f"infer_die_size: chuck is at {before} - at least "
                               "one axis is 0, which divides by zero. Jog off "
                               "that axis and try again.")
        self.set_die_size(probe_x, probe_y)
        after = self._parse_die_position(self.get_xy_position())
        if after is None:
            raise RuntimeError(
                "infer_die_size: cannot read ?P after probing. The die size is "
                f"now the probe value X{probe_x}Y{probe_y} and CANNOT be put "
                "back automatically - nothing can read what it was. Set it "
                "manually from SET PRMTR before running.")

        # ?P counts WHOLE dies, so probe*after/before recovers the true size
        # only when it divides exactly. Near the origin it does not: at
        # ?P X-5Y-1 with a real 3521x1642 this infers 2817 and 0. The result
        # is WRITTEN BACK, so a bad inference does not merely misreport - it
        # reconfigures the prober. Hence the round-trip check below.
        exact = all((probe * a) % b == 0 and a != 0
                    for probe, b, a in ((probe_x, before[0], after[0]),
                                        (probe_y, before[1], after[1])))
        size_x = round(probe_x * after[0] / before[0]) if after[0] else 0
        size_y = round(probe_y * after[1] / before[1]) if after[1] else 0

        if not exact or size_x <= 0 or size_y <= 0:
            raise RuntimeError(
                f"infer_die_size: ?P {before} -> {after} against probe "
                f"X{probe_x}Y{probe_y} does not divide exactly, so the answer "
                f"would be a guess (it computes X{size_x}Y{size_y}). The die "
                f"size is now the probe value X{probe_x}Y{probe_y} and cannot "
                "be restored automatically - set it from SET PRMTR, or move "
                "the chuck further from the origin on both axes and re-run.")

        # Necessary condition for the answer to be right: putting it back must
        # reproduce the reading we started from. Catches a plausible-looking
        # but wrong inference instead of leaving the machine quietly misset.
        self.set_die_size(size_x, size_y)
        back = self._parse_die_position(self.get_xy_position())
        if back != before:
            raise RuntimeError(
                f"infer_die_size: inferred X{size_x}Y{size_y}, but restoring it "
                f"made ?P read {back} instead of the original {before} - so that "
                "is NOT the size that was set. The die size is now "
                f"X{size_x}Y{size_y}; set the correct one from SET PRMTR.")
        return (size_x, size_y)

    def set_wafer_diameter(self, diameter):
        self.write(f"SP4D{int(diameter)}")

    def set_coordinate_quadrant(self, quadrant):
        self.write(f"SM11Q{int(quadrant)}")

    def set_count_pulse_width(self, width):
        self.write(f"SM32P{int(width)}")

    def set_current_cassette(self, cassette):
        self.write(f"SM70C{int(cassette)}")

    def set_date_time(self, when=None):
        when = when or datetime.datetime.now()
        self.write(f"TI{when.hour:02d}:{when.minute:02d}")

    def set_first_die(self):
        self.write("FD")

    def set_flat_orientation(self, orientation):
        self.write(f"SM3F{int(orientation)}")

    def set_probe_clean_count(self, count, w):
        self.write(f"SM12C{int(count)}W{int(w)}")

    def set_probe_quadrant(self, quadrant):
        self.write(f"SM2Q{int(quadrant)}")

    def set_profiler_retry_count(self, retries):
        self.write(f"SM42R{int(retries)}")

    def set_reference_die_coordinate(self, x, y):
        self.write(f"SP2X{int(x)}Y{int(y)}")

    def set_reprobe_count(self, count):
        self.write(f"SP14R{int(count)}")

    def set_starting_wafer_number(self, number):
        self.write(f"SM16N{int(number)}")

    def set_touchdown_counter(self, count):
        self.write(f"SP19C{int(count)}")

    def set_units(self, unit):
        self.write(f"SM1U{int(unit)}")

    def set_yield_to_pass_wafer(self, yield_pct):
        self.write(f"SP33Y{int(yield_pct)}")

    def set_z_autoalign_height(self, z):
        self.write(f"SP9Z{int(z)}")

    def set_z_travel_mode(self, mode):
        """SM5E<n> - how the prober decides where Z stops.

        THIS MACHINE WANTS EDGE SENSE, NOT MODE 2. Its own first screenshot
        read "EDGE-SENSE.SEP" before any configuration was sent; LaMP's SM5E2
        (auto profile) overwrote that, and ZU/ZD have been silent no-ops ever
        since - along with the panel's AUTO PROBE key. Auto profile waits on a
        profiler measurement that does not happen here, so nothing has a
        contact height to move to.

        The correct value for edge sense is NOT known. LaMP's table only
        captions 2, and no query reads the setting back, so take it off the
        prober's SET MODE page rather than guessing - a wrong mode changes how
        Z behaves at contact, which matters with needles over a wafer.

        Either way the touchdown remains SENSED: edge sense detects contact
        during Z-up and applies SP5Z overtravel past it. Never substitute an
        open-loop ZM for that.
        """
        return self.send_command(f"SM5E{int(mode)}")

    def set_z_clearance(self, z):
        self.write(f"SP6Z{int(z)}")

    def set_z_down_limit(self, z):
        self.write(f"SP8Z{int(z)}")

    def set_z_overtravel(self, z):
        self.write(f"SP5Z{int(z)}")

    def set_z_undertravel(self, z):
        self.write(f"SP10Z{int(z)}")

    def set_z_up_limit(self, z):
        self.write(f"SP7Z{int(z)}")

    def set_zprofile_height(self):
        self.write("PH")

    def set_wafer_x_expansion(self, coefficient):
        self.write(f"SX4C{int(coefficient)}")

    def set_wafer_y_expansion(self, coefficient):
        self.write(f"SX5C{int(coefficient)}")

    def set_die_size_mm(self, x_mm, y_mm):
        self.set_die_size(round(x_mm * 1000), round(y_mm * 1000))

    def set_die_size_mil(self, x_mil, y_mil):
        self.set_die_size(round(x_mil * 10), round(y_mil * 10))
