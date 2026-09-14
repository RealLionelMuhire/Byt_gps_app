"""
Tests for app/services/live_position.py's resolve_live_position stamping
Device.position_confirmed_at exactly when it actually confirms a new
position (see that field's doc: distinct from last_update, which is bumped
on every packet regardless of validity).
"""

from datetime import datetime, timedelta

from app.models.device import Device
from app.services.live_position import resolve_live_position

DEFAULT_STOP_THRESHOLD = 5.0


def _device(**kwargs):
    return Device(imei="123456789012345", name="Test Device", lifecycle="sold", **kwargs)


def test_first_ever_fix_stamps_position_confirmed_at():
    device = _device()
    now = datetime(2026, 1, 1, 12, 0, 0)

    result = resolve_live_position(device, -1.9, 30.1, 0.0, DEFAULT_STOP_THRESHOLD, now)

    assert result.updated is True
    assert device.position_confirmed_at == now


def test_in_radius_update_stamps_position_confirmed_at():
    device = _device(last_latitude=-1.9, last_longitude=30.1, position_confirmed_at=datetime(2026, 1, 1, 11, 0, 0))
    now = datetime(2026, 1, 1, 12, 0, 0)

    # ~1m away, well within CONFIRM_RADIUS_METERS.
    result = resolve_live_position(device, -1.900005, 30.1, 0.0, DEFAULT_STOP_THRESHOLD, now)

    assert result.updated is True
    assert device.position_confirmed_at == now


def test_moving_update_stamps_position_confirmed_at_even_far_away():
    device = _device(last_latitude=-1.9, last_longitude=30.1, position_confirmed_at=datetime(2026, 1, 1, 11, 0, 0))
    now = datetime(2026, 1, 1, 12, 0, 0)

    # Far away, but reported speed >= threshold — trusted immediately.
    result = resolve_live_position(device, -1.95, 30.15, 20.0, DEFAULT_STOP_THRESHOLD, now)

    assert result.updated is True
    assert device.position_confirmed_at == now


def test_held_candidate_does_not_stamp_position_confirmed_at():
    original_confirmed_at = datetime(2026, 1, 1, 11, 0, 0)
    device = _device(last_latitude=-1.9, last_longitude=30.1, position_confirmed_at=original_confirmed_at)
    now = datetime(2026, 1, 1, 12, 0, 0)

    # Far away, reportedly stopped — held for corroboration, not confirmed.
    result = resolve_live_position(device, -1.95, 30.15, 0.0, DEFAULT_STOP_THRESHOLD, now)

    assert result.updated is False
    assert result.held is True
    assert device.position_confirmed_at == original_confirmed_at


def test_corroborated_pending_stamps_position_confirmed_at():
    device = _device(
        last_latitude=-1.9, last_longitude=30.1,
        position_confirmed_at=datetime(2026, 1, 1, 10, 0, 0),
        pending_latitude=-1.95, pending_longitude=30.15,
        pending_since=datetime(2026, 1, 1, 11, 59, 0),
    )
    now = datetime(2026, 1, 1, 12, 0, 0)

    # Lands within CONFIRM_RADIUS_METERS of the fresh pending candidate.
    result = resolve_live_position(device, -1.950001, 30.150001, 0.0, DEFAULT_STOP_THRESHOLD, now)

    assert result.updated is True
    assert device.position_confirmed_at == now
