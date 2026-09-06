"""
Tests for the two alarm-notification cron scripts (server/scripts/), which
follow cron_expiry.py's pattern: sync top-level function, own SessionLocal(),
DB commit before any push attempt. Both scripts' SessionLocal is
monkeypatched to a sessionmaker bound to the same in-memory engine as the
shared `db_session` fixture (see conftest.py), and their
send_push_notification import is monkeypatched to a recording stub.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import scripts.cron_alarm_escalation as escalation_module
import scripts.cron_alarm_digest as digest_module
from app.models.user import User
from app.models.device import Device
from app.models.location import Location


@pytest.fixture()
def cron_env(db_session, monkeypatch):
    engine = db_session.get_bind()
    test_session_local = sessionmaker(bind=engine)
    monkeypatch.setattr(escalation_module, "SessionLocal", test_session_local)
    monkeypatch.setattr(digest_module, "SessionLocal", test_session_local)

    sent = []

    async def fake_send_push_notification(user, title, body, data, channel_id=None):
        sent.append({"user_id": user.id, "title": title, "body": body, "data": data})
        return True

    monkeypatch.setattr(escalation_module, "send_push_notification", fake_send_push_notification)
    monkeypatch.setattr(digest_module, "send_push_notification", fake_send_push_notification)

    return sent


@pytest.fixture()
def device_and_user(db_session):
    user = User(
        clerk_user_id="clerk_1", email="owner@example.com",
        first_name="Test", last_name="Owner", expo_push_token="ExponentPushToken[test]",
    )
    db_session.add(user)
    db_session.commit()

    device = Device(imei="123456789012345", name="Toyota Hilux", user_id=user.id, lifecycle="sold")
    db_session.add(device)
    db_session.commit()

    return device, user


def test_escalation_resends_unacknowledged_critical_alarm_past_window(cron_env, device_and_user, db_session):
    sent = cron_env
    device, user = device_and_user

    stale_sos = Location(
        device_id=device.id, latitude=-1.9, longitude=30.05,
        alarm_type="sos", is_alarm=True,
        timestamp=datetime.utcnow() - timedelta(minutes=escalation_module.ESCALATION_WINDOW_MINUTES + 1),
    )
    db_session.add(stale_sos)
    db_session.commit()

    escalation_module.escalate_unacknowledged_critical_alarms()

    assert len(sent) == 1
    db_session.refresh(stale_sos)
    assert stale_sos.escalated_at is not None


def test_escalation_skips_acknowledged_alarm(cron_env, device_and_user, db_session):
    sent = cron_env
    device, user = device_and_user

    acked_sos = Location(
        device_id=device.id, latitude=-1.9, longitude=30.05,
        alarm_type="sos", is_alarm=True,
        timestamp=datetime.utcnow() - timedelta(minutes=escalation_module.ESCALATION_WINDOW_MINUTES + 1),
        acknowledged_at=datetime.utcnow(),
    )
    db_session.add(acked_sos)
    db_session.commit()

    escalation_module.escalate_unacknowledged_critical_alarms()

    assert sent == []


def test_escalation_skips_alarm_still_within_window(cron_env, device_and_user, db_session):
    sent = cron_env
    device, user = device_and_user

    fresh_sos = Location(
        device_id=device.id, latitude=-1.9, longitude=30.05,
        alarm_type="sos", is_alarm=True, timestamp=datetime.utcnow(),
    )
    db_session.add(fresh_sos)
    db_session.commit()

    escalation_module.escalate_unacknowledged_critical_alarms()

    assert sent == []


def test_escalation_never_resends_twice(cron_env, device_and_user, db_session):
    sent = cron_env
    device, user = device_and_user

    already_escalated = Location(
        device_id=device.id, latitude=-1.9, longitude=30.05,
        alarm_type="sos", is_alarm=True,
        timestamp=datetime.utcnow() - timedelta(minutes=escalation_module.ESCALATION_WINDOW_MINUTES + 1),
        escalated_at=datetime.utcnow(),
    )
    db_session.add(already_escalated)
    db_session.commit()

    escalation_module.escalate_unacknowledged_critical_alarms()

    assert sent == []


def test_escalation_ignores_non_critical_unacknowledged_alarms(cron_env, device_and_user, db_session):
    sent = cron_env
    device, user = device_and_user

    stale_vibration = Location(
        device_id=device.id, latitude=-1.9, longitude=30.05,
        alarm_type="vibration", is_alarm=True,
        timestamp=datetime.utcnow() - timedelta(minutes=escalation_module.ESCALATION_WINDOW_MINUTES + 1),
    )
    db_session.add(stale_vibration)
    db_session.commit()

    escalation_module.escalate_unacknowledged_critical_alarms()

    assert sent == []


def test_digest_groups_pending_alarms_per_user_and_marks_them_digested(cron_env, device_and_user, db_session):
    sent = cron_env
    device, user = device_and_user

    loc1 = Location(device_id=device.id, latitude=-1.9, longitude=30.05, alarm_type="vibration", is_alarm=True, timestamp=datetime.utcnow())
    loc2 = Location(device_id=device.id, latitude=-1.9, longitude=30.05, alarm_type="vibration", is_alarm=True, timestamp=datetime.utcnow())
    loc3 = Location(device_id=device.id, latitude=-1.9, longitude=30.05, alarm_type="low_battery", is_alarm=True, timestamp=datetime.utcnow())
    db_session.add_all([loc1, loc2, loc3])
    db_session.commit()

    digest_module.send_alarm_digests()

    assert len(sent) == 1
    assert sent[0]["user_id"] == user.id
    for loc in (loc1, loc2, loc3):
        db_session.refresh(loc)
        assert loc.digested_at is not None


def test_digest_skips_alarms_already_accounted_for(cron_env, device_and_user, db_session):
    sent = cron_env
    device, user = device_and_user

    already_handled = Location(
        device_id=device.id, latitude=-1.9, longitude=30.05,
        alarm_type="vibration", is_alarm=True, timestamp=datetime.utcnow(),
        digested_at=datetime.utcnow(),
    )
    db_session.add(already_handled)
    db_session.commit()

    digest_module.send_alarm_digests()

    assert sent == []
