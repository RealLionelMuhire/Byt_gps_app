"""
Trip detection service - segment location history into trips based on stop duration.

Uses user's TripSettings to determine when a stop splits a trip vs. is a short stop.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

from sqlalchemy.orm import Session

from app.models.location import Location
from app.models.trip_settings import TripSettings
from app.api.locations import (
    compute_distance_for_device_time_range,
    compute_offline_gaps,
    location_quality_filters,
)


@dataclass
class SuggestedTrip:
    """A detected trip segment from location history"""
    start_time: datetime
    end_time: datetime
    point_count: int
    total_distance_km: float
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    # Time spent offline (gaps exceeding TIME_GAP_SEGMENT_BREAK_SECONDS)
    # within this segment's span — distinct from driving time/distance,
    # never summed into total_distance_km.
    offline_seconds: float = 0.0


def _fetch_locations(
    device_id: int,
    start_time: datetime,
    end_time: datetime,
    db: Session,
) -> List[Location]:
    """Fetch GPS-valid locations in time range, ordered by timestamp."""
    query = db.query(Location).filter(
        *location_quality_filters(device_id),
        Location.timestamp >= start_time,
        Location.timestamp <= end_time,
    )
    return query.order_by(Location.timestamp.asc()).all()


def detect_trip_segments(
    device_id: int,
    start_time: datetime,
    end_time: datetime,
    settings: TripSettings,
    db: Session,
) -> List[SuggestedTrip]:
    """
    Segment location history into trips based on stop duration.

    - Locations with speed < stop_speed_threshold_kmh are "stopped"
    - Consecutive stops spanning >= stop_splits_trip_after_minutes = split point
    - A reporting gap spanning >= stop_splits_trip_after_minutes (device
      offline, so there's no data to form a "stopped" run at all) is
      treated the same as a stop and also splits the timeline
    - Segments shorter than minimum_trip_duration_minutes are filtered out
    """
    locations = _fetch_locations(device_id, start_time, end_time, db)
    if len(locations) < 2:
        return []

    stop_threshold_min = timedelta(minutes=settings.stop_splits_trip_after_minutes)
    min_duration = timedelta(minutes=settings.minimum_trip_duration_minutes)
    speed_threshold = settings.stop_speed_threshold_kmh

    # Find runs of consecutive "stopped" points (speed < threshold), plus
    # standalone reporting gaps big enough to count as a stop on their own.
    # A run/gap lasting >= stop_threshold_min splits the timeline.
    # Segment boundaries: (start_idx, end_idx) for each segment
    seg_pairs: List[tuple[int, int]] = []
    seg_start = 0
    i = 0
    while i < len(locations):
        if i > 0 and (locations[i].timestamp - locations[i - 1].timestamp) >= stop_threshold_min:
            if i > seg_start:
                seg_pairs.append((seg_start, i))
            seg_start = i
        if locations[i].speed is not None and locations[i].speed < speed_threshold:
            stop_start_idx = i
            while i < len(locations) and (
                locations[i].speed is not None and locations[i].speed < speed_threshold
            ):
                i += 1
            stop_duration = locations[i - 1].timestamp - locations[stop_start_idx].timestamp
            if stop_duration >= stop_threshold_min:
                # Segment ends before this stop
                if stop_start_idx > seg_start:
                    seg_pairs.append((seg_start, stop_start_idx))
                # Next segment starts after this stop
                seg_start = i
        else:
            i += 1
    if seg_start < len(locations):
        seg_pairs.append((seg_start, len(locations)))

    segments: List[SuggestedTrip] = []
    for s_start, s_end in seg_pairs:
        if s_end - s_start < 2:
            continue
        seg_locs = locations[s_start:s_end]
        seg_start_time = seg_locs[0].timestamp
        seg_end_time = seg_locs[-1].timestamp
        duration = seg_end_time - seg_start_time
        if duration < min_duration:
            continue
        total_dist, _ = compute_distance_for_device_time_range(
            device_id, seg_start_time, seg_end_time, db
        )
        offline_seconds = sum(g["gap_seconds"] for g in compute_offline_gaps(seg_locs))
        segments.append(SuggestedTrip(
            start_time=seg_start_time,
            end_time=seg_end_time,
            point_count=len(seg_locs),
            total_distance_km=total_dist,
            start_lat=seg_locs[0].latitude,
            start_lon=seg_locs[0].longitude,
            end_lat=seg_locs[-1].latitude,
            end_lon=seg_locs[-1].longitude,
            offline_seconds=offline_seconds,
        ))

    return segments
