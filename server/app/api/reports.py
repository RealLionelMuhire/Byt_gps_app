"""Reports API: period aggregations of a device's GPS history (see
app/services/reports.py for what each table means and how it's computed).

Gated on the existing "history.period" feature rather than a new key — a
report is a summary of exactly the data period history already exposes,
and a new key would need its own features/plan_features seed migration.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.locations import location_quality_filters, verify_device_access
from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.device import Device
from app.models.location import Location
from app.models.user import User
from app.services.entitlements import require_feature
from app.services.geocoding import (
    format_fallback_location,
    reverse_geocode_many,
    reverse_geocode_many_cached_only,
)
from app.services.reports import (
    MAX_REPORT_DAYS,
    build_activity,
    build_geofence_visits,
    build_overspeed_episodes,
    cap,
    count_alarms,
    summarize_geofence_visits,
)
from app.services.trip_settings_service import get_or_create_trip_settings

router = APIRouter()

# Every real UTC offset is within [-12:00, +14:00].
_TZ_OFFSET_QUERY = Query(
    0, ge=-12 * 60, le=14 * 60,
    description="Viewer's UTC offset in minutes; daily buckets follow the viewer's local calendar days",
)


def _validate_period(start: datetime, end: datetime) -> None:
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if (end - start) > timedelta(days=MAX_REPORT_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"A report can span at most {MAX_REPORT_DAYS} days; narrow the start/end range.",
        )


def _naive_utc(ts: datetime) -> datetime:
    """Location timestamps are stored naive-UTC; an aware query param
    (the app sends "...Z") must be normalized before the report code
    compares it against them in Python."""
    if ts.tzinfo is not None:
        return (ts - ts.utcoffset()).replace(tzinfo=None)
    return ts


def _load_locations(db: Session, device_id: int, start: datetime, end: datetime) -> list:
    # Column-only rows, not ORM objects: a month of fixes is ~250k rows and
    # build_activity only reads these four attributes.
    return (
        db.query(Location.timestamp, Location.latitude, Location.longitude, Location.speed)
        .filter(*location_quality_filters(device_id))
        .filter(Location.timestamp >= start, Location.timestamp <= end)
        .order_by(Location.timestamp.asc())
        .all()
    )


def _load_alarms(db: Session, device_id: int, start: datetime, end: datetime) -> list:
    return (
        db.query(
            Location.timestamp, Location.alarm_type, Location.geofence_name, Location.road_name,
            Location.speed, Location.latitude, Location.longitude,
        )
        .filter(Location.device_id == device_id, Location.is_alarm == True)  # noqa: E712
        .filter(Location.timestamp >= start, Location.timestamp <= end)
        .order_by(Location.timestamp.asc())
        .all()
    )


def _attach_places(rows: List[dict], lat_key: str, lon_key: str, out_key: str,
                   places: Dict[Tuple[float, float], Optional[str]]) -> None:
    for row in rows:
        lat, lon = row[lat_key], row[lon_key]
        row[out_key] = places.get((lat, lon)) or format_fallback_location(lat, lon)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _serialize(rows: List[dict]) -> List[dict]:
    return [{k: _iso(v) for k, v in row.items()} for row in rows]


@router.get("/fleet", dependencies=[require_feature("history.period")])
async def get_fleet_report(
    start: datetime = Query(..., description="Period start (UTC)"),
    end: datetime = Query(..., description="Period end (UTC)"),
    tz_offset_minutes: int = _TZ_OFFSET_QUERY,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Summary totals for every device the caller owns, one row each, for
    comparing vehicles side by side. Scoped to the caller's own devices
    even for an admin — the whole platform's fleet isn't a report anyone
    can read on a phone, and admins have the dashboard for that."""
    start, end = _naive_utc(start), _naive_utc(end)
    _validate_period(start, end)
    offset = timedelta(minutes=tz_offset_minutes)
    settings = get_or_create_trip_settings(user.id, db)

    devices = db.query(Device).filter(Device.user_id == user.id).order_by(Device.name.asc()).all()
    rows = []
    for device in devices:
        activity = build_activity(
            _load_locations(db, device.id, start, end),
            stop_speed_threshold_kmh=settings.stop_speed_threshold_kmh,
            min_stop_seconds=settings.min_stop_segment_minutes * 60,
            period_start=start, period_end=end, offset=offset,
        )
        alarms = _load_alarms(db, device.id, start, end)
        summary = {k: _iso(v) for k, v in activity["summary"].items()}
        summary["overspeed_count"] = sum(1 for a in alarms if a.alarm_type == "Over speed")
        summary["alarm_count"] = sum(c["count"] for c in count_alarms(alarms))
        rows.append({"device_id": device.id, "device_name": device.name, "summary": summary})

    return {
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "tz_offset_minutes": tz_offset_minutes,
        "devices": rows,
    }


