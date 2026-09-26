"""
Tests for the customer's receipt email (app/services/email.py's
receipt_details, queued by app/api/onboarding.py's _email_receipt): one
complete receipt per PAID activation — plan, vehicles, coverage period,
amount, reference — with dates in the customers' time zone (UTC+2).

send_email is replaced with a recorder; FastAPI's TestClient runs
background tasks before returning, so a queued receipt shows up at once.
"""

from datetime import datetime

import pytest

import app.api.onboarding as onboarding_module
from app.models.subscription import Payment
from app.services.email import receipt_details
from tests.test_plan_expiry_freshness import make_plan, make_user
from tests.test_vehicle_subscriptions import active_sub, intouch, make_vehicles, succeed  # noqa: F401


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send_email(to_email, to_name, template_id, template_params):
        calls.append({"to": to_email, "params": template_params})
        return True

    monkeypatch.setattr(onboarding_module, "send_email", fake_send_email)
    return calls


def test_a_paid_activation_emails_one_complete_receipt(client, db_session, current_clerk_id, intouch, sent):
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "basic", price=14090, max_devices=None)
    v = make_vehicles(db_session, user, 2)
    current_clerk_id["value"] = user.clerk_user_id
    tx = client.post("/api/payments/initiate", json={
        "planId": "basic", "phone": "0788000000", "vehicleIds": [v[0].id, v[1].id],
    }).json()["txRef"]
    succeed(db_session, tx)

    assert client.post("/api/subscriptions", json={"planId": "basic"}).status_code == 201

    (email,) = sent
    params = email["params"]
    assert email["to"] == "clerk_user@example.com"
    assert params["plan_name"] == "Basic"
    assert params["vehicles"] == "RA000 (Car 0), RA001 (Car 1)"
    assert params["amount_paid"] == "28,180 RWF"
    assert params["tx_ref"] == tx
    assert " – " in params["coverage"]
    for line in ("Vehicles: RA000", "Coverage:", "Amount paid: 28,180 RWF", f"Payment reference: {tx}"):
        assert line in params["message"]


def test_a_trial_sends_no_receipt(client, db_session, current_clerk_id, sent):
    user = make_user(db_session, "clerk_user")
    make_plan(db_session, "trial", price=0, max_devices=1)
    make_vehicles(db_session, user, 1)
    current_clerk_id["value"] = user.clerk_user_id

    assert client.post("/api/subscriptions", json={"planId": "trial"}).status_code == 201
    assert sent == []


def test_paid_extra_vehicles_email_an_add_receipt_free_slots_do_not(
    client, db_session, current_clerk_id, intouch, sent,
):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=3000, max_devices=None)
    v = make_vehicles(db_session, user, 3)
    active_sub(db_session, user, basic, quantity=2, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id

    # v[1] takes the free slot — no payment, no receipt.
    assert client.post("/api/subscriptions/vehicles", json={"vehicleIds": [v[1].id]}).status_code == 200
    assert sent == []

    tx = client.post("/api/payments/initiate", json={
        "planId": "basic", "phone": "0788000000", "purpose": "add_vehicles", "vehicleIds": [v[2].id],
    }).json()["txRef"]
    succeed(db_session, tx)
    assert client.post("/api/subscriptions/vehicles", json={"vehicleIds": [v[2].id], "txRef": tx}).status_code == 200

    (email,) = sent
    assert email["params"]["purchase"] == "Vehicles added to plan"
    assert email["params"]["vehicles"] == "RA002 (Car 2)"
    assert email["params"]["coverage"].startswith("Added to your plan until ")


def test_retrying_a_completed_upgrade_does_not_email_twice(client, db_session, current_clerk_id, intouch, sent):
    user = make_user(db_session, "clerk_user")
    basic = make_plan(db_session, "basic", price=3000, max_devices=None)
    make_plan(db_session, "annual", price=30000, max_devices=None)
    v = make_vehicles(db_session, user, 1)
    active_sub(db_session, user, basic, quantity=1, covered=[v[0]])
    current_clerk_id["value"] = user.clerk_user_id
    tx = client.post("/api/payments/initiate", json={
        "planId": "annual", "phone": "0788000000", "vehicleIds": [v[0].id],
    }).json()["txRef"]
    succeed(db_session, tx)
    body = {"planId": "annual", "txRef": tx}

    assert client.post("/api/subscriptions/upgrade", json=body).status_code == 201
    assert client.post("/api/subscriptions/upgrade", json=body).status_code == 201
    assert len(sent) == 1


def test_receipt_dates_are_in_rwanda_time():
    # 23:30 UTC on Sep 25 is already Sep 26 in Kigali (UTC+2).
    class V:
        plate, nickname = "RI 183 F", "sukhoi"

    payment = Payment(tx_ref="IP1", amount=14090, currency="RWF", verified_at=datetime(2026, 9, 25, 23, 30))
    params = receipt_details(
        plan_name="6 Month Plan", payment=payment, vehicles=[V()],
        period_start=datetime(2026, 9, 25, 23, 30), period_end=datetime(2027, 3, 24, 23, 30),
        added_vehicles=False,
    )

    assert params["paid_on"] == "Sep 26, 2026"
    assert params["coverage"] == "Sep 26, 2026 – Mar 25, 2027"
    assert params["vehicles"] == "RI 183 F (sukhoi)"
