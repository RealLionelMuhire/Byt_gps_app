"""
Route-level tests for Phase 3 of the plan/subscription consolidation:
enforcement points now check expires_at inline (_get_active_subscription in
app/api/onboarding.py) instead of trusting the cached Subscription.status
column alone, which scripts/cron_expiry.py only refreshes every 15 minutes.

Each test sets up a Subscription row that is STALE — status="active" in the
DB, but with expires_at already in the past, exactly the state a real
subscription sits in during the window between lapsing and the next cron
tick — and asserts the endpoint behaves as if it were genuinely inactive.

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from datetime import datetime, timedelta

from app.models.user import User, Role
from app.models.device import Device
from app.models.vehicle import Vehicle
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


def make_device(db, owner: User, imei, lifecycle="sold"):
    device = Device(
        imei=imei,
        name="Test Device",
        lifecycle=lifecycle,
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


def make_vehicle(db, owner: User, device: Device, nickname="My Car"):
    vehicle = Vehicle(
        clerk_user_id=owner.clerk_user_id,
        device_id=device.id if device else None,
        nickname=nickname,
        plate="RAA123B",
        make="Toyota",
        model="Corolla",
    )
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return vehicle


def make_plan(db, slug, price=2450.0, max_devices=3):
    """Subscription.plan_id/Payment.plan_id are real FKs (migrations
    041/042) — every test needs an actual subscription_plans row to point
    at, not just a bare slug string."""
    plan = SubscriptionPlan(
        name=slug.capitalize(), slug=slug, billing_type="recurrent",
        billing_model="prepaid", charge_scope="flat", price=price, currency="RWF",
        duration_value=1, duration_unit="month", max_devices=max_devices, is_active=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def make_stale_active_subscription(db, owner: User, plan: SubscriptionPlan):
    """A Subscription row stuck at status="active" past its own expires_at —
    the exact state cron_expiry.py hasn't caught up to yet."""
    sub = Subscription(
        clerk_user_id=owner.clerk_user_id,
        plan_id=plan.id,
        status="active",
        price=2450,
        started_at=datetime.utcnow() - timedelta(days=40),
        expires_at=datetime.utcnow() - timedelta(days=10),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


# ── create_vehicle (POST /api/vehicles) — vehicle-limit enforcement ─────────

def test_stale_subscription_does_not_grant_its_vehicle_limit(client, db_session, current_clerk_id):
    """basic's fallback limit is 3; trial's is 1. A stale "active" basic
    subscription must NOT be honored — the account should be measured
    against trial's limit of 1 instead."""
    owner = make_user(db_session, "clerk_owner")
    device1 = make_device(db_session, owner, imei="100000000000001")
    make_vehicle(db_session, owner, device1)  # 1 vehicle already registered
    device2 = make_device(db_session, owner, imei="100000000000002")
    basic_plan = make_plan(db_session, "basic", max_devices=3)
    make_stale_active_subscription(db_session, owner, basic_plan)
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/vehicles", json={
        "nickname": "Second Car", "plate": "RAB456C",
        "make": "Honda", "model": "Civic", "deviceImei": device2.imei,
    })
    assert resp.status_code == 403
    assert "trial" in resp.json()["detail"].lower()


def test_genuinely_active_subscription_still_grants_its_limit(client, db_session, current_clerk_id):
    """Sanity check the fix doesn't over-correct: a real, unexpired basic
    subscription (limit 3) still allows a second vehicle."""
    owner = make_user(db_session, "clerk_owner")
    device1 = make_device(db_session, owner, imei="100000000000003")
    make_vehicle(db_session, owner, device1)
    device2 = make_device(db_session, owner, imei="100000000000004")
    basic_plan = make_plan(db_session, "basic", max_devices=3)
    sub = Subscription(
        clerk_user_id=owner.clerk_user_id, plan_id=basic_plan.id, status="active",
        price=2450, started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=20),
    )
    db_session.add(sub)
    db_session.commit()
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/vehicles", json={
        "nickname": "Second Car", "plate": "RAB456C",
        "make": "Honda", "model": "Civic", "deviceImei": device2.imei,
    })
    assert resp.status_code == 201


# ── create_subscription (POST /api/subscriptions) — renewal idempotency ────

def test_create_subscription_does_not_short_circuit_on_stale_expired_sub(client, db_session, current_clerk_id):
    """A customer who just paid for "fleet" after their "basic" subscription
    lapsed must get the new subscription recorded, not be silently handed
    back the stale expired one."""
    owner = make_user(db_session, "clerk_owner")
    basic_plan = make_plan(db_session, "basic")
    fleet_plan = make_plan(db_session, "fleet", price=15000.0, max_devices=None)
    stale = make_stale_active_subscription(db_session, owner, basic_plan)
    payment = Payment(
        clerk_user_id=owner.clerk_user_id, tx_ref="tx-fleet-1", plan_id=fleet_plan.id,
        amount=15000, currency="RWF", status="successful", verified_at=datetime.utcnow(),
    )
    db_session.add(payment)
    db_session.commit()
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/subscriptions", json={"planId": "fleet"})
    assert resp.status_code == 201
    assert resp.json()["subscriptionId"] != stale.id

    new_sub = db_session.query(Subscription).filter(Subscription.id == resp.json()["subscriptionId"]).one()
    assert new_sub.plan.slug == "fleet"
    assert new_sub.status == "active"


# ── upgrade_subscription (POST /api/subscriptions/upgrade) ─────────────────

def test_upgrade_allows_renewing_the_same_plan_after_stale_expiry(client, db_session, current_clerk_id):
    """Renewing to the SAME plan right after it lapsed (but before cron
    flips its status) must succeed, not 400 with "Already on this plan" —
    that stale row shouldn't be treated as still covering the account."""
    owner = make_user(db_session, "clerk_owner")
    basic_plan = make_plan(db_session, "basic")
    stale = make_stale_active_subscription(db_session, owner, basic_plan)
    payment = Payment(
        clerk_user_id=owner.clerk_user_id, tx_ref="tx-basic-renew", plan_id=basic_plan.id,
        amount=2450, currency="RWF", status="successful", verified_at=datetime.utcnow(),
    )
    db_session.add(payment)
    db_session.commit()
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/subscriptions/upgrade", json={"planId": "basic", "txRef": "tx-basic-renew"})
    assert resp.status_code == 201
    assert resp.json()["subscriptionId"] != stale.id

    new_sub = db_session.query(Subscription).filter(Subscription.id == resp.json()["subscriptionId"]).one()
    assert new_sub.plan.slug == "basic"
    assert new_sub.status == "active"
    assert new_sub.expires_at > datetime.utcnow()
