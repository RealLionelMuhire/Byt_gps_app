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


# ── Staged reminders (migration 050) ─────────────────────────────────────────

from app.models.subscription import SubscriptionReminder
from app.services.expiry_reminders import due_stage


def _reminders(db_session, sub):
    return db_session.query(SubscriptionReminder).filter_by(subscription_id=sub.id).order_by(SubscriptionReminder.id).all()


def test_due_stage_picks_the_tightest_window_and_skips_day_one_notices():
    now = datetime(2026, 9, 28, 12, 0)
    month_ago = now - timedelta(days=25)
    assert due_stage("active", month_ago, now + timedelta(days=13), now) == "14d"
    assert due_stage("active", month_ago, now + timedelta(days=6), now) == "7d"
    assert due_stage("active", month_ago, now + timedelta(days=2), now) == "3d"
    assert due_stage("active", month_ago, now + timedelta(days=20), now) is None
    assert due_stage("active", month_ago, now - timedelta(days=1), now) is None
    assert due_stage("cancelled", month_ago, now + timedelta(days=2), now) is None
    # A 14-day trial that just started: no "14 days left" on day one...
    assert due_stage("active", now, now + timedelta(days=14), now) is None
    # ...but the final notice always goes out, even for a 2-day subscription.
    assert due_stage("active", now, now + timedelta(days=2), now) == "3d"


def test_staged_reminders_each_sent_once_and_logged_with_outcome(cron_env, user, basic_plan, db_session, monkeypatch):
    pushes, emails = cron_env
    sub = Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=basic_plan.id, status="active", price=2450,
        started_at=datetime.utcnow() - timedelta(days=24), expires_at=datetime.utcnow() + timedelta(days=6),
    )
    db_session.add(sub)
    db_session.commit()

    async def email_not_configured(user, subscription, plan_name, days_left):
        emails.append(("expiring", user.id, plan_name, days_left))
        return False  # e.g. EmailJS template not configured

    monkeypatch.setattr(cron_expiry_module, "send_subscription_expiring_email", email_not_configured)

    cron_expiry_module.notify_expiring_subscriptions()
    cron_expiry_module.notify_expiring_subscriptions()
    rows = _reminders(db_session, sub)
    assert [(r.stage, r.email_sent, r.push_sent) for r in rows] == [("7d", False, True)]
    assert len(pushes) == 1

    # Three days later the final notice is due too.
    sub.expires_at = datetime.utcnow() + timedelta(days=2)
    db_session.commit()
    rows_before = len(_reminders(db_session, sub))
    cron_expiry_module.notify_expiring_subscriptions()
    rows = _reminders(db_session, sub)
    assert len(rows) == rows_before + 1
    assert rows[-1].stage == "3d"
    assert rows[-1].expires_at == sub.expires_at


def test_extending_expiry_makes_reminders_due_again(client, cron_env, user, basic_plan, db_session, current_clerk_id):
    from app.models.user import Role
    pushes, _ = cron_env
    admin = User(clerk_user_id="clerk_admin", email="a@example.com", first_name="A", last_name="A", role=Role.ADMIN)
    db_session.add(admin)
    sub = Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=basic_plan.id, status="active", price=2450,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    db_session.add(sub)
    db_session.commit()
    cron_expiry_module.notify_expiring_subscriptions()
    db_session.refresh(sub)
    assert sub.expiry_reminder_sent_at is not None

    current_clerk_id["value"] = "clerk_admin"
    new_expiry = (datetime.utcnow() + timedelta(days=1, hours=12)).replace(microsecond=0)
    resp = client.patch(f"/api/admin/subscriptions/{user.id}/expiry", json={"expires_at": new_expiry.isoformat() + "Z"})
    assert resp.status_code == 200
    db_session.refresh(sub)
    assert sub.expiry_reminder_sent_at is None

    cron_expiry_module.notify_expiring_subscriptions()
    assert len(pushes) == 2
    stages = [(r.stage, r.expires_at) for r in _reminders(db_session, sub)]
    assert len(stages) == 2 and stages[0][1] != stages[1][1]


def test_expired_notice_is_logged(cron_env, user, basic_plan, db_session):
    sub = Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=basic_plan.id, status="active", price=2450,
        expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    db_session.add(sub)
    db_session.commit()
    cron_expiry_module.check_expired_subscriptions()
    cron_expiry_module.check_expired_subscriptions()  # already expired now — nothing re-sent
    rows = _reminders(db_session, sub)
    assert [(r.stage, r.email_sent, r.push_sent) for r in rows] == [("expired", True, True)]
