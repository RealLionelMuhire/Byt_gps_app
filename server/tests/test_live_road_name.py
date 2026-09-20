"""
Tests for GET /api/locations/{device_id}/live_road_name (app/api/locations.py)
— an on-demand reverse-geocode of a device's current confirmed live position
(Device.last_latitude/last_longitude), backing the Track IQ Flutter app's
Monitoring screen "Driving: <road name>" status box.

Uses the `db_session`/`client`/`current_clerk_id` fixtures from conftest.py
for route-level tests, matching test_speed_limit.py's own pattern —
including monkeypatching `reverse_geocode` on the module it was imported
into (app.api.locations), the same style test_speed_limit.py uses for
app.tcp_server's `_resolve_overspeed_road_name`.
"""

import time

import pytest

import app.api.locations as locations_module
from app.models.device import Device
from app.models.user import User, Role
from app.services.geocoding import format_fallback_location


@pytest.fixture(autouse=True)
def _reset_road_name_rate_limit(monkeypatch):
    """`_last_road_name_request` is a module-level dict keyed by device_id,
    not reset between tests on its own — since db_session gives every test
    a fresh in-memory DB (device ids commonly restart at 1), a leftover
    entry from an earlier test could spuriously 429 an unrelated later one.
    Resetting it before every test in this file keeps them independent."""
    monkeypatch.setattr(locations_module, "_last_road_name_request", {})


def _make_owner_and_device(db_session, clerk_id="clerk_road_name_owner", **device_kwargs):
    user = User(
        clerk_user_id=clerk_id, email=f"{clerk_id}@example.com",
        first_name="Test", last_name="Owner", role=Role.USER,
        onboarding_step=0, onboarding_complete=False,
    )
    db_session.add(user)
    db_session.commit()

    device = Device(
        imei="123456789012345", name="Test Device", user_id=user.id, lifecycle="sold",
        **device_kwargs,
    )
    db_session.add(device)
    db_session.commit()
    db_session.refresh(device)
    return user, device


# ---------------------------------------------------------------------------
# Route-level: auth/ownership, missing-position, and the happy path's
# response shape.
# ---------------------------------------------------------------------------

def test_live_road_name_returns_the_resolved_name(client, db_session, current_clerk_id, monkeypatch):
    user, device = _make_owner_and_device(
        db_session, last_latitude=-1.9441, last_longitude=30.0619,
    )
    current_clerk_id["value"] = user.clerk_user_id
    monkeypatch.setattr(locations_module, "reverse_geocode", lambda lat, lon: "KN 247 Street")

    response = client.get(f"/api/locations/{device.id}/live_road_name")

    assert response.status_code == 200
    body = response.json()
    assert body["device_id"] == device.id
    assert body["latitude"] == -1.9441
    assert body["longitude"] == 30.0619
    assert body["road_name"] == "KN 247 Street"


def test_live_road_name_falls_back_to_coordinates_when_unresolved(
    client, db_session, current_clerk_id, monkeypatch,
):
    user, device = _make_owner_and_device(
        db_session, last_latitude=-1.9441, last_longitude=30.0619,
    )
    current_clerk_id["value"] = user.clerk_user_id
    monkeypatch.setattr(locations_module, "reverse_geocode", lambda lat, lon: None)

    response = client.get(f"/api/locations/{device.id}/live_road_name")

    assert response.status_code == 200
    assert response.json()["road_name"] == format_fallback_location(-1.9441, 30.0619)


def test_live_road_name_falls_back_on_timeout(client, db_session, current_clerk_id, monkeypatch):
    user, device = _make_owner_and_device(
        db_session, last_latitude=-1.9441, last_longitude=30.0619,
    )
    current_clerk_id["value"] = user.clerk_user_id

    def slow_lookup(lat, lon):
        time.sleep(0.3)
        return "Should never be seen"

    monkeypatch.setattr(locations_module, "reverse_geocode", slow_lookup)
    monkeypatch.setattr(locations_module, "LIVE_ROAD_NAME_GEOCODE_TIMEOUT_SECONDS", 0.05)

    response = client.get(f"/api/locations/{device.id}/live_road_name")

    assert response.status_code == 200
    assert response.json()["road_name"] == format_fallback_location(-1.9441, 30.0619)


def test_live_road_name_404s_when_device_has_no_live_position_yet(
    client, db_session, current_clerk_id,
):
    user, device = _make_owner_and_device(db_session)  # last_latitude/longitude left null
    current_clerk_id["value"] = user.clerk_user_id

    response = client.get(f"/api/locations/{device.id}/live_road_name")

    assert response.status_code == 404


