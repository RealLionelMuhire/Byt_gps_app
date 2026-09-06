"""
Route-level tests for geofence CRUD (app/api/geofences.py).

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py
— a real FastAPI app (the actual geofences router) against an isolated
in-memory SQLite DB, with `require_auth` overridden to a controllable
clerk_user_id.
"""

from app.models.user import User, Role
from app.models.geofence import Geofence


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
