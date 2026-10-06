"""
Tests for the "newest trip shows 0.0 km while the vehicle is moving" bug.

An auto-started trip (tcp_server.py's handle_location) is persisted with a
0.0 total_distance_km placeholder, and only end_active_trips_for_device
(app/services/trip_service.py) ever computes the real figure — when the
stale checker, a disconnect, or a manual end closes it. Two fixes covered:

- Reads (GET /api/trips, GET /api/trips/{id}) compute an ACTIVE trip's
  distance live from its location points instead of returning the stored
  placeholder. Ended trips keep their stored value, untouched.
- The close path no longer saves a permanent 0.0 km when the distance
  computation fails — it leaves the trip active so the next pass retries.

Uses the `db_session`/`client`/`current_clerk_id` fixtures from conftest.py,
same owner/device setup style as test_live_road_name.py.
"""

from datetime import datetime, timedelta

import pytest

import app.services.trip_service as trip_service_module
from app.core.config import settings
from app.models.device import Device
from app.models.location import Location
from app.models.trip import Trip
from app.models.user import User, Role
from app.api.locations import haversine_km
from app.services.trip_service import end_active_trips_for_device


T0 = datetime(2026, 10, 6, 8, 0, 0)

# Four fixes 10 minutes apart (under TIME_GAP_SEGMENT_BREAK_SECONDS, so one
# segment), stepping 0.01° of longitude each — roughly 1.1 km per hop.
ROUTE = [(-1.95, 30.05 + i * 0.01) for i in range(4)]


def _expected_route_km():
    return round(
        sum(
            haversine_km(lon1, lat1, lon2, lat2)
            for (lat1, lon1), (lat2, lon2) in zip(ROUTE, ROUTE[1:])
        ),
        3,
    )


@pytest.fixture(autouse=True)
def _no_external_calls(monkeypatch):
    # Entitlement gating and reverse geocoding are out of scope here.
    monkeypatch.setattr(settings, "ENTITLEMENT_MODE", "off")
    monkeypatch.setattr(
        trip_service_module, "build_trip_display_name", lambda *a: "Start → End"
    )


def _make_owner_and_device(db_session, clerk_id="clerk_trip_owner"):
    user = User(
        clerk_user_id=clerk_id, email=f"{clerk_id}@example.com",
        first_name="Test", last_name="Owner", role=Role.USER,
        onboarding_step=0, onboarding_complete=False,
    )
    db_session.add(user)
    db_session.commit()

    device = Device(imei="123456789012345", name="Test Device", user_id=user.id, lifecycle="sold")
    db_session.add(device)
    db_session.commit()
    db_session.refresh(device)
    return user, device


def _add_route_points(db_session, device):
    for i, (lat, lon) in enumerate(ROUTE):
        db_session.add(Location(
            device_id=device.id, latitude=lat, longitude=lon, speed=40.0,
            gps_valid=True, is_outlier=False,
            timestamp=T0 + timedelta(minutes=10 * i),
        ))
    db_session.commit()


def _add_trip(db_session, user, device, *, end_time=None, total_distance_km=0.0):
    trip = Trip(
        device_id=device.id, user_id=user.id, name="Trip",
        start_time=T0, end_time=end_time, total_distance_km=total_distance_km,
    )
    db_session.add(trip)
    db_session.commit()
    db_session.refresh(trip)
    return trip


# ---------------------------------------------------------------------------
# Reads: live distance for active trips
# ---------------------------------------------------------------------------

def test_list_trips_reports_live_distance_for_an_active_trip(client, db_session, current_clerk_id):
    user, device = _make_owner_and_device(db_session)
    _add_route_points(db_session, device)
    trip = _add_trip(db_session, user, device)  # active, stored 0.0 placeholder
    current_clerk_id["value"] = user.clerk_user_id

    response = client.get(f"/api/trips?device_id={device.id}")

    assert response.status_code == 200
    [row] = response.json()
    assert row["end_time"] is None
    assert row["total_distance_km"] == pytest.approx(_expected_route_km())
    assert row["total_distance_km"] > 3.0
    # Read-only: the stored placeholder is left for the close path to set.
    db_session.refresh(trip)
    assert trip.total_distance_km == 0.0


