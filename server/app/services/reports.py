"""
Reports: period aggregations of a device's own GPS history — the
summary/daily/drives/stops/overspeed/geofence-visit/alarm tables fleet
platforms (Wialon, Traccar, Navixy, GPSWox, ...) ship as their core report
set, built only from data this backend already stores.

Everything driving/stopped/offline here comes from
app.api.locations.classify_period_spans — the exact classification
GET /{device_id}/period-route draws on the map — so a report's distance
and time totals always match period playback for the same range.

Pure functions over duck-typed rows (only .timestamp/.latitude/.longitude/
.speed are read from locations; .timestamp/.alarm_type/.geofence_name/
.road_name/.speed/.latitude/.longitude from alarms), so the route can
pass lightweight column-only query rows and tests can pass plain objects.

Not covered, deliberately: engine hours / idling (ignition state isn't
persisted per fix — see Location), and anything fuel-sensor or CAN based
(neither supported device model has those inputs).
"""

from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from app.api.locations import (
    TIME_GAP_SEGMENT_BREAK_SECONDS,
    classify_period_spans,
    haversine_km,
)
from app.services.speed_limit import HYSTERESIS_KMH

# Upper bound on a report's span. One day longer than period-route's
# MAX_PERIOD_DAYS so "this month" (up to 31 calendar days) always fits.
MAX_REPORT_DAYS = 31

# Per-list caps so a pathological period (a GPS that flaps between moving
# and stopped all month) can't produce an unbounded response. Totals are
# always computed over everything; only the itemized lists are capped, and
# the response says so via `truncated`.
MAX_LIST_ITEMS = 500

OVERSPEED_ALARM = "Over speed"
FENCE_ENTER = "Enter fence"
FENCE_EXIT = "Exit fence"
FENCE_ALARMS = (FENCE_ENTER, FENCE_EXIT)


def _local_day(ts: datetime, offset: timedelta) -> date:
    return (ts + offset).date()


def _day_start_utc(day: date, offset: timedelta) -> datetime:
    return datetime.combine(day, datetime.min.time()) - offset


def split_seconds_by_day(start: datetime, end: datetime, offset: timedelta) -> Dict[date, float]:
    """Seconds of the [start, end] interval falling on each local calendar
    day (local = UTC + offset), so a span crossing midnight is credited to
    both days rather than wholly to the day it started on."""
    result: Dict[date, float] = {}
    if end <= start:
        return result
    cursor = start
    while cursor < end:
        day = _local_day(cursor, offset)
        next_midnight = _day_start_utc(day + timedelta(days=1), offset)
        chunk_end = min(end, next_midnight)
        result[day] = result.get(day, 0.0) + (chunk_end - cursor).total_seconds()
        cursor = chunk_end
    return result


def _empty_day(day: date) -> dict:
    return {
        "date": day.isoformat(),
        "distance_km": 0.0,
        "driving_seconds": 0.0,
        "stopped_seconds": 0.0,
        "offline_seconds": 0.0,
        "drive_count": 0,
        "stop_count": 0,
        "max_speed_kmh": 0.0,
    }


def _max_speed(points) -> float:
    return max((p.speed for p in points if p.speed is not None), default=0.0)


def _centroid(points):
    return (
        sum(p.latitude for p in points) / len(points),
        sum(p.longitude for p in points) / len(points),
    )


