"""
Tests for per-vehicle subscriptions (migration 048,
app/services/subscription_billing.py): choosing which vehicles a
subscription covers, per-vehicle pricing, free slots, prorated extra
slots, and the vehicle_not_covered entitlement check.

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

import math
from datetime import datetime, timedelta

import pytest

import app.api.onboarding as onboarding_module
from app.core.config import settings
from app.models.device import Device
from app.models.subscription import Payment, Subscription, SubscriptionVehicle
from app.models.user import Role
from app.models.vehicle import Vehicle
from tests.test_entitlements import catalog, full_plan  # noqa: F401
from tests.test_plan_expiry_freshness import make_plan, make_user


@pytest.fixture
def intouch(monkeypatch):
    calls = []

    async def fake_request_payment(amount, phone, transaction_id):
        calls.append({"amount": amount, "phone": phone, "tx_ref": transaction_id})
        return {"success": True, "responsecode": "1000", "message": "ok"}

    monkeypatch.setattr(onboarding_module, "intouch_request_payment", fake_request_payment)
    return calls


def make_vehicles(db, user, n, with_devices=False):
    vehicles = []
    for i in range(n):
        device_id = None
        if with_devices:
            d = Device(imei=f"35000000000{user.id:02d}{i:02d}", name=f"D{i}", user_id=user.id, lifecycle="sold")
            db.add(d)
            db.flush()
            device_id = d.id
        v = Vehicle(clerk_user_id=user.clerk_user_id, device_id=device_id, nickname=f"Car {i}",
                    plate=f"RA{i:03d}", make="Toyota", model="Hilux")
        db.add(v)
        vehicles.append(v)
    db.commit()
    return vehicles


def active_sub(db, user, plan, *, quantity, covered, started_days_ago=10, period_days=30):
    now = datetime.utcnow()
    sub = Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=plan.id, status="active", price=plan.price,
        started_at=now - timedelta(days=started_days_ago),
        expires_at=now - timedelta(days=started_days_ago) + timedelta(days=period_days),
        quantity=quantity,
    )
    db.add(sub)
    db.flush()
    for v in covered:
        db.add(SubscriptionVehicle(subscription_id=sub.id, vehicle_id=v.id))
    db.commit()
    return sub


def covered(db, sub):
    return {sv.vehicle_id for sv in db.query(SubscriptionVehicle).filter_by(subscription_id=sub.id)}


def succeed(db, tx_ref):
    p = db.query(Payment).filter_by(tx_ref=tx_ref).one()
    p.status = "successful"
    db.commit()
    return p


# --- Paying for chosen vehicles ----------------------------------------------


def test_payment_is_price_times_chosen_vehicles_and_records_them(client, db_session, current_clerk_id, intouch):
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "basic", price=2000, max_devices=None)
    v = make_vehicles(db_session, user, 3)
    current_clerk_id["value"] = user.clerk_user_id

    resp = client.post("/api/payments/initiate", json={
        "planId": "basic", "phone": "0788000000", "vehicleIds": [v[0].id, v[2].id],
    })

    assert resp.status_code == 200, resp.text
    assert intouch[0]["amount"] == 4000
    payment = db_session.query(Payment).one()
    assert (payment.purpose, sorted(payment.vehicle_ids)) == ("subscribe", sorted([v[0].id, v[2].id]))


def test_activation_covers_exactly_the_paid_vehicles(client, db_session, current_clerk_id, intouch):
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "basic", price=2000, max_devices=None)
    v = make_vehicles(db_session, user, 3)
    current_clerk_id["value"] = user.clerk_user_id
    tx = client.post("/api/payments/initiate", json={
        "planId": "basic", "phone": "0788000000", "vehicleIds": [v[1].id],
    }).json()["txRef"]
    succeed(db_session, tx)

    # Even if the client asks for more vehicles, only the paid one is covered.
    resp = client.post("/api/subscriptions", json={"planId": "basic", "vehicleIds": [v[0].id, v[1].id, v[2].id]})

    assert resp.status_code == 201, resp.text
    sub = db_session.get(Subscription, resp.json()["subscriptionId"])
    assert covered(db_session, sub) == {v[1].id}
    assert (sub.quantity, sub.price) == (1, 2000)


def test_older_app_without_a_selection_pays_for_every_vehicle(client, db_session, current_clerk_id, intouch):
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "basic", price=2000, max_devices=None)
    make_vehicles(db_session, user, 2)
    current_clerk_id["value"] = user.clerk_user_id

    client.post("/api/payments/initiate", json={"planId": "basic", "phone": "0788000000"})

    assert intouch[0]["amount"] == 4000
    assert len(db_session.query(Payment).one().vehicle_ids) == 2


@pytest.mark.parametrize("pick, message", [
    ("other", "aren't on your account"),
    ("none", "at least one vehicle"),
    ("too_many", "covers up to 2 vehicles"),
])
def test_invalid_selections_are_refused_before_charging(client, db_session, current_clerk_id, intouch, pick, message):
    user = make_user(db_session, "clerk_user")
    other = make_user(db_session, "clerk_other")
    make_plan(db_session, "basic", price=2000, max_devices=2)
    mine = make_vehicles(db_session, user, 3)
    theirs = make_vehicles(db_session, other, 1)
    current_clerk_id["value"] = user.clerk_user_id
    ids = {"other": [theirs[0].id], "none": [], "too_many": [v.id for v in mine]}[pick]

    resp = client.post("/api/payments/initiate", json={"planId": "basic", "phone": "0788000000", "vehicleIds": ids})

    assert resp.status_code == 400
    assert message in resp.json()["detail"]
    assert intouch == []


def test_trial_covers_the_chosen_vehicle(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "trial", price=0, max_devices=1)
    v = make_vehicles(db_session, user, 2)
    current_clerk_id["value"] = user.clerk_user_id

    resp = client.post("/api/subscriptions", json={"planId": "trial", "vehicleIds": [v[1].id]})

    assert resp.status_code == 201, resp.text
    assert covered(db_session, db_session.get(Subscription, resp.json()["subscriptionId"])) == {v[1].id}


def test_trial_from_an_older_app_covers_up_to_its_cap(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "trial", price=0, max_devices=1)
    v = make_vehicles(db_session, user, 2)
    current_clerk_id["value"] = user.clerk_user_id

    resp = client.post("/api/subscriptions", json={"planId": "trial"})

    assert resp.status_code == 201
    assert covered(db_session, db_session.get(Subscription, resp.json()["subscriptionId"])) == {v[0].id}


def test_switching_plans_covers_the_new_payments_vehicles(client, db_session, current_clerk_id, intouch):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=2000, max_devices=None)
    make_plan(db_session, "annual", price=20000, max_devices=None)
    v = make_vehicles(db_session, user, 3)
    active_sub(db_session, user, basic, quantity=1, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id
    tx = client.post("/api/payments/initiate", json={
        "planId": "annual", "phone": "0788000000", "vehicleIds": [v[0].id, v[1].id],
    }).json()["txRef"]
    succeed(db_session, tx)

    resp = client.post("/api/subscriptions/upgrade", json={"planId": "annual", "txRef": tx})

    assert resp.status_code == 201, resp.text
    new_sub = db_session.get(Subscription, resp.json()["subscriptionId"])
    assert covered(db_session, new_sub) == {v[0].id, v[1].id}
    assert (new_sub.quantity, new_sub.price) == (2, 40000)


# --- Adding vehicles to the current subscription -----------------------------


def test_a_free_slot_is_used_without_payment(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=3000, max_devices=None)
    v = make_vehicles(db_session, user, 2)
    sub = active_sub(db_session, user, basic, quantity=2, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id

    q = client.get("/api/payments/quote", params={
        "planId": "basic", "purpose": "add_vehicles", "vehicleIds": str(v[1].id),
    }).json()
    assert (q["amount"], q["requiresPayment"]) == (0, False)

    resp = client.post("/api/subscriptions/vehicles", json={"vehicleIds": [v[1].id]})

    assert resp.status_code == 200, resp.text
    assert covered(db_session, sub) == {v[0].id, v[1].id}
    assert resp.json()["quantity"] == 2


def test_extra_slots_are_prorated_to_the_renewal_date(client, db_session, current_clerk_id, intouch):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=3000, max_devices=None)
    v = make_vehicles(db_session, user, 3)
    # 30-day period, 10 days used → 20 of 30 days left → 2,000 per extra slot.
    sub = active_sub(db_session, user, basic, quantity=1, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id

    q = client.get("/api/payments/quote", params={
        "planId": "basic", "purpose": "add_vehicles", "vehicleIds": f"{v[1].id},{v[2].id}",
    }).json()
    unit = math.ceil(3000 * 20 / 30)
    assert q["amount"] == pytest.approx(2 * unit, abs=2)
    assert q["billableVehicles"] == 2
    assert "prorated" in q["lineItems"][-1]["description"]
    assert q["expiresAt"].startswith(sub.expires_at.isoformat()[:16])

    # Without paying, the extra vehicles can't be added.
    assert client.post("/api/subscriptions/vehicles", json={"vehicleIds": [v[1].id, v[2].id]}).status_code == 402

    tx = client.post("/api/payments/initiate", json={
        "planId": "basic", "phone": "0788000000", "purpose": "add_vehicles",
        "vehicleIds": [v[1].id, v[2].id],
    }).json()["txRef"]
    assert intouch[0]["amount"] == pytest.approx(q["amount"], abs=2)
    succeed(db_session, tx)

    resp = client.post("/api/subscriptions/vehicles", json={"vehicleIds": [v[1].id, v[2].id], "txRef": tx})
    assert resp.status_code == 200, resp.text
    assert resp.json()["quantity"] == 3
    assert covered(db_session, sub) == {v[0].id, v[1].id, v[2].id}

    # Retrying the same completed add returns the same result.
    retry = client.post("/api/subscriptions/vehicles", json={"vehicleIds": [v[1].id, v[2].id], "txRef": tx})
    assert (retry.status_code, retry.json()["quantity"]) == (200, 3)


def test_an_add_payment_cannot_cover_different_vehicles(client, db_session, current_clerk_id, intouch):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=3000, max_devices=None)
    v = make_vehicles(db_session, user, 3)
    active_sub(db_session, user, basic, quantity=1, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id
    tx = client.post("/api/payments/initiate", json={
        "planId": "basic", "phone": "0788000000", "purpose": "add_vehicles", "vehicleIds": [v[1].id],
    }).json()["txRef"]
    succeed(db_session, tx)

    resp = client.post("/api/subscriptions/vehicles", json={"vehicleIds": [v[2].id], "txRef": tx})
    assert resp.status_code == 409


def test_an_add_payment_cannot_fund_a_new_subscription(client, db_session, current_clerk_id, intouch):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=3000, max_devices=None)
    make_plan(db_session, "annual", price=30000, max_devices=None)
    v = make_vehicles(db_session, user, 2)
    active_sub(db_session, user, basic, quantity=1, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id
    tx = client.post("/api/payments/initiate", json={
        "planId": "basic", "phone": "0788000000", "purpose": "add_vehicles", "vehicleIds": [v[1].id],
    }).json()["txRef"]
    succeed(db_session, tx)

    assert client.post("/api/subscriptions/upgrade", json={"planId": "basic", "txRef": tx}).status_code in (400, 409)


def test_adding_an_already_covered_vehicle_is_refused(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=3000, max_devices=None)
    v = make_vehicles(db_session, user, 1)
    active_sub(db_session, user, basic, quantity=2, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id

    resp = client.post("/api/subscriptions/vehicles", json={"vehicleIds": [v[0].id]})
    assert resp.status_code == 400
    assert "Already on your plan" in resp.json()["detail"]


def test_vehicles_can_be_added_to_a_since_deactivated_current_plan(client, db_session, current_clerk_id, intouch):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=3000, max_devices=None)
    basic.is_active = False
    v = make_vehicles(db_session, user, 2)
    active_sub(db_session, user, basic, quantity=1, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id

    resp = client.post("/api/payments/initiate", json={
        "planId": "basic", "phone": "0788000000", "purpose": "add_vehicles", "vehicleIds": [v[1].id],
    })
    assert resp.status_code == 200, resp.text


# --- What the app reads --------------------------------------------------------


def test_billing_lists_covered_vehicles_and_slots(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=3000, max_devices=None)
    v = make_vehicles(db_session, user, 2)
    active_sub(db_session, user, basic, quantity=2, covered=[v[1]])
    current_clerk_id["value"] = user.clerk_user_id

    body = client.get("/api/billing").json()

    assert body["quantity"] == 2
    assert [c["id"] for c in body["coveredVehicles"]] == [v[1].id]
    assert body["coveredVehicles"][0]["plate"] == "RA001"


def test_plan_list_includes_the_monthly_price_per_vehicle(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    plan = make_plan(db_session, "half_year", price=14090, max_devices=None)
    plan.duration_value, plan.duration_unit = 6, "month"
    db_session.commit()
    current_clerk_id["value"] = user.clerk_user_id

    (listed,) = client.get("/api/subscription-plans").json()
    assert listed["monthly_price"] == pytest.approx(14090 * 30 / 180, abs=0.01)


def test_quote_reports_a_selection_problem_as_blocked(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "basic", price=3000, max_devices=1)
    v = make_vehicles(db_session, user, 2)
    current_clerk_id["value"] = user.clerk_user_id

    q = client.get("/api/payments/quote", params={"planId": "basic", "vehicleIds": f"{v[0].id},{v[1].id}"}).json()
    assert "covers up to 1 vehicle" in q["blockedReason"]


# --- vehicle_not_covered -----------------------------------------------------


def test_uncovered_vehicles_premium_features_are_refused_in_enforce_mode(
    client, db_session, current_clerk_id, catalog, monkeypatch,
):
    monkeypatch.setattr(settings, "ENTITLEMENT_MODE", "enforce")
    user = make_user(db_session, "clerk_user")
    plan = full_plan(db_session, "basic")
    v = make_vehicles(db_session, user, 2, with_devices=True)
    active_sub(db_session, user, plan, quantity=1, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get(f"/api/locations/{v[0].device_id}/alarms").status_code == 200
    resp = client.get(f"/api/locations/{v[1].device_id}/alarms")
    assert resp.status_code == 402
    assert resp.json()["detail"]["reason"] == "vehicle_not_covered"

    ent = client.get("/api/me/entitlements").json()
    assert ent["covered_vehicle_ids"] == [v[0].id]


def test_uncovered_vehicle_is_only_logged_in_log_mode(client, db_session, current_clerk_id, catalog, monkeypatch):
    from app.models.entitlement import EntitlementCheckLog
    monkeypatch.setattr(settings, "ENTITLEMENT_MODE", "log")
    user = make_user(db_session, "clerk_user")
    plan = full_plan(db_session, "basic")
    v = make_vehicles(db_session, user, 2, with_devices=True)
    active_sub(db_session, user, plan, quantity=1, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get(f"/api/locations/{v[1].device_id}/alarms").status_code == 200
    (row,) = db_session.query(EntitlementCheckLog).all()
    assert row.reason == "vehicle_not_covered"


def test_admin_assignment_covers_every_vehicle(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "basic", price=3000, max_devices=None)
    v = make_vehicles(db_session, user, 3)
    current_clerk_id["value"] = admin.clerk_user_id

    resp = client.put(f"/api/admin/subscriptions/{user.id}", json={"plan_id": "basic"})

    assert resp.status_code == 200, resp.text
    sub = db_session.query(Subscription).filter_by(clerk_user_id=user.clerk_user_id, status="active").one()
    assert covered(db_session, sub) == {x.id for x in v}
    assert sub.quantity == 3
