"""Location API endpoints"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from pydantic import BaseModel
from math import radians, degrees, cos, sin, asin, atan2, sqrt

from app.core.database import get_db
from app.core.auth import get_current_user, require_device_access
from app.models.location import Location
from app.models.location_quality_log import LocationQualityLog
from app.models.device import Device
from app.models.user import User, Role
from app.services.trip_settings_service import get_or_create_trip_settings
from app.services.geocoding import reverse_geocode_many

logger = logging.getLogger(__name__)
router = APIRouter()


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Calculate great-circle distance between two points in kilometers"""
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return 6371 * c


def bearing_degrees(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Initial great-circle bearing from point1 to point2, in degrees [0, 360)."""
    lon1r, lat1r, lon2r, lat2r = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2r - lon1r
    x = sin(dlon) * cos(lat2r)
    y = cos(lat1r) * sin(lat2r) - sin(lat1r) * cos(lat2r) * cos(dlon)
    return (degrees(atan2(x, y)) + 360) % 360


METERS_PER_DEGREE = 111_320.0  # approx meters per degree of latitude

# Starting threshold for GPS jump detection. Tunable — 180km/h comfortably
# covers Rwanda's road network (highway speeds rarely exceed ~120km/h) with
# headroom for GPS timing jitter; revisit if deployed in regions/vehicles
# with legitimately higher sustained speeds.
MAX_PLAUSIBLE_SPEED_KMH = 180.0


def implied_speed_kmh(
    lon1: float, lat1: float, t1: datetime,
    lon2: float, lat2: float, t2: datetime,
) -> Optional[float]:
    """Speed implied by straight-line distance between two fixes over their time delta.
    Returns None if the time delta isn't positive (can't compute a meaningful speed)."""
    dt_hours = (t2 - t1).total_seconds() / 3600.0
    if dt_hours <= 0:
        return None
    return haversine_km(lon1, lat1, lon2, lat2) / dt_hours


def classify_outlier(
    prev2: Optional["Location"], prev1: Optional["Location"],
    new_lon: float, new_lat: float, new_ts: datetime,
) -> tuple[bool, bool]:
    """
    Decide outlier status for an incoming point using a 3-point window
    (prev2, prev1, new-point), so a single bad fix causing a jump-and-return
    is caught on whichever leg is unambiguous rather than always blaming the
    newest point. Returns (new_point_is_outlier, retroactively_flag_prev1).

    Logic: if the forward leg (prev1 -> new) implies an implausible speed,
    but skipping prev1 entirely (prev2 -> new) implies a plausible one, then
    prev1 looks like the actual spike (jump-and-return: prev2 good, prev1
    bad, new is back near prev2) — flag prev1 retroactively, not new. If the
    skip-leg is also implausible (or there's no prev2 for context), we can't
    disambiguate, so the new point is flagged as the conservative default.

    If prev1 is ALREADY flagged as an outlier (e.g. the point right after a
    previously-identified spike), it isn't a reliable reference — of course
    the leg away from a known-bad point looks implausible, that's not
    evidence against `new`. In that case continuity is judged against prev2
    (the last known-good point) instead, so the legitimate "back on the
    road" point following an already-flagged spike doesn't get flagged too.
    If prev2 is ALSO already flagged (a back-to-back bad pair — e.g. two
    consecutive Null Island fixes), there's no reliable reference left in
    this window at all; rather than compare `new` against another known-bad
    point and risk a false positive, `new` is left unflagged (conservative:
    an under-flagged point just falls through like today, whereas a
    wrongly-flagged legitimate point would visibly drop real data).

    Reused identically by live ingestion (tcp_server.py) and the retroactive
    backfill script so behavior never drifts between the two.
    """
    if prev1 is None:
        return False, False

    if prev1.is_outlier:
        if prev2 is None or prev2.is_outlier:
            return False, False
        speed_prev2_new = implied_speed_kmh(
            prev2.longitude, prev2.latitude, prev2.timestamp, new_lon, new_lat, new_ts
        )
        return (speed_prev2_new is not None and speed_prev2_new > MAX_PLAUSIBLE_SPEED_KMH), False

    speed_prev1_new = implied_speed_kmh(
        prev1.longitude, prev1.latitude, prev1.timestamp, new_lon, new_lat, new_ts
    )
    if speed_prev1_new is None or speed_prev1_new <= MAX_PLAUSIBLE_SPEED_KMH:
        return False, False

    if prev2 is None:
        return True, False

    speed_prev2_new = implied_speed_kmh(
        prev2.longitude, prev2.latitude, prev2.timestamp, new_lon, new_lat, new_ts
    )
    if speed_prev2_new is not None and speed_prev2_new <= MAX_PLAUSIBLE_SPEED_KMH:
        return False, True

    return True, False


# Gap above which consecutive points are treated as a SEGMENT BREAK (device
# offline/disconnected) rather than continuous travel — not connected by a
# route line, not summed into distance. ~8.5x the observed p99 reporting
# gap (105s) on healthy connections (median ~11s, p95 ~20s), so normal
# reporting jitter never triggers it, while real offline-then-elsewhere
# gaps (observed: 14-29h on device 1) are caught with a lot of margin.
TIME_GAP_SEGMENT_BREAK_SECONDS = 15 * 60


def segment_locations_by_gap(
    locations: List["Location"], gap_seconds: float = TIME_GAP_SEGMENT_BREAK_SECONDS
) -> List[List["Location"]]:
    """Split a chronologically-ordered location list into segments, starting a
    new segment whenever the gap to the previous point exceeds gap_seconds.
    Never merges points across a segment break — callers must not sum
    distance or draw a route line across segment boundaries."""
    if not locations:
        return []
    segments: List[List["Location"]] = [[locations[0]]]
    for prev, curr in zip(locations, locations[1:]):
        if (curr.timestamp - prev.timestamp).total_seconds() > gap_seconds:
            segments.append([])
        segments[-1].append(curr)
    return segments


def compute_offline_gaps(
    locations: List["Location"], gap_seconds: float = TIME_GAP_SEGMENT_BREAK_SECONDS
) -> List[dict]:
    """Report every gap in a chronologically-ordered location list that
    exceeds gap_seconds, as its own concept distinct from driving
    time/distance (e.g. surfaced as "device offline for 3h 20m")."""
    gaps = []
    for prev, curr in zip(locations, locations[1:]):
        delta = (curr.timestamp - prev.timestamp).total_seconds()
        if delta > gap_seconds:
            gaps.append({
                "gap_start": prev.timestamp,
                "gap_end": curr.timestamp,
                "gap_seconds": delta,
            })
    return gaps


def find_low_speed_runs(
    locations: List["Location"], speed_threshold_kmh: float
) -> List[Tuple[int, int]]:
    """
    Find maximal runs of consecutive points with speed < speed_threshold_kmh
    (a point with speed=None is never considered stopped). Returns a list of
    (start_idx, end_idx) pairs, end exclusive.

    This is the "stopped" concept trip auto-segmentation already applies via
    TripSettings.stop_speed_threshold_kmh (see detect_trip_segments), pulled
    out as its own reusable primitive so period-route can classify stopped
    spans using the identical threshold/comparison rather than a duplicated
    inline check. detect_trip_segments itself is left on its own inline walk
    (it interleaves this with a reporting-gap check over a shared index and
    has no test coverage of its own) rather than being rewired onto this —
    consolidating them is a follow-up, not bundled into this change.
    """
    runs: List[Tuple[int, int]] = []
    i = 0
    n = len(locations)
    while i < n:
        if locations[i].speed is not None and locations[i].speed < speed_threshold_kmh:
            start = i
            while i < n and locations[i].speed is not None and locations[i].speed < speed_threshold_kmh:
                i += 1
            runs.append((start, i))
        else:
            i += 1
    return runs


# Below this movement distance, the prev->curr bearing is dominated by GPS
# position noise rather than actual heading, so a course-vs-bearing
# comparison would be meaningless — treated as "not computable" instead.
MIN_MOVEMENT_KM_FOR_BEARING = 0.005  # 5 meters


def compute_quality_log_fields(
    prev1: Optional["Location"], lon: float, lat: float, course: int, satellites: int, ts: datetime,
) -> dict:
    """
    Derive the per-point GPS quality diagnostics logged to
    location_quality_log (see that model for field meanings): implied
    speed and reporting gap vs. the previous point, direction-consistency
    (reported course vs. the movement bearing implied by prev1 -> this
    point), and whether the gap itself is a segment break. All of these
    are None/False when there's no previous point (device's first fix) or
    (for course_delta_degrees) when movement is too small to imply a
    meaningful bearing. Reused identically by live ingestion
    (tcp_server.py) and the quality-log backfill script.
    """
    if prev1 is None:
        return {
            "satellites": satellites,
            "implied_speed_kmh": None,
            "course_delta_degrees": None,
            "gap_seconds": None,
            "is_segment_break": False,
        }

    gap_seconds = (ts - prev1.timestamp).total_seconds()
    speed = implied_speed_kmh(prev1.longitude, prev1.latitude, prev1.timestamp, lon, lat, ts)

    course_delta = None
    if haversine_km(prev1.longitude, prev1.latitude, lon, lat) >= MIN_MOVEMENT_KM_FOR_BEARING:
        movement_bearing = bearing_degrees(prev1.longitude, prev1.latitude, lon, lat)
        diff = abs(course - movement_bearing) % 360
        course_delta = 360 - diff if diff > 180 else diff

    return {
        "satellites": satellites,
        "implied_speed_kmh": speed,
        "course_delta_degrees": course_delta,
        "gap_seconds": gap_seconds,
        "is_segment_break": gap_seconds > TIME_GAP_SEGMENT_BREAK_SECONDS,
    }


def location_quality_filters(device_id: int):
    """
    Shared quality-gate filters for querying "real" GPS points for a device:
    excludes points the device didn't itself report a fix for (gps_valid)
    and points flagged as implausible jumps (is_outlier). Reused by every
    call site that computes distance, route geometry, or trip segmentation
    so quality-filtering never drifts between them. Deliberately NOT used by
    /history, which stays a raw/unfiltered diagnostic view.
    """
    return (
        Location.device_id == device_id,
        Location.gps_valid == True,
        Location.is_outlier == False,
    )


def _perpendicular_distance(point: "Location", start: "Location", end: "Location") -> float:
    """Perpendicular distance from `point` to line (start, end), in degrees (lon/lat plane)."""
    x, y = point.longitude, point.latitude
    x1, y1 = start.longitude, start.latitude
    x2, y2 = end.longitude, end.latitude
    if x1 == x2 and y1 == y2:
        return sqrt((x - x1) ** 2 + (y - y1) ** 2)
    num = abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1)
    den = sqrt((y2 - y1) ** 2 + (x2 - x1) ** 2)
    return num / den


def douglas_peucker(points: List["Location"], epsilon: float) -> List["Location"]:
    """
    Ramer-Douglas-Peucker simplification (iterative/stack-based, to avoid
    recursion-depth issues on long tracks). `epsilon` is the max perpendicular
    distance (degrees) a point may deviate before being dropped. Endpoints kept.
    """
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        dmax, index = 0.0, start
        for i in range(start + 1, end):
            d = _perpendicular_distance(points[i], points[start], points[end])
            if d > dmax:
                index, dmax = i, d
        if dmax > epsilon:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
    return [p for p, k in zip(points, keep) if k]


# Max period-route span. Bounds worst-case query size and response size;
# reject rather than silently truncate so callers know to narrow the range.
MAX_PERIOD_DAYS = 30

# How far back /since-last-stop looks to find the device's most recent
# qualifying stop. Bounds the query so this stays a quick live-trail lookup
# rather than paging through a device's whole history; comfortably covers a
# full day's park/drive/re-park cycle for a vehicle that stops at least
# daily. If no qualifying stop is found within this window, the earliest
# point in the window is used as the trail start instead (see
# get_route_since_last_stop).
SINCE_LAST_STOP_LOOKBACK_HOURS = 48

# Backstop for a "driving" segment that's still too large after (optional)
# Douglas-Peucker simplification. DP alone handles the common case; this
# only kicks in on pathologically dense/noisy segments.
MAX_POINTS_PER_DRIVING_SEGMENT = 2000


def _downsample_uniform(points: List["Location"], max_points: int) -> List["Location"]:
    """Uniformly stride through points down to at most max_points, always
    keeping the first and last point."""
    if len(points) <= max_points:
        return points
    step = (len(points) - 1) / (max_points - 1)
    indices = sorted({round(i * step) for i in range(max_points)})
    return [points[i] for i in indices]


def _make_driving_or_stopped_leftover(
    points: List["Location"], simplify: bool, epsilon_deg: float
) -> Optional[dict]:
    """Build the segment for a run of points that fell between stop-runs
    (i.e. wasn't classified "stopped"). A lone point can't form a driving
    segment (no distance/duration to show), so it's reported as a
    zero-duration "stopped" segment instead of being silently dropped."""
    if not points:
        return None
    if len(points) == 1:
        return _make_stopped_segment(points)
    return _make_driving_segment(points, simplify, epsilon_deg)


def _make_driving_segment(points: List["Location"], simplify: bool, epsilon_deg: float) -> dict:
    distance_km = 0.0
    for i in range(1, len(points)):
        distance_km += haversine_km(
            points[i - 1].longitude, points[i - 1].latitude,
            points[i].longitude, points[i].latitude,
        )
    display_points = douglas_peucker(points, epsilon_deg) if (simplify and len(points) > 2) else points
    if len(display_points) > MAX_POINTS_PER_DRIVING_SEGMENT:
        display_points = _downsample_uniform(display_points, MAX_POINTS_PER_DRIVING_SEGMENT)
    return {
        "type": "driving",
        "start_time": points[0].timestamp.isoformat(),
        "end_time": points[-1].timestamp.isoformat(),
        "duration_seconds": (points[-1].timestamp - points[0].timestamp).total_seconds(),
        "distance_km": round(distance_km, 3),
        "point_count": len(display_points),
        "raw_point_count": len(points),
        "points": [
            {
                "latitude": p.latitude,
                "longitude": p.longitude,
                "timestamp": p.timestamp.isoformat(),
                "speed": p.speed,
                "course": p.course,
            }
            for p in display_points
        ],
    }


def _make_stopped_segment(points: List["Location"]) -> dict:
    """Represents a stopped span as its centroid (mean lat/lon across the
    run) rather than an arbitrary single point, so minor GPS jitter while
    parked doesn't bias the reported position."""
    lat = sum(p.latitude for p in points) / len(points)
    lon = sum(p.longitude for p in points) / len(points)
    return {
        "type": "stopped",
        "start_time": points[0].timestamp.isoformat(),
        "end_time": points[-1].timestamp.isoformat(),
        "duration_seconds": (points[-1].timestamp - points[0].timestamp).total_seconds(),
        "latitude": lat,
        "longitude": lon,
        "point_count": len(points),
    }


def _make_offline_segment(gap: dict, last_known: "Location", next_known: "Location") -> dict:
    return {
        "type": "offline",
        "start_time": gap["gap_start"].isoformat(),
        "end_time": gap["gap_end"].isoformat(),
        "duration_seconds": gap["gap_seconds"],
        "last_known": {
            "latitude": last_known.latitude,
            "longitude": last_known.longitude,
            "timestamp": last_known.timestamp.isoformat(),
        },
        "next_known": {
            "latitude": next_known.latitude,
            "longitude": next_known.longitude,
            "timestamp": next_known.timestamp.isoformat(),
        },
    }


def build_period_segments(
    locations: List["Location"],
    stop_speed_threshold_kmh: float,
    min_stop_seconds: float,
    simplify: bool = True,
    tolerance_meters: float = 20.0,
) -> List[dict]:
    """
    Build an ordered, typed timeline of "driving" / "stopped" / "offline"
    segments spanning a whole period (potentially many trips), for the
    Historical Routes period-route view.

    Reuses the same primitives single-trip routes and auto-segmentation
    already use, so this view never drifts from the location-quality work
    done elsewhere:
      - "offline": segment_locations_by_gap / compute_offline_gaps
        (TIME_GAP_SEGMENT_BREAK_SECONDS — the teleport-fix threshold)
      - "stopped": find_low_speed_runs (stop_speed_threshold_kmh — the same
        setting trip auto-segmentation uses), filtered to runs lasting at
        least min_stop_seconds so brief dips (e.g. a red light) don't
        fragment "driving" into noise
      - "driving": whatever's left between stops/gaps, optionally
        Douglas-Peucker simplified per segment (never across a boundary)

    Caller is responsible for pre-filtering to quality-passing, in-range,
    chronologically-ordered locations (see location_quality_filters).
    """
    if not locations:
        return []

    epsilon_deg = tolerance_meters / METERS_PER_DEGREE
    gap_segments = segment_locations_by_gap(locations)
    offline_gaps = compute_offline_gaps(locations)

    result: List[dict] = []
    for idx, seg in enumerate(gap_segments):
        stop_runs = [
            (s, e) for (s, e) in find_low_speed_runs(seg, stop_speed_threshold_kmh)
            if (seg[e - 1].timestamp - seg[s].timestamp).total_seconds() >= min_stop_seconds
        ]
        cursor = 0
        for s, e in stop_runs:
            leftover = _make_driving_or_stopped_leftover(seg[cursor:s], simplify, epsilon_deg)
            if leftover:
                result.append(leftover)
            result.append(_make_stopped_segment(seg[s:e]))
            cursor = e
        leftover = _make_driving_or_stopped_leftover(seg[cursor:], simplify, epsilon_deg)
        if leftover:
            result.append(leftover)

        if idx < len(offline_gaps):
            result.append(_make_offline_segment(offline_gaps[idx], seg[-1], gap_segments[idx + 1][0]))

    return result


def find_since_last_stop_window(
    locations: List["Location"],
    stop_speed_threshold_kmh: float,
    min_stop_seconds: float,
    fallback_start: datetime,
) -> Tuple[List["Location"], datetime, Optional[dict], bool]:
    """
    Find the device's most recent qualifying stop within `locations`
    (chronologically-ordered, quality-filtered, already bounded to the
    caller's lookback window) and the live trail since it, for
    /since-last-stop.

    Gap-segments first (so a stop run never spans an offline gap), then
    scans segments newest -> oldest so the most recent qualifying stop wins
    even if the latest gap-segment (e.g. since coming back online) has no
    stop of its own yet.

    Returns (trail_locations, since_time, last_stop, currently_stopped):
      - trail_locations: points from the stop's end (inclusive) to the end
        of `locations`, or [] if still within that stop (no movement since)
      - since_time: timestamp the trail starts from (the stop's end time,
        or the earliest available point / fallback_start if no qualifying
        stop was found)
      - last_stop: {start_time, end_time, latitude, longitude} (centroid)
        of the most recent qualifying stop, or None if none was found
      - currently_stopped: True if the device hasn't moved since that stop
    """
    gap_segments = segment_locations_by_gap(locations)
    seg_offsets = []
    cursor = 0
    for seg in gap_segments:
        seg_offsets.append(cursor)
        cursor += len(seg)

    for seg_idx in range(len(gap_segments) - 1, -1, -1):
        seg = gap_segments[seg_idx]
        stop_runs = [
            (s, e) for (s, e) in find_low_speed_runs(seg, stop_speed_threshold_kmh)
            if (seg[e - 1].timestamp - seg[s].timestamp).total_seconds() >= min_stop_seconds
        ]
        if stop_runs:
            s, e = stop_runs[-1]
            last_stop = {
                "start_time": seg[s].timestamp.isoformat(),
                "end_time": seg[e - 1].timestamp.isoformat(),
                "latitude": sum(p.latitude for p in seg[s:e]) / (e - s),
                "longitude": sum(p.longitude for p in seg[s:e]) / (e - s),
            }
            # Only "still stopped" if this is the newest segment and the
            # stop run reaches its last point — no movement (and no new
            # offline gap) since.
            currently_stopped = (seg_idx == len(gap_segments) - 1 and e == len(seg))
            trail_start_idx = seg_offsets[seg_idx] + (e - 1)
            trail_locations = [] if currently_stopped else locations[trail_start_idx:]
            return trail_locations, locations[trail_start_idx].timestamp, last_stop, currently_stopped

    if locations:
        # No qualifying stop within the lookback window — fall back to the
        # earliest point available rather than returning nothing.
        return locations, locations[0].timestamp, None, False
    return [], fallback_start, None, False


# Geocoding a whole period could, worst case, hit many genuinely distinct
# driving-segment endpoints -- Nominatim allows only 1 req/sec, so cap total
# geocoding wall-clock time per request rather than let it stall the
# response indefinitely. Segments not resolved within the cap just fall
# back to coordinate strings; the route itself is never blocked on this.
# 45s comfortably covers ~20 distinct new locations (a full week's worth of
# driving segments, typically) at ~1-2s each (network + fair-use delay);
# a first-time 30-day period with many distinct stops may still exceed it
# for some segments -- those just fall back, and get cached for next time
# (the background geocoding thread isn't cancelled by the timeout, so the
# DB cache still ends up warmed even for calls that fell back).
PERIOD_ROUTE_GEOCODING_TIMEOUT_SECONDS = 45.0


def _driving_segment_endpoints(segments: List[dict]) -> List[Tuple[float, float]]:
    """Collect (lat, lon) for the start and end point of every "driving"
    segment, for a single batched reverse-geocoding pass."""
    coords: List[Tuple[float, float]] = []
    for s in segments:
        if s["type"] == "driving" and s["points"]:
            coords.append((s["points"][0]["latitude"], s["points"][0]["longitude"]))
            coords.append((s["points"][-1]["latitude"], s["points"][-1]["longitude"]))
    return coords


def _attach_driving_place_names(segments: List[dict], places: Dict[Tuple[float, float], Optional[str]]) -> None:
    """Attach start_place/end_place/display_name to each "driving" segment
    in place, from a {(lat, lon): place_name_or_None} lookup (see
    reverse_geocode_many). Falls back to a coordinate string wherever
    geocoding didn't resolve (failed, timed out, or no point data)."""
    for s in segments:
        if s["type"] != "driving" or not s["points"]:
            continue
        start_lat, start_lon = s["points"][0]["latitude"], s["points"][0]["longitude"]
        end_lat, end_lon = s["points"][-1]["latitude"], s["points"][-1]["longitude"]
        start_place = places.get((start_lat, start_lon)) or f"{start_lat:.4f}, {start_lon:.4f}"
        end_place = places.get((end_lat, end_lon)) or f"{end_lat:.4f}, {end_lon:.4f}"
        s["start_place"] = start_place
        s["end_place"] = end_place
        s["display_name"] = f"{start_place} → {end_place}"


def verify_device_access(device_id: int, user: User, db: Session) -> Device:
    """Verify device exists and the caller owns it (or is admin)."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return require_device_access(device, user)


def compute_distance_for_device_time_range(
    device_id: int, start_time: datetime, end_time: datetime, db: Session
) -> tuple[float, List[Location]]:
    """
    Compute total distance for device in time range. Reusable by trips API.
    Returns (total_distance_km, locations). Only GPS-valid points are used.
    Distance is never summed across a segment break (see
    TIME_GAP_SEGMENT_BREAK_SECONDS) — an offline-then-elsewhere gap
    contributes zero distance rather than a straight-line "teleport".
    """
    query = db.query(Location).filter(*location_quality_filters(device_id))
    query = query.filter(Location.timestamp >= start_time)
    query = query.filter(Location.timestamp <= end_time)
    locations = query.order_by(Location.timestamp.asc()).all()

    total_distance = 0.0
    for segment in segment_locations_by_gap(locations):
        for i in range(1, len(segment)):
            prev = segment[i - 1]
            curr = segment[i]
            total_distance += haversine_km(prev.longitude, prev.latitude, curr.longitude, curr.latitude)

    return round(total_distance, 3), locations


def fetch_route_line_for_range(
    device_id: int, start_time: datetime, end_time: datetime, db: Session
) -> dict:
    """
    Fetch route line structure for device in time range. Reusable by trips API.

    Returns a MultiLineString-shaped dict: one sub-segment per contiguous run
    of points with no segment break (see TIME_GAP_SEGMENT_BREAK_SECONDS)
    between them, plus a `gaps` list describing what was skipped. Consumers
    must draw each segment as its own polyline — never connect across a gap.
    """
    query = db.query(Location).filter(*location_quality_filters(device_id))
    query = query.filter(Location.timestamp >= start_time)
    query = query.filter(Location.timestamp <= end_time)
    locations = query.order_by(Location.timestamp.asc()).all()

    segments = []
    for seg in segment_locations_by_gap(locations):
        segments.append({
            "coordinates": [[loc.longitude, loc.latitude] for loc in seg],
            "timestamps": [loc.timestamp.isoformat() for loc in seg],
            "speeds": [loc.speed for loc in seg],
            "courses": [loc.course for loc in seg],
            "start_time": seg[0].timestamp.isoformat(),
            "end_time": seg[-1].timestamp.isoformat(),
            "point_count": len(seg),
        })

    gaps = [
        {
            "gap_start": g["gap_start"].isoformat(),
            "gap_end": g["gap_end"].isoformat(),
            "gap_seconds": g["gap_seconds"],
        }
        for g in compute_offline_gaps(locations)
    ]

    return {
        "type": "MultiLineString",
        "segments": segments,
        "gaps": gaps,
        "properties": {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "point_count": len(locations),
            "segment_count": len(segments),
            "offline_seconds": sum(g["gap_seconds"] for g in gaps),
        }
    }


# Pydantic schemas
class LocationResponse(BaseModel):
    id: int
    device_id: int
    latitude: float
    longitude: float
    speed: float
    course: int
    satellites: int
    gps_valid: bool
    is_outlier: bool
    is_alarm: bool
    alarm_type: Optional[str]
    timestamp: datetime
    received_at: datetime
    
    class Config:
        from_attributes = True


class LocationHistoryResponse(BaseModel):
    device_id: int
    device_name: str
    device_imei: str
    total_points: int
    locations: List[LocationResponse]


class DistanceResponse(BaseModel):
    device_id: int
    device_name: str
    device_imei: str
    start_time: datetime
    end_time: datetime
    point_count: int
    total_distance_km: float


class RouteSegment(BaseModel):
    coordinates: List[List[float]]
    timestamps: List[str]
    speeds: List[float]
    courses: List[int]
    start_time: str
    end_time: str
    point_count: int


class RouteGap(BaseModel):
    gap_start: str
    gap_end: str
    gap_seconds: float


class RouteLineStringResponse(BaseModel):
    type: str
    segments: List[RouteSegment]
    gaps: List[RouteGap]
    properties: dict


class LocationQualityLogEntry(BaseModel):
    id: int
    location_id: int
    device_id: int
    timestamp: datetime
    satellites: int
    implied_speed_kmh: Optional[float]
    course_delta_degrees: Optional[float]
    gap_seconds: Optional[float]
    is_outlier: bool
    is_segment_break: bool

    class Config:
        from_attributes = True


class LocationQualityLogResponse(BaseModel):
    device_id: int
    device_name: str
    device_imei: str
    total_points: int
    entries: List[LocationQualityLogEntry]


@router.get("/{device_id}/latest", response_model=LocationResponse)
async def get_latest_location(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get latest location for a device."""
    verify_device_access(device_id, user, db)

    location = db.query(Location).filter(
        Location.device_id == device_id
    ).order_by(Location.timestamp.desc()).first()

    if not location:
        raise HTTPException(status_code=404, detail="No location data found for device")

    return location


@router.get("/{device_id}/history", response_model=LocationHistoryResponse)
async def get_location_history(
    device_id: int,
    start_time: Optional[datetime] = Query(None, description="Start time (UTC)"),
    end_time: Optional[datetime] = Query(None, description="End time (UTC)"),
    limit: int = Query(1000, ge=1, le=10000, description="Maximum number of points"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get location history for a device."""
    device = verify_device_access(device_id, user, db)
    
    # Build query
    query = db.query(Location).filter(Location.device_id == device_id)
    
    # Apply time filters
    if start_time:
        query = query.filter(Location.timestamp >= start_time)
    else:
        # Default to last 24 hours
        start_time = datetime.utcnow() - timedelta(hours=24)
        query = query.filter(Location.timestamp >= start_time)
    
    if end_time:
        query = query.filter(Location.timestamp <= end_time)
    
    # Get total count
    total = query.count()
    
    # Get locations
    locations = query.order_by(Location.timestamp.desc()).limit(limit).all()
    
    return LocationHistoryResponse(
        device_id=device.id,
        device_name=device.name,
        device_imei=device.imei,
        total_points=total,
        locations=locations
    )


@router.get("/{device_id}/quality-log", response_model=LocationQualityLogResponse)
async def get_location_quality_log(
    device_id: int,
    start_time: Optional[datetime] = Query(None, description="Start time (UTC)"),
    end_time: Optional[datetime] = Query(None, description="End time (UTC)"),
    limit: int = Query(1000, ge=1, le=10000, description="Maximum number of entries"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Per-point GPS quality diagnostics: satellites, implied speed vs. the
    previous point, direction-consistency (reported course vs. movement
    bearing), reporting gap, and outlier/segment-break flags. For ongoing
    tuning and as an audit trail — not just debugging.
    """
    device = verify_device_access(device_id, user, db)

    query = db.query(LocationQualityLog).filter(LocationQualityLog.device_id == device_id)

    if start_time:
        query = query.filter(LocationQualityLog.timestamp >= start_time)
    else:
        start_time = datetime.utcnow() - timedelta(hours=24)
        query = query.filter(LocationQualityLog.timestamp >= start_time)

    if end_time:
        query = query.filter(LocationQualityLog.timestamp <= end_time)

    total = query.count()
    entries = query.order_by(LocationQualityLog.timestamp.desc()).limit(limit).all()

    return LocationQualityLogResponse(
        device_id=device.id,
        device_name=device.name,
        device_imei=device.imei,
        total_points=total,
        entries=entries,
    )


def _build_route_response(
    device: "Device",
    locations: List["Location"],
    start_time: datetime,
    end_time: datetime,
    simplify: bool,
    tolerance_meters: float,
) -> dict:
    """
    Build the simplified-route FeatureCollection shared by /route and
    /since-last-stop: segment by gap, then (optionally) Douglas-Peucker
    simplify within each segment — never blending simplification across a
    segment break. Caller is responsible for pre-filtering to
    quality-passing, in-range, chronologically-ordered locations (see
    location_quality_filters).
    """
    original_point_count = len(locations)
    epsilon_deg = tolerance_meters / METERS_PER_DEGREE

    # Segment first, then simplify (if requested) within each segment — never
    # let simplification blend points across a segment break.
    location_segments = segment_locations_by_gap(locations)
    gaps = [
        {
            "gap_start": g["gap_start"].isoformat(),
            "gap_end": g["gap_end"].isoformat(),
            "gap_seconds": g["gap_seconds"],
        }
        for g in compute_offline_gaps(locations)
    ]

    features = []
    segments_meta = []
    for seg_id, seg in enumerate(location_segments):
        seg_locations = douglas_peucker(seg, epsilon_deg) if (simplify and len(seg) > 2) else seg
        for loc in seg_locations:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [loc.longitude, loc.latitude]
                },
                "properties": {
                    "timestamp": loc.timestamp.isoformat(),
                    "speed": loc.speed,
                    "course": loc.course,
                    "is_alarm": loc.is_alarm,
                    "segment_id": seg_id
                }
            })
        segments_meta.append({
            "segment_id": seg_id,
            "start_time": seg[0].timestamp.isoformat(),
            "end_time": seg[-1].timestamp.isoformat(),
            "point_count": len(seg_locations)
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "segments": segments_meta,
        "gaps": gaps,
        "properties": {
            "device_id": device.id,
            "device_name": device.name,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "point_count": len(features),
            "simplified": simplify,
            "original_point_count": original_point_count,
            "segment_count": len(segments_meta),
            "offline_seconds": sum(g["gap_seconds"] for g in gaps)
        }
    }


@router.get("/{device_id}/route")
async def get_device_route(
    device_id: int,
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    simplify: bool = Query(False, description="Simplify route via Douglas-Peucker to reduce point count"),
    tolerance_meters: float = Query(
        20.0, ge=1.0, le=1000.0,
        description="Max deviation in meters a point may contribute before being dropped (only used when simplify=true)"
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get device route (optimized for map display)."""
    device = verify_device_access(device_id, user, db)

    query = db.query(Location).filter(*location_quality_filters(device_id))

    if start_time:
        query = query.filter(Location.timestamp >= start_time)
    else:
        start_time = datetime.utcnow() - timedelta(hours=24)
        query = query.filter(Location.timestamp >= start_time)

    if end_time:
        query = query.filter(Location.timestamp <= end_time)

    locations = query.order_by(Location.timestamp.asc()).all()

    return _build_route_response(
        device, locations, start_time, end_time or datetime.utcnow(), simplify, tolerance_meters
    )


@router.get("/{device_id}/distance", response_model=DistanceResponse)
async def get_device_distance(
    device_id: int,
    start_time: Optional[datetime] = Query(None, description="Start time (UTC)"),
    end_time: Optional[datetime] = Query(None, description="End time (UTC)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Get total distance covered by a device within a time range.

    Distance is calculated using the Haversine formula across consecutive GPS points.
    Only GPS-valid points are used.
    """
    device = verify_device_access(device_id, user, db)

    if not start_time:
        start_time = datetime.utcnow() - timedelta(hours=24)
    if not end_time:
        end_time = datetime.utcnow()

    total_distance, locations = compute_distance_for_device_time_range(
        device_id, start_time, end_time, db
    )

    return DistanceResponse(
        device_id=device.id,
        device_name=device.name,
        device_imei=device.imei,
        start_time=start_time,
        end_time=end_time,
        point_count=len(locations),
        total_distance_km=total_distance
    )


@router.get("/{device_id}/route-line", response_model=RouteLineStringResponse)
async def get_device_route_line(
    device_id: int,
    start_time: Optional[datetime] = Query(None, description="Start time (UTC)"),
    end_time: Optional[datetime] = Query(None, description="End time (UTC)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Get device route as a MultiLineString (one sub-segment per contiguous
    run of points, split at offline/time-gap breaks) with timestamps
    aligned to coordinates, plus a `gaps` list of the breaks themselves.
    """
    device = verify_device_access(device_id, user, db)

    if not start_time:
        start_time = datetime.utcnow() - timedelta(hours=24)
    if not end_time:
        end_time = datetime.utcnow()

    result = fetch_route_line_for_range(device_id, start_time, end_time, db)
    result["properties"]["device_id"] = device.id
    result["properties"]["device_name"] = device.name
    result["properties"]["device_imei"] = device.imei
    return result


@router.get("/{device_id}/period-route")
async def get_period_route(
    device_id: int,
    start: datetime = Query(..., description="Period start (UTC)"),
    end: datetime = Query(..., description="Period end (UTC)"),
    simplify: bool = Query(True, description="Simplify driving segments via Douglas-Peucker to reduce point count"),
    tolerance_meters: float = Query(
        20.0, ge=1.0, le=1000.0,
        description="Max deviation in meters a point may contribute before being dropped (only used when simplify=true)"
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Aggregate ALL trips/segments for a device within [start, end] into one
    ordered, typed timeline for Historical Routes: "driving" (route points +
    distance), "stopped" (location + duration), and "offline" (gap +
    last/next known position) segments.

    Unlike /route or /route-line, which return one flat time range with no
    stop/offline typing, this classifies every span of the period using the
    same settings and thresholds as trip auto-segmentation and the
    teleport-fix gap logic (location_quality_filters, stop_speed_threshold_kmh,
    min_stop_segment_minutes, TIME_GAP_SEGMENT_BREAK_SECONDS), so this view
    never drifts from the rest of the location-quality work.
    """
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if (end - start) > timedelta(days=MAX_PERIOD_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"Period exceeds max of {MAX_PERIOD_DAYS} days; narrow the start/end range."
        )

    device = verify_device_access(device_id, user, db)
    settings = get_or_create_trip_settings(user.id, db)

    query = db.query(Location).filter(*location_quality_filters(device_id))
    query = query.filter(Location.timestamp >= start, Location.timestamp <= end)
    locations = query.order_by(Location.timestamp.asc()).all()

    segments = build_period_segments(
        locations,
        stop_speed_threshold_kmh=settings.stop_speed_threshold_kmh,
        min_stop_seconds=settings.min_stop_segment_minutes * 60,
        simplify=simplify,
        tolerance_meters=tolerance_meters,
    )

    coords = _driving_segment_endpoints(segments)
    if coords:
        # `places` is passed into reverse_geocode_many and written into as
        # each point resolves, so if the overall deadline fires mid-batch we
        # still have whatever finished before it -- points resolved in time
        # get real names, only the still-in-flight ones fall back to
        # coordinates (see reverse_geocode_many's docstring). The background
        # thread is not cancelled by the timeout and keeps populating both
        # `places` (harmlessly, after the response has already been built
        # from a stale read) and the DB cache for next time.
        places: Dict[Tuple[float, float], Optional[str]] = {}
        try:
            await asyncio.wait_for(
                asyncio.to_thread(reverse_geocode_many, coords, places),
                timeout=PERIOD_ROUTE_GEOCODING_TIMEOUT_SECONDS,
            )
        except Exception as e:
            distinct_requested = len(set(coords))
            logger.warning(
                "Geocoding for period-route (device %s) did not finish within %.0fs "
                "(%d/%d distinct point(s) resolved in time): %s",
                device_id, PERIOD_ROUTE_GEOCODING_TIMEOUT_SECONDS,
                len(places), distinct_requested, e,
            )
        _attach_driving_place_names(segments, places)

    total_distance_km = round(sum(s["distance_km"] for s in segments if s["type"] == "driving"), 3)
    total_offline_seconds = sum(s["duration_seconds"] for s in segments if s["type"] == "offline")

    return {
        "device_id": device.id,
        "device_name": device.name,
        "device_imei": device.imei,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "segments": segments,
        "properties": {
            "point_count_raw": len(locations),
            "segment_count": len(segments),
            "driving_segment_count": sum(1 for s in segments if s["type"] == "driving"),
            "stopped_segment_count": sum(1 for s in segments if s["type"] == "stopped"),
            "offline_segment_count": sum(1 for s in segments if s["type"] == "offline"),
            "total_distance_km": total_distance_km,
            "total_offline_seconds": total_offline_seconds,
            "simplified": simplify,
            "tolerance_meters": tolerance_meters,
            "stop_speed_threshold_kmh": settings.stop_speed_threshold_kmh,
            "min_stop_segment_minutes": settings.min_stop_segment_minutes,
        },
    }


@router.get("/{device_id}/since-last-stop")
async def get_route_since_last_stop(
    device_id: int,
    simplify: bool = Query(False, description="Simplify route via Douglas-Peucker to reduce point count"),
    tolerance_meters: float = Query(
        20.0, ge=1.0, le=1000.0,
        description="Max deviation in meters a point may contribute before being dropped (only used when simplify=true)"
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Live "since last stop" trail: the route from the end of the device's
    most recent sustained stop up to now, in the same FeatureCollection
    format as /route (see _build_route_response) — including outlier
    filtering and the simplify/tolerance_meters knobs.

    Finds the stop using find_low_speed_runs (the same stop-detection
    primitive period-route uses) with the caller's trip settings
    (stop_speed_threshold_kmh, min_stop_segment_minutes), gap-segmenting
    first so a run never spans an offline gap. Segments are scanned newest
    -> oldest so the most recent qualifying stop wins even if the device's
    latest gap-segment (e.g. since coming back online) has no stop of its
    own yet.

    If that stop is still ongoing (no movement since), the route is empty —
    the client shows nothing until real movement resumes. If no qualifying
    stop is found within SINCE_LAST_STOP_LOOKBACK_HOURS, the trail falls
    back to the earliest point in that window.
    """
    device = verify_device_access(device_id, user, db)
    settings = get_or_create_trip_settings(user.id, db)

    now = datetime.utcnow()
    lookback_start = now - timedelta(hours=SINCE_LAST_STOP_LOOKBACK_HOURS)

    query = db.query(Location).filter(*location_quality_filters(device_id))
    query = query.filter(Location.timestamp >= lookback_start, Location.timestamp <= now)
    locations = query.order_by(Location.timestamp.asc()).all()

    trail_locations, since_time, last_stop, currently_stopped = find_since_last_stop_window(
        locations,
        stop_speed_threshold_kmh=settings.stop_speed_threshold_kmh,
        min_stop_seconds=settings.min_stop_segment_minutes * 60,
        fallback_start=lookback_start,
    )

    response = _build_route_response(device, trail_locations, since_time, now, simplify, tolerance_meters)
    response["properties"]["last_stop"] = last_stop
    response["properties"]["currently_stopped"] = currently_stopped
    response["properties"]["stop_speed_threshold_kmh"] = settings.stop_speed_threshold_kmh
    response["properties"]["min_stop_segment_minutes"] = settings.min_stop_segment_minutes
    return response


@router.get("/{device_id}/alarms", response_model=List[LocationResponse])
async def get_device_alarms(
    device_id: int,
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get alarm events for a device"""
    verify_device_access(device_id, user, db)

    query = db.query(Location).filter(
        Location.device_id == device_id,
        Location.is_alarm == True
    )
    
    if start_time:
        query = query.filter(Location.timestamp >= start_time)
    else:
        start_time = datetime.utcnow() - timedelta(days=7)
        query = query.filter(Location.timestamp >= start_time)
    
    if end_time:
        query = query.filter(Location.timestamp <= end_time)
    
    alarms = query.order_by(Location.timestamp.desc()).limit(limit).all()
    
    return alarms


@router.get("/nearby")
async def get_nearby_devices(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(10, ge=0.1, le=100, description="Search radius in kilometers"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Find devices near a location, scoped to the caller's own devices (admins see all)."""
    query = db.query(Device).filter(
        Device.last_latitude.isnot(None),
        Device.last_longitude.isnot(None)
    )
    if user.role not in (Role.SUPER_ADMIN, Role.ADMIN):
        query = query.filter(Device.user_id == user.id)
    devices = query.all()

    nearby = []
    for device in devices:
        distance = haversine_km(longitude, latitude, device.last_longitude, device.last_latitude)
        if distance <= radius_km:
            nearby.append({
                "device_id": device.id,
                "device_name": device.name,
                "imei": device.imei,
                "latitude": device.last_latitude,
                "longitude": device.last_longitude,
                "distance_km": round(distance, 2),
                "last_update": device.last_update
            })
    
    nearby.sort(key=lambda x: x['distance_km'])
    
    return {
        "center": {"latitude": latitude, "longitude": longitude},
        "radius_km": radius_km,
        "devices_found": len(nearby),
        "devices": nearby
    }
