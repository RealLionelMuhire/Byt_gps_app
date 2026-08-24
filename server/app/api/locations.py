"""Location API endpoints"""

from typing import List, Optional
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
            "end_time": (end_time or datetime.utcnow()).isoformat(),
            "point_count": len(features),
            "simplified": simplify,
            "original_point_count": original_point_count,
            "segment_count": len(segments_meta),
            "offline_seconds": sum(g["gap_seconds"] for g in gaps)
        }
    }


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
