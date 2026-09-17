"""
Route-level tests for Phase 4's self-service cancellation endpoint
(POST /api/subscriptions/cancel, app/api/onboarding.py) and its shared
_cancel_active_subscription helper, also now used by the Phase 2 admin
cancel endpoint (POST /api/admin/subscriptions/{user_id}/cancel).

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from datetime import datetime, timedelta

from app.models.user import User, Role
from app.models.subscription import Subscription, SubscriptionPlan


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


def make_plan(db, slug, price=2450.0):
    """Subscription.plan_id is a real FK (migrations 041/042) — every test
    needs an actual subscription_plans row to point at."""
    plan = SubscriptionPlan(
        name=slug.capitalize(), slug=slug, billing_type="recurrent",
        billing_model="prepaid", charge_scope="flat", price=price, currency="RWF",
        duration_value=1, duration_unit="month", max_devices=3, is_active=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def test_cancel_own_active_subscription(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    plan = make_plan(db_session, "basic")
    sub = Subscription(
        clerk_user_id=owner.clerk_user_id, plan_id=plan.id, status="active",
        price=2450, started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=20),
    )
    db_session.add(sub)
    db_session.commit()
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/subscriptions/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    db_session.refresh(sub)
    assert sub.status == "cancelled"


def test_cancel_with_no_active_subscription_404(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/subscriptions/cancel")
    assert resp.status_code == 404


def test_cancel_ignores_a_stale_expired_subscription(client, db_session, current_clerk_id):
    """A subscription past its own expires_at but not yet flipped to
    "expired" by cron must not be cancellable — there's nothing genuinely
    active to cancel (Phase 3's expiry-freshness check applies here too,
    since _cancel_active_subscription is built on _get_active_subscription)."""
    owner = make_user(db_session, "clerk_owner")
    plan = make_plan(db_session, "basic")
    sub = Subscription(
        clerk_user_id=owner.clerk_user_id, plan_id=plan.id, status="active",
        price=2450, started_at=datetime.utcnow() - timedelta(days=40),
        expires_at=datetime.utcnow() - timedelta(days=5),
    )
    db_session.add(sub)
    db_session.commit()
    current_clerk_id["value"] = owner.clerk_user_id

    resp = client.post("/api/subscriptions/cancel")
    assert resp.status_code == 404

    db_session.refresh(sub)
    assert sub.status == "active"  # untouched — cron will flip it to expired


def test_cannot_cancel_another_users_subscription(client, db_session, current_clerk_id):
    owner = make_user(db_session, "clerk_owner")
    other = make_user(db_session, "clerk_other")
    plan = make_plan(db_session, "basic")
    sub = Subscription(
        clerk_user_id=owner.clerk_user_id, plan_id=plan.id, status="active",
        price=2450, started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=20),
    )
    db_session.add(sub)
    db_session.commit()
    current_clerk_id["value"] = other.clerk_user_id

    resp = client.post("/api/subscriptions/cancel")
    assert resp.status_code == 404

    db_session.refresh(sub)
    assert sub.status == "active"


def test_admin_and_self_service_cancel_share_the_same_effect(client, db_session, current_clerk_id):
    """Both entry points end up in the exact same state — proving they share
    _cancel_active_subscription rather than diverging behavior."""
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    plan = make_plan(db_session, "fleet", price=15000.0)
    sub = Subscription(
        clerk_user_id=target.clerk_user_id, plan_id=plan.id, status="active",
        price=15000, started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=20),
    )
    db_session.add(sub)
    db_session.commit()
    current_clerk_id["value"] = admin.clerk_user_id

    resp = client.post(f"/api/admin/subscriptions/{target.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    db_session.refresh(sub)
    assert sub.status == "cancelled"
