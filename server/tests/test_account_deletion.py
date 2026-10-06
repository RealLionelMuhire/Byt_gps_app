"""In-app account deletion (DELETE /api/auth/me) and the shared cleanup in
app/services/account_deletion.py that it and Clerk's user.deleted webhook
both run."""

import asyncio
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth as auth_module
from app.api.webhooks import handle_user_deleted
from app.core.auth import require_auth
from app.core.config import settings
from app.core.database import get_db
from app.models.contact_request import ContactRequest
from app.models.device import Device
from app.models.geofence import Geofence
from app.models.location import Location
from app.models.subscription import Subscription, SubscriptionPlan
from app.models.trip import Trip
from app.models.trip_settings import TripSettings
from app.models.user import Role, User
from app.models.vehicle import Vehicle


def _seed_user(db, clerk_id="clerk_me", imei="111", role=Role.USER):
    """A user with one of everything that used to block or survive deletion."""
    user = User(clerk_user_id=clerk_id, email=f"{clerk_id}@example.com", first_name="A", last_name="B", role=role)
    db.add(user)
    db.commit()
    device = Device(imei=imei, name="d", lifecycle="sold", user_id=user.id)
    db.add(device)
    db.commit()
    loc = Location(device_id=device.id, latitude=-1.95, longitude=30.06, timestamp=datetime.utcnow())
    db.add(loc)
    db.commit()
    db.add_all([
        Trip(device_id=device.id, user_id=user.id, name="t", start_time=datetime.utcnow(),
             start_location_id=loc.id),
        Geofence(user_id=user.id, name="g", center_latitude=1, center_longitude=1, radius_meters=100),
        TripSettings(user_id=user.id),
        Vehicle(clerk_user_id=clerk_id, nickname="Car", plate="RAB123A", make="Toyota", model="RAV4",
                device_id=device.id),
        ContactRequest(clerk_user_id=clerk_id, type="support", name="A", phone="0788", message="hi"),
    ])
    db.commit()
    return user, device


def _assert_deleted(db, user_id, clerk_id="clerk_me", imei="111"):
    db.expire_all()
    assert db.query(User).filter_by(clerk_user_id=clerk_id).first() is None
    device = db.query(Device).filter_by(imei=imei).one()
    assert (device.user_id, device.lifecycle) == (None, "in_stock")
    assert db.query(Location).filter_by(device_id=device.id).count() == 0
    assert db.query(Trip).filter_by(device_id=device.id).count() == 0
    assert db.query(Vehicle).filter_by(clerk_user_id=clerk_id).count() == 0
    assert db.query(ContactRequest).filter_by(clerk_user_id=clerk_id).count() == 0
    assert db.query(Geofence).filter_by(user_id=user_id).count() == 0
    assert db.query(TripSettings).filter_by(user_id=user_id).count() == 0


def test_webhook_deletes_user_with_trips_geofences_and_trip_settings(db_session):
    # Regression: these used to make db.delete(user) fail (the ORM backrefs
    # tried to NULL their non-nullable user_id), so the webhook 500ed.
    user, _ = _seed_user(db_session)
    asyncio.run(handle_user_deleted("clerk_me", db_session))
    _assert_deleted(db_session, user.id)


def test_deletion_leaves_other_users_data_alone(db_session):
    _seed_user(db_session)
    other, other_device = _seed_user(db_session, clerk_id="clerk_other", imei="222")
    asyncio.run(handle_user_deleted("clerk_me", db_session))
    db_session.expire_all()
    assert db_session.query(User).filter_by(clerk_user_id="clerk_other").one().id == other.id
    assert db_session.query(Location).filter_by(device_id=other_device.id).count() == 1
    assert db_session.query(Trip).filter_by(user_id=other.id).count() == 1
    assert db_session.query(Geofence).filter_by(user_id=other.id).count() == 1


# ── DELETE /api/auth/me ────────────────────────────────────────────────────────


class _FakeClerk:
    """Stands in for httpx.AsyncClient; records the DELETE it receives."""

    def __init__(self, status_code=200, raise_error=False):
        self.status_code = status_code
        self.raise_error = raise_error
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def delete(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if self.raise_error:
            raise httpx.ConnectError("down")
        return httpx.Response(self.status_code, text="")


@pytest.fixture()
def auth_client(db_session, monkeypatch):
    monkeypatch.setattr(settings, "CLERK_SECRET_KEY", "sk_test_x")
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/api/auth")
    app.dependency_overrides[get_db] = lambda: db_session

    async def _auth():
        return "clerk_me"

    app.dependency_overrides[require_auth] = _auth
    return TestClient(app)


def _use_clerk(monkeypatch, fake):
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", fake)
    return fake


def test_delete_me_deletes_in_clerk_then_locally(auth_client, db_session, monkeypatch):
    user, _ = _seed_user(db_session)
    clerk = _use_clerk(monkeypatch, _FakeClerk(200))

    res = auth_client.delete("/api/auth/me")

    assert res.status_code == 204
    assert clerk.calls == ["https://api.clerk.com/v1/users/clerk_me"]
    _assert_deleted(db_session, user.id)


def test_delete_me_keeps_financial_record(auth_client, db_session, monkeypatch):
    _seed_user(db_session)
    plan = SubscriptionPlan(name="Basic", slug="basic", price=2450, currency="RWF")
    db_session.add(plan)
    db_session.commit()
    db_session.add(Subscription(clerk_user_id="clerk_me", plan_id=plan.id, status="active", price=2450,
                                expires_at=datetime.utcnow()))
    db_session.commit()
    _use_clerk(monkeypatch, _FakeClerk(200))

    assert auth_client.delete("/api/auth/me").status_code == 204

    db_session.expire_all()
    assert db_session.query(Subscription).filter_by(clerk_user_id="clerk_me").one().status == "cancelled"


@pytest.mark.parametrize("fake", [_FakeClerk(500), _FakeClerk(raise_error=True)])
def test_delete_me_clerk_failure_changes_nothing(auth_client, db_session, monkeypatch, fake):
    _seed_user(db_session)
    _use_clerk(monkeypatch, fake)

    assert auth_client.delete("/api/auth/me").status_code == 502

    db_session.expire_all()
    assert db_session.query(User).filter_by(clerk_user_id="clerk_me").count() == 1
    assert db_session.query(Location).count() == 1


def test_delete_me_already_gone_from_clerk_still_cleans_up(auth_client, db_session, monkeypatch):
    user, _ = _seed_user(db_session)
    _use_clerk(monkeypatch, _FakeClerk(404))

    assert auth_client.delete("/api/auth/me").status_code == 204
    _assert_deleted(db_session, user.id)


def test_delete_me_refuses_the_only_super_admin(auth_client, db_session, monkeypatch):
    _seed_user(db_session, role=Role.SUPER_ADMIN)
    clerk = _use_clerk(monkeypatch, _FakeClerk(200))

    assert auth_client.delete("/api/auth/me").status_code == 409
    assert clerk.calls == []


def test_delete_me_allows_a_super_admin_when_another_exists(auth_client, db_session, monkeypatch):
    _seed_user(db_session, role=Role.SUPER_ADMIN)
    db_session.add(User(clerk_user_id="clerk_boss", email="boss@example.com", first_name="B", last_name="B",
                        role=Role.SUPER_ADMIN))
    db_session.commit()
    _use_clerk(monkeypatch, _FakeClerk(200))

    assert auth_client.delete("/api/auth/me").status_code == 204