def test_live_road_name_404s_for_an_unknown_device(client, db_session, current_clerk_id):
    user, _device = _make_owner_and_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get("/api/locations/999999/live_road_name").status_code == 404


def test_live_road_name_is_owner_or_admin_only(client, db_session, current_clerk_id):
    """require_device_access answers a non-owner with 404, not 403 — see
    test_speed_limit.py's identical test for why (a non-owner gets the
    same response whether the device doesn't exist or simply isn't theirs)."""
    _, device = _make_owner_and_device(
        db_session, clerk_id="clerk_owner", last_latitude=0, last_longitude=0,
    )
    other = User(
        clerk_user_id="clerk_other", email="other@example.com",
        first_name="Other", last_name="User", role=Role.USER,
        onboarding_step=0, onboarding_complete=False,
    )
    db_session.add(other)
    db_session.commit()

    current_clerk_id["value"] = "clerk_other"
    assert client.get(f"/api/locations/{device.id}/live_road_name").status_code == 404


def test_live_road_name_admin_can_read_another_users_device(
    client, db_session, current_clerk_id, monkeypatch,
):
    _, device = _make_owner_and_device(
        db_session, clerk_id="clerk_owner", last_latitude=0, last_longitude=0,
    )
    admin = User(
        clerk_user_id="clerk_admin", email="admin@example.com",
        first_name="Admin", last_name="User", role=Role.ADMIN,
        onboarding_step=0, onboarding_complete=False,
    )
    db_session.add(admin)
    db_session.commit()
    monkeypatch.setattr(locations_module, "reverse_geocode", lambda lat, lon: "Main Street")

    current_clerk_id["value"] = "clerk_admin"
    response = client.get(f"/api/locations/{device.id}/live_road_name")

    assert response.status_code == 200
    assert response.json()["road_name"] == "Main Street"


# ---------------------------------------------------------------------------
# Per-device throttle — a read endpoint the Flutter client polls every
# 30-60s while a vehicle is selected and driving, not a device command, so
# it has its own small in-process floor rather than going through
# CommandSettings/_enforce_command_policy.
# ---------------------------------------------------------------------------

def test_live_road_name_rate_limits_a_second_request_too_soon(
    client, db_session, current_clerk_id, monkeypatch,
):
    user, device = _make_owner_and_device(db_session, last_latitude=0, last_longitude=0)
    current_clerk_id["value"] = user.clerk_user_id
    monkeypatch.setattr(locations_module, "reverse_geocode", lambda lat, lon: "Main Street")

    first = client.get(f"/api/locations/{device.id}/live_road_name")
    second = client.get(f"/api/locations/{device.id}/live_road_name")

    assert first.status_code == 200
    assert second.status_code == 429


def test_live_road_name_allows_a_request_once_the_interval_has_passed(
    client, db_session, current_clerk_id, monkeypatch,
):
    user, device = _make_owner_and_device(db_session, last_latitude=0, last_longitude=0)
    current_clerk_id["value"] = user.clerk_user_id
    monkeypatch.setattr(locations_module, "reverse_geocode", lambda lat, lon: "Main Street")
    monkeypatch.setattr(locations_module, "_LIVE_ROAD_NAME_MIN_INTERVAL_SECONDS", 0.05)

    first = client.get(f"/api/locations/{device.id}/live_road_name")
    time.sleep(0.1)
    second = client.get(f"/api/locations/{device.id}/live_road_name")

    assert first.status_code == 200
    assert second.status_code == 200


def test_live_road_name_throttle_is_per_device(
    client, db_session, current_clerk_id, monkeypatch,
):
    user = User(
        clerk_user_id="clerk_multi_device", email="multi@example.com",
        first_name="Test", last_name="Owner", role=Role.USER,
        onboarding_step=0, onboarding_complete=False,
    )
    db_session.add(user)
    db_session.commit()

    device_a = Device(
        imei="111111111111111", name="Device A", user_id=user.id, lifecycle="sold",
        last_latitude=0, last_longitude=0,
    )
    device_b = Device(
        imei="222222222222222", name="Device B", user_id=user.id, lifecycle="sold",
        last_latitude=1, last_longitude=1,
    )
    db_session.add_all([device_a, device_b])
    db_session.commit()
    db_session.refresh(device_a)
    db_session.refresh(device_b)

    current_clerk_id["value"] = user.clerk_user_id
    monkeypatch.setattr(locations_module, "reverse_geocode", lambda lat, lon: "Main Street")

    response_a = client.get(f"/api/locations/{device_a.id}/live_road_name")
    response_b = client.get(f"/api/locations/{device_b.id}/live_road_name")

    assert response_a.status_code == 200
    assert response_b.status_code == 200
