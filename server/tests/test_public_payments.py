"""
Tests for the web /pay page's public endpoints (app/api/onboarding.py
public_pay_account / public_initiate_payment / public_payment_status) and
the signed links that open it (app/services/pay_links.py).

Before this, POST /api/payments/public/initiate trusted a typed email
address: anyone could learn whether an address had an account (404 vs
200) and send Mobile Money prompts against it to any phone, with none of
the signed-in endpoint's rules — inactive plans, the same plan already
active, slot caps and add_vehicles proration were all skipped.

Uses the `client`/`db_session` fixtures from conftest.py.
"""

import time
from urllib.parse import parse_qs, urlparse

import pytest

import app.api.onboarding as onboarding_module
from app.models.subscription import Payment, Subscription
from app.services.email import _pay_url
from app.services.pay_links import PAY_LINK_TTL_SECONDS, make_pay_token, verify_pay_token
from tests.test_plan_expiry_freshness import make_plan, make_user
from tests.test_vehicle_subscriptions import active_sub, intouch, make_vehicles  # noqa: F401


@pytest.fixture(autouse=True)
def reset_rate_limit():
    onboarding_module._public_pay_attempts.clear()
    yield
    onboarding_module._public_pay_attempts.clear()


def initiate(client, token, plan="basic", **extra):
    return client.post("/api/payments/public/initiate",
                       json={"token": token, "phone": "0781234567", "planId": plan, **extra})


# --- Signed links -------------------------------------------------------------


def test_pay_token_round_trips_and_rejects_tampering_and_expiry():
    token = make_pay_token("clerk_owner")
    assert verify_pay_token(token) == "clerk_owner"

    payload, sig = token.split(".")
    assert verify_pay_token(f"{payload}.{'0' * len(sig)}") is None
    forged = make_pay_token("clerk_other").split(".")[0]
    assert verify_pay_token(f"{forged}.{sig}") is None
    assert verify_pay_token("") is None
    assert verify_pay_token("not-a-token") is None

    old = make_pay_token("clerk_owner", now=time.time() - PAY_LINK_TTL_SECONDS - 60)
    assert verify_pay_token(old) is None


def test_billing_email_links_carry_a_token_not_the_email_or_phone(db_session):
    owner = make_user(db_session, "clerk_owner", email="owner@example.com")
    owner.phone_number = "+250781234567"
    url = _pay_url(owner, "premium")
    assert "owner" not in url.split("token=")[0] and "example.com" not in url and "781234567" not in url
    assert urlparse(url).path.endswith("/pay/")
    query = parse_qs(urlparse(url).query)
    assert query["plan"] == ["premium"]
    assert verify_pay_token(query["token"][0]) == "clerk_owner"


# --- Who the link is for ----------------------------------------------------


