"""
Regression tests for recurring (is_recurring=True) subscriptions and the
status plumbing around them.

A recurring subscription's expires_at is never advanced (nothing renews it)
and cron_expiry.py never completes it — so every reader of that date must
guard on is_recurring:

 - admin_dashboard's status/counts/expiring buckets/reminder-due flag
 - plan_add_vehicles' proration (0 would make add-vehicles unreachable:
   quote 400 "no payment needed" vs activation 402 "payment required")
 - GET /api/billing's status (must report completed/cancelled from the
   latest row, not only status=="active" ones)
 - admin assignment stays one-time unless explicitly overridden
"""

from datetime import datetime, timedelta

from app.api.admin_dashboard import (
    _client_rows,
    _effective_status,
    _reminder_summary,
)
from app.models.subscription import Subscription, SubscriptionPlan
from app.models.user import Role, User
from app.models.vehicle import Vehicle
from app.services.subscription_billing import plan_add_vehicles

NOW = datetime.utcnow()


def make_user(db, clerk_id, role=Role.USER):
    u = User(clerk_user_id=clerk_id, email=f"{clerk_id}@example.com",
             first_name="Test", last_name="User", role=role)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_plan(db, slug="basic", price=2450.0, billing_type="recurrent"):
    p = SubscriptionPlan(
        name="Basic", slug=slug, billing_type=billing_type, billing_model="prepaid",
        charge_scope="per_device", price=price, currency="RWF",
        duration_value=1, duration_unit="month", max_devices=3, is_active=True,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def make_sub(db, user, plan, *, status="active", is_recurring=True,
             started_days_ago=40, expires_days_ago=5, quantity=1):
    s = Subscription(
        clerk_user_id=user.clerk_user_id,
        plan_id=plan.id,
        status=status,
        price=plan.price,
        started_at=NOW - timedelta(days=started_days_ago),
        expires_at=NOW - timedelta(days=expires_days_ago) if expires_days_ago is not None
        else NOW + timedelta(days=30),
        is_recurring=is_recurring,
        quantity=quantity,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ── admin_dashboard status derivation ────────────────────────────────────────

def test_effective_status_recurring_past_expiry_is_active():
    sub = Subscription(clerk_user_id="c", plan_id=1, status="active", price=1.0,
                       started_at=NOW - timedelta(days=40),
                       expires_at=NOW - timedelta(days=5), is_recurring=True)
    assert _effective_status(sub, NOW) == "active"


def test_effective_status_one_time_past_expiry_is_expired():
    sub = Subscription(clerk_user_id="c", plan_id=1, status="active", price=1.0,
                       started_at=NOW - timedelta(days=40),
                       expires_at=NOW - timedelta(days=5), is_recurring=False)
    assert _effective_status(sub, NOW) == "expired"


def test_client_row_recurring_past_expiry_is_active_without_countdown(db_session):
    user = make_user(db_session, "clerk_rec")
    plan = make_plan(db_session)
    make_sub(db_session, user, plan, is_recurring=True)

    rows = {r["clerk_user_id"]: r for r in _client_rows(db_session, NOW)}
    row = rows[user.clerk_user_id]

    assert row["status"] == "active"
    assert row["days_remaining"] is None      # stale date must not count down
    assert row["expiry_bucket"] is None       # …and must not look "expiring"
    assert row["reminder_due"] is None        # cron never sends this one


def test_client_row_one_time_lapsed_still_flags_expiry(db_session):
    user = make_user(db_session, "clerk_once")
    plan = make_plan(db_session)
    make_sub(db_session, user, plan, is_recurring=False)

    rows = {r["clerk_user_id"]: r for r in _client_rows(db_session, NOW)}
    row = rows[user.clerk_user_id]

    assert row["status"] == "expired"
    assert row["reminder_due"] == "expired"


def test_reminder_summary_recurring_never_due_for_expiry():
    sub = Subscription(clerk_user_id="c", plan_id=1, status="active", price=1.0,
                       started_at=NOW - timedelta(days=40),
                       expires_at=NOW - timedelta(days=5), is_recurring=True)
    assert _reminder_summary(sub, [], NOW)["reminder_due"] is None


# ── add-vehicles proration ───────────────────────────────────────────────────

def _two_vehicles(db, clerk_id):
    return [
        Vehicle(clerk_user_id=clerk_id, nickname=f"V{i}", plate=f"RWA{i:03d}",
                make="Make", model="Model")
        for i in (1, 2)
    ]


def test_add_vehicles_recurring_past_expiry_charges_full_period(db_session):
    user = make_user(db_session, "clerk_add_rec")
    plan = make_plan(db_session)
    sub = make_sub(db_session, user, plan, is_recurring=True, quantity=1)
    db_session.add_all(_two_vehicles(db_session, user.clerk_user_id))
    db_session.commit()
    vehicles = db_session.query(Vehicle).filter(Vehicle.clerk_user_id == user.clerk_user_id).all()

    result = plan_add_vehicles(db_session, sub, {"price": 2450.0, "max_devices": None},
                               "Basic", vehicles)

    assert result.extra_slots == 1            # 2 vehicles, 1 paid slot
    assert result.unit_price == 2450.0        # a full period, not 0
    assert result.amount == 2450.0
    assert result.remaining_days > 0


def test_add_vehicles_one_time_past_expiry_still_quotes_nothing(db_session):
    user = make_user(db_session, "clerk_add_once")
    plan = make_plan(db_session)
    sub = make_sub(db_session, user, plan, is_recurring=False, quantity=1)
    db_session.add_all(_two_vehicles(db_session, user.clerk_user_id))
    db_session.commit()
    vehicles = db_session.query(Vehicle).filter(Vehicle.clerk_user_id == user.clerk_user_id).all()

    result = plan_add_vehicles(db_session, sub, {"price": 2450.0, "max_devices": None},
                               "Basic", vehicles)

    assert result.unit_price == 0.0
    assert result.amount == 0.0


# ── GET /api/billing ─────────────────────────────────────────────────────────

def test_billing_reports_completed_from_latest_subscription(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_billing_done")
    plan = make_plan(db_session)
    make_sub(db_session, user, plan, status="completed", is_recurring=False)
    current_clerk_id["value"] = user.clerk_user_id

    body = client.get("/api/billing").json()

    assert body["status"] == "completed"
    assert body["startedAt"] is not None
    # active-row-scoped fields stay empty: the app treats a non-null
    # expiresAt as "has an active plan"
    assert body["expiresAt"] is None
    assert body["currentPlan"] == "trial"


def test_billing_reports_cancelled_from_latest_subscription(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_billing_cancel")
    plan = make_plan(db_session)
    sub = make_sub(db_session, user, plan, status="cancelled", is_recurring=False)
    sub.expires_at = NOW + timedelta(days=20)
    db_session.commit()
    current_clerk_id["value"] = user.clerk_user_id

    body = client.get("/api/billing").json()

    assert body["status"] == "cancelled"
    assert body["expiresAt"] is None
    assert body["currentPlan"] == "trial"


def test_billing_active_sub_reports_active_with_expiry(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_billing_active")
    plan = make_plan(db_session)
    make_sub(db_session, user, plan, is_recurring=False, expires_days_ago=-20)
    current_clerk_id["value"] = user.clerk_user_id

    body = client.get("/api/billing").json()

    assert body["status"] == "active"
    assert body["expiresAt"] is not None
    assert body["currentPlan"] == "basic"


# ── admin assignment recurrence default ──────────────────────────────────────

def test_admin_assign_defaults_to_one_time(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin_sub", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target_sub")
    make_plan(db_session)
    current_clerk_id["value"] = admin.clerk_user_id

    resp = client.put(f"/api/admin/subscriptions/{target.id}", json={"plan_id": "basic"})

    assert resp.status_code == 200
    assert resp.json()["is_recurring"] is False


def test_admin_assign_explicit_recurring_override(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin_sub2", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target_sub2")
    make_plan(db_session)
    current_clerk_id["value"] = admin.clerk_user_id

    resp = client.put(
        f"/api/admin/subscriptions/{target.id}",
        json={"plan_id": "basic", "is_recurring": True},
    )

    assert resp.status_code == 200
    assert resp.json()["is_recurring"] is True
