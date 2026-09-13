"""
Tests for POST /api/webhooks/intouchpay's email side-effects: a receipt email
on a payment reconciled successful, a failure email on one reconciled
failed. get_transaction_status/classify_status (the actual IntouchPay call)
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
from app.models.subscription import Payment


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

    async def fake_receipt(user, payment, plan_name):
        sent.append(("receipt", user.id, payment.tx_ref, plan_name))
        return True

    async def fake_failed(user, payment):
        sent.append(("failed", user.id, payment.tx_ref))
        return True

    monkeypatch.setattr(webhooks, "send_payment_receipt_email", fake_receipt)
    monkeypatch.setattr(webhooks, "send_payment_failed_email", fake_failed)
    return sent


@pytest.fixture()
def user_and_payment(db_session):
    user = User(clerk_user_id="clerk_1", email="owner@example.com", first_name="Test", last_name="Owner")
    db_session.add(user)
    db_session.commit()

    payment = Payment(clerk_user_id="clerk_1", tx_ref="IPabc123", plan_id="basic", amount=2450, currency="RWF", status="pending")
    db_session.add(payment)
    db_session.commit()
    return user, payment


def test_webhook_sends_receipt_email_on_success(webhook_client, emails_sent, user_and_payment, monkeypatch):
    user, payment = user_and_payment

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "successful"}

    monkeypatch.setattr(webhooks, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(webhooks, "classify_status", lambda resp: "successful")

    resp = webhook_client.post("/api/webhooks/intouchpay", json={"requesttransactionid": "IPabc123", "status": "successful"})

    assert resp.status_code == 200
    assert emails_sent == [("receipt", user.id, "IPabc123", "Basic")]


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
