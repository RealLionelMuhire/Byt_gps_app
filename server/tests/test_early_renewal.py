"""
Tests for early renewal (purpose "renew"): paying for the current plan
before it ends adds one period to its expires_at, for the vehicle slots it
already has — through GET /api/payments/quote, POST /api/payments/initiate,
POST /api/subscriptions/renew, the webhook (fulfil_payment) and the public
/pay page.

Before this, a same-plan payment was refused until the plan had expired,
while the "plan ending soon" email told customers to renew before that date.

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from datetime import datetime, timedelta

import pytest

import app.api.onboarding as onboarding_module
from app.api.subscriptions import plan_config
from app.models.subscription import Payment, Subscription
from app.services.pay_links import make_pay_token
from app.services.subscription_billing import fulfil_payment
from tests.test_payment_fulfilment import active_subs, confirm_webhook, receipts  # noqa: F401
from tests.test_plan_expiry_freshness import make_plan, make_user
from tests.test_vehicle_subscriptions import active_sub, intouch, make_vehicles, succeed  # noqa: F401


@pytest.fixture
def owner_on_basic(db_session, current_clerk_id):
    """An owner with 3 vehicles, 2 of them on a 3-slot Basic plan with 20 of
    its 30 days left."""
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic", price=1000.0, max_devices=5)
    cars = make_vehicles(db_session, owner, 3)
    sub = active_sub(db_session, owner, basic, quantity=3, covered=cars[:2], started_days_ago=10)
    current_clerk_id["value"] = owner.clerk_user_id
    return owner, basic, cars, sub


def days(db):
    return plan_config(db, "basic")["days"]


def renew_payment(client, slug="basic"):
    return client.post("/api/payments/initiate", json={"planId": slug, "phone": "0781234567", "purpose": "renew"})


def test_quote_prices_another_period_of_the_current_slots(client, db_session, owner_on_basic):
    _, _, cars, sub = owner_on_basic
    resp = client.get("/api/payments/quote", params={"planId": "basic", "purpose": "renew"})
    assert resp.status_code == 200, resp.text
    q = resp.json()
    assert q["amount"] == 3000.0  # 3 slots × 1000, not the 2 covered vehicles
    assert q["billableVehicles"] == 3
    assert {v["id"] for v in q["vehicles"]} == {c.id for c in cars[:2]}
    assert q["blockedReason"] is None and q["requiresPayment"] is True
    starts = datetime.fromisoformat(q["startsAt"].rstrip("Z"))
    expires = datetime.fromisoformat(q["expiresAt"].rstrip("Z"))
    assert abs((starts - sub.expires_at).total_seconds()) < 1
    assert abs((expires - starts) - timedelta(days=days(db_session))) < timedelta(seconds=1)


def test_paying_early_extends_the_current_plan_from_its_end_date(
    client, db_session, owner_on_basic, intouch,
):
    owner, _, cars, sub = owner_on_basic
    old_end = sub.expires_at

    resp = renew_payment(client)
    assert resp.status_code == 200, resp.text
    tx_ref = resp.json()["txRef"]
    assert intouch[0]["amount"] == 3000.0
    payment = db_session.query(Payment).filter_by(tx_ref=tx_ref).one()
    assert payment.purpose == "renew"
    assert sorted(payment.vehicle_ids) == sorted(c.id for c in cars[:2])

    assert client.post("/api/subscriptions/renew", json={"txRef": tx_ref}).status_code == 402  # still pending
    succeed(db_session, tx_ref)
    resp = client.post("/api/subscriptions/renew", json={"txRef": tx_ref})
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscriptionId"] == sub.id

    db_session.refresh(sub)
    assert sub.expires_at == old_end + timedelta(days=days(db_session))
    assert sub.quantity == 3
    assert [s.id for s in active_subs(db_session, owner)] == [sub.id]

    # A retry (lost response) returns the same result and doesn't extend again.
    again = client.post("/api/subscriptions/renew", json={"txRef": tx_ref})
    assert again.status_code == 200
    db_session.refresh(sub)
    assert sub.expires_at == old_end + timedelta(days=days(db_session))


def test_webhook_applies_the_renewal_and_the_app_call_then_matches(
    client, db_session, owner_on_basic, intouch, confirm_webhook, receipts,
):
    _, _, _, sub = owner_on_basic
    old_end = sub.expires_at
    tx_ref = renew_payment(client).json()["txRef"]

    confirm_webhook(tx_ref)

    db_session.refresh(sub)
    assert sub.expires_at == old_end + timedelta(days=days(db_session))
    assert receipts == [(tx_ref, sub.id, False)]
    resp = client.post("/api/subscriptions/renew", json={"txRef": tx_ref})
    assert resp.status_code == 200, resp.text
    db_session.refresh(sub)
    assert sub.expires_at == old_end + timedelta(days=days(db_session))


def test_a_renewal_confirmed_after_the_plan_lapsed_starts_a_fresh_period(
    client, db_session, owner_on_basic, intouch,
):
    owner, basic, cars, sub = owner_on_basic
    tx_ref = renew_payment(client).json()["txRef"]
    sub.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()
    payment = succeed(db_session, tx_ref)

    fulfilment = fulfil_payment(db_session, payment)

    assert fulfilment is not None and fulfilment.subscription.id != sub.id
    [new] = active_subs(db_session, owner)
    assert new.plan_id == basic.id
    assert new.expires_at > datetime.utcnow() + timedelta(days=days(db_session) - 1)


@pytest.mark.parametrize("case", ["already_renewed", "recurring", "deactivated", "other_plan"])
def test_renewal_is_refused_before_any_charge(client, db_session, owner_on_basic, intouch, case):
    _, basic, _, sub = owner_on_basic
    slug = "basic"
    if case == "already_renewed":
        sub.expires_at = datetime.utcnow() + timedelta(days=days(db_session) + 5)
    elif case == "recurring":
        sub.is_recurring = True
    elif case == "deactivated":
        basic.is_active = False
    elif case == "other_plan":
        make_plan(db_session, "premium", price=5000.0)
        slug = "premium"
    db_session.commit()

    resp = renew_payment(client, slug)
    assert resp.status_code in (400, 409), resp.text
    assert intouch == []
    if case != "other_plan":
        quote = client.get("/api/payments/quote", params={"planId": "basic", "purpose": "renew"}).json()
        assert quote["blockedReason"]


def test_nothing_to_renew_without_an_active_plan(client, db_session, current_clerk_id, intouch):
    owner = make_user(db_session, "clerk_owner")
    make_plan(db_session, "basic")
    make_vehicles(db_session, owner, 1)
    current_clerk_id["value"] = owner.clerk_user_id
    assert renew_payment(client).status_code == 409
    assert intouch == []


def test_renew_endpoint_refuses_other_purposes(client, db_session, owner_on_basic):
    owner, basic, cars, _ = owner_on_basic
    db_session.add(Payment(
        clerk_user_id=owner.clerk_user_id, tx_ref="IPsub", plan_id=basic.id, amount=1000, currency="RWF",
        status="successful", purpose="subscribe", vehicle_ids=[cars[0].id], verified_at=datetime.utcnow(),
    ))
    db_session.commit()
    assert client.post("/api/subscriptions/renew", json={"txRef": "IPsub"}).status_code == 409


def test_paying_for_the_current_plan_as_a_new_subscription_points_to_renewal(client, owner_on_basic, intouch):
    resp = client.post("/api/payments/initiate", json={"planId": "basic", "phone": "0781234567"})
    assert resp.status_code == 409
    assert "renew it early" in resp.json()["detail"]
    assert intouch == []


def test_the_public_pay_page_can_renew_early(client, db_session, owner_on_basic, intouch):
    onboarding_module._public_pay_attempts.clear()
    _, _, _, sub = owner_on_basic
    account = client.get("/api/payments/public/account", params={"token": make_pay_token("clerk_owner")}).json()
    assert account["activePlanSlug"] == "basic" and account["activeQuantity"] == 3
    resp = client.post("/api/payments/public/initiate", json={
        "token": make_pay_token("clerk_owner"), "phone": "0781234567", "planId": "basic", "purpose": "renew",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount"] == 3000.0
    payment = db_session.query(Payment).filter_by(tx_ref=resp.json()["txRef"]).one()
    assert payment.purpose == "renew"
    onboarding_module._public_pay_attempts.clear()
