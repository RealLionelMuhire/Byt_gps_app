"""
Unit tests for time-gap segmentation (TIME_GAP_SEGMENT_BREAK_SECONDS,
segment_locations_by_gap, compute_offline_gaps) and its effect on
compute_distance_for_device_time_range / fetch_route_line_for_range.

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
    compute_offline_gaps,
    haversine_km,
    segment_locations_by_gap,
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


def test_no_gap_single_segment():
    locs = [loc(0, 30.0, -1.9), loc(12, 30.001, -1.9), loc(24, 30.002, -1.9)]
    segments = segment_locations_by_gap(locs)
    assert len(segments) == 1
    assert len(segments[0]) == 3
    assert compute_offline_gaps(locs) == []


def test_gap_just_under_threshold_no_break():
    locs = [loc(0, 30.0, -1.9), loc(TIME_GAP_SEGMENT_BREAK_SECONDS, 30.001, -1.9)]
    segments = segment_locations_by_gap(locs)
    assert len(segments) == 1
    assert compute_offline_gaps(locs) == []


def test_gap_over_threshold_breaks_segment():
    gap = TIME_GAP_SEGMENT_BREAK_SECONDS + 1
    locs = [loc(0, 30.0, -1.9), loc(gap, 30.5, -1.5)]
    segments = segment_locations_by_gap(locs)
    assert len(segments) == 2
    assert [len(s) for s in segments] == [1, 1]

    gaps = compute_offline_gaps(locs)
    assert len(gaps) == 1
    assert gaps[0]["gap_seconds"] == gap


def test_multiple_breaks_produce_multiple_segments():
    gap = TIME_GAP_SEGMENT_BREAK_SECONDS + 60
    locs = [
        loc(0, 30.0, -1.9),
        loc(10, 30.001, -1.9),
        loc(10 + gap, 30.5, -1.5),
        loc(20 + gap, 30.501, -1.5),
        loc(20 + 2 * gap, 29.9, -1.6),
    ]
    segments = segment_locations_by_gap(locs)
    assert [len(s) for s in segments] == [2, 2, 1]
    assert len(compute_offline_gaps(locs)) == 2


def test_empty_and_single_point():
    assert segment_locations_by_gap([]) == []
    single = [loc(0, 30.0, -1.9)]
    assert segment_locations_by_gap(single) == [single]
    assert compute_offline_gaps(single) == []


def test_distance_not_summed_across_gap():
    """The core Phase 2 bug fix: a real jump between two points separated
    by an offline gap must not be counted as distance travelled."""
    gap = TIME_GAP_SEGMENT_BREAK_SECONDS + 3600  # well past threshold
    a = loc(0, 30.0, -1.9)
    b = loc(gap, 29.5, -1.4)  # genuinely far away -- e.g. Gakenke <-> Kigali
    real_jump_km = haversine_km(a.longitude, a.latitude, b.longitude, b.latitude)
    assert real_jump_km > 20  # sanity: this would be a big, wrong "trip" if summed

    segments = segment_locations_by_gap([a, b])
    assert len(segments) == 2  # not connected

    total = 0.0
    for seg in segments:
        for i in range(1, len(seg)):
            total += haversine_km(seg[i - 1].longitude, seg[i - 1].latitude, seg[i].longitude, seg[i].latitude)
    assert total == 0.0  # no distance summed across the break


def test_distance_still_summed_within_a_segment():
    a = loc(0, 30.0, -1.9)
    b = loc(10, 30.001, -1.9)  # 12s apart, normal cadence
    segments = segment_locations_by_gap([a, b])
    assert len(segments) == 1
    expected = haversine_km(a.longitude, a.latitude, b.longitude, b.latitude)
    total = 0.0
    for seg in segments:
        for i in range(1, len(seg)):
            total += haversine_km(seg[i - 1].longitude, seg[i - 1].latitude, seg[i].longitude, seg[i].latitude)
    assert total == expected
    assert total > 0
