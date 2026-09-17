"""
Server-side speed-limit evaluation.

A user-configurable alternative to the device's own fixed-firmware "Over
speed" alarm (GT06 alarm byte 0x06, app/protocol_parser.py) — neither
supported hardware model (TK903ELE, G900LS J16-4G) exposes a command to
read or set that firmware threshold, so it can never be surfaced or
changed from this backend (same gap app/services/geofencing.py's own
docstring documents for geofence zones). Device.speed_limit_kmh
(migration 029) lets an owner pick their own number instead; this module
evaluates it on every incoming location fix in app/tcp_server.py's
handle_location.

Edge-triggered, not level-triggered: fires only on the fix that first
crosses ABOVE the limit, then stays quiet (Device.is_overspeeding stays
True) for every subsequent still-over-limit fix, until a fix comes in at
or under the limit — which resets the flag, with no alarm of its own, so
the next crossing fires fresh. Without this, a vehicle cruising over the
limit for ten minutes at one fix every few seconds would synthesize a new
"Over speed" alarm (and Location.is_alarm row) on nearly every single fix.

Unlike app/services/geofencing.py's evaluate_geofences, there's no
"don't fire on first observation" special case here: Device.is_overspeeding
defaults to False for every device regardless of whether it's ever been
evaluated, so the very first fix processed after an owner sets a
threshold correctly fires if the vehicle is already over it at that
moment — which is exactly the alert an owner setting a limit while
already speeding would expect, unlike a geofence (where "already inside
a zone that just got created" isn't itself alarm-worthy).

Hysteresis: re-arming (going from is_overspeeding=True back to False)
requires the fix to drop all the way to (limit - HYSTERESIS_KMH), not
merely to-or-under the bare limit. Real GPS "speed" readings are noisy —
a vehicle genuinely cruising right around the limit can bounce a few
km/h either side of it fix to fix, and without this margin each blip
back over the bare limit resynthesizes a "new" alarm, producing dozens
of alerts an hour for one continuous driving episode. The hysteresis
band turns that into one alarm per real slow-down-then-speed-up cycle.
This only affects when the flag resets to False — the crossing that
originally sets it True is still the bare `speed_kmh > limit` line, so
"first crossing above the limit" is unaffected by the margin below it.
"""

from app.models.device import Device

# See module docstring's "Hysteresis" section.
HYSTERESIS_KMH = 5.0


def evaluate_speed_limit(device: Device, speed_kmh: float, gps_valid: bool) -> bool:
    """
    Compares `speed_kmh` (this fix's reported speed) against
    device.speed_limit_kmh and updates device.is_overspeeding to match.
    Returns True exactly when this fix is what pushed the device from
    disarmed to over-limit — i.e. "synthesize a new Over speed alarm for
    this fix" — and False in every other case: already armed (regardless
    of how far above the limit this fix is, or whether it dipped back
    toward the limit without reaching the hysteresis floor), under limit
    while already disarmed, an invalid fix, or no threshold configured.

    Mutates `device` (is_overspeeding) but does not commit — same
    convention as evaluate_geofences: the caller commits alongside the
    Location row for this same fix, in the same transaction.
    """
    if device.speed_limit_kmh is None or not gps_valid:
        return False

    limit = device.speed_limit_kmh

    if device.is_overspeeding:
        # Already armed for this excursion — only a drop past the
        # hysteresis floor disarms it; nothing here can fire a new alarm.
        if speed_kmh <= max(limit - HYSTERESIS_KMH, 0.0):
            device.is_overspeeding = False
        return False

    now_over = speed_kmh > limit
    device.is_overspeeding = now_over
    return now_over