def build_activity(
    locations: list,
    stop_speed_threshold_kmh: float,
    min_stop_seconds: float,
    period_start: datetime,
    period_end: datetime,
    offset: timedelta,
) -> dict:
    """Summary totals, a per-local-day breakdown (every day in the period,
    zero-filled), and itemized drives/stops/offline periods."""
    days: Dict[date, dict] = {}
    day = _local_day(period_start, offset)
    last_day = _local_day(period_end, offset)
    while day <= last_day:
        days[day] = _empty_day(day)
        day += timedelta(days=1)

    def _bucket(d: date) -> Optional[dict]:
        # Points are already filtered to [period_start, period_end], so this
        # only misses on a boundary rounding edge — dropped, not invented.
        return days.get(d)

    drives: List[dict] = []
    stops: List[dict] = []
    offline: List[dict] = []

    for kind, points in classify_period_spans(locations, stop_speed_threshold_kmh, min_stop_seconds):
        start, end = points[0].timestamp, points[-1].timestamp
        duration = (end - start).total_seconds()
        seconds_key = {"driving": "driving_seconds", "stopped": "stopped_seconds", "offline": "offline_seconds"}[kind]
        for d, secs in split_seconds_by_day(start, end, offset).items():
            bucket = _bucket(d)
            if bucket:
                bucket[seconds_key] += secs

        if kind == "driving":
            distance = 0.0
            for prev, curr in zip(points, points[1:]):
                leg = haversine_km(prev.longitude, prev.latitude, curr.longitude, curr.latitude)
                distance += leg
                bucket = _bucket(_local_day(curr.timestamp, offset))
                if bucket:
                    bucket["distance_km"] += leg
            for p in points:
                bucket = _bucket(_local_day(p.timestamp, offset))
                if bucket and p.speed is not None and p.speed > bucket["max_speed_kmh"]:
                    bucket["max_speed_kmh"] = p.speed
            bucket = _bucket(_local_day(start, offset))
            if bucket:
                bucket["drive_count"] += 1
            drives.append({
                "start_time": start,
                "end_time": end,
                "duration_seconds": duration,
                "distance_km": distance,
                "max_speed_kmh": _max_speed(points),
                "avg_speed_kmh": (distance / (duration / 3600)) if duration > 0 else None,
                "start_latitude": points[0].latitude,
                "start_longitude": points[0].longitude,
                "end_latitude": points[-1].latitude,
                "end_longitude": points[-1].longitude,
            })
        elif kind == "stopped":
            # A lone-point "stopped" span (see classify_period_spans) is a
            # classification leftover, not a real parking — only stops that
            # actually met the owner's minimum duration are itemized/counted.
            if duration < min_stop_seconds:
                continue
            bucket = _bucket(_local_day(start, offset))
            if bucket:
                bucket["stop_count"] += 1
            lat, lon = _centroid(points)
            stops.append({
                "start_time": start,
                "end_time": end,
                "duration_seconds": duration,
                "latitude": lat,
                "longitude": lon,
            })
        else:
            offline.append({
                "start_time": start,
                "end_time": end,
                "duration_seconds": duration,
                "latitude": points[0].latitude,
                "longitude": points[0].longitude,
            })

    daily = []
    for bucket in days.values():
        bucket["distance_km"] = round(bucket["distance_km"], 3)
        daily.append(bucket)

    total_distance = sum(d["distance_km"] for d in drives)
    driving_seconds = sum(d["duration_seconds"] for d in drives)
    summary = {
        "distance_km": round(total_distance, 3),
        "driving_seconds": driving_seconds,
        "stopped_seconds": sum(s["duration_seconds"] for s in stops),
        "offline_seconds": sum(o["duration_seconds"] for o in offline),
        "drive_count": len(drives),
        "stop_count": len(stops),
        "offline_count": len(offline),
        "max_speed_kmh": max((d["max_speed_kmh"] for d in drives), default=0.0),
        "avg_moving_speed_kmh": round(total_distance / (driving_seconds / 3600), 1) if driving_seconds > 0 else None,
        "first_movement_at": drives[0]["start_time"] if drives else None,
        "last_movement_at": drives[-1]["end_time"] if drives else None,
        "active_days": sum(1 for d in daily if d["drive_count"] > 0),
        "point_count": len(locations),
    }
    for d in drives:
        d["distance_km"] = round(d["distance_km"], 3)
        if d["avg_speed_kmh"] is not None:
            d["avg_speed_kmh"] = round(d["avg_speed_kmh"], 1)

    return {"summary": summary, "daily": daily, "drives": drives, "stops": stops, "offline_periods": offline}


def build_overspeed_episodes(
    alarms: list, locations: list, speed_limit_kmh: Optional[float], stored: Optional[dict] = None,
) -> List[dict]:
    """One row per "Over speed" alarm (each is already exactly one
    edge-triggered episode — see app/services/speed_limit.py), enriched
    with the episode's peak speed and how long it lasted.

    `stored` maps alarm location id -> its OverspeedEpisode (migration
    055). When an alarm has one, its recorded peak and duration are used
    as-is — the same record the alert and the history route's red segment
    come from, judged against the limit in force at the time. Only alarms
    from before that table existed fall back to the reconstruction below.

    The episode is read forward from the alarm's own fix through the
    quality-filtered locations for as long as speed stays above the same
    re-arm floor speed_limit.py uses (limit - HYSTERESIS_KMH), stopping at
    an offline gap or the next episode. Uses the device's CURRENT limit —
    the limit in force at the time isn't stored — so with no limit set
    (removed since), only the alarm fix's own speed is known and
    duration is None rather than guessed."""
    episodes: List[dict] = []
    speeding = sorted((a for a in alarms if a.alarm_type == OVERSPEED_ALARM), key=lambda a: a.timestamp)
    timestamps = [loc.timestamp for loc in locations]
    floor = (speed_limit_kmh - HYSTERESIS_KMH) if speed_limit_kmh is not None else None

    stored = stored or {}
    for idx, alarm in enumerate(speeding):
        alarm_speed = alarm.speed or 0.0
        max_speed = alarm_speed
        end_time = alarm.timestamp
        duration: Optional[float] = None
        episode = stored.get(getattr(alarm, "id", None))
        if episode is not None:
            max_speed = max(max_speed, episode.max_speed_kmh)
            duration = ((episode.ended_at or episode.last_fix_at) - episode.started_at).total_seconds()
        elif floor is not None:
            next_alarm_at = speeding[idx + 1].timestamp if idx + 1 < len(speeding) else None
            j = bisect_left(timestamps, alarm.timestamp)
            prev_ts = alarm.timestamp
            while j < len(locations):
                loc = locations[j]
                if next_alarm_at is not None and loc.timestamp >= next_alarm_at:
                    break
                if (loc.timestamp - prev_ts).total_seconds() > TIME_GAP_SEGMENT_BREAK_SECONDS:
                    break
                if loc.speed is None or loc.speed <= floor:
                    break
                max_speed = max(max_speed, loc.speed)
                end_time = loc.timestamp
                prev_ts = loc.timestamp
                j += 1
            duration = (end_time - alarm.timestamp).total_seconds()
        episodes.append({
            "timestamp": alarm.timestamp,
            "speed_kmh": alarm_speed,
            "max_speed_kmh": max_speed,
            "duration_seconds": duration,
            "road_name": alarm.road_name,
            "latitude": alarm.latitude,
            "longitude": alarm.longitude,
        })
    return episodes


