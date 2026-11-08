"""Tracker link health — what the 2026-11-04..08 Haojue EX outage exposed.

The tracker's link died at 10:17 on Nov 4. The logged-in socket was never
timed out (the device stayed "online"); for days the tracker opened sockets
that sent nothing, and on Nov 5 logged in every 1-2 min without sending a
fix. Nobody was told. Also covered: never ACK packets we drop before login
(an ACK makes the tracker delete the fix from its memory), and the
`device_status` event that keeps the app's Commands screen in sync.
"""

import asyncio
import struct
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

import app.tcp_server as tcp_server_module
from app.models.device import Device
from app.models.user import Role, User
from app.protocol_parser import ProtocolParser
from app.services.tracker_watchdog import TrackerWatchdog, diagnose
from app.tcp_server import GPSTrackerConnection, TCPServer

pytestmark = pytest.mark.anyio
# The parser reads the login IMEI as 8 BCD bytes -> 16 hex digits.
IMEI = "0123456789012345"


@pytest.fixture()
def anyio_backend():
    return "asyncio"


def _packet(protocol: int, payload: bytes, serial: int = 1) -> bytes:
    body = bytes([len(payload) + 5, protocol]) + payload + struct.pack(">H", serial)
    crc = ProtocolParser.calculate_crc(body)
    return b"\x78\x78" + body + struct.pack(">H", crc) + b"\x0d\x0a"


def _login_packet() -> bytes:
    return _packet(0x01, bytes.fromhex(IMEI))


def _heartbeat_packet(battery_level: int = 6, gsm: int = 4, relay_cut: bool = False) -> bytes:
    terminal_info = 0x01 | (0x80 if relay_cut else 0)
    return _packet(0x13, bytes([terminal_info, battery_level, gsm, 0x00, 0x02]))


