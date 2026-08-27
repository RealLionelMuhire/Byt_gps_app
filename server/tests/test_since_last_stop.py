"""
Unit tests for find_since_last_stop_window, the stop-detection logic
backing GET /api/locations/{device_id}/since-last-stop.

Pure in-memory tests against fake location objects (duck-typed: only
.timestamp/.longitude/.latitude/.speed/.course are read by the code under
test) -- no DB needed for the segmentation logic itself.
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.locations import (
    TIME_GAP_SEGMENT_BREAK_SECONDS,
    find_since_last_stop_window,
)


@dataclass
class FakeLoc:
    timestamp: datetime
    longitude: float
    latitude: float
    speed: float = 0.0
    course: int = 0


T0 = datetime(2026, 8, 22, 12, 0, 0)


def loc(offset_seconds, lon, lat, speed=0.0):
    return FakeLoc(timestamp=T0 + timedelta(seconds=offset_seconds), longitude=lon, latitude=lat, speed=speed)


STOP_SPEED = 5.0
MIN_STOP_SECONDS = 600  # 10 minutes
FALLBACK = T0 - timedelta(hours=48)


def run(locations):
    return find_since_last_stop_window(
        locations,
        stop_speed_threshold_kmh=STOP_SPEED,
        min_stop_seconds=MIN_STOP_SECONDS,
        fallback_start=FALLBACK,
    )


def test_empty_locations_falls_back_to_lookback_start():
    trail, since_time, last_stop, currently_stopped = run([])
    assert trail == []
    assert since_time == FALLBACK
    assert last_stop is None
    assert currently_stopped is False


def test_no_qualifying_stop_falls_back_to_earliest_point():
    # Continuous driving, no stop at all in the window.
    locs = [loc(i * 60, 30.0 + i * 0.001, -1.9, speed=40) for i in range(10)]
    trail, since_time, last_stop, currently_stopped = run(locs)
    assert trail == locs
    assert since_time == locs[0].timestamp
    assert last_stop is None
    assert currently_stopped is False


def test_currently_stopped_returns_empty_trail():
    # Drove, then stopped for >= MIN_STOP_SECONDS, and hasn't moved since
    # (the stop run reaches the very last point).
    locs = [
        loc(0, 30.0, -1.9, speed=40),
        loc(60, 30.01, -1.9, speed=42),
    ]
    stop_start = 120
    locs += [loc(stop_start + i * 60, 30.02, -1.9, speed=0) for i in range(12)]  # 11 min stopped
    trail, since_time, last_stop, currently_stopped = run(locs)
    assert currently_stopped is True
    assert trail == []
    assert last_stop["start_time"] == locs[2].timestamp.isoformat()
    assert last_stop["end_time"] == locs[-1].timestamp.isoformat()
    assert since_time == locs[-1].timestamp


def test_stop_then_movement_returns_trail_since_stop_end():
    locs = [loc(0, 30.0, -1.9, speed=40)]
    stop_start = 60
    stop_points = [loc(stop_start + i * 60, 30.02, -1.9, speed=0) for i in range(12)]  # 11 min stopped
    locs += stop_points
    move_start = stop_start + 11 * 60 + 60
    move_points = [loc(move_start + i * 60, 30.03 + i * 0.001, -1.9, speed=40) for i in range(5)]
    locs += move_points

    trail, since_time, last_stop, currently_stopped = run(locs)

    assert currently_stopped is False
    # Trail starts at the stop's last (end) point, inclusive, through to now.
    assert trail[0] is stop_points[-1]
    assert trail[-1] is move_points[-1]
    assert since_time == stop_points[-1].timestamp
    assert last_stop["start_time"] == stop_points[0].timestamp.isoformat()
    assert last_stop["end_time"] == stop_points[-1].timestamp.isoformat()


def test_brief_stop_below_min_duration_is_ignored():
    locs = [loc(0, 30.0, -1.9, speed=40)]
    # Only 3 minutes stopped -- below MIN_STOP_SECONDS, shouldn't count as a stop.
    locs += [loc(60 + i * 60, 30.02, -1.9, speed=0) for i in range(3)]
    locs += [loc(300 + i * 60, 30.03 + i * 0.001, -1.9, speed=40) for i in range(3)]

    trail, since_time, last_stop, currently_stopped = run(locs)

    assert last_stop is None
    assert currently_stopped is False
    assert trail == locs
    assert since_time == locs[0].timestamp


def test_most_recent_qualifying_stop_wins_across_multiple_stops():
    locs = [loc(0, 30.0, -1.9, speed=40)]
    first_stop = [loc(60 + i * 60, 30.01, -1.9, speed=0) for i in range(12)]  # 11 min
    locs += first_stop
    drive_between = [loc(780 + i * 60, 30.02 + i * 0.001, -1.9, speed=40) for i in range(3)]
    locs += drive_between
    second_stop_start = 780 + 3 * 60 + 60
    second_stop = [loc(second_stop_start + i * 60, 30.05, -1.9, speed=0) for i in range(12)]  # 11 min
    locs += second_stop
    move_after = [
        loc(second_stop_start + 11 * 60 + 60 + i * 60, 30.06 + i * 0.001, -1.9, speed=40) for i in range(3)
    ]
    locs += move_after

    trail, since_time, last_stop, currently_stopped = run(locs)

    assert currently_stopped is False
    assert last_stop["start_time"] == second_stop[0].timestamp.isoformat()
    assert last_stop["end_time"] == second_stop[-1].timestamp.isoformat()
    assert trail[0] is second_stop[-1]
    assert trail[-1] is move_after[-1]


def test_stop_found_in_earlier_gap_segment_when_latest_segment_has_none():
    # Stop, then an offline gap, then resumed driving with no new stop yet
    # -- the most recent gap-segment has no qualifying stop of its own, so
    # the stop from before the gap should still be found.
    locs = [loc(0, 30.0, -1.9, speed=40)]
    stop_points = [loc(60 + i * 60, 30.01, -1.9, speed=0) for i in range(12)]  # 11 min
    locs += stop_points
    gap_start = 780
    resume_time = gap_start + TIME_GAP_SEGMENT_BREAK_SECONDS + 120
    resumed = [loc(resume_time + i * 60, 30.02 + i * 0.001, -1.9, speed=40) for i in range(3)]
    locs += resumed

    trail, since_time, last_stop, currently_stopped = run(locs)

    assert currently_stopped is False
    assert last_stop["end_time"] == stop_points[-1].timestamp.isoformat()
    # Trail spans from the stop's end straight through to now (offline gap
    # in between is reported by _build_route_response's own gap logic, not
    # excluded from the trail here).
    assert trail[0] is stop_points[-1]
    assert trail[-1] is resumed[-1]
