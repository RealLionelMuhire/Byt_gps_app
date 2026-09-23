"""
Tests for payment consumption (migration 046, app/api/onboarding.py's
_claim_payment): a successful payment funds exactly one subscription, for
exactly the plan it paid for.

Before this, POST /api/subscriptions accepted any past successful payment
for the plan (free renewal after every expiry), and POST
/api/subscriptions/upgrade accepted any successful payment by the caller
(cheap plan's payment buys an expensive plan, repeatedly).

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from datetime import datetime, timedelta

import app.api.onboarding as onboarding_module
from app.api.onboarding import _claim_payment
from app.models.subscription import Payment, Subscription
from tests.test_plan_expiry_freshness import make_plan, make_user


def make_payment(db, owner, plan, tx_ref, consumed_by=None):
    payment = Payment(
        clerk_user_id=owner.clerk_user_id, tx_ref=tx_ref, plan_id=plan.id,
        amount=plan.price, currency="RWF", status="successful",
        verified_at=datetime.utcnow() - timedelta(minutes=5),
        consumed_at=datetime.utcnow() if consumed_by else None,
        subscription_id=consumed_by.id if consumed_by else None,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def make_subscription(db, owner, plan, *, expired=False):
    now = datetime.utcnow()
    sub = Subscription(
        clerk_user_id=owner.clerk_user_id, plan_id=plan.id, status="active",
        price=plan.price, started_at=now - timedelta(days=40 if expired else 1),
        expires_at=now - timedelta(days=10) if expired else now + timedelta(days=29),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def sub_count(db, owner):
    return db.query(Subscription).filter_by(clerk_user_id=owner.clerk_user_id).count()


# --- POST /api/subscriptions -------------------------------------------------


def test_subscribe_consumes_the_payment_and_links_it(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    payment = make_payment(db_session, owner, basic, "tx1")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/subscriptions", json={"planId": "basic"})

    assert resp.status_code == 201, resp.text
    db_session.refresh(payment)
    assert payment.consumed_at is not None
    assert payment.subscription_id == resp.json()["subscriptionId"]


def test_expired_subscriptions_payment_cannot_renew_it_for_free(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    old_sub = make_subscription(db_session, owner, basic, expired=True)
    make_payment(db_session, owner, basic, "tx1", consumed_by=old_sub)
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/subscriptions", json={"planId": "basic"})

    # 402 = "no usable payment yet" — the app keeps waiting for a real one.
    assert resp.status_code == 402
    assert sub_count(db_session, owner) == 1


def test_subscribe_twice_after_expiry_needs_a_second_payment(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    make_payment(db_session, owner, basic, "tx1")
    current_clerk_id["value"] = owner.clerk_user_id
    first = client.post("/api/subscriptions", json={"planId": "basic"})
    assert first.status_code == 201

    # Let it lapse, then try again with no new payment.
    sub = db_session.get(Subscription, first.json()["subscriptionId"])
    sub.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    assert client.post("/api/subscriptions", json={"planId": "basic"}).status_code == 402

    make_payment(db_session, owner, basic, "tx2")
    assert client.post("/api/subscriptions", json={"planId": "basic"}).status_code == 201


def test_another_plans_payment_does_not_fund_a_subscription(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    make_plan(db_session, "fleet", price=15000, max_devices=None)
    make_payment(db_session, owner, basic, "tx1")
    current_clerk_id["value"] = owner.clerk_user_id

    assert client.post("/api/subscriptions", json={"planId": "fleet"}).status_code == 402
    assert sub_count(db_session, owner) == 0


# --- POST /api/subscriptions/upgrade -----------------------------------------


def test_upgrade_rejects_a_payment_made_for_a_cheaper_plan(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    make_plan(db_session, "fleet", price=15000, max_devices=None)
    current = make_subscription(db_session, owner, basic)
    make_payment(db_session, owner, basic, "tx_basic")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/subscriptions/upgrade", json={"planId": "fleet", "txRef": "tx_basic"})

    assert resp.status_code == 409
    db_session.refresh(current)
    assert current.status == "active"
    assert sub_count(db_session, owner) == 1


def test_upgrade_consumes_the_payment(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    fleet = make_plan(db_session, "fleet", price=15000, max_devices=None)
    make_subscription(db_session, owner, basic)
    payment = make_payment(db_session, owner, fleet, "tx_fleet")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/subscriptions/upgrade", json={"planId": "fleet", "txRef": "tx_fleet"})

    assert resp.status_code == 201, resp.text
    db_session.refresh(payment)
    assert payment.subscription_id == resp.json()["subscriptionId"]


def test_retrying_a_successful_upgrade_returns_the_same_subscription(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    fleet = make_plan(db_session, "fleet", price=15000, max_devices=None)
    make_subscription(db_session, owner, basic)
    make_payment(db_session, owner, fleet, "tx_fleet")
    current_clerk_id["value"] = owner.clerk_user_id
    body = {"planId": "fleet", "txRef": "tx_fleet"}

    first = client.post("/api/subscriptions/upgrade", json=body)
    retry = client.post("/api/subscriptions/upgrade", json=body)

    assert first.status_code == retry.status_code == 201
    assert retry.json()["subscriptionId"] == first.json()["subscriptionId"]
    assert sub_count(db_session, owner) == 2  # the original (cancelled) + one upgrade


def test_a_used_payment_cannot_fund_a_second_switch(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    fleet = make_plan(db_session, "fleet", price=15000, max_devices=None)
    make_subscription(db_session, owner, basic)
    make_payment(db_session, owner, fleet, "tx_fleet")
    current_clerk_id["value"] = owner.clerk_user_id
    assert client.post(
        "/api/subscriptions/upgrade", json={"planId": "fleet", "txRef": "tx_fleet"}
    ).status_code == 201

    # Back to basic, then try to ride the same fleet payment again.
    resp = client.post("/api/subscriptions/upgrade", json={"planId": "basic", "txRef": "tx_fleet"})

    assert resp.status_code == 409
    assert sub_count(db_session, owner) == 2


# --- POST /api/payments/initiate ---------------------------------------------


def test_initiate_refuses_to_charge_for_the_plan_already_active(
    client, db_session, current_clerk_id, monkeypatch,
):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    make_subscription(db_session, owner, basic)
    current_clerk_id["value"] = owner.clerk_user_id

    async def must_not_charge(**kwargs):
        raise AssertionError("IntouchPay must not be called")

    monkeypatch.setattr(onboarding_module, "intouch_request_payment", must_not_charge)

    resp = client.post("/api/payments/initiate", json={"planId": "basic", "phone": "0780000000"})

    assert resp.status_code == 409
    assert "already on this plan" in resp.json()["detail"]
    assert db_session.query(Payment).count() == 0


# --- _claim_payment ----------------------------------------------------------


def test_claim_payment_only_succeeds_once(db_session):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    payment = make_payment(db_session, owner, basic, "tx1")
    first_sub = make_subscription(db_session, owner, basic)
    second_sub = make_subscription(db_session, owner, basic)

    assert _claim_payment(db_session, payment, first_sub) is True
    assert _claim_payment(db_session, payment, second_sub) is False
    db_session.commit()
    db_session.refresh(payment)
    assert payment.subscription_id == first_sub.id
