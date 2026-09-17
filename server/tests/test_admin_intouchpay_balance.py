"""
Route-level tests for GET /api/billing/admin/intouchpay-balance
(app/api/onboarding.py), which surfaces app/services/intouchpay.py's
get_balance() to the admin portal/app. intouch_get_balance is monkeypatched
at the onboarding module level (the name it was imported under) so these
tests never make a real IntouchPay call — that's covered separately by
test_intouchpay_service.py.

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from app.api import onboarding
from app.models.user import User, Role
from app.services.intouchpay import IntouchPayError


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


def test_admin_can_read_balance(client, db_session, current_clerk_id, monkeypatch):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    current_clerk_id["value"] = admin.clerk_user_id

    async def fake_get_balance():
        return {"status": "success", "balance": 125000, "currency": "RWF"}

    monkeypatch.setattr(onboarding, "intouch_get_balance", fake_get_balance)

    resp = client.get("/api/billing/admin/intouchpay-balance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["balance"] == 125000.0
    assert body["currency"] == "RWF"


def test_non_admin_forbidden(client, db_session, current_clerk_id, monkeypatch):
    user = make_user(db_session, "clerk_regular", role=Role.USER)
    current_clerk_id["value"] = user.clerk_user_id

    async def fake_get_balance():
        return {"status": "success", "balance": 125000, "currency": "RWF"}

    monkeypatch.setattr(onboarding, "intouch_get_balance", fake_get_balance)

    resp = client.get("/api/billing/admin/intouchpay-balance")
    assert resp.status_code == 403


def test_auth_error_body_surfaced_without_a_balance(client, db_session, current_clerk_id, monkeypatch):
    """An IntouchPay auth failure comes back as an ordinary 200 with no
    "balance" key — the endpoint must surface that as-is, not crash trying
    to coerce a missing balance into a float."""
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    current_clerk_id["value"] = admin.clerk_user_id

    async def fake_get_balance():
        return {"success": False, "responsecode": "0005", "message": "Invalid Password"}

    monkeypatch.setattr(onboarding, "intouch_get_balance", fake_get_balance)

    resp = client.get("/api/billing/admin/intouchpay-balance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["balance"] is None
    assert body["responsecode"] == "0005"
    assert body["message"] == "Invalid Password"


def test_unreachable_intouchpay_returns_502(client, db_session, current_clerk_id, monkeypatch):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    current_clerk_id["value"] = admin.clerk_user_id

    async def fake_get_balance():
        raise IntouchPayError("connection refused")

    monkeypatch.setattr(onboarding, "intouch_get_balance", fake_get_balance)

    resp = client.get("/api/billing/admin/intouchpay-balance")
    assert resp.status_code == 502
