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
from app.tcp_server import TCPServer, _broadcast_geofence_transitions
from app.models.user import User
from app.models.device import Device
from app.models.alert_settings import AlertSettings
from app.models.alarm_push_state import AlarmPushState
from app.models.location import Location
from app.models.geofence import Geofence
from app.services.geofencing import GeofenceTransition

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
        sent.append({"user_id": user.id, "title": title, "body": body, "alarm_type": data.get("alarm_type")})
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


async def test_geofence_push_body_includes_geofence_name(alarm_env, device_and_user, db_session):
    """Enter/Exit fence pushes should name the actual geofence, not just
    say "Enter fence" — see _apply_geofence_transitions /
    _broadcast_geofence_transitions in app/tcp_server.py, which stamp
    geofence_name onto the alarm data dict passed through to this method."""
    server, sent = alarm_env
    device, user = device_and_user

    location = _make_location(db_session, device, "enter fence")
    await server._send_push_notification(
        device.id, {"alarm_type": "enter fence", "geofence_name": "Home Base"}, location_id=location.id,
    )

    assert len(sent) == 1
    assert sent[0]["body"] == "Toyota Hilux • Entered geofence: Home Base"


async def test_exit_fence_push_body_says_exited(alarm_env, device_and_user, db_session):
    server, sent = alarm_env
    device, user = device_and_user

    location = _make_location(db_session, device, "exit fence")
    await server._send_push_notification(
        device.id, {"alarm_type": "exit fence", "geofence_name": "Home Base"}, location_id=location.id,
    )

    assert sent[0]["body"] == "Toyota Hilux • Exited geofence: Home Base"


async def test_fence_push_falls_back_to_generic_label_when_name_missing(alarm_env, device_and_user, db_session):
    """Defensive: every synthesized fence transition carries geofence_name,
    but the push path must not blow up (or show "None") if it's ever absent."""
    server, sent = alarm_env
    device, user = device_and_user

    location = _make_location(db_session, device, "enter fence")
    await server._send_push_notification(device.id, {"alarm_type": "enter fence"}, location_id=location.id)

    assert sent[0]["body"] == "Toyota Hilux • Vehicle entered a geofence zone"


async def test_overspeed_push_body_includes_road_name(alarm_env, device_and_user, db_session):
    """Over speed pushes should name the road when a reverse-geocoded name
    is available on the alarm data (see the overspeed block in
    TCPServer.handle_location, app/tcp_server.py)."""
    server, sent = alarm_env
    device, user = device_and_user

    location = _make_location(db_session, device, "over speed")
    await server._send_push_notification(
        device.id,
        {"alarm_type": "over speed", "road_name": "KN 247 Street", "latitude": -1.9, "longitude": 30.05},
        location_id=location.id,
    )

    assert sent[0]["body"] == "Toyota Hilux • Overspeeding on KN 247 Street"


async def test_overspeed_push_falls_back_to_coordinates_when_road_name_missing(alarm_env, device_and_user, db_session):
    """Cache-miss case: the road name isn't resolved yet, so the push must
    still send immediately with coordinates rather than waiting on
    Nominatim (see the latency design note in the overspeed block)."""
    server, sent = alarm_env
    device, user = device_and_user

    location = _make_location(db_session, device, "over speed")
    await server._send_push_notification(
        device.id,
        {"alarm_type": "over speed", "road_name": None, "latitude": -1.9432, "longitude": 30.0521},
        location_id=location.id,
    )

    assert sent[0]["body"] == "Toyota Hilux • Overspeeding near -1.9432, 30.0521"


async def test_broadcast_geofence_transitions_end_to_end_enriches_push_body(alarm_env, device_and_user, db_session):
    """Full-stack check: a real GeofenceTransition run through
    _broadcast_geofence_transitions -> broadcast_alarm -> _send_push_notification
    ends up naming the actual geofence in the push body, not just "Enter fence"."""
    server, sent = alarm_env
    device, user = device_and_user

    class FakeWsManager:
        async def broadcast(self, device_id, payload):
            pass

    server.ws_manager = FakeWsManager()

    geofence = Geofence(
        user_id=user.id, name="Home Base", center_latitude=-1.9, center_longitude=30.05,
        radius_meters=200, is_active=True, alert_on_enter=True, alert_on_exit=True,
    )
    db_session.add(geofence)
    db_session.commit()
    db_session.refresh(geofence)

    location = _make_location(db_session, device, "enter fence")
    transitions = [GeofenceTransition(geofence=geofence, entered=True)]

    await _broadcast_geofence_transitions(
        server, device.id,
        {"latitude": -1.9, "longitude": 30.05, "timestamp": location.timestamp},
        transitions, location_id=location.id,
    )

    assert len(sent) == 1
    assert sent[0]["body"] == "Toyota Hilux • Entered geofence: Home Base"
