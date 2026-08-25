"""
Unit tests for the period-route segmentation logic (find_low_speed_runs,
build_period_segments) backing GET /api/locations/{device_id}/period-route.

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
    build_period_segments,
    find_low_speed_runs,
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
MIN_STOP_SECONDS = 600  # 10 minutes, the confirmed default


# --- find_low_speed_runs ---


def test_find_low_speed_runs_no_stops():
    locs = [loc(0, 30.0, -1.9, speed=40), loc(10, 30.01, -1.9, speed=42)]
    assert find_low_speed_runs(locs, STOP_SPEED) == []


def test_find_low_speed_runs_single_run():
    locs = [
        loc(0, 30.0, -1.9, speed=40),
        loc(10, 30.01, -1.9, speed=2),
        loc(20, 30.011, -1.9, speed=1),
        loc(30, 30.012, -1.9, speed=45),
    ]
    assert find_low_speed_runs(locs, STOP_SPEED) == [(1, 3)]


def test_find_low_speed_runs_none_speed_not_stopped():
    locs = [loc(0, 30.0, -1.9, speed=None), loc(10, 30.01, -1.9, speed=None)]
    assert find_low_speed_runs(locs, STOP_SPEED) == []


def test_find_low_speed_runs_multiple_runs():
    locs = [
        loc(0, 30.0, -1.9, speed=1),
        loc(10, 30.01, -1.9, speed=40),
        loc(20, 30.02, -1.9, speed=2),
        loc(30, 30.03, -1.9, speed=3),
    ]
    assert find_low_speed_runs(locs, STOP_SPEED) == [(0, 1), (2, 4)]


# --- build_period_segments: typing and ordering ---


def test_empty_locations_returns_no_segments():
    assert build_period_segments([], STOP_SPEED, MIN_STOP_SECONDS) == []


def test_pure_driving_no_stops_no_gaps():
    # simplify=False: these points are collinear, so DP would legitimately
    # reduce them to 2 -- this test is about segment typing, not simplification
    # (see test_simplify_keeps_segment_endpoints for that).
    locs = [loc(i * 10, 30.0 + i * 0.001, -1.9, speed=40) for i in range(5)]
    segments = build_period_segments(locs, STOP_SPEED, MIN_STOP_SECONDS, simplify=False)
    assert [s["type"] for s in segments] == ["driving"]
    assert segments[0]["point_count"] == 5
    assert segments[0]["distance_km"] > 0


def test_stop_below_min_duration_folded_into_driving():
    """A brief low-speed dip shorter than min_stop_seconds must not split
    driving into its own "stopped" segment."""
    locs = [
        loc(0, 30.0, -1.9, speed=40),
        loc(10, 30.001, -1.9, speed=2),   # dips below threshold...
        loc(20, 30.002, -1.9, speed=3),   # ...for only 10s, well under 600s
        loc(30, 30.003, -1.9, speed=40),
    ]
    segments = build_period_segments(locs, STOP_SPEED, MIN_STOP_SECONDS, simplify=False)
    assert [s["type"] for s in segments] == ["driving"]
    assert segments[0]["point_count"] == 4


def test_stop_above_min_duration_produces_driving_stopped_driving():
    locs = (
        [loc(i * 30, 30.0 + i * 0.001, -1.9, speed=40) for i in range(3)]
        + [loc(90 + i * 60, 30.5, -1.5, speed=1) for i in range(12)]  # 11 min stopped
        + [loc(90 + 12 * 60 + i * 30, 30.9, -1.3, speed=40) for i in range(3)]
    )
    segments = build_period_segments(locs, STOP_SPEED, MIN_STOP_SECONDS)
    assert [s["type"] for s in segments] == ["driving", "stopped", "driving"]
    stopped = segments[1]
    assert stopped["duration_seconds"] >= MIN_STOP_SECONDS
    assert stopped["point_count"] == 12
    assert "latitude" in stopped and "longitude" in stopped
    # segments must be chronologically ordered end-to-end
    assert segments[0]["end_time"] <= stopped["start_time"]
    assert stopped["end_time"] <= segments[2]["start_time"]


def test_offline_gap_produces_offline_segment_between_driving_runs():
    gap = TIME_GAP_SEGMENT_BREAK_SECONDS + 3600
    locs = [
        loc(0, 30.0, -1.9, speed=40),
        loc(10, 30.001, -1.9, speed=42),
        loc(gap, 29.5, -1.4, speed=38),   # genuinely far away, after a long gap
        loc(gap + 10, 29.501, -1.4, speed=39),
    ]
    segments = build_period_segments(locs, STOP_SPEED, MIN_STOP_SECONDS)
    assert [s["type"] for s in segments] == ["driving", "offline", "driving"]
    offline = segments[1]
    assert offline["duration_seconds"] == gap - 10
    assert offline["last_known"]["longitude"] == 30.001
    assert offline["next_known"]["longitude"] == 29.5


def test_gakenke_scenario_stop_immediately_after_offline_gap():
    """Mirrors the real device-1 case: a long offline gap (device travels
    off-grid, e.g. Gakenke -> Kigali) immediately followed by a stop once it
    reconnects, then driving resumes. Segments must come back in the right
    order and each typed correctly, with the offline gap's next_known tying
    to the start of the following stop."""
    gap = TIME_GAP_SEGMENT_BREAK_SECONDS + 29 * 3600  # ~29h, as observed on device 1

    locs = [loc(0, 30.0, -1.9, speed=35), loc(10, 30.001, -1.9, speed=36)]  # driving before gap
    gap_start_ts = 10 + gap
    locs += [loc(gap_start_ts + i * 60, 29.7, -1.6, speed=1) for i in range(12)]  # 11 min stopped, right after reconnect
    drive_start_ts = gap_start_ts + 12 * 60
    locs += [loc(drive_start_ts + i * 30, 29.8 + i * 0.01, -1.5, speed=40) for i in range(4)]  # driving resumes

    segments = build_period_segments(locs, STOP_SPEED, MIN_STOP_SECONDS)
    assert [s["type"] for s in segments] == ["driving", "offline", "stopped", "driving"]

    offline = segments[1]
    stopped = segments[2]
    assert offline["next_known"]["timestamp"] == stopped["start_time"]
    assert offline["duration_seconds"] > 3600 * 20  # sanity: this is the long gap, not a short one
    assert stopped["duration_seconds"] >= MIN_STOP_SECONDS


def test_lone_point_between_gaps_becomes_zero_duration_stopped_segment():
    """A single isolated ping between two offline gaps can't form a driving
    segment (no distance/duration) -- it must still surface, not vanish, so
    it's reported as a zero-duration "stopped" segment instead."""
    gap = TIME_GAP_SEGMENT_BREAK_SECONDS + 3600
    locs = (
        [loc(0, 30.0, -1.9, speed=40), loc(10, 30.001, -1.9, speed=41)]                       # real driving run
        + [loc(gap, 29.5, -1.4, speed=40)]                                                     # lone point, isolated by gaps on both sides
        + [loc(2 * gap, 29.0, -1.3, speed=40), loc(2 * gap + 10, 28.999, -1.3, speed=39)]       # real driving run
    )
    segments = build_period_segments(locs, STOP_SPEED, MIN_STOP_SECONDS)
    assert [s["type"] for s in segments] == ["driving", "offline", "stopped", "offline", "driving"]
    lone = segments[2]
    assert lone["duration_seconds"] == 0
    assert lone["point_count"] == 1


def test_simplify_keeps_segment_endpoints():
    locs = [loc(i * 10, 30.0 + i * 0.0001, -1.9 + (0.0005 if i == 25 else 0), speed=40) for i in range(50)]
    segments = build_period_segments(locs, STOP_SPEED, MIN_STOP_SECONDS, simplify=True, tolerance_meters=5.0)
    assert len(segments) == 1
    driving = segments[0]
    assert driving["raw_point_count"] == 50
    assert driving["point_count"] <= 50
    assert driving["points"][0]["timestamp"] == locs[0].timestamp.isoformat()
    assert driving["points"][-1]["timestamp"] == locs[-1].timestamp.isoformat()
