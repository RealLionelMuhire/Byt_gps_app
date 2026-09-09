"""
Resolve the CONFIRMED live position shown on the map (device.last_latitude/
longitude and the WebSocket location broadcast) from a raw incoming ping.

Root problem this fixes: those two surfaces previously took every raw ping
verbatim, with no gps_valid/is_outlier gate — unlike every historical/trip/
route query, which already goes through location_quality_filters(). TCP logs
for device 1 (2026-09-08 ~21:16-21:26) showed two failure modes as a result:

  1. A satellites=0 fix (gps_valid=false) still overwrote the live position.
  2. Every device reconnect reset GPS lock and reported a ~15-25m-shifted
     position for a stationary vehicle, with a couple of transient points
     showing a small nonzero "creep" speed while the fix re-converged —
     each one momentarily jumped the marker before it settled back down.

Case 1 is already solved by checking gps_valid/is_outlier (computed
upstream by classify_outlier) before calling into this module at all — see
tcp_server.py. Case 2 needed new logic: the jump is small and slow enough
(a few km/h, well under MAX_PLAUSIBLE_SPEED_KMH) that the existing
speed-implausibility outlier check never flags it. The signal that *does*
distinguish it from a real departure is that it happens while the device
reports itself stopped — a real trip starting is corroborated by the next
point continuing in the same direction at real speed, so it's never held.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from app.api.locations import haversine_km
from app.models.device import Device

# Normal consumer-GPS jitter envelope while parked. A jump at or below this
# is treated as noise, not movement — confirmed immediately, no hold.
CONFIRM_RADIUS_METERS = 8.0

# A held candidate older than this is stale — discard it rather than let a
# later, unrelated point confirm it by coincidence (e.g. the vehicle parking
# again near an old candidate hours later).
PENDING_MAX_AGE_SECONDS = 300


@dataclass
class LivePositionResult:
    updated: bool  # True if device.last_latitude/longitude changed
    held: bool     # True if this ping was staged as a pending candidate instead


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return haversine_km(lon1, lat1, lon2, lat2) * 1000.0


def resolve_live_position(
    device: Device,
    new_lat: float,
    new_lon: float,
    new_speed: Optional[float],
    stop_speed_threshold_kmh: float,
    now: datetime,
) -> LivePositionResult:
    """
    Update device.last_latitude/last_longitude (and pending_* bookkeeping)
    in place. Caller must only call this for a ping that already passed the
    gps_valid/is_outlier gate — this function does not re-check those.

    - First-ever fix: confirmed immediately.
    - Within CONFIRM_RADIUS_METERS of the current confirmed position: normal
      jitter or continued smooth movement — confirmed immediately.
    - Reported speed >= stop_speed_threshold_kmh: the device itself claims
      to be moving — trust it immediately, never hold a real trip hostage
      waiting for corroboration.
    - Otherwise (a jump while reportedly stopped): held as a pending
      candidate. Confirmed only once a second point lands within
      CONFIRM_RADIUS_METERS of that candidate (two agreeing points beat one
      reconnect transient) and the candidate isn't stale.
    """
    if device.last_latitude is None or device.last_longitude is None:
        device.last_latitude = new_lat
        device.last_longitude = new_lon
        device.pending_latitude = None
        device.pending_longitude = None
        device.pending_since = None
        return LivePositionResult(updated=True, held=False)

    dist_from_confirmed = _distance_meters(device.last_latitude, device.last_longitude, new_lat, new_lon)
    moving_now = new_speed is not None and new_speed >= stop_speed_threshold_kmh

    if dist_from_confirmed <= CONFIRM_RADIUS_METERS or moving_now:
        device.last_latitude = new_lat
        device.last_longitude = new_lon
        device.pending_latitude = None
        device.pending_longitude = None
        device.pending_since = None
        return LivePositionResult(updated=True, held=False)

    pending_is_fresh = (
        device.pending_latitude is not None
        and device.pending_since is not None
        and (now - device.pending_since) <= timedelta(seconds=PENDING_MAX_AGE_SECONDS)
    )
    if pending_is_fresh:
        dist_to_pending = _distance_meters(device.pending_latitude, device.pending_longitude, new_lat, new_lon)
        if dist_to_pending <= CONFIRM_RADIUS_METERS:
            device.last_latitude = new_lat
            device.last_longitude = new_lon
            device.pending_latitude = None
            device.pending_longitude = None
            device.pending_since = None
            return LivePositionResult(updated=True, held=False)

    # No fresh, corroborating candidate yet — stage this point and keep
    # showing the last confirmed position.
    device.pending_latitude = new_lat
    device.pending_longitude = new_lon
    device.pending_since = now
    return LivePositionResult(updated=False, held=True)
