"""
Tests for POST /api/webhooks/intouchpay's email side-effects: a failure
email on a payment reconciled failed, and NO email on success — the receipt
is sent at activation instead (see tests/test_receipt_email.py), once the
vehicles and period are final. get_transaction_status/classify_status (the actual IntouchPay call)
and the email service are both monkeypatched — this only tests that the
right email is triggered for the right outcome, not real IntouchPay/EmailJS
delivery.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.api import webhooks
from app.models.user import User
from app.models.subscription import Payment, SubscriptionPlan
from app.models.disbursement import Disbursement


@pytest.fixture()
def webhook_client(db_session, monkeypatch):
    app = FastAPI()
    app.include_router(webhooks.router, prefix="/api/webhooks")

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def emails_sent(monkeypatch):
    sent = []

    async def fake_failed(user, payment):
        sent.append(("failed", user.id, payment.tx_ref))
        return True

    monkeypatch.setattr(webhooks, "send_payment_failed_email", fake_failed)
    return sent


@pytest.fixture()
def user_and_payment(db_session):
    user = User(clerk_user_id="clerk_1", email="owner@example.com", first_name="Test", last_name="Owner")
    db_session.add(user)
    db_session.commit()

    # Payment.plan_id is a real FK (migrations 041/042) — needs an actual
    # subscription_plans row, not a bare slug string.
    plan = SubscriptionPlan(
        name="Basic", slug="basic", billing_type="recurrent", billing_model="prepaid",
        charge_scope="flat", price=2450, currency="RWF", duration_value=1,
        duration_unit="month", max_devices=3, is_active=True,
    )
    db_session.add(plan)
    db_session.commit()

    payment = Payment(clerk_user_id="clerk_1", tx_ref="IPabc123", plan_id=plan.id, amount=2450, currency="RWF", status="pending")
    db_session.add(payment)
    db_session.commit()
    return user, payment


def test_webhook_sends_no_email_on_success(webhook_client, emails_sent, user_and_payment, monkeypatch):
    user, payment = user_and_payment

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "successful"}

    monkeypatch.setattr(webhooks, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(webhooks, "classify_status", lambda resp: "successful")

    resp = webhook_client.post("/api/webhooks/intouchpay", json={"requesttransactionid": "IPabc123", "status": "successful"})

    assert resp.status_code == 200
    assert emails_sent == []  # the receipt goes out at activation


def test_webhook_sends_failed_email_on_confirmed_failure(webhook_client, emails_sent, user_and_payment, monkeypatch):
    user, payment = user_and_payment

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "failed"}

    monkeypatch.setattr(webhooks, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(webhooks, "classify_status", lambda resp: "unknown")

    resp = webhook_client.post("/api/webhooks/intouchpay", json={"requesttransactionid": "IPabc123", "status": "failed"})

    assert resp.status_code == 200
    assert emails_sent == [("failed", user.id, "IPabc123")]


def test_webhook_no_email_when_still_pending(webhook_client, emails_sent, user_and_payment, monkeypatch):
    user, payment = user_and_payment

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "pending"}

    monkeypatch.setattr(webhooks, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(webhooks, "classify_status", lambda resp: "unknown")

    resp = webhook_client.post("/api/webhooks/intouchpay", json={"requesttransactionid": "IPabc123", "status": "pending"})

    assert resp.status_code == 200
    assert emails_sent == []


# ── disbursement (B2C deposit) callback ─────────────────────────────────────

@pytest.fixture()
def pending_disbursement(db_session):
    disbursement = Disbursement(
        clerk_user_id="clerk_1", phone="250781234567", tx_ref="IDdeposit123",
        amount=1000, currency="RWF", reason="Refund", status="pending",
        initiated_by_clerk_user_id="clerk_admin",
    )
    db_session.add(disbursement)
    db_session.commit()
    return disbursement


def test_webhook_marks_disbursement_successful(webhook_client, pending_disbursement, db_session, monkeypatch):
    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "successful", "responsecode": "2001"}

    monkeypatch.setattr(webhooks, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(webhooks, "classify_status", lambda resp: "successful")

    resp = webhook_client.post(
        "/api/webhooks/intouchpay",
        json={"requesttransactionid": "IDdeposit123", "status": "successful", "transactionid": "PROV-99"},
    )

    assert resp.status_code == 200
    db_session.refresh(pending_disbursement)
    assert pending_disbursement.status == "successful"
    assert pending_disbursement.verified_at is not None


def test_webhook_marks_disbursement_failed_on_confirmed_failure(webhook_client, pending_disbursement, db_session, monkeypatch):
    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "failed"}

    monkeypatch.setattr(webhooks, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(webhooks, "classify_status", lambda resp: "unknown")

    resp = webhook_client.post(
        "/api/webhooks/intouchpay", json={"requesttransactionid": "IDdeposit123", "status": "failed"},
    )

    assert resp.status_code == 200
    db_session.refresh(pending_disbursement)
    assert pending_disbursement.status == "failed"


def test_webhook_leaves_disbursement_pending_when_unresolved(webhook_client, pending_disbursement, db_session, monkeypatch):
    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "pending"}

    monkeypatch.setattr(webhooks, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(webhooks, "classify_status", lambda resp: "unknown")

    resp = webhook_client.post(
        "/api/webhooks/intouchpay", json={"requesttransactionid": "IDdeposit123", "status": "pending"},
    )

    assert resp.status_code == 200
    db_session.refresh(pending_disbursement)
    assert pending_disbursement.status == "pending"


def test_webhook_ignores_already_resolved_disbursement(webhook_client, pending_disbursement, db_session, monkeypatch):
    pending_disbursement.status = "successful"
    db_session.commit()

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        raise AssertionError("should not re-check an already-resolved disbursement")

    monkeypatch.setattr(webhooks, "get_transaction_status", fake_get_transaction_status)

    resp = webhook_client.post(
        "/api/webhooks/intouchpay", json={"requesttransactionid": "IDdeposit123", "status": "successful"},
    )

    assert resp.status_code == 200


def test_webhook_unknown_tx_ref_acked_without_error(webhook_client, monkeypatch):
    resp = webhook_client.post(
        "/api/webhooks/intouchpay", json={"requesttransactionid": "no-such-tx-ref", "status": "successful"},
    )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
