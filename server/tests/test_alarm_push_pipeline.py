"""
Tests for the alarm notification-delivery discipline layered on top of
app/tcp_server.py's TCPServer._send_push_notification: severity/mute
filtering (pre-existing, previously untested), critical/SOS bypass,
push deduplication, and the digest/escalation hand-off markers on Location.

TCPServer._send_push_notification opens its own SessionLocal() rather than
taking a Session via dependency injection, so these tests monkeypatch
app.tcp_server.SessionLocal to a sessionmaker bound to the same in-memory
engine as the shared `db_session` fixture (see conftest.py) — every session
it opens shares the same underlying SQLite connection (StaticPool), so
writes made through `db_session` are visible to it as long as `db_session`
has committed first.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import app.tcp_server as tcp_server_module
from app.tcp_server import TCPServer
from app.models.user import User
from app.models.device import Device
from app.models.alert_settings import AlertSettings
from app.models.alarm_push_state import AlarmPushState
from app.models.location import Location

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def alarm_env(db_session, monkeypatch):
    """Wires TCPServer's push path to the test DB and a recording push stub."""
    engine = db_session.get_bind()
    test_session_local = sessionmaker(bind=engine)
    monkeypatch.setattr(tcp_server_module, "SessionLocal", test_session_local)

    sent = []

    async def fake_send_push_notification(user, title, body, data, channel_id=None):
        sent.append({"user_id": user.id, "title": title, "alarm_type": data.get("alarm_type")})
        return True

    monkeypatch.setattr(tcp_server_module, "send_push_notification", fake_send_push_notification)

    server = TCPServer()
    return server, sent


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


def _make_location(db_session, device, alarm_type, timestamp=None):
    location = Location(
        device_id=device.id, latitude=-1.9, longitude=30.05,
        alarm_type=alarm_type, is_alarm=True,
        timestamp=timestamp or datetime.utcnow(),
    )
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)
    return location


async def test_severity_below_threshold_is_suppressed_and_left_for_digest(alarm_env, device_and_user, db_session):
    """Pre-existing behavior, now backstopped by a test: min_push_severity
    filters out a low-severity alarm. Newly added: it's left eligible for
    the digest job (digested_at stays NULL)."""
    server, sent = alarm_env
    device, user = device_and_user
    db_session.add(AlertSettings(device_id=device.id, min_push_severity="high"))
    db_session.commit()

    location = _make_location(db_session, device, "shock")
    await server._send_push_notification(device.id, {"alarm_type": "shock"}, location_id=location.id)

    assert sent == []
    db_session.refresh(location)
    assert location.digested_at is None


async def test_explicit_per_type_mute_is_never_digested(alarm_env, device_and_user, db_session):
    """Pre-existing behavior: an explicitly muted alarm type never pushes.
    Newly added: it's immediately marked "accounted for" so the digest job
    doesn't resurrect something the user explicitly muted."""
    server, sent = alarm_env
    device, user = device_and_user
    db_session.add(AlertSettings(device_id=device.id, vibration_push_enabled=False))
    db_session.commit()

    location = _make_location(db_session, device, "shock")
    await server._send_push_notification(device.id, {"alarm_type": "shock"}, location_id=location.id)

    assert sent == []
    db_session.refresh(location)
    assert location.digested_at is not None


async def test_severity_at_or_above_threshold_still_sends(alarm_env, device_and_user, db_session):
    """Pre-existing behavior: default settings (min_push_severity='low')
    push everything. Newly added: an immediately-sent alarm is stamped as
    accounted for too."""
    server, sent = alarm_env
    device, user = device_and_user

    location = _make_location(db_session, device, "ignition on")
    await server._send_push_notification(device.id, {"alarm_type": "ignition on"}, location_id=location.id)

    assert len(sent) == 1
    db_session.refresh(location)
    assert location.digested_at is not None


async def test_sos_bypasses_master_switch_severity_and_mute(alarm_env, device_and_user, db_session):
    """Deliberate safety decision: SOS ignores push_notifications_enabled,
    sos_push_enabled, and min_push_severity entirely."""
    server, sent = alarm_env
    device, user = device_and_user
    db_session.add(AlertSettings(
        device_id=device.id, push_notifications_enabled=False,
        sos_push_enabled=False, min_push_severity="high",
    ))
    db_session.commit()

    location = _make_location(db_session, device, "sos")
    await server._send_push_notification(device.id, {"alarm_type": "sos"}, location_id=location.id)

    assert len(sent) == 1
    assert sent[0]["alarm_type"] == "sos"


async def test_repeat_alarm_within_dedup_window_is_suppressed(alarm_env, device_and_user, db_session):
    server, sent = alarm_env
    device, user = device_and_user

    loc1 = _make_location(db_session, device, "shock")
    await server._send_push_notification(device.id, {"alarm_type": "shock"}, location_id=loc1.id)
    assert len(sent) == 1

    loc2 = _make_location(db_session, device, "shock")
    await server._send_push_notification(device.id, {"alarm_type": "shock"}, location_id=loc2.id)

    # Second push suppressed as a duplicate within the window ...
    assert len(sent) == 1
    db_session.refresh(loc2)
    # ... but still marked accounted for, so it never reaches the digest.
    assert loc2.digested_at is not None


async def test_alarm_sends_again_once_dedup_window_has_passed(alarm_env, device_and_user, db_session):
    server, sent = alarm_env
    device, user = device_and_user

    stale_state = AlarmPushState(
        device_id=device.id, alarm_type="shock",
        last_push_at=datetime.utcnow() - timedelta(minutes=tcp_server_module.PUSH_DEDUP_WINDOW_MINUTES + 1),
        last_alarm_state="fired",
    )
    db_session.add(stale_state)
    db_session.commit()

    location = _make_location(db_session, device, "shock")
    await server._send_push_notification(device.id, {"alarm_type": "shock"}, location_id=location.id)

    assert len(sent) == 1


async def test_sos_repeated_within_window_is_never_deduped(alarm_env, device_and_user, db_session):
    """Deliberate safety decision: unlike every other alarm type, a repeated
    SOS press must never be suppressed as a "duplicate" — that could hide
    that the user is still in danger."""
    server, sent = alarm_env
    device, user = device_and_user

    loc1 = _make_location(db_session, device, "sos")
    await server._send_push_notification(device.id, {"alarm_type": "sos"}, location_id=loc1.id)
    loc2 = _make_location(db_session, device, "sos")
    await server._send_push_notification(device.id, {"alarm_type": "sos"}, location_id=loc2.id)

    assert len(sent) == 2


async def test_broadcast_alarm_still_fires_ws_broadcast_unconditionally_alongside_push(
    alarm_env, device_and_user, db_session,
):
    """Regression check: broadcast_alarm() must keep sending the WS
    broadcast regardless of any push filtering/dedup/mute decision — the
    WS path is real-time/unfiltered and untouched by this whole feature."""
    server, sent = alarm_env
    device, user = device_and_user
    db_session.add(AlertSettings(device_id=device.id, vibration_push_enabled=False))
    db_session.commit()

    broadcasts = []

    class FakeWsManager:
        async def broadcast(self, device_id, payload):
            broadcasts.append((device_id, payload))

    server.ws_manager = FakeWsManager()

    location = _make_location(db_session, device, "shock")
    await server.broadcast_alarm(device.id, {"alarm_type": "shock", "timestamp": location.timestamp}, location_id=location.id)

    # WS broadcast fires regardless of the per-type mute that suppressed the push.
    assert len(broadcasts) == 1
    assert broadcasts[0][1]["alarm_type"] == "shock"
    assert sent == []