@router.get("/{device_id}", dependencies=[require_feature("history.period")])
async def get_device_report(
    device_id: int,
    background_tasks: BackgroundTasks,
    start: datetime = Query(..., description="Period start (UTC)"),
    end: datetime = Query(..., description="Period end (UTC)"),
    tz_offset_minutes: int = _TZ_OFFSET_QUERY,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Everything one device did over [start, end], as report tables:
    summary, per-day breakdown, drives, stops, overspeed episodes,
    geofence visits (+ per-zone totals), alarm counts, offline periods.

    Uses the caller's own trip settings (stop speed / minimum stop length)
    — the same ones period-route uses — so the report and period playback
    agree on what counts as a stop.
    """
    start, end = _naive_utc(start), _naive_utc(end)
    _validate_period(start, end)
    device = verify_device_access(device_id, user, db)
    settings = get_or_create_trip_settings(user.id, db)
    offset = timedelta(minutes=tz_offset_minutes)

    locations = _load_locations(db, device_id, start, end)
    alarms = _load_alarms(db, device_id, start, end)

    activity = build_activity(
        locations,
        stop_speed_threshold_kmh=settings.stop_speed_threshold_kmh,
        min_stop_seconds=settings.min_stop_segment_minutes * 60,
        period_start=start, period_end=end, offset=offset,
    )
    overspeed = build_overspeed_episodes(alarms, locations, device.speed_limit_kmh)
    visits = build_geofence_visits(alarms, start, end)
    alarm_counts = count_alarms(alarms)

    drives, drives_truncated = cap(activity["drives"])
    stops, stops_truncated = cap(activity["stops"])
    offline, offline_truncated = cap(activity["offline_periods"])
    overspeed_items, overspeed_truncated = cap(overspeed)
    visit_items, visits_truncated = cap(visits)

    # Same cache-only + background-warm pattern as period-route: never hold
    # the response on Nominatim; unresolved points read as coordinates this
    # time and get real names on the next request.
    coords = (
        [(d["start_latitude"], d["start_longitude"]) for d in drives]
        + [(d["end_latitude"], d["end_longitude"]) for d in drives]
        + [(s["latitude"], s["longitude"]) for s in stops]
    )
    places: Dict[Tuple[float, float], Optional[str]] = {}
    if coords:
        missing = reverse_geocode_many_cached_only(coords, places)
        if missing:
            background_tasks.add_task(reverse_geocode_many, missing)
    _attach_places(drives, "start_latitude", "start_longitude", "start_place", places)
    _attach_places(drives, "end_latitude", "end_longitude", "end_place", places)
    _attach_places(stops, "latitude", "longitude", "place", places)

    summary = {k: _iso(v) for k, v in activity["summary"].items()}
    summary["overspeed_count"] = len(overspeed)
    summary["alarm_count"] = sum(c["count"] for c in alarm_counts)
    summary["geofence_visit_count"] = len(visits)

    return {
        "device_id": device.id,
        "device_name": device.name,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "tz_offset_minutes": tz_offset_minutes,
        "speed_limit_kmh": device.speed_limit_kmh,
        "stop_speed_threshold_kmh": settings.stop_speed_threshold_kmh,
        "min_stop_segment_minutes": settings.min_stop_segment_minutes,
        "summary": summary,
        "daily": activity["daily"],
        "drives": _serialize(drives),
        "stops": _serialize(stops),
        "offline_periods": _serialize(offline),
        "overspeed": _serialize(overspeed_items),
        "geofence_visits": _serialize(visit_items),
        "geofence_zones": summarize_geofence_visits(visits),
        "alarm_counts": alarm_counts,
        "truncated": {
            "drives": drives_truncated,
            "stops": stops_truncated,
            "offline_periods": offline_truncated,
            "overspeed": overspeed_truncated,
            "geofence_visits": visits_truncated,
        },
    }
