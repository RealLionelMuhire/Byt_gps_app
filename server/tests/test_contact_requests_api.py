"""
Route-level tests for:
  - phone_number round-tripping through POST /api/auth/sync
  - POST/GET /api/contact-requests (app/api/contact_requests.py)

Builds its own FastAPI app (real `auth` + `contact_requests` routers)
since the shared `client` fixture in conftest.py doesn't include them.
Reuses conftest's `db_session`/`current_clerk_id` fixtures.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.auth import require_auth
from app.api import auth, contact_requests
from app.models.user import User, Role


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


@pytest.fixture()
def client(db_session, current_clerk_id):
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(contact_requests.router, prefix="/api/contact-requests")

    def _override_get_db():
        yield db_session

    async def _override_require_auth():
        return current_clerk_id["value"]

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_auth] = _override_require_auth

    with TestClient(app) as c:
        yield c


# ── phone_number via /api/auth/sync ────────────────────────────────────────

def test_sync_stores_phone_number_on_create(client, current_clerk_id):
    current_clerk_id["value"] = "clerk_phone_new"
    resp = client.post("/api/auth/sync", json={
        "clerk_user_id": "clerk_phone_new",
        "email": "phone_new@example.com",
        "name": "Phone New",
        "phone_number": "250781234567",
    })
    assert resp.status_code == 200
    assert resp.json()["phone_number"] == "250781234567"


def test_sync_updates_phone_number_on_existing_user(client, db_session, current_clerk_id):
    current_clerk_id["value"] = "clerk_phone_existing"
    make_user(db_session, "clerk_phone_existing", email="existing@example.com")

    resp = client.post("/api/auth/sync", json={
        "clerk_user_id": "clerk_phone_existing",
        "email": "existing@example.com",
        "phone_number": "250788888888",
    })
    assert resp.status_code == 200
    assert resp.json()["phone_number"] == "250788888888"


def test_sync_without_phone_number_does_not_clear_existing_value(client, db_session, current_clerk_id):
    current_clerk_id["value"] = "clerk_phone_keep"
    user = make_user(db_session, "clerk_phone_keep", email="keep@example.com")
    user.phone_number = "250700000000"
    db_session.commit()

    resp = client.post("/api/auth/sync", json={
        "clerk_user_id": "clerk_phone_keep",
        "email": "keep@example.com",
    })
    assert resp.status_code == 200
    assert resp.json()["phone_number"] == "250700000000"


# ── POST /api/contact-requests ─────────────────────────────────────────────

def test_create_contact_request(client, db_session, current_clerk_id):
    current_clerk_id["value"] = "clerk_requester"
    make_user(db_session, "clerk_requester")

    resp = client.post("/api/contact-requests", json={
        "type": "order_gps",
        "name": "Jane Doe",
        "phone": "250781112222",
        "message": "I'd like to order a GPS tracker.",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["clerk_user_id"] == "clerk_requester"
    assert body["type"] == "order_gps"


def test_create_contact_request_rejects_unknown_type(client, db_session, current_clerk_id):
    current_clerk_id["value"] = "clerk_requester2"
    make_user(db_session, "clerk_requester2")

    resp = client.post("/api/contact-requests", json={
        "type": "not_a_real_type",
        "name": "Jane Doe",
        "phone": "250781112222",
        "message": "hello",
    })
    assert resp.status_code == 422


# ── GET /api/contact-requests (admin-only) ─────────────────────────────────

def test_list_contact_requests_requires_admin(client, db_session, current_clerk_id):
    current_clerk_id["value"] = "clerk_plain_user"
    make_user(db_session, "clerk_plain_user", role=Role.USER)

    resp = client.get("/api/contact-requests")
    assert resp.status_code == 403


def test_list_contact_requests_as_admin(client, db_session, current_clerk_id):
    current_clerk_id["value"] = "clerk_requester3"
    make_user(db_session, "clerk_requester3")
    resp = client.post("/api/contact-requests", json={
        "type": "support",
        "name": "Jane Doe",
        "phone": "250781112222",
        "message": "Need help.",
    })
    assert resp.status_code == 201

    current_clerk_id["value"] = "clerk_admin"
    make_user(db_session, "clerk_admin", role=Role.ADMIN)

    resp = client.get("/api/contact-requests")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["type"] == "support"
    assert items[0]["clerk_user_id"] == "clerk_requester3"
