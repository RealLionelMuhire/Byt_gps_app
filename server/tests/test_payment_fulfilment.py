"""
Tests for app/services/subscription_billing.py's fulfil_payment: a payment
confirmed by the IntouchPay webhook, the cron reconciliation or the public
pay page's status poll is completed exactly as the customer's own
activation call would complete it — and that call, arriving afterwards,
returns the same result instead of failing or duplicating it.

Before this, the webhook created a brand-new 30-day subscription for EVERY
successful payment: a plan switch left two active subscriptions (the old
one never cancelled), an add-vehicles payment became a separate
subscription instead of extra slots on the current one (and the app's own
POST /api/subscriptions/vehicles then 409'd "already used").

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import scripts.cron_expiry as cron_expiry_module
from app.api import webhooks
from app.models.subscription import Payment, Subscription
from app.services.subscription_billing import fulfil_payment
from tests.test_plan_expiry_freshness import make_plan, make_user
from tests.test_vehicle_subscriptions import active_sub, covered, make_vehicles


@pytest.fixture
def receipts(monkeypatch):
    sent = []

    async def fake_receipt(db, payment, fulfilment):
        sent.append((payment.tx_ref, fulfilment.subscription.id, fulfilment.added_vehicles))
        return True

    monkeypatch.setattr(webhooks, "send_fulfilment_receipt", fake_receipt)
    monkeypatch.setattr(cron_expiry_module, "send_fulfilment_receipt", fake_receipt)
    return sent


@pytest.fixture
def confirm_webhook(client, monkeypatch):
    """POST the IntouchPay callback for `tx_ref`, with IntouchPay's own
    status check reporting success."""
    client.app.include_router(webhooks.router, prefix="/api/webhooks")

    async def fake_status(tx_ref, provider_tx_id=None):
        return {"status": "successful"}

    monkeypatch.setattr(webhooks, "get_transaction_status", fake_status)
    monkeypatch.setattr(webhooks, "classify_status", lambda resp: "successful")

    def confirm(tx_ref):
        resp = client.post("/api/webhooks/intouchpay", json={"requesttransactionid": tx_ref, "status": "successful"})
        assert resp.status_code == 200, resp.text
    return confirm


def pending_payment(db, user, plan, tx_ref, vehicles, *, purpose="subscribe", amount=None):
    p = Payment(
        clerk_user_id=user.clerk_user_id, tx_ref=tx_ref, plan_id=plan.id,
        amount=amount if amount is not None else plan.price * max(1, len(vehicles)),
        currency="RWF", status="pending", purpose=purpose,
        vehicle_ids=[v.id for v in vehicles], verified_at=datetime.utcnow(),
    )
    db.add(p)
    db.commit()
    return p


def active_subs(db, user):
    now = datetime.utcnow()
    return (
        db.query(Subscription)
        .filter(Subscription.clerk_user_id == user.clerk_user_id, Subscription.status == "active",
                Subscription.expires_at > now)
        .all()
    )


# --- New subscription ---------------------------------------------------------


def test_webhook_activates_a_first_subscription_and_the_app_gets_the_same_one(
    client, db_session, current_clerk_id, confirm_webhook, receipts,
):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    cars = make_vehicles(db_session, owner, 2)
    payment = pending_payment(db_session, owner, basic, "IPnew", cars)

    confirm_webhook("IPnew")

    [sub] = active_subs(db_session, owner)
    assert sub.plan_id == basic.id and sub.quantity == 2
    assert covered(db_session, sub) == {c.id for c in cars}
    db_session.refresh(payment)
    assert payment.consumed_at is not None and payment.subscription_id == sub.id
    assert receipts == [("IPnew", sub.id, False)]

    # The app's own activation call, arriving after the webhook.
    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.post("/api/subscriptions", json={"planId": "basic"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["subscriptionId"] == sub.id
    assert len(active_subs(db_session, owner)) == 1


# --- Plan switch --------------------------------------------------------------


def test_webhook_plan_switch_cancels_the_old_plan_instead_of_running_two(
    client, db_session, current_clerk_id, confirm_webhook, receipts,
):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    premium = make_plan(db_session, "premium", price=5000.0)
    cars = make_vehicles(db_session, owner, 1)
    old = active_sub(db_session, owner, basic, quantity=1, covered=cars)
    pending_payment(db_session, owner, premium, "IPswitch", cars)

    confirm_webhook("IPswitch")

    [sub] = active_subs(db_session, owner)
    assert sub.plan_id == premium.id
    db_session.refresh(old)
    assert old.status == "cancelled"

    # The app's upgrade call afterwards is treated as a retry, not an error.
    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.post("/api/subscriptions/upgrade", json={"planId": "premium", "txRef": "IPswitch"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["subscriptionId"] == sub.id
    assert len(active_subs(db_session, owner)) == 1


# --- Adding vehicles ----------------------------------------------------------


def test_webhook_add_vehicles_payment_adds_slots_to_the_current_subscription(
    client, db_session, current_clerk_id, confirm_webhook, receipts,
):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    cars = make_vehicles(db_session, owner, 3)
    sub = active_sub(db_session, owner, basic, quantity=1, covered=cars[:1])
    pending_payment(db_session, owner, basic, "IPadd", cars[1:], purpose="add_vehicles", amount=3000)

    confirm_webhook("IPadd")

    assert [s.id for s in active_subs(db_session, owner)] == [sub.id]  # no second subscription
    db_session.refresh(sub)
    assert sub.quantity == 3
    assert covered(db_session, sub) == {c.id for c in cars}
    assert receipts == [("IPadd", sub.id, True)]

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.post("/api/subscriptions/vehicles", json={"vehicleIds": [c.id for c in cars[1:]], "txRef": "IPadd"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscriptionId"] == sub.id
    assert resp.json()["quantity"] == 3


def test_add_vehicles_payment_is_left_alone_when_its_plan_is_no_longer_active(db_session):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    premium = make_plan(db_session, "premium", price=5000.0)
    cars = make_vehicles(db_session, owner, 2)
    active_sub(db_session, owner, premium, quantity=1, covered=cars[:1])
    payment = pending_payment(db_session, owner, basic, "IPstale", cars[1:], purpose="add_vehicles")
    payment.status = "successful"
    db_session.commit()

    assert fulfil_payment(db_session, payment) is None
    db_session.refresh(payment)
    assert payment.consumed_at is None
    assert len(active_subs(db_session, owner)) == 1


# --- Same plan already active ------------------------------------------------


def test_a_payment_for_the_already_active_plan_never_creates_a_second_subscription(db_session):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    cars = make_vehicles(db_session, owner, 1)
    sub = active_sub(db_session, owner, basic, quantity=1, covered=cars)
    payment = pending_payment(db_session, owner, basic, "IPsame", cars)
    payment.status = "successful"
    db_session.commit()

    assert fulfil_payment(db_session, payment) is None
    db_session.refresh(payment)
    assert payment.consumed_at is None
    assert [s.id for s in active_subs(db_session, owner)] == [sub.id]


def test_fulfil_ignores_pending_and_already_consumed_payments(db_session):
    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    cars = make_vehicles(db_session, owner, 1)
    payment = pending_payment(db_session, owner, basic, "IPpend", cars)

    assert fulfil_payment(db_session, payment) is None  # still pending
    payment.status = "successful"
    db_session.commit()
    assert fulfil_payment(db_session, payment) is not None
    assert fulfil_payment(db_session, payment) is None  # already consumed
    assert len(active_subs(db_session, owner)) == 1


# --- Cron reconciliation -------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_cron_reconciliation_also_completes_the_purchase(db_session, monkeypatch, receipts):
    monkeypatch.setattr(cron_expiry_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))

    async def fake_status(tx_ref, provider_tx_id=None):
        return {"status": "successful"}

    monkeypatch.setattr(cron_expiry_module, "get_transaction_status", fake_status)
    monkeypatch.setattr(cron_expiry_module, "classify_status", lambda resp: "successful")

    owner = make_user(db_session, "clerk_owner")
    basic = make_plan(db_session, "basic")
    cars = make_vehicles(db_session, owner, 1)
    payment = pending_payment(db_session, owner, basic, "IPcron", cars)
    payment.verified_at = datetime.utcnow() - timedelta(hours=1)  # past the reconcile grace window
    db_session.commit()

    await cron_expiry_module._reconcile_pending_intouchpay_payments_async()

    db_session.expire_all()
    [sub] = active_subs(db_session, owner)
    assert sub.plan_id == basic.id
    assert [r[0] for r in receipts] == ["IPcron"]
