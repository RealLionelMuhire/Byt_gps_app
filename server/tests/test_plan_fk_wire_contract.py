"""
Route-level tests confirming Phase 6's FK-ification of Subscription.plan_id/
Payment.plan_id (migrations 041/042, app/models/subscription.py) did NOT
change any client-facing JSON contract: every endpoint must keep serializing
a plan's *slug* (a string), resolved through the new `plan` relationship,
never the raw (now-integer) plan_id column. Flutter's BillingInfo/
BillingPayment and the admin portal's SubscriptionPlan/PaymentInfo/
DeviceSubscriptionInfo types all still expect a string here.

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from datetime import datetime, timedelta

from app.models.user import User, Role
from app.models.device import Device
from app.models.subscription import Subscription, Payment, SubscriptionPlan


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


def make_plan(db, slug="basic", name="Basic", price=2450.0, max_devices=3):
    plan = SubscriptionPlan(
        name=name, slug=slug, billing_type="recurrent", billing_model="prepaid",
        charge_scope="flat", price=price, currency="RWF", duration_value=1,
        duration_unit="month", max_devices=max_devices, is_active=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def make_device(db, owner: User, imei="123456789012345", lifecycle="sold"):
    device = Device(
        imei=imei, name="Test Device", lifecycle=lifecycle,
        user_id=owner.id if owner else None, status="online",
        last_latitude=-1.9, last_longitude=30.05, last_update=datetime.utcnow(),
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def test_get_billing_returns_plan_slug_strings_not_ids(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    plan = make_plan(db_session, slug="basic")
    sub = Subscription(
        clerk_user_id=owner.clerk_user_id, plan_id=plan.id, status="active",
        price=2450, started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=20),
    )
    payment = Payment(
        clerk_user_id=owner.clerk_user_id, tx_ref="tx-1", plan_id=plan.id,
        amount=2450, currency="RWF", status="successful", verified_at=datetime.utcnow(),
    )
    db_session.add_all([sub, payment])
    db_session.commit()
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.get("/api/billing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currentPlan"] == "basic"
    assert isinstance(body["currentPlan"], str)
    assert body["payments"][0]["planId"] == "basic"
    assert isinstance(body["payments"][0]["planId"], str)


def test_get_billing_defaults_to_trial_slug_with_no_subscription(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.get("/api/billing")
    assert resp.status_code == 200
    assert resp.json()["currentPlan"] == "trial"
    assert resp.json()["expiresAt"] is None


def test_device_billing_returns_plan_slug_strings(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    plan = make_plan(db_session, slug="fleet", name="Fleet", price=15000.0, max_devices=None)
    device = make_device(db_session, owner)
    sub = Subscription(
        clerk_user_id=owner.clerk_user_id, plan_id=plan.id, status="active",
        price=15000, started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=20),
    )
    payment = Payment(
        clerk_user_id=owner.clerk_user_id, tx_ref="tx-2", plan_id=plan.id,
        amount=15000, currency="RWF", status="successful", verified_at=datetime.utcnow(),
    )
    db_session.add_all([sub, payment])
    db_session.commit()
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.get(f"/api/devices/{device.id}/billing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"]["slug"] == "fleet"
    assert isinstance(body["plan"]["id"], int)
    assert body["subscription"]["plan_slug"] == "fleet"
    assert body["payments"][0]["plan_id"] == "fleet"
    assert isinstance(body["payments"][0]["plan_id"], str)


def test_list_devices_returns_plan_slug_string_in_subscription_block(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    plan = make_plan(db_session, slug="basic")
    device = make_device(db_session, owner)
    sub = Subscription(
        clerk_user_id=owner.clerk_user_id, plan_id=plan.id, status="active",
        price=2450, started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=20),
    )
    db_session.add(sub)
    db_session.commit()
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.get("/api/devices")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["subscription"]["plan_slug"] == "basic"
    assert body[0]["plan"]["slug"] == "basic"
    assert isinstance(body[0]["plan_id"], int)
    assert body[0]["plan_id"] == plan.id
