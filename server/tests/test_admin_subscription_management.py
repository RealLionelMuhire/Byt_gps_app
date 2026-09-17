"""
Route-level tests for the admin per-user subscription-management endpoints
added in app/api/onboarding.py (Phase 2 of the plan/subscription
consolidation — see app/services/plan_resolution.py for Phase 1):

    GET   /api/admin/subscriptions/{user_id}
    PUT   /api/admin/subscriptions/{user_id}
    PATCH /api/admin/subscriptions/{user_id}/expiry
    POST  /api/admin/subscriptions/{user_id}/cancel

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py
— a real FastAPI app (the actual onboarding router) against an isolated
in-memory SQLite DB, with `require_auth` overridden to a controllable
clerk_user_id. `require_admin` is NOT overridden, so admin access is
exercised for real via the target user's actual `role` column.
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


def make_plan(db, slug="basic", name="Basic", price=2450.0, max_devices=3, is_active=True):
    plan = SubscriptionPlan(
        name=name, slug=slug, billing_type="recurrent", billing_model="prepaid",
        charge_scope="flat", price=price, currency="RWF",
        duration_value=1, duration_unit="month", max_devices=max_devices,
        is_active=is_active,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def as_admin(current_clerk_id, admin_user):
    current_clerk_id["value"] = admin_user.clerk_user_id


def test_get_subscription_none_when_user_has_never_subscribed(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    as_admin(current_clerk_id, admin)

    resp = client.get(f"/api/admin/subscriptions/{target.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "none"
    assert body["subscription_id"] is None
    assert body["plan_id"] is None


def test_get_subscription_requires_admin(client, db_session, current_clerk_id):
    target = make_user(db_session, "clerk_target")
    non_admin = make_user(db_session, "clerk_regular", role=Role.USER)
    current_clerk_id["value"] = non_admin.clerk_user_id

    resp = client.get(f"/api/admin/subscriptions/{target.id}")
    assert resp.status_code == 403


def test_get_subscription_unknown_user_404(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    as_admin(current_clerk_id, admin)

    resp = client.get("/api/admin/subscriptions/999999")
    assert resp.status_code == 404


def test_assign_plan_creates_active_subscription_with_defaults(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    make_plan(db_session, slug="basic", price=2450.0)
    as_admin(current_clerk_id, admin)

    resp = client.put(f"/api/admin/subscriptions/{target.id}", json={"plan_id": "basic"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["plan_id"] == "basic"
    assert body["plan_name"] == "Basic"
    assert body["price"] == 2450.0
    assert body["expires_at"] is not None

    sub = db_session.query(Subscription).filter(
        Subscription.clerk_user_id == target.clerk_user_id
    ).one()
    assert sub.status == "active"
    assert sub.plan.slug == "basic"


def test_assign_plan_honors_price_and_expiry_overrides(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    make_plan(db_session, slug="fleet", price=15000.0)
    as_admin(current_clerk_id, admin)

    custom_expiry = (datetime.utcnow() + timedelta(days=400)).isoformat()
    resp = client.put(
        f"/api/admin/subscriptions/{target.id}",
        json={"plan_id": "fleet", "price": 0.0, "expires_at": custom_expiry},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["price"] == 0.0
    assert body["expires_at"].startswith(custom_expiry[:10])


def test_assign_plan_cancels_existing_active_subscription(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    make_plan(db_session, slug="basic", price=2450.0)
    make_plan(db_session, slug="fleet", price=15000.0)
    as_admin(current_clerk_id, admin)

    client.put(f"/api/admin/subscriptions/{target.id}", json={"plan_id": "basic"})
    resp = client.put(f"/api/admin/subscriptions/{target.id}", json={"plan_id": "fleet"})
    assert resp.status_code == 200
    assert resp.json()["plan_id"] == "fleet"

    subs = db_session.query(Subscription).filter(
        Subscription.clerk_user_id == target.clerk_user_id
    ).order_by(Subscription.created_at.asc()).all()
    assert len(subs) == 2
    assert subs[0].plan.slug == "basic" and subs[0].status == "cancelled"
    assert subs[1].plan.slug == "fleet" and subs[1].status == "active"


def test_assign_plan_unknown_plan_404(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    as_admin(current_clerk_id, admin)

    resp = client.put(f"/api/admin/subscriptions/{target.id}", json={"plan_id": "does-not-exist"})
    assert resp.status_code == 404


def test_extend_expiry_adjusts_current_subscription(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    plan = make_plan(db_session, "basic")
    sub = Subscription(
        clerk_user_id=target.clerk_user_id, plan_id=plan.id, status="active",
        price=2450, started_at=datetime.utcnow() - timedelta(days=10),
        expires_at=datetime.utcnow() + timedelta(days=5),
    )
    db_session.add(sub)
    db_session.commit()
    as_admin(current_clerk_id, admin)

    new_expiry = (datetime.utcnow() + timedelta(days=60)).isoformat()
    resp = client.patch(
        f"/api/admin/subscriptions/{target.id}/expiry", json={"expires_at": new_expiry}
    )
    assert resp.status_code == 200
    assert resp.json()["expires_at"].startswith(new_expiry[:10])
    assert resp.json()["status"] == "active"


def test_extend_expiry_reactivates_an_expired_subscription(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    plan = make_plan(db_session, "basic")
    sub = Subscription(
        clerk_user_id=target.clerk_user_id, plan_id=plan.id, status="active",
        price=2450, started_at=datetime.utcnow() - timedelta(days=40),
        expires_at=datetime.utcnow() - timedelta(days=5),
    )
    db_session.add(sub)
    db_session.commit()
    as_admin(current_clerk_id, admin)

    new_expiry = (datetime.utcnow() + timedelta(days=30)).isoformat()
    resp = client.patch(
        f"/api/admin/subscriptions/{target.id}/expiry", json={"expires_at": new_expiry}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    db_session.refresh(sub)
    assert sub.status == "active"


def test_extend_expiry_leaves_a_cancelled_subscription_cancelled(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    plan = make_plan(db_session, "basic")
    sub = Subscription(
        clerk_user_id=target.clerk_user_id, plan_id=plan.id, status="cancelled",
        price=2450, started_at=datetime.utcnow() - timedelta(days=40),
        expires_at=datetime.utcnow() - timedelta(days=5),
    )
    db_session.add(sub)
    db_session.commit()
    as_admin(current_clerk_id, admin)

    new_expiry = (datetime.utcnow() + timedelta(days=30)).isoformat()
    resp = client.patch(
        f"/api/admin/subscriptions/{target.id}/expiry", json={"expires_at": new_expiry}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_extend_expiry_with_no_subscription_404(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    as_admin(current_clerk_id, admin)

    resp = client.patch(
        f"/api/admin/subscriptions/{target.id}/expiry",
        json={"expires_at": datetime.utcnow().isoformat()},
    )
    assert resp.status_code == 404


def test_cancel_active_subscription(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    plan = make_plan(db_session, "basic")
    sub = Subscription(
        clerk_user_id=target.clerk_user_id, plan_id=plan.id, status="active",
        price=2450, started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=20),
    )
    db_session.add(sub)
    db_session.commit()
    as_admin(current_clerk_id, admin)

    resp = client.post(f"/api/admin/subscriptions/{target.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    db_session.refresh(sub)
    assert sub.status == "cancelled"


def test_cancel_with_no_active_subscription_404(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    as_admin(current_clerk_id, admin)

    resp = client.post(f"/api/admin/subscriptions/{target.id}/cancel")
    assert resp.status_code == 404


def test_cancel_requires_admin(client, db_session, current_clerk_id):
    target = make_user(db_session, "clerk_target")
    non_admin = make_user(db_session, "clerk_regular", role=Role.USER)
    current_clerk_id["value"] = non_admin.clerk_user_id

    resp = client.post(f"/api/admin/subscriptions/{target.id}/cancel")
    assert resp.status_code == 403