def test_list_trips_keeps_the_stored_distance_for_an_ended_trip(client, db_session, current_clerk_id):
    user, device = _make_owner_and_device(db_session)
    _add_route_points(db_session, device)
    _add_trip(db_session, user, device, end_time=T0 + timedelta(minutes=30), total_distance_km=4.2)
    current_clerk_id["value"] = user.clerk_user_id

    response = client.get(f"/api/trips?device_id={device.id}")

    assert response.status_code == 200
    [row] = response.json()
    assert row["total_distance_km"] == 4.2


def test_list_trips_active_trip_with_no_points_yet_reads_zero(client, db_session, current_clerk_id):
    user, device = _make_owner_and_device(db_session)
    _add_trip(db_session, user, device)
    current_clerk_id["value"] = user.clerk_user_id

    response = client.get(f"/api/trips?device_id={device.id}")

    assert response.status_code == 200
    assert response.json()[0]["total_distance_km"] == 0.0


def test_get_trip_reports_live_distance_for_an_active_trip(client, db_session, current_clerk_id):
    user, device = _make_owner_and_device(db_session)
    _add_route_points(db_session, device)
    trip = _add_trip(db_session, user, device)
    current_clerk_id["value"] = user.clerk_user_id

    response = client.get(f"/api/trips/{trip.id}?device_id={device.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["end_time"] is None
    assert body["total_distance_km"] == pytest.approx(_expected_route_km())


# ---------------------------------------------------------------------------
# Close path: no permanent 0.0 km on failure
# ---------------------------------------------------------------------------

def test_ending_a_trip_persists_its_real_distance(db_session):
    user, device = _make_owner_and_device(db_session)
    _add_route_points(db_session, device)
    trip = _add_trip(db_session, user, device)

    assert end_active_trips_for_device(device.id, db_session) == 1

    db_session.refresh(trip)
    assert trip.end_time == T0 + timedelta(minutes=30)
    assert trip.total_distance_km == pytest.approx(_expected_route_km())
    assert trip.display_name == "Start → End"


def test_distance_failure_leaves_the_trip_active_instead_of_saving_zero(db_session, monkeypatch):
    user, device = _make_owner_and_device(db_session)
    _add_route_points(db_session, device)
    trip = _add_trip(db_session, user, device)

    def _boom(*a, **kw):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(trip_service_module, "compute_distance_for_device_time_range", _boom)

    assert end_active_trips_for_device(device.id, db_session) == 0

    db_session.refresh(trip)
    assert trip.end_time is None  # still active — retried on the next pass
    assert trip.total_distance_km == 0.0

    # Next pass, once the failure clears, closes it with the real distance.
    monkeypatch.undo()
    monkeypatch.setattr(settings, "ENTITLEMENT_MODE", "off")
    monkeypatch.setattr(trip_service_module, "build_trip_display_name", lambda *a: "Start → End")
    assert end_active_trips_for_device(device.id, db_session) == 1
    db_session.refresh(trip)
    assert trip.end_time is not None
    assert trip.total_distance_km == pytest.approx(_expected_route_km())


def test_manual_end_reports_503_when_the_trip_could_not_be_closed(
    client, db_session, current_clerk_id, monkeypatch,
):
    user, device = _make_owner_and_device(db_session)
    _add_route_points(db_session, device)
    trip = _add_trip(db_session, user, device)
    current_clerk_id["value"] = user.clerk_user_id

    def _boom(*a, **kw):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(trip_service_module, "compute_distance_for_device_time_range", _boom)

    response = client.post(f"/api/trips/{trip.id}/end?device_id={device.id}")

    assert response.status_code == 503
    db_session.refresh(trip)
    assert trip.end_time is None


def test_manual_end_returns_the_closed_trip_with_its_distance(client, db_session, current_clerk_id):
    user, device = _make_owner_and_device(db_session)
    _add_route_points(db_session, device)
    trip = _add_trip(db_session, user, device)
    current_clerk_id["value"] = user.clerk_user_id

    response = client.post(f"/api/trips/{trip.id}/end?device_id={device.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["end_time"] is not None
    assert body["total_distance_km"] == pytest.approx(_expected_route_km())


# ---------------------------------------------------------------------------
# Close path: the trip ends when the vehicle arrived, not when the
# owner's "parked for N minutes" wait (stop_splits_trip_after_minutes)
# finally confirmed it
# ---------------------------------------------------------------------------

def _add_parked_tail(db_session, device, *, start_offset_minutes, minutes, every=5):
    lat, lon = ROUTE[-1]
    for m in range(0, minutes + 1, every):
        db_session.add(Location(
            device_id=device.id, latitude=lat, longitude=lon, speed=0.0,
            gps_valid=True, is_outlier=False,
            timestamp=T0 + timedelta(minutes=start_offset_minutes + m),
        ))
    db_session.commit()


def test_ending_a_trip_excludes_the_parked_wait_from_its_duration(db_session):
    user, device = _make_owner_and_device(db_session)
    _add_route_points(db_session, device)  # moving T0..T0+30
    # Arrives (speed 0) at T0+35, keeps pinging while parked for an hour.
    _add_parked_tail(db_session, device, start_offset_minutes=35, minutes=60)
    trip = _add_trip(db_session, user, device)

    assert end_active_trips_for_device(device.id, db_session) == 1

    db_session.refresh(trip)
    assert trip.end_time == T0 + timedelta(minutes=35)
    assert trip.total_distance_km == pytest.approx(_expected_route_km())


def test_short_move_followed_by_long_parking_is_discarded(db_session):
    user, device = _make_owner_and_device(db_session)
    lat, lon = ROUTE[0]
    db_session.add(Location(
        device_id=device.id, latitude=lat, longitude=lon, speed=20.0,
        gps_valid=True, is_outlier=False, timestamp=T0,
    ))
    db_session.commit()
    # Two minutes after a single moving fix it's parked — then pings for an
    # hour. Under the default 5-minute minimum, this isn't a real trip.
    _add_parked_tail(db_session, device, start_offset_minutes=2, minutes=60)
    trip = _add_trip(db_session, user, device)

    assert end_active_trips_for_device(device.id, db_session) == 0
    assert db_session.get(Trip, trip.id) is None


# ---------------------------------------------------------------------------
# The owner-configurable "parked for N minutes ends the trip" setting
# (GET/PUT /api/trips/settings — what the app's Trip detection screen calls)
# ---------------------------------------------------------------------------

def test_trip_settings_default_to_sixty_minutes(client, db_session, current_clerk_id):
    user, _ = _make_owner_and_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id

    response = client.get("/api/trips/settings")

    assert response.status_code == 200
    assert response.json()["stop_splits_trip_after_minutes"] == 60


def test_trip_settings_update_only_the_parked_interval(client, db_session, current_clerk_id):
    user, _ = _make_owner_and_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    before = client.get("/api/trips/settings").json()

    response = client.put("/api/trips/settings", json={"stop_splits_trip_after_minutes": 15})

    assert response.status_code == 200
    after = response.json()
    assert after["stop_splits_trip_after_minutes"] == 15
    assert {k: v for k, v in after.items() if k != "stop_splits_trip_after_minutes"} == {
        k: v for k, v in before.items() if k != "stop_splits_trip_after_minutes"
    }
    assert client.get("/api/trips/settings").json()["stop_splits_trip_after_minutes"] == 15


@pytest.mark.parametrize("minutes", [0, 10081])
def test_trip_settings_reject_out_of_range_intervals(client, db_session, current_clerk_id, minutes):
    user, _ = _make_owner_and_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id

    response = client.put("/api/trips/settings", json={"stop_splits_trip_after_minutes": minutes})

    assert response.status_code == 422