def build_geofence_visits(alarms: list, period_start: datetime, period_end: datetime) -> List[dict]:
    """Pair each zone's server-side "Enter fence" with its next "Exit fence"
    into a visit with a dwell time. Only alarms carrying geofence_name
    count — the hardware's own fence bytes (no geofence_name) were never
    real zone crossings (see tcp_server.py's HARDWARE_FENCE_ALARM_TYPES).

    A visit whose enter happened before the period has enter_time None
    (already inside at period start); one with no exit yet has exit_time
    None (still inside at period end). Dwell is clamped to the period in
    both cases, so it never counts time outside the report range."""
    fence = sorted(
        (a for a in alarms if a.alarm_type in FENCE_ALARMS and a.geofence_name),
        key=lambda a: a.timestamp,
    )
    open_visits: Dict[str, dict] = {}
    visits: List[dict] = []

    def _close(visit: dict, exit_time: Optional[datetime]) -> None:
        visit["exit_time"] = exit_time
        effective_start = visit["enter_time"] or period_start
        effective_end = exit_time or min(period_end, datetime.utcnow())
        visit["duration_seconds"] = max((effective_end - effective_start).total_seconds(), 0.0)
        visits.append(visit)

    for alarm in fence:
        name = alarm.geofence_name
        if alarm.alarm_type == FENCE_ENTER:
            # A repeated enter with no exit in between (a missed exit fix)
            # keeps the earlier enter — the vehicle was inside since then.
            open_visits.setdefault(name, {
                "geofence_name": name,
                "enter_time": alarm.timestamp,
                "latitude": alarm.latitude,
                "longitude": alarm.longitude,
            })
        else:
            visit = open_visits.pop(name, None) or {
                "geofence_name": name,
                "enter_time": None,
                "latitude": alarm.latitude,
                "longitude": alarm.longitude,
            }
            _close(visit, alarm.timestamp)

    for visit in open_visits.values():
        _close(visit, None)

    visits.sort(key=lambda v: v["enter_time"] or v["exit_time"] or period_start)
    return visits


def summarize_geofence_visits(visits: List[dict]) -> List[dict]:
    """Per-zone totals (visit count, total dwell), most time spent first."""
    by_zone: Dict[str, dict] = defaultdict(lambda: {"visit_count": 0, "total_seconds": 0.0})
    for v in visits:
        zone = by_zone[v["geofence_name"]]
        zone["visit_count"] += 1
        zone["total_seconds"] += v["duration_seconds"]
    rows = [{"geofence_name": name, **totals} for name, totals in by_zone.items()]
    rows.sort(key=lambda r: r["total_seconds"], reverse=True)
    return rows


def count_alarms(alarms: list) -> List[dict]:
    """Alarm counts by type, most frequent first. Hardware fence bytes
    without a geofence_name are excluded for the same reason
    build_geofence_visits skips them."""
    counts = Counter(
        a.alarm_type for a in alarms
        if a.alarm_type and not (a.alarm_type in FENCE_ALARMS and not a.geofence_name)
    )
    return [{"alarm_type": t, "count": c} for t, c in counts.most_common()]


def cap(items: List[dict]) -> tuple:
    """(first MAX_LIST_ITEMS items, whether anything was cut)."""
    return items[:MAX_LIST_ITEMS], len(items) > MAX_LIST_ITEMS
