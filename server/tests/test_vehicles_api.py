"""
Route-level tests for vehicle CRUD (app/api/onboarding.py) and the RBAC /
orphan-unlink fixes made alongside it (app/api/devices.py).

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py
— a real FastAPI app (the actual routers) against an isolated in-memory
SQLite DB, with `require_auth` overridden to a controllable clerk_user_id.
"""

from datetime import datetime

from app.models.user import User, Role
from app.models.device import Device
from app.models.vehicle import Vehicle


def make_user(db, clerk_id, role=Role.USER, email=None):
    user = User(
        clerk_user_id=clerk_id,
        email=email or f"{clerk_id}@example.com",
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


def make_device(db, owner: User, imei="123456789012345", lifecycle="sold"):
    device = Device(
        imei=imei,
        name="Test Device",
        lifecycle=lifecycle,
        user_id=owner.id if owner else None,
        status="online",
        last_latitude=-1.9,
        last_longitude=30.05,
        last_update=datetime.utcnow(),
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def make_vehicle(db, owner: User, device: Device, nickname="My Car"):
    vehicle = Vehicle(
        clerk_user_id=owner.clerk_user_id,
        device_id=device.id if device else None,
        nickname=nickname,
        plate="RAA123B",
        make="Toyota",
        model="Corolla",
    )
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return vehicle


# ── PUT /api/vehicles/{id} ─────────────────────────────────────────────────

def test_owner_can_edit_own_vehicle_nickname(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    device = make_device(db_session, owner)
    vehicle = make_vehicle(db_session, owner, device, nickname="Old Name")

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.put(f"/api/vehicles/{vehicle.id}", json={"nickname": "New Name"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["nickname"] == "New Name"
    assert body["plate"] == "RAA123B"  # unchanged / immutable via this endpoint

    db_session.refresh(vehicle)
    assert vehicle.nickname == "New Name"


def test_owner_edit_rejects_blank_nickname(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    device = make_device(db_session, owner)
    vehicle = make_vehicle(db_session, owner, device)

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.put(f"/api/vehicles/{vehicle.id}", json={"nickname": "   "})

    assert resp.status_code == 400


def test_non_owner_cannot_edit_vehicle(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    device = make_device(db_session, owner)
    vehicle = make_vehicle(db_session, owner, device)

    current_clerk_id["value"] = other.clerk_user_id
    resp = client.put(f"/api/vehicles/{vehicle.id}", json={"nickname": "Hijacked"})

    assert resp.status_code in (403, 404)

    db_session.refresh(vehicle)
    assert vehicle.nickname != "Hijacked"


def test_non_owner_gets_404_on_nonexistent_vehicle(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.put("/api/vehicles/999999", json={"nickname": "Ghost"})
    assert resp.status_code == 404


def test_admin_can_edit_another_users_vehicle(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    device = make_device(db_session, owner)
    vehicle = make_vehicle(db_session, owner, device, nickname="Owner's Car")

    current_clerk_id["value"] = admin.clerk_user_id
    resp = client.put(f"/api/vehicles/{vehicle.id}", json={"nickname": "Renamed By Admin"})

    assert resp.status_code == 200
    assert resp.json()["nickname"] == "Renamed By Admin"

    db_session.refresh(vehicle)
    assert vehicle.nickname == "Renamed By Admin"
    # Vehicle stays attributed to the original owner, not the admin.
    assert vehicle.clerk_user_id == owner.clerk_user_id


# ── DELETE /api/vehicles/{id} ───────────────────────────────────────────────

def test_delete_vehicle_unlinks_device_to_inventory(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    device = make_device(db_session, owner, lifecycle="sold")
    vehicle = make_vehicle(db_session, owner, device)
    device_id = device.id

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.delete(f"/api/vehicles/{vehicle.id}")

    assert resp.status_code == 204
    assert db_session.query(Vehicle).filter(Vehicle.id == vehicle.id).first() is None

    db_session.expire_all()
    released = db_session.query(Device).filter(Device.id == device_id).first()
    assert released is not None
    assert released.user_id is None
    assert released.lifecycle == "in_stock"
    assert released.pairing_pin is not None


def test_delete_vehicle_with_no_linked_device_succeeds(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    vehicle = make_vehicle(db_session, owner, device=None)

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.delete(f"/api/vehicles/{vehicle.id}")

    assert resp.status_code == 204
    assert db_session.query(Vehicle).filter(Vehicle.id == vehicle.id).first() is None


def test_non_owner_cannot_delete_vehicle(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    device = make_device(db_session, owner)
    vehicle = make_vehicle(db_session, owner, device)

    current_clerk_id["value"] = other.clerk_user_id
    resp = client.delete(f"/api/vehicles/{vehicle.id}")

    assert resp.status_code in (403, 404)
    assert db_session.query(Vehicle).filter(Vehicle.id == vehicle.id).first() is not None


def test_admin_can_delete_another_users_vehicle(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    admin = make_user(db_session, "clerk_admin", role=Role.SUPER_ADMIN)
    device = make_device(db_session, owner)
    vehicle = make_vehicle(db_session, owner, device)

    current_clerk_id["value"] = admin.clerk_user_id
    resp = client.delete(f"/api/vehicles/{vehicle.id}")

    assert resp.status_code == 204
    assert db_session.query(Vehicle).filter(Vehicle.id == vehicle.id).first() is None


# ── GET /api/vehicles — RBAC scoping ────────────────────────────────────────

def test_list_vehicles_scoped_to_owner(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    device1 = make_device(db_session, owner, imei="111111111111111")
    device2 = make_device(db_session, other, imei="222222222222222")
    make_vehicle(db_session, owner, device1, nickname="Owner Car")
    make_vehicle(db_session, other, device2, nickname="Other Car")

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.get("/api/vehicles")

    assert resp.status_code == 200
    nicknames = [v["nickname"] for v in resp.json()["vehicles"]]
    assert nicknames == ["Owner Car"]


def test_list_vehicles_admin_sees_all(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    device1 = make_device(db_session, owner, imei="111111111111111")
    make_vehicle(db_session, owner, device1, nickname="Owner Car")

    current_clerk_id["value"] = admin.clerk_user_id
    resp = client.get("/api/vehicles")

    assert resp.status_code == 200
    nicknames = [v["nickname"] for v in resp.json()["vehicles"]]
    assert "Owner Car" in nicknames


# ── POST /api/vehicles — ownership + admin-on-behalf-of ────────────────────

def test_create_vehicle_rejects_device_not_owned_by_caller(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    device = make_device(db_session, owner, imei="333333333333333")

    current_clerk_id["value"] = other.clerk_user_id
    resp = client.post("/api/vehicles", json={
        "nickname": "Sneaky", "plate": "RAB999Z", "make": "Honda", "model": "Civic",
        "deviceImei": device.imei,
    })

    assert resp.status_code == 403


def test_admin_can_create_vehicle_for_clients_device(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    device = make_device(db_session, owner, imei="444444444444444")

    current_clerk_id["value"] = admin.clerk_user_id
    resp = client.post("/api/vehicles", json={
        "nickname": "Admin Registered", "plate": "RAC111X", "make": "Nissan", "model": "Note",
        "deviceImei": device.imei,
    })

    assert resp.status_code == 201
    vehicle_id = resp.json()["vehicleId"]

    created = db_session.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    assert created is not None
    # Attributed to the device's owner, not the admin who made the call.
    assert created.clerk_user_id == owner.clerk_user_id


# ── DELETE /api/devices/{id} — orphan-unlink fix ────────────────────────────

def test_delete_device_unlinks_dangling_vehicle(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    device = make_device(db_session, owner)
    vehicle = make_vehicle(db_session, owner, device)
    vehicle_id = vehicle.id

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.delete(f"/api/devices/{device.id}")

    assert resp.status_code == 204

    db_session.expire_all()
    surviving_vehicle = db_session.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    assert surviving_vehicle is not None  # kept, not deleted
    assert surviving_vehicle.device_id is None  # but unlinked, not dangling
