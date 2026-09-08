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
"""

from app.models.device import Device


def evaluate_speed_limit(device: Device, speed_kmh: float, gps_valid: bool) -> bool:
    """
    Compares `speed_kmh` (this fix's reported speed) against
    device.speed_limit_kmh and updates device.is_overspeeding to match
    reality either way. Returns True exactly when this fix is what pushed
    the device from under-limit-or-never-flagged to over-limit — i.e.
    "synthesize a new Over speed alarm for this fix" — and False in every
    other case (already over limit as of the previous fix, under limit,
    an invalid fix, or no threshold configured at all).

    Mutates `device` (is_overspeeding) but does not commit — same
    convention as evaluate_geofences: the caller commits alongside the
    Location row for this same fix, in the same transaction.
    """
    if device.speed_limit_kmh is None or not gps_valid:
        return False

    now_over = speed_kmh > device.speed_limit_kmh
    was_over = device.is_overspeeding
    device.is_overspeeding = now_over
    return now_over and not was_over
