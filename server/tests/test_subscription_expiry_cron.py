"""
Tests for scripts/cron_expiry.py's expiry-warning and expiry-marking logic.
Follows test_alarm_cron_jobs.py's pattern: cron_expiry's SessionLocal is
monkeypatched to a sessionmaker bound to the same in-memory engine as the
shared db_session fixture, and its send_push_notification/email imports are
monkeypatched to recording stubs.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import scripts.cron_expiry as cron_expiry_module
from app.models.user import User
from app.models.subscription import Subscription, SubscriptionPlan


@pytest.fixture()
def cron_env(db_session, monkeypatch):
    engine = db_session.get_bind()
    test_session_local = sessionmaker(bind=engine)
    monkeypatch.setattr(cron_expiry_module, "SessionLocal", test_session_local)

    pushes = []
    emails = []

    async def fake_send_push_notification(user, title, body, data, channel_id=None):
        pushes.append({"user_id": user.id, "title": title, "body": body, "data": data})
        return True

    async def fake_send_subscription_expiring_email(user, subscription, plan_name, days_left):
        emails.append(("expiring", user.id, plan_name, days_left))
        return True

    async def fake_send_subscription_expired_email(user, plan_name):
        emails.append(("expired", user.id, plan_name))
        return True

    async def fake_send_payment_failed_email(user, payment):
        emails.append(("payment_failed", user.id))
        return True

    monkeypatch.setattr(cron_expiry_module, "send_push_notification", fake_send_push_notification)
    monkeypatch.setattr(cron_expiry_module, "send_subscription_expiring_email", fake_send_subscription_expiring_email)
    monkeypatch.setattr(cron_expiry_module, "send_subscription_expired_email", fake_send_subscription_expired_email)
    monkeypatch.setattr(cron_expiry_module, "send_payment_failed_email", fake_send_payment_failed_email)

    return pushes, emails


@pytest.fixture()
def user(db_session):
    u = User(clerk_user_id="clerk_1", email="owner@example.com", first_name="Test", last_name="Owner")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def basic_plan(db_session):
    """Subscription.plan_id is a real FK (migrations 041/042) — every
    Subscription in this file needs an actual subscription_plans row."""
    plan = SubscriptionPlan(
        name="Basic", slug="basic", billing_type="recurrent", billing_model="prepaid",
        charge_scope="flat", price=2450, currency="RWF", duration_value=1,
        duration_unit="month", max_devices=3, is_active=True,
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


def test_notify_expiring_subscriptions_sends_once(cron_env, user, basic_plan, db_session):
    pushes, emails = cron_env

    sub = Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=basic_plan.id, status="active",
        price=2450, expires_at=datetime.utcnow() + timedelta(days=2),
    )
    db_session.add(sub)
    db_session.commit()

    cron_expiry_module.notify_expiring_subscriptions()

    assert len(pushes) == 1
    assert pushes[0]["data"]["type"] == "subscription_expiring"
    assert len([e for e in emails if e[0] == "expiring"]) == 1

    db_session.refresh(sub)
    assert sub.expiry_reminder_sent_at is not None

    # Running it again must not re-send — the reminder flag guards this.
    cron_expiry_module.notify_expiring_subscriptions()
    assert len(pushes) == 1
    assert len([e for e in emails if e[0] == "expiring"]) == 1


def test_notify_expiring_subscriptions_ignores_far_out_subscription(cron_env, user, basic_plan, db_session):
    pushes, emails = cron_env

    sub = Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=basic_plan.id, status="active",
        price=2450, expires_at=datetime.utcnow() + timedelta(days=10),
    )
    db_session.add(sub)
    db_session.commit()

    cron_expiry_module.notify_expiring_subscriptions()

    assert pushes == []
    assert emails == []


def test_notify_expiring_subscriptions_ignores_already_expired(cron_env, user, basic_plan, db_session):
    pushes, emails = cron_env

    sub = Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=basic_plan.id, status="active",
        price=2450, expires_at=datetime.utcnow() - timedelta(days=1),
    )
    db_session.add(sub)
    db_session.commit()

    cron_expiry_module.notify_expiring_subscriptions()

    assert pushes == []
    assert emails == []


def test_check_expired_subscriptions_sends_push_and_email(cron_env, user, basic_plan, db_session):
    pushes, emails = cron_env

    sub = Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=basic_plan.id, status="active",
        price=2450, expires_at=datetime.utcnow() - timedelta(days=1),
    )
    db_session.add(sub)
    db_session.commit()

    cron_expiry_module.check_expired_subscriptions()

    db_session.refresh(sub)
    assert sub.status == "expired"
    assert len(pushes) == 1
    assert pushes[0]["data"]["type"] == "subscription_expired"
    assert len([e for e in emails if e[0] == "expired"]) == 1
