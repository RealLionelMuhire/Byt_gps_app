"""
Route-level tests for geofence CRUD (app/api/geofences.py).

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py
— a real FastAPI app (the actual geofences router) against an isolated
in-memory SQLite DB, with `require_auth` overridden to a controllable
clerk_user_id.
"""

from app.models.user import User, Role
from app.models.device import Device
from app.models.geofence import Geofence
from app.models.geofence_device import GeofenceDevice


def make_user(db, clerk_id, role=Role.USER):
    user = User(
        clerk_user_id=clerk_id,
        email=f"{clerk_id}@example.com",
        first_name="Test",
        last_name="User",
        role=role,
        onboarding_step=0,
        onboarding_complete=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_device(db, owner: User, imei):
    device = Device(imei=imei, name="Device", lifecycle="sold", user_id=owner.id, status="online")
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


VALID_BODY = {
    "name": "Home",
    "center_latitude": -1.9,
    "center_longitude": 30.05,
    "radius_meters": 200,
}


def test_create_geofence(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json=VALID_BODY)

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Home"
    assert body["center_latitude"] == -1.9
    assert body["is_active"] is True
    assert body["alert_on_enter"] is True
    assert body["alert_on_exit"] is True

    geofence = db_session.query(Geofence).filter_by(id=body["id"]).first()
    assert geofence.user_id == owner.id


def test_list_geofences_only_returns_own(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    db_session.add(Geofence(user_id=owner.id, name="Mine", center_latitude=-1.9,
                             center_longitude=30.05, radius_meters=200))
    db_session.add(Geofence(user_id=other.id, name="Not mine", center_latitude=-1.9,
                             center_longitude=30.05, radius_meters=200))
    db_session.commit()

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.get("/api/geofences")

    assert resp.status_code == 200
    names = [g["name"] for g in resp.json()]
    assert names == ["Mine"]


def test_get_other_users_geofence_is_404(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    geofence = Geofence(user_id=other.id, name="Not mine", center_latitude=-1.9,
                         center_longitude=30.05, radius_meters=200)
    db_session.add(geofence)
    db_session.commit()
    db_session.refresh(geofence)

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.get(f"/api/geofences/{geofence.id}")

    assert resp.status_code == 404


def test_update_geofence_partial(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    geofence = Geofence(user_id=owner.id, name="Home", center_latitude=-1.9,
                         center_longitude=30.05, radius_meters=200, alert_on_exit=True)
    db_session.add(geofence)
    db_session.commit()
    db_session.refresh(geofence)

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.put(f"/api/geofences/{geofence.id}", json={"radius_meters": 500, "alert_on_exit": False})

    assert resp.status_code == 200
    body = resp.json()
    assert body["radius_meters"] == 500
    assert body["alert_on_exit"] is False
    assert body["name"] == "Home"  # untouched fields preserved


def test_delete_geofence(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    geofence = Geofence(user_id=owner.id, name="Home", center_latitude=-1.9,
                         center_longitude=30.05, radius_meters=200)
    db_session.add(geofence)
    db_session.commit()
    db_session.refresh(geofence)
    geofence_id = geofence.id

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.delete(f"/api/geofences/{geofence_id}")

    assert resp.status_code == 204
    assert db_session.query(Geofence).filter_by(id=geofence_id).first() is None


def test_cannot_delete_other_users_geofence(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    geofence = Geofence(user_id=other.id, name="Not mine", center_latitude=-1.9,
                         center_longitude=30.05, radius_meters=200)
    db_session.add(geofence)
    db_session.commit()
    db_session.refresh(geofence)

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.delete(f"/api/geofences/{geofence.id}")

    assert resp.status_code == 404
    assert db_session.query(Geofence).filter_by(id=geofence.id).first() is not None


def test_invalid_radius_is_rejected(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json={**VALID_BODY, "radius_meters": 5})

    assert resp.status_code == 422


def test_invalid_latitude_is_rejected(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json={**VALID_BODY, "center_latitude": 200})

    assert resp.status_code == 422


# --- Polygon geofences ---

POLYGON_BODY = {
    "name": "Warehouse yard",
    "shape_type": "polygon",
    "points": [
        {"lat": -1.91, "lng": 30.04},
        {"lat": -1.91, "lng": 30.06},
        {"lat": -1.89, "lng": 30.06},
        {"lat": -1.89, "lng": 30.04},
    ],
}


def test_create_polygon_geofence(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json=POLYGON_BODY)

    assert resp.status_code == 201
    body = resp.json()
    assert body["shape_type"] == "polygon"
    assert body["center_latitude"] is None
    assert body["radius_meters"] is None
    assert len(body["points"]) == 4
    assert {"lat": -1.91, "lng": 30.04} in body["points"]

    geofence = db_session.query(Geofence).filter_by(id=body["id"]).first()
    assert geofence.shape_type == "polygon"
    assert geofence.geom is not None
    assert geofence.center_latitude is None


def test_create_polygon_requires_at_least_3_points(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json={**POLYGON_BODY, "points": POLYGON_BODY["points"][:2]})

    assert resp.status_code == 422


def test_create_polygon_rejects_circle_fields(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json={**POLYGON_BODY, "radius_meters": 200})

    assert resp.status_code == 422


def test_create_circle_rejects_points(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json={**VALID_BODY, "points": POLYGON_BODY["points"]})

    assert resp.status_code == 422


def test_get_polygon_geofence_returns_points_not_circle_fields(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id
    created = client.post("/api/geofences", json=POLYGON_BODY).json()

    resp = client.get(f"/api/geofences/{created['id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["shape_type"] == "polygon"
    assert body["center_latitude"] is None
    assert body["center_longitude"] is None
    assert body["radius_meters"] is None
    assert len(body["points"]) == 4


def test_update_polygon_points_replaces_ring(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id
    created = client.post("/api/geofences", json=POLYGON_BODY).json()

    new_points = [
        {"lat": 0.0, "lng": 0.0}, {"lat": 0.0, "lng": 1.0}, {"lat": 1.0, "lng": 1.0},
    ]
    resp = client.put(f"/api/geofences/{created['id']}", json={"points": new_points, "shape_type": "polygon"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["points"]) == 3
    assert {"lat": 0.0, "lng": 0.0} in body["points"]


def test_update_circle_to_polygon_switches_shape(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id
    created = client.post("/api/geofences", json=VALID_BODY).json()

    resp = client.put(
        f"/api/geofences/{created['id']}",
        json={"shape_type": "polygon", "points": POLYGON_BODY["points"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["shape_type"] == "polygon"
    assert body["center_latitude"] is None
    assert body["radius_meters"] is None
    assert len(body["points"]) == 4


def test_update_polygon_points_without_shape_type_is_rejected(client, db_session, current_clerk_id):
    """Sending circle fields (or points) without declaring shape_type on a
    row of the other shape is rejected rather than silently ignored."""
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id
    created = client.post("/api/geofences", json=VALID_BODY).json()

    resp = client.put(f"/api/geofences/{created['id']}", json={"points": POLYGON_BODY["points"]})

    assert resp.status_code == 400


# --- Device scoping (device_ids) ---


def test_create_geofence_with_no_device_ids_is_unscoped(client, db_session, current_clerk_id):
    """Omitting device_ids entirely must not implicitly apply to the
    owner's whole fleet — it defaults to an empty (unscoped) list."""
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json=VALID_BODY)

    assert resp.status_code == 201
    assert resp.json()["device_ids"] == []


def test_create_geofence_with_device_ids(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    d1 = make_device(db_session, owner, imei="600000000000001")
    d2 = make_device(db_session, owner, imei="600000000000002")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json={**VALID_BODY, "device_ids": [d1.id, d2.id]})

    assert resp.status_code == 201
    body = resp.json()
    assert sorted(body["device_ids"]) == sorted([d1.id, d2.id])

    links = db_session.query(GeofenceDevice).filter_by(geofence_id=body["id"]).all()
    assert {l.device_id for l in links} == {d1.id, d2.id}


def test_create_geofence_rejects_device_ids_not_owned_by_caller(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    other_device = make_device(db_session, other, imei="600000000000003")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json={**VALID_BODY, "device_ids": [other_device.id]})

    assert resp.status_code == 400
    assert db_session.query(Geofence).count() == 0  # nothing partially created


def test_create_geofence_rejects_nonexistent_device_ids(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/geofences", json={**VALID_BODY, "device_ids": [999999]})

    assert resp.status_code == 400


def test_update_device_ids_replaces_the_assigned_set(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    d1 = make_device(db_session, owner, imei="600000000000004")
    d2 = make_device(db_session, owner, imei="600000000000005")
    current_clerk_id["value"] = owner.clerk_user_id
    created = client.post("/api/geofences", json={**VALID_BODY, "device_ids": [d1.id]}).json()

    resp = client.put(f"/api/geofences/{created['id']}", json={"device_ids": [d2.id]})

    assert resp.status_code == 200
    assert resp.json()["device_ids"] == [d2.id]


def test_update_without_device_ids_leaves_assignment_untouched(client, db_session, current_clerk_id):
    """device_ids omitted from the PUT body (vs. sent as []) must not
    clear existing assignments — matches this endpoint's existing
    partial-update semantics for every other field."""
    owner = make_user(db_session, "clerk_owner")
    d1 = make_device(db_session, owner, imei="600000000000006")
    current_clerk_id["value"] = owner.clerk_user_id
    created = client.post("/api/geofences", json={**VALID_BODY, "device_ids": [d1.id]}).json()

    resp = client.put(f"/api/geofences/{created['id']}", json={"name": "Renamed"})

    assert resp.status_code == 200
    assert resp.json()["device_ids"] == [d1.id]


def test_update_device_ids_empty_list_clears_assignment(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    d1 = make_device(db_session, owner, imei="600000000000007")
    current_clerk_id["value"] = owner.clerk_user_id
    created = client.post("/api/geofences", json={**VALID_BODY, "device_ids": [d1.id]}).json()

    resp = client.put(f"/api/geofences/{created['id']}", json={"device_ids": []})

    assert resp.status_code == 200
    assert resp.json()["device_ids"] == []


## Cascade delete of geofence_devices rows (ON DELETE CASCADE, migration
## 026) isn't exercised here: this suite's SQLite fixture never enables
## `PRAGMA foreign_keys=ON`, and the ORM relationship uses
## passive_deletes=True (trusts the DB to cascade) like the pre-existing
## geofence_device_state relationship — neither is cascade-tested in this
## harness. The DDL itself mirrors that already-trusted pattern.
