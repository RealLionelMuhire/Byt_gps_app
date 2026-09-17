"""
Regression tests for GET /api/devices/ (app/api/devices.py's list_devices)
carrying owner_name/owner_email per device.

Before this fix, an admin viewing the fleet-wide device list (every device
in the system — see list_devices' own admin-sees-everything docstring) had
no way to tell whose vehicle was whose: every row showed only the
vehicle's own name/plate, no owner, which read as if the whole fleet
belonged to the admin.

Uses the `client`/`db_session`/`current_clerk_id` fixtures from
conftest.py, same pattern as test_vehicles_api.py/test_speed_limit.py.
"""

from datetime import datetime

from app.models.user import User, Role
from app.models.device import Device


def make_user(db, clerk_id, role=Role.USER, email=None, first_name="Test", last_name="User"):
    user = User(
        clerk_user_id=clerk_id,
        email=email or f"{clerk_id}@example.com",
        first_name=first_name,
        last_name=last_name,
        role=role,
        onboarding_step=0,
        onboarding_complete=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_device(db, owner, imei="123456789012345"):
    device = Device(
        imei=imei,
        name="Test Device",
        lifecycle="sold",
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


def test_admin_device_list_shows_the_real_owner_name(client, db_session, current_clerk_id):
    owner = make_user(
        db_session, "clerk_owner", email="jane@example.com",
        first_name="Jane", last_name="Doe",
    )
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    make_device(db_session, owner)

    current_clerk_id["value"] = admin.clerk_user_id
    resp = client.get("/api/devices/")

    assert resp.status_code == 200
    devices = resp.json()
    assert len(devices) == 1
    assert devices[0]["owner_name"] == "Jane Doe"
    assert devices[0]["owner_email"] == "jane@example.com"


def test_admin_device_list_falls_back_to_email_when_no_name_on_file(
    client, db_session, current_clerk_id,
):
    owner = make_user(
        db_session, "clerk_owner", email="jane@example.com",
        first_name="", last_name="",
    )
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    make_device(db_session, owner)

    current_clerk_id["value"] = admin.clerk_user_id
    resp = client.get("/api/devices/")

    assert resp.json()[0]["owner_name"] == "jane@example.com"


def test_unassigned_device_has_no_owner_name(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    make_device(db_session, owner=None)

    current_clerk_id["value"] = admin.clerk_user_id
    resp = client.get("/api/devices/")

    assert resp.json()[0]["owner_name"] is None
    assert resp.json()[0]["owner_email"] is None


def test_owner_sees_their_own_name_on_their_own_device(client, db_session, current_clerk_id):
    """Populated for every caller, not gated to admin-only — a regular
    user seeing their own name on their own device is harmless, and
    keeping the field unconditional avoids a second, role-based branch in
    the response-building code."""
    owner = make_user(
        db_session, "clerk_owner", email="jane@example.com",
        first_name="Jane", last_name="Doe",
    )
    make_device(db_session, owner)

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.get("/api/devices/")

    assert resp.status_code == 200
    assert resp.json()[0]["owner_name"] == "Jane Doe"
