"""
Regression tests for the server-rendered /admin/devices inventory page.

_render_admin_devices() referenced a `devices` name that was never defined
and dashboard.py never imported HTTPException, so every route rendering this
page raised NameError (HTTP 500): the inventory page itself, plus the
assign / invite / add / delete / verify / unpair handlers whenever they hit
an HTTPException error path.

The shared conftest `client` app only mounts the API routers, so this file
builds its own app around app.dashboard.router and forges the admin session
cookie with dashboard._sign_session.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from app.core.database import get_db
from app.dashboard import SESSION_COOKIE, _sign_session, router as dashboard_router
from app.models.device import Device
from app.models.user import Role, User


def make_user(db, clerk_id, role=Role.USER):
    u = User(clerk_user_id=clerk_id, email=f"{clerk_id}@example.com",
             first_name="Test", last_name="User", role=role)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def admin(db_session):
    return make_user(db_session, "clerk_admin", role=Role.ADMIN)


@pytest.fixture()
def page_client(db_session, admin):
    app = FastAPI()
    app.include_router(dashboard_router)

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        c.cookies.set(SESSION_COOKIE, _sign_session(admin.clerk_user_id))
        yield c


def test_admin_devices_page_renders(page_client, db_session):
    db_session.add(Device(imei="123456789012345", name="Tracker A"))
    db_session.commit()

    resp = page_client.get("/admin/devices")

    assert resp.status_code == 200
    assert "Device Inventory" in resp.text
    assert "123456789012345" in resp.text


def test_admin_devices_redirects_anonymous(page_client):
    page_client.cookies.clear()

    resp = page_client.get("/admin/devices", follow_redirects=False)

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/admin/login"


def test_assign_unknown_client_shows_error_banner_not_500(page_client, db_session):
    db_session.add(Device(imei="333333333333333", name="Tracker B"))
    db_session.commit()

    resp = page_client.post(
        "/admin/devices/333333333333333/assign",
        data={"client_email": "nobody@example.com"},
    )

    assert resp.status_code == 200
    assert "Client not found" in resp.text


def test_assign_conflict_shows_error_banner_not_500(page_client, db_session):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    db_session.add(Device(imei="444444444444444", name="Tracker C", user_id=owner.id))
    db_session.commit()

    resp = page_client.post(
        "/admin/devices/444444444444444/assign",
        data={"client_email": other.email},
    )

    assert resp.status_code == 200
    assert "already assigned to another client" in resp.text