class _Writer:
    def __init__(self):
        self.packets = []
        self.closed = False

    def write(self, data):
        self.packets.append(bytes(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class _Reader:
    """Hands out `chunks` one per read, then stays quiet (or EOFs)."""

    def __init__(self, *chunks, then_eof=True):
        self.chunks = list(chunks)
        self.then_eof = then_eof

    async def read(self, _n):
        if self.chunks:
            return self.chunks.pop(0)
        if self.then_eof:
            return b""
        await asyncio.sleep(10)


class _WsManager:
    def __init__(self):
        self.sent = []

    async def broadcast(self, device_id, payload):
        self.sent.append((device_id, payload))

    def statuses(self):
        return [p for _, p in self.sent if p.get("type") == "device_status"]


@pytest.fixture()
def env(db_session, monkeypatch):
    monkeypatch.setattr(tcp_server_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    device = Device(imei=IMEI, name="Haojue EX", lifecycle="sold", status="offline")
    db_session.add(device)
    db_session.commit()
    server = TCPServer()
    server.ws_manager = _WsManager()
    return server, device


def _conn(server, reader, port=1):
    return GPSTrackerConnection(reader=reader, writer=_Writer(), address=("41.186.0.1", port), server=server)


def _logged_in(server, reader, port=1):
    conn = _conn(server, reader, port)
    conn.device_imei = IMEI
    conn.authenticated = True
    server.register_device(IMEI, conn)
    return conn


# -- 2: never ACK what's dropped before login ------------------------------


async def test_a_packet_before_login_is_dropped_without_an_ack(env, db_session):
    server, device = env
    conn = _conn(server, _Reader(_heartbeat_packet(battery_level=3)))

    await conn.handle()

    assert conn.writer.packets == [], "an ACK would make the tracker delete the fix from its memory"
    db_session.refresh(device)
    assert device.battery_level is None


async def test_an_accepted_login_is_acked_and_announced_online(env, db_session):
    server, device = env
    conn = _conn(server, _Reader(_login_packet(), then_eof=False))
    task = asyncio.create_task(conn.handle())
    await asyncio.sleep(0.05)

    assert len(conn.writer.packets) == 1 and conn.writer.packets[0][3] == 0x01
    [status] = server.ws_manager.statuses()
    assert status["device_id"] == device.id and status["status"] == "online"
    task.cancel()


async def test_a_rejected_login_is_not_acked(env, db_session):
    server, _ = env
    unknown = _packet(0x01, bytes.fromhex("0999999999999999"))
    conn = _conn(server, _Reader(unknown))

    await conn.handle()

    assert conn.writer.packets == []
    assert conn.writer.closed


async def test_a_login_that_fails_is_not_acked(env, monkeypatch):
    server, _ = env

    def _db_down(_imei):
        raise RuntimeError("db down")

    monkeypatch.setattr(tcp_server_module, "_db_handle_login", _db_down)
    conn = _conn(server, _Reader(_login_packet()))

    await conn.handle()

    assert conn.writer.packets == [], "an ACK would make it send fixes we'd drop"


# -- 1: a logged-in socket that goes silent is a dead link -------------------


async def test_a_silent_logged_in_socket_is_closed_and_marked_offline(env, db_session, monkeypatch, caplog):
    server, device = env
    device.status = "online"
    db_session.commit()
    monkeypatch.setattr(tcp_server_module.settings, "TCP_IDLE_TIMEOUT_SECONDS", 0.05)
    conn = _logged_in(server, _Reader(then_eof=False))

    with caplog.at_level("WARNING", logger=tcp_server_module.logger.name):
        await asyncio.wait_for(conn.handle(), timeout=2)

    assert conn.writer.closed
    assert IMEI not in server.device_connections
    db_session.refresh(device)
    assert device.status == "offline"
    assert any("dead link" in r.getMessage() for r in caplog.records)
    [status] = server.ws_manager.statuses()
    assert status["status"] == "offline", "the app is told at once, not on its next refresh"


async def test_a_reporting_tracker_is_not_timed_out(env, db_session, monkeypatch):
    server, device = env
    monkeypatch.setattr(tcp_server_module.settings, "TCP_IDLE_TIMEOUT_SECONDS", 0.2)

    class _Heartbeats:
        def __init__(self):
            self.left = 3

        async def read(self, _n):
            await asyncio.sleep(0.1)  # each gap shorter than the timeout
            if not self.left:
                return b""
            self.left -= 1
            return _heartbeat_packet()

    conn = _logged_in(server, _Heartbeats())
    await asyncio.wait_for(conn.handle(), timeout=3)

    assert len(conn.writer.packets) == 3, "every heartbeat was ACKed; the socket ended on EOF"


# -- device_status: battery / signal / online, live ------------------------


async def test_every_heartbeat_broadcasts_battery_signal_and_status(env, db_session):
    server, device = env
    conn = _logged_in(server, _Reader(_heartbeat_packet(6, 4), _heartbeat_packet(3, 1)))

    await conn.handle()

    statuses = server.ws_manager.statuses()
    online = [s for s in statuses if s["status"] == "online"]
    assert [(s["battery_level"], s["gsm_signal"]) for s in online] == [(100, 4), (40, 1)], (
        "sent on every heartbeat, not only when the relay changes"
    )
    assert online[1]["relay_cut"] is False and online[1]["last_update"]
    assert statuses[-1]["status"] == "offline", "then the EOF takes it offline"


# -- 3: evidence for the watchdog --------------------------------------------


async def test_sockets_that_send_nothing_are_recorded(env, monkeypatch):
    server, _ = env
    monkeypatch.setattr(tcp_server_module.settings, "TCP_LOGIN_TIMEOUT_SECONDS", 0.02)
    since = datetime.utcnow() - timedelta(seconds=1)

    await asyncio.wait_for(_conn(server, _Reader(then_eof=False)).handle(), timeout=2)  # times out
    await _conn(server, _Reader()).handle()  # closed before login, nothing sent
    await _conn(server, _Reader(b"\x00\x01")).handle()  # sent bytes: not "silent"

    assert server.silent_sockets_since(since) == 2


async def test_a_login_with_no_data_is_recorded(env):
    server, _ = env
    since = datetime.utcnow() - timedelta(seconds=1)

    await _conn(server, _Reader(_login_packet()), port=1).handle()
    await _conn(server, _Reader(_login_packet(), _heartbeat_packet()), port=2).handle()

    assert server.logins_without_data_since(IMEI, since) == 1


# -- 3: the watchdog -----------------------------------------------------------

NOW = datetime(2026, 11, 4, 12, 0, 0)


class _Evidence:
    def __init__(self, silent=0, logins=0):
        self.silent, self.logins = silent, logins

    def silent_sockets_since(self, _since):
        return self.silent

    def logins_without_data_since(self, _imei, _since):
        return self.logins


def _device(minutes_ago, device_id=1):
    return SimpleNamespace(
        id=device_id, name="Haojue EX", imei=IMEI, last_update=NOW - timedelta(minutes=minutes_ago)
    )


def test_with_evidence_of_a_broken_link_it_alerts_after_30_minutes():
    watchdog = TrackerWatchdog(_Evidence(silent=12))
    assert watchdog.evaluate([_device(29)], NOW) == []
    [notice] = watchdog.evaluate([_device(31)], NOW)
    assert notice.kind == "silent" and notice.title == "Haojue EX stopped reporting"
    assert "12 connection(s)" in notice.body and "data bundle" in notice.body


def test_without_evidence_it_waits_longer_so_a_parked_tracker_does_not_page_at_night():
    watchdog = TrackerWatchdog(_Evidence())
    assert watchdog.evaluate([_device(60)], NOW) == []
    [notice] = watchdog.evaluate([_device(121)], NOW)
    assert "No connection attempts" in notice.body


def test_it_alerts_once_per_silence_then_says_when_it_is_back():
    watchdog = TrackerWatchdog(_Evidence(logins=5))
    silent = _device(40)
    assert len(watchdog.evaluate([silent], NOW)) == 1
    assert watchdog.evaluate([silent], NOW + timedelta(minutes=5)) == []

    back = SimpleNamespace(**{**vars(silent), "last_update": NOW + timedelta(minutes=10)})
    [notice] = watchdog.evaluate([back], NOW + timedelta(minutes=10))
    assert notice.kind == "recovered" and notice.title == "Haojue EX is reporting again"


def test_diagnosis_prefers_the_strongest_evidence():
    assert "logged in 3 times" in diagnose(silent_sockets=50, logins_without_data=3)
    assert "opened and sent nothing" in diagnose(silent_sockets=50, logins_without_data=1)


async def test_check_once_pushes_every_admin_with_a_token(db_session):
    db_session.add_all([
        Device(imei=IMEI, name="Haojue EX", lifecycle="sold", status="offline",
               last_update=NOW - timedelta(hours=3)),
        Device(imei="9", name="Unused", lifecycle="registered", status="offline",
               last_update=NOW - timedelta(hours=3)),
        Device(imei="8", name="Long gone", lifecycle="sold", status="offline",
               last_update=NOW - timedelta(days=30)),
        User(clerk_user_id="a", email="admin@x", first_name="A", last_name="B", role=Role.ADMIN, fcm_token="tok-admin"),
        User(clerk_user_id="b", email="notoken@x", first_name="A", last_name="B", role=Role.SUPER_ADMIN),
        User(clerk_user_id="c", email="owner@x", first_name="A", last_name="B", role=Role.USER, fcm_token="tok-owner"),
    ])
    db_session.commit()
    pushed = []

    async def _push(user, title, body, data):
        pushed.append((user.fcm_token, title, data))

    watchdog = TrackerWatchdog(
        _Evidence(), session_factory=sessionmaker(bind=db_session.get_bind()), push=_push, clock=lambda: NOW
    )
    notices = await watchdog.check_once()

    assert [n.device_name for n in notices] == ["Haojue EX"]
    assert pushed == [("tok-admin", "Haojue EX stopped reporting",
                       {"type": "tracker_health", "kind": "silent", "device_id": notices[0].device_id})]
