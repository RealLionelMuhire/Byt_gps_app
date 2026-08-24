"""Unit tests for compute_quality_log_fields (Phase 4 GPS quality logging)."""

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.locations import TIME_GAP_SEGMENT_BREAK_SECONDS, compute_quality_log_fields


@dataclass
class FakeLoc:
    timestamp: datetime
    longitude: float
    latitude: float


T0 = datetime(2026, 8, 22, 12, 0, 0)


def test_no_previous_point_returns_all_none():
    fields = compute_quality_log_fields(None, 30.0, -1.9, course=90, satellites=9, ts=T0)
    assert fields["satellites"] == 9
    assert fields["implied_speed_kmh"] is None
    assert fields["course_delta_degrees"] is None
    assert fields["gap_seconds"] is None
    assert fields["is_segment_break"] is False


def test_normal_cadence_no_segment_break():
    prev = FakeLoc(timestamp=T0, longitude=30.0, latitude=-1.9)
    fields = compute_quality_log_fields(prev, 30.001, -1.9, course=90, satellites=10, ts=T0 + timedelta(seconds=12))
    assert fields["gap_seconds"] == 12
    assert fields["is_segment_break"] is False
    assert fields["implied_speed_kmh"] is not None and fields["implied_speed_kmh"] > 0


def test_large_gap_flags_segment_break():
    prev = FakeLoc(timestamp=T0, longitude=30.0, latitude=-1.9)
    ts = T0 + timedelta(seconds=TIME_GAP_SEGMENT_BREAK_SECONDS + 1)
    fields = compute_quality_log_fields(prev, 30.001, -1.9, course=90, satellites=10, ts=ts)
    assert fields["is_segment_break"] is True
    assert fields["gap_seconds"] == TIME_GAP_SEGMENT_BREAK_SECONDS + 1


def test_course_delta_none_when_stationary():
    # Same coordinates -> movement too small for a meaningful bearing
    prev = FakeLoc(timestamp=T0, longitude=30.0, latitude=-1.9)
    fields = compute_quality_log_fields(prev, 30.0, -1.9, course=90, satellites=10, ts=T0 + timedelta(seconds=12))
    assert fields["course_delta_degrees"] is None


def test_course_delta_zero_when_consistent():
    # Move due east (~0.01 deg lon at this latitude is well above the noise
    # floor) and report course=90 (east) -- should match closely.
    prev = FakeLoc(timestamp=T0, longitude=30.0, latitude=-1.9)
    fields = compute_quality_log_fields(prev, 30.01, -1.9, course=90, satellites=10, ts=T0 + timedelta(seconds=12))
    assert fields["course_delta_degrees"] is not None
    assert fields["course_delta_degrees"] < 5  # near-zero delta: reported course matches actual movement


def test_course_delta_large_when_inconsistent():
    # Move due east but report course=270 (west) -- should be ~180 degrees off.
    prev = FakeLoc(timestamp=T0, longitude=30.0, latitude=-1.9)
    fields = compute_quality_log_fields(prev, 30.01, -1.9, course=270, satellites=10, ts=T0 + timedelta(seconds=12))
    assert fields["course_delta_degrees"] > 170
