"""
Tests for the data behind the app's billing screens: plan features on
GET /api/subscription-plans, the extra GET /api/billing fields, and
GET /api/payments/quote (the checkout summary).

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from datetime import datetime, timedelta

import pytest

from app.models.device import Device
from app.models.entitlement import PlanFeature
from app.models.subscription import Subscription
from tests.test_entitlements import catalog, full_plan  # noqa: F401
from tests.test_plan_expiry_freshness import make_plan, make_user


def subscribe(db, user, plan, *, days_left=20):
    now = datetime.utcnow()
    sub = Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=plan.id, status="active", price=plan.price,
        started_at=now - timedelta(days=10), expires_at=now + timedelta(days=days_left),
    )
    db.add(sub)
    db.commit()
    return sub


# --- GET /api/subscription-plans features ------------------------------------


def test_plan_list_includes_each_plans_features_in_catalog_order(
    client, db_session, current_clerk_id, catalog,
):
    user = make_user(db_session, "clerk_user")
    basic = full_plan(db_session, "basic")
    row = db_session.query(PlanFeature).filter_by(plan_id=basic.id, feature_key="geofences.max_zones").one()
    row.limit_value = 3
    db_session.query(PlanFeature).filter_by(plan_id=basic.id, feature_key="commands.fuel_cut").delete()
    db_session.commit()
    current_clerk_id["value"] = user.clerk_user_id

    (plan,) = client.get("/api/subscription-plans").json()

    keys = [f["key"] for f in plan["features"]]
    assert keys[0] == "tracking.live"
    assert "commands.fuel_cut" not in keys
    assert "vehicles.max" not in keys  # shown from max_devices instead
    zones = next(f for f in plan["features"] if f["key"] == "geofences.max_zones")
    assert (zones["name"], zones["kind"], zones["limit"], zones["unit"]) == ("Number of zones", "limit", 3, "zones")


def test_disabled_plan_feature_rows_are_not_listed(client, db_session, current_clerk_id, catalog):
    user = make_user(db_session, "clerk_user")
    basic = full_plan(db_session, "basic")
    db_session.query(PlanFeature).filter_by(plan_id=basic.id, feature_key="tracking.live").update({"enabled": False})
    db_session.commit()
    current_clerk_id["value"] = user.clerk_user_id

    (plan,) = client.get("/api/subscription-plans").json()
    assert "tracking.live" not in [f["key"] for f in plan["features"]]


# --- GET /api/billing -------------------------------------------------------


def test_billing_includes_plan_name_and_start(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    subscribe(db_session, user, make_plan(db_session, "basic"))
    current_clerk_id["value"] = user.clerk_user_id

    body = client.get("/api/billing").json()

    assert body["currentPlan"] == "basic"
    assert body["currentPlanName"] == "Basic"
    assert body["startedAt"] is not None


def test_billing_payments_carry_their_currency(client, db_session, current_clerk_id):
    from app.models.subscription import Payment
    user = make_user(db_session, "clerk_user")
    plan = make_plan(db_session, "basic")
    db_session.add(Payment(clerk_user_id=user.clerk_user_id, tx_ref="tx1", plan_id=plan.id,
                           amount=2450, currency="RWF", status="successful"))
    db_session.commit()
    current_clerk_id["value"] = user.clerk_user_id

    (payment,) = client.get("/api/billing").json()["payments"]
    assert (payment["amount"], payment["currency"]) == (2450, "RWF")


def test_billing_without_a_plan_has_no_name(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    current_clerk_id["value"] = user.clerk_user_id

    body = client.get("/api/billing").json()
    assert body["currentPlanName"] is None
    assert body["startedAt"] is None


# --- GET /api/payments/quote -------------------------------------------------


def quote(client, plan_id):
    resp = client.get("/api/payments/quote", params={"planId": plan_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_quote_for_a_new_customer(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "basic", price=2450)
    current_clerk_id["value"] = user.clerk_user_id

    q = quote(client, "basic")

    assert (q["planId"], q["planName"], q["amount"], q["currency"]) == ("basic", "Basic", 2450, "RWF")
    assert q["requiresPayment"] is True
    assert q["replaces"] is None and q["blockedReason"] is None
    assert q["billableVehicles"] is None
    assert q["durationDays"] == 30


def test_quote_per_vehicle_plan_multiplies_by_paired_devices(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    plan = make_plan(db_session, "fleet", price=1000, max_devices=None)
    plan.charge_scope = "per_device"
    for i in range(3):
        db_session.add(Device(imei=f"10000000000000{i}", name="Car", user_id=user.id, lifecycle="sold"))
    db_session.commit()
    current_clerk_id["value"] = user.clerk_user_id

    q = quote(client, "fleet")

    assert (q["amount"], q["unitPrice"], q["billableVehicles"], q["chargeScope"]) == (3000, 1000, 3, "per_device")


def test_quote_for_a_switch_says_what_it_replaces(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic")
    make_plan(db_session, "fleet", price=15000, max_devices=None)
    subscribe(db_session, user, basic)
    current_clerk_id["value"] = user.clerk_user_id

    q = quote(client, "fleet")

    assert q["replaces"]["planId"] == "basic"
    assert q["replaces"]["planName"] == "Basic"
    assert q["blockedReason"] is None


def test_quote_for_the_current_plan_is_blocked_before_any_payment(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic")
    subscribe(db_session, user, basic)
    current_clerk_id["value"] = user.clerk_user_id

    q = quote(client, "basic")

    assert "already on this plan" in q["blockedReason"]
    assert q["replaces"] is None


def test_quote_for_a_used_trial_is_blocked(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    trial = make_plan(db_session, "trial", price=0, max_devices=1)
    sub = subscribe(db_session, user, trial)
    sub.status = "expired"
    db_session.commit()
    current_clerk_id["value"] = user.clerk_user_id

    q = quote(client, "trial")

    assert q["requiresPayment"] is False
    assert q["amount"] == 0
    assert "trial already used" in q["blockedReason"].lower()


def test_quote_for_an_unknown_plan_is_400(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    current_clerk_id["value"] = user.clerk_user_id
    assert client.get("/api/payments/quote", params={"planId": "nope"}).status_code == 400