def test_account_lookup_masks_the_email_and_needs_a_valid_token(client, db_session):
    owner = make_user(db_session, "clerk_owner", email="muhire@example.com")
    owner.phone_number = "0781234567"
    db_session.commit()
    make_vehicles(db_session, owner, 2)

    resp = client.get("/api/payments/public/account", params={"token": make_pay_token("clerk_owner")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["maskedEmail"] == "mu****@example.com"
    assert body["phone"] == "0781234567"
    assert body["vehicleCount"] == 2
    assert body["activePlanSlug"] is None

    assert client.get("/api/payments/public/account", params={"token": "bogus"}).status_code == 401


# --- Initiating -----------------------------------------------------------------


def test_an_email_alone_is_no_longer_enough(client, db_session, intouch):
    make_user(db_session, "clerk_owner", email="owner@example.com")
    make_plan(db_session, "basic")

    resp = client.post("/api/payments/public/initiate",
                       json={"email": "owner@example.com", "phone": "0781234567", "planId": "basic"})
    assert resp.status_code == 422
    assert initiate(client, "bogus").status_code == 401
    expired = make_pay_token("clerk_owner", now=time.time() - PAY_LINK_TTL_SECONDS - 60)
    assert initiate(client, expired).status_code == 401
    assert intouch == []


def test_a_valid_link_charges_the_account_it_was_issued_for(client, db_session, intouch):
    owner = make_user(db_session, "clerk_owner")
    make_user(db_session, "clerk_other")
    make_plan(db_session, "basic", price=2000.0)
    cars = make_vehicles(db_session, owner, 2)

    resp = initiate(client, make_pay_token("clerk_owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert resp.json()["amount"] == 4000.0
    [call] = intouch
    assert call["amount"] == 4000.0

    payment = db_session.query(Payment).filter_by(tx_ref=resp.json()["txRef"]).one()
    assert payment.clerk_user_id == "clerk_owner"
    assert payment.purpose == "subscribe"
    assert sorted(payment.vehicle_ids) == sorted(c.id for c in cars)


def test_without_a_selection_only_the_plans_cap_is_charged(client, db_session, intouch):
    owner = make_user(db_session, "clerk_owner")
    make_plan(db_session, "basic", price=1000.0, max_devices=2)
    make_vehicles(db_session, owner, 3)

    resp = initiate(client, make_pay_token("clerk_owner"))
    assert resp.status_code == 200, resp.text
    assert intouch[0]["amount"] == 2000.0


@pytest.mark.parametrize("case", ["same_plan_active", "inactive_plan", "trial", "over_cap_selection", "foreign_vehicle"])
def test_the_signed_in_rules_apply_before_any_charge(client, db_session, intouch, case):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    basic = make_plan(db_session, "basic", max_devices=1)
    make_plan(db_session, "trial", price=0.0, max_devices=1)
    cars = make_vehicles(db_session, owner, 2)
    plan, extra = "basic", {}
    if case == "same_plan_active":
        active_sub(db_session, owner, basic, quantity=1, covered=cars[:1])
    elif case == "inactive_plan":
        basic.is_active = False
        db_session.commit()
    elif case == "trial":
        plan, extra = "trial", {"vehicleIds": [cars[0].id]}
    elif case == "over_cap_selection":
        extra = {"vehicleIds": [c.id for c in cars]}
    elif case == "foreign_vehicle":
        extra = {"vehicleIds": [make_vehicles(db_session, other, 1)[0].id]}

    resp = initiate(client, make_pay_token("clerk_owner"), plan=plan, **extra)
    assert resp.status_code in (400, 409), resp.text
    assert intouch == []
    assert db_session.query(Payment).count() == 0


def test_add_vehicles_is_prorated_like_the_signed_in_flow(client, db_session, current_clerk_id, intouch):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic", price=3000.0)
    cars = make_vehicles(db_session, owner, 2)
    active_sub(db_session, owner, basic, quantity=1, covered=cars[:1], started_days_ago=15, period_days=30)

    resp = initiate(client, make_pay_token("clerk_owner"), purpose="add_vehicles", vehicleIds=[cars[1].id])
    assert resp.status_code == 200, resp.text
    current_clerk_id["value"] = owner.clerk_user_id
    quote = client.get("/api/payments/quote",
                       params={"planId": "basic", "purpose": "add_vehicles", "vehicleIds": str(cars[1].id)})
    assert resp.json()["amount"] == quote.json()["amount"] < 3000.0


def test_payment_prompts_per_account_are_rate_limited(client, db_session, intouch):
    owner = make_user(db_session, "clerk_owner")
    make_plan(db_session, "basic")
    make_vehicles(db_session, owner, 1)
    token = make_pay_token("clerk_owner")

    codes = [initiate(client, token).status_code for _ in range(onboarding_module._PUBLIC_PAY_MAX_ATTEMPTS + 1)]
    assert codes[:-1] == [200] * onboarding_module._PUBLIC_PAY_MAX_ATTEMPTS
    assert codes[-1] == 429
    assert len(intouch) == onboarding_module._PUBLIC_PAY_MAX_ATTEMPTS


# --- Status polling ---------------------------------------------------------------


def test_status_poll_activates_once_however_often_it_is_polled(client, db_session, intouch, monkeypatch):
    owner = make_user(db_session, "clerk_owner")
    make_plan(db_session, "basic")
    make_vehicles(db_session, owner, 1)
    tx_ref = initiate(client, make_pay_token("clerk_owner")).json()["txRef"]

    async def fake_status(tx_ref, provider_tx_id=None):
        return {"status": "successful"}

    monkeypatch.setattr(onboarding_module, "get_transaction_status", fake_status)
    monkeypatch.setattr(onboarding_module, "classify_status", lambda resp: "successful")

    for _ in range(3):
        resp = client.get(f"/api/payments/public/status/{tx_ref}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "successful"

    subs = db_session.query(Subscription).filter_by(clerk_user_id="clerk_owner", status="active").all()
    assert len(subs) == 1
