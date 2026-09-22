"""
Tests for time-aware geofence history (app/models/geofence_version.py,
GET /api/geofences/history) — which zones Historical Routes should draw for
a device over a past period.

Time is controlled by monkeypatching app.api.geofences._utcnow, so each
create/update/delete lands at a known moment.
"""

from datetime import datetime

import pytest

from app.api import geofences as geofences_api
from app.models.geofence_version import GeofenceVersion
from tests.test_geofences_api import make_device, make_user, VALID_BODY


@pytest.fixture
def clock(monkeypatch):
    state = {"now": datetime(2026, 6, 1)}
    monkeypatch.setattr(geofences_api, "_utcnow", lambda: state["now"])
    return state


def at(day, hour=0):
    return datetime(2026, 6, day, hour)


def history(client, device_id, start, end):
    resp = client.get(
        "/api/geofences/history",
        params={"device_id": device_id, "start": start.isoformat() + "Z", "end": end.isoformat() + "Z"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def owner_device(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    device = make_device(db_session, owner, "111111111111111")
    current_clerk_id["value"] = owner.clerk_user_id
    return owner, device


def create_zone(client, clock, when, device_ids, **overrides):
    clock["now"] = when
    resp = client.post("/api/geofences", json={**VALID_BODY, "device_ids": device_ids, **overrides})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def update_zone(client, clock, when, geofence_id, **fields):
    clock["now"] = when
    resp = client.put(f"/api/geofences/{geofence_id}", json=fields)
    assert resp.status_code == 200, resp.text


def test_zone_created_after_route_is_not_shown(client, clock, owner_device):
    _, device = owner_device
    create_zone(client, clock, at(22, 10), [device.id])

    assert history(client, device.id, at(21), at(22)) == []


def test_zone_created_mid_range_is_shown_with_its_real_start(client, clock, owner_device):
    _, device = owner_device
    zone_id = create_zone(client, clock, at(22, 10), [device.id])

    result = history(client, device.id, at(20), at(24))

    assert len(result) == 1
    assert result[0]["geofence_id"] == zone_id
    assert result[0]["name"] == "Home"
    assert result[0]["radius_meters"] == 200
    assert result[0]["periods"] == [{"active_from": "2026-06-22T10:00:00Z", "active_to": None}]


def test_disabled_zone_window_ends_when_it_was_disabled(client, clock, owner_device):
    _, device = owner_device
    zone_id = create_zone(client, clock, at(21, 8), [device.id])
    update_zone(client, clock, at(23, 15), zone_id, is_active=False)

    result = history(client, device.id, at(20), at(25))
    assert result[0]["periods"] == [
        {"active_from": "2026-06-21T08:00:00Z", "active_to": "2026-06-23T15:00:00Z"}
    ]
    # Entirely after it was disabled -> not shown.
    assert history(client, device.id, at(24), at(25)) == []


def test_re_enabled_zone_has_two_separate_windows(client, clock, owner_device):
    _, device = owner_device
    zone_id = create_zone(client, clock, at(20), [device.id])
    update_zone(client, clock, at(21), zone_id, is_active=False)
    update_zone(client, clock, at(23), zone_id, is_active=True)

    result = history(client, device.id, at(19), at(25))

    assert len(result) == 1
    assert result[0]["periods"] == [
        {"active_from": "2026-06-20T00:00:00Z", "active_to": "2026-06-21T00:00:00Z"},
        {"active_from": "2026-06-23T00:00:00Z", "active_to": None},
    ]
    # The gap itself shows nothing.
    assert history(client, device.id, at(21, 12), at(22, 12)) == []


def test_created_disabled_zone_is_not_shown(client, clock, owner_device):
    _, device = owner_device
    create_zone(client, clock, at(20), [device.id], is_active=False)

    assert history(client, device.id, at(19), at(25)) == []


def test_deleted_zone_history_survives_deletion(client, clock, owner_device, db_session):
    _, device = owner_device
    zone_id = create_zone(client, clock, at(20), [device.id])
    clock["now"] = at(22)
    assert client.delete(f"/api/geofences/{zone_id}").status_code == 204

    result = history(client, device.id, at(19), at(25))
    assert result[0]["periods"] == [
        {"active_from": "2026-06-20T00:00:00Z", "active_to": "2026-06-22T00:00:00Z"}
    ]
    assert history(client, device.id, at(23), at(25)) == []


def test_zone_not_assigned_to_device_is_not_shown(client, clock, owner_device, db_session):
    owner, device = owner_device
    other_device = make_device(db_session, owner, "222222222222222")
    create_zone(client, clock, at(20), [other_device.id])

    assert history(client, device.id, at(19), at(25)) == []


def test_device_assigned_later_only_shows_from_assignment(client, clock, owner_device, db_session):
    owner, device = owner_device
    other_device = make_device(db_session, owner, "222222222222222")
    zone_id = create_zone(client, clock, at(20), [other_device.id])
    update_zone(client, clock, at(22), zone_id, device_ids=[other_device.id, device.id])

    result = history(client, device.id, at(19), at(25))
    assert result[0]["periods"] == [{"active_from": "2026-06-22T00:00:00Z", "active_to": None}]


def test_rename_and_reassignment_merge_into_one_window(client, clock, owner_device, db_session):
    owner, device = owner_device
    other_device = make_device(db_session, owner, "222222222222222")
    zone_id = create_zone(client, clock, at(20), [device.id])
    update_zone(client, clock, at(21), zone_id, name="Depot")
    update_zone(client, clock, at(22), zone_id, device_ids=[device.id, other_device.id])

    result = history(client, device.id, at(19), at(25))

    assert len(result) == 1
    assert result[0]["name"] == "Depot"
    assert result[0]["periods"] == [{"active_from": "2026-06-20T00:00:00Z", "active_to": None}]


def test_moved_zone_returns_each_shape_with_its_own_window(client, clock, owner_device):
    _, device = owner_device
    zone_id = create_zone(client, clock, at(20), [device.id])
    update_zone(client, clock, at(22), zone_id, radius_meters=500)

    result = sorted(history(client, device.id, at(19), at(25)), key=lambda e: e["radius_meters"])

    assert [e["radius_meters"] for e in result] == [200, 500]
    assert result[0]["periods"] == [{"active_from": "2026-06-20T00:00:00Z", "active_to": "2026-06-22T00:00:00Z"}]
    assert result[1]["periods"] == [{"active_from": "2026-06-22T00:00:00Z", "active_to": None}]


def test_alert_setting_edits_do_not_create_versions(client, clock, owner_device, db_session):
    _, device = owner_device
    zone_id = create_zone(client, clock, at(20), [device.id])
    update_zone(client, clock, at(21), zone_id, alert_on_exit=False, description="gate")

    assert db_session.query(GeofenceVersion).filter_by(geofence_id=zone_id).count() == 1


def test_polygon_zone_history_returns_points(client, clock, owner_device):
    _, device = owner_device
    points = [{"lat": -1.9, "lng": 30.0}, {"lat": -1.9, "lng": 30.1}, {"lat": -1.8, "lng": 30.1}]
    clock["now"] = at(20)
    resp = client.post("/api/geofences", json={
        "name": "Yard", "shape_type": "polygon", "points": points, "device_ids": [device.id],
    })
    assert resp.status_code == 201, resp.text

    result = history(client, device.id, at(19), at(25))

    assert result[0]["shape_type"] == "polygon"
    assert result[0]["points"] == points
    assert result[0]["radius_meters"] is None


def test_history_for_other_users_device_is_404(client, clock, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    device = make_device(db_session, other, "333333333333333")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.get("/api/geofences/history", params={
        "device_id": device.id, "start": at(19).isoformat(), "end": at(25).isoformat(),
    })
    assert resp.status_code == 404


def test_history_rejects_bad_range(client, clock, owner_device):
    _, device = owner_device
    for start, end in [(at(25), at(20)), (datetime(2026, 5, 1), at(20))]:
        resp = client.get("/api/geofences/history", params={
            "device_id": device.id, "start": start.isoformat(), "end": end.isoformat(),
        })
        assert resp.status_code == 400
