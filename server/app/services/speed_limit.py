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

Noise safeguard: a fix only counts if it's one history would draw —
gps_valid, not flagged is_outlier (an implausible position jump, see
app/api/locations.py's classify_outlier), and a reported speed no higher
than MAX_PLAUSIBLE_SPEED_KMH (the same 180 km/h cap the jump filter uses).
Such a fix is ignored entirely: it can't fire, arm, disarm, or extend an
episode. Before this an outlier fix could fire an alarm at a point the
route then hid.

Episodes: track_overspeed_episode mirrors the same state machine into an
OverspeedEpisode row (migration 055) — opened on the firing fix, closed on
the disarming fix — so the history endpoints' red segments and the alarm
are one record. Each episode keeps the threshold it opened with; changing
the limit mid-episode closes it ("limit_changed") instead of silently
re-judging it against a different number (see close_open_episode).
"""

from typing import Optional

from sqlalchemy.orm import Session

from app.api.locations import MAX_PLAUSIBLE_SPEED_KMH, TIME_GAP_SEGMENT_BREAK_SECONDS
from app.models.device import Device
from app.models.location import Location
from app.models.overspeed_episode import OverspeedEpisode

# See module docstring's "Hysteresis" section.
HYSTERESIS_KMH = 5.0


def is_evaluable_fix(speed_kmh: float, gps_valid: bool, is_outlier: bool) -> bool:
    """Whether a fix may affect overspeed state at all — see the module
    docstring's "Noise safeguard" section."""
    return gps_valid and not is_outlier and speed_kmh <= MAX_PLAUSIBLE_SPEED_KMH


def evaluate_speed_limit(
    device: Device, speed_kmh: float, gps_valid: bool, is_outlier: bool = False,
) -> bool:
    """
    Compares `speed_kmh` (this fix's reported speed) against
    device.speed_limit_kmh and updates device.is_overspeeding to match.
    Returns True exactly when this fix is what pushed the device from
    disarmed to over-limit — i.e. "synthesize a new Over speed alarm for
    this fix" — and False in every other case: already armed (regardless
    of how far above the limit this fix is, or whether it dipped back
    toward the limit without reaching the hysteresis floor), under limit
    while already disarmed, a fix that fails is_evaluable_fix, or no
    threshold configured (which also disarms, so a later re-set limit
    starts clean rather than inheriting a stuck armed flag).

    Mutates `device` (is_overspeeding) but does not commit — same
    convention as evaluate_geofences: the caller commits alongside the
    Location row for this same fix, in the same transaction.
    """
    if device.speed_limit_kmh is None:
        device.is_overspeeding = False
        return False
    if not is_evaluable_fix(speed_kmh, gps_valid, is_outlier):
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


def _open_episode(db: Session, device_id: int) -> Optional[OverspeedEpisode]:
    return (
        db.query(OverspeedEpisode)
        .filter(OverspeedEpisode.device_id == device_id, OverspeedEpisode.ended_at.is_(None))
        .order_by(OverspeedEpisode.started_at.desc())
        .first()
    )


def _close_at_last_fix(episode: OverspeedEpisode, reason: str) -> None:
    """Close without a closing fix of its own: the episode ends where its
    last fix was (end_latitude/end_longitude already track that)."""
    episode.ended_at = episode.last_fix_at
    episode.end_reason = reason


def close_open_episode(db: Session, device: Device, reason: str = "limit_changed") -> None:
    """Close the device's open episode, if any, at its last fix, and disarm
    the device. Called when the owner changes or clears the limit
    mid-episode (PUT /api/devices/{id}/speed_limit): the episode keeps the
    threshold it was judged against, and the next fix is evaluated fresh
    against the new limit (firing a new alarm if it's already over it —
    same as setting a limit while already speeding). Does not commit."""
    episode = _open_episode(db, device.id)
    if episode is not None:
        _close_at_last_fix(episode, reason)
    device.is_overspeeding = False


def track_overspeed_episode(
    db: Session,
    device: Device,
    location: Location,
    speed_kmh: float,
    evaluable: bool,
    was_overspeeding: bool,
    crossed: bool,
    alarm_location_id: Optional[int],
) -> Optional[OverspeedEpisode]:
    """
    Mirror evaluate_speed_limit's transition for this fix into
    OverspeedEpisode rows. Call right after evaluate_speed_limit with the
    device's is_overspeeding from *before* it (`was_overspeeding`) and its
    return value (`crossed`). `location` must already be flushed (needs
    its id). Returns the episode this fix opened/extended/closed, if any.
    Does not commit.
    """
    if crossed:
        # Defensive: the invariant is at most one open episode per device,
        # but a crash between commits must never leave two open forever.
        stale = _open_episode(db, device.id)
        if stale is not None:
            _close_at_last_fix(stale, "signal_lost")
        episode = OverspeedEpisode(
            device_id=device.id,
            alarm_location_id=alarm_location_id,
            start_location_id=location.id,
            started_at=location.timestamp,
            start_latitude=location.latitude,
            start_longitude=location.longitude,
            threshold_kmh=device.speed_limit_kmh,
            hysteresis_kmh=HYSTERESIS_KMH,
            max_speed_kmh=speed_kmh,
            last_fix_at=location.timestamp,
            end_latitude=location.latitude,
            end_longitude=location.longitude,
        )
        db.add(episode)
        return episode

    if not was_overspeeding:
        return None

    if device.speed_limit_kmh is None:
        # Limit cleared without going through the PUT endpoint's own close.
        episode = _open_episode(db, device.id)
        if episode is not None:
            _close_at_last_fix(episode, "limit_changed")
        return episode

    if not evaluable:
        return None

    # Episodes opened before migration 055 was live have no row: nothing
    # to extend or close, the next crossing starts a tracked one.
    episode = _open_episode(db, device.id)
    if episode is None:
        return None

    gap_seconds = (location.timestamp - episode.last_fix_at).total_seconds()
    if not device.is_overspeeding and gap_seconds > TIME_GAP_SEGMENT_BREAK_SECONDS:
        # The device went quiet mid-episode and came back already slow: we
        # don't know when it really slowed down, so the episode ends at its
        # last over-limit fix rather than stretching across the gap.
        _close_at_last_fix(episode, "signal_lost")
        return episode

    episode.last_fix_at = location.timestamp
    episode.end_latitude = location.latitude
    episode.end_longitude = location.longitude
    if device.is_overspeeding:
        episode.max_speed_kmh = max(episode.max_speed_kmh, speed_kmh)
    else:
        episode.ended_at = location.timestamp
        episode.end_reason = "speed_dropped"
    return episode
