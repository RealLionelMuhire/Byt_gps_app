"""
Tests for wiring a WHERE# command's reply into the same location pipeline a
passive 0x12 packet uses (app/tcp_server.py's handle_command_response ->
handle_location), and for the command/response serialization fix
(_command_lock) that was found to be a prerequisite bug while building this.

Follows tests/test_alarm_cron_jobs.py's pattern: SessionLocal is
monkeypatched to a sessionmaker bound to the same in-memory engine as the
shared db_session fixture.
"""

import asyncio

import pytest
from sqlalchemy.orm import sessionmaker

import app.tcp_server as tcp_server_module
from app.tcp_server import GPSTrackerConnection
from app.models.device import Device
from app.models.location import Location

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    return "asyncio"


class _FakeWriter:
    def write(self, data):
        pass

    async def drain(self):
        pass


class _FakeServer:
    def __init__(self):
        self.broadcasts = []
        self.alarms = []

    async def broadcast_location_update(self, device_id, live_data):
        self.broadcasts.append((device_id, live_data))

    async def broadcast_alarm(self, device_id, data, location_id=None):
        self.alarms.append((device_id, data, location_id))


@pytest.fixture()
def tcp_env(db_session, monkeypatch):
    engine = db_session.get_bind()
    test_session_local = sessionmaker(bind=engine)
    monkeypatch.setattr(tcp_server_module, "SessionLocal", test_session_local)


def _make_connection(imei="123456789012345"):
    conn = GPSTrackerConnection(reader=None, writer=_FakeWriter(), address=("1.2.3.4", 1234), server=_FakeServer())
    conn.device_imei = imei
    conn.authenticated = True
    return conn


async def test_where_reply_creates_location_and_updates_device(tcp_env, db_session):
    device = Device(imei="123456789012345", name="Test Device", lifecycle="sold")
    db_session.add(device)
    db_session.commit()

    conn = _make_connection()
    conn._pending_command = "WHERE#"

    content = "LastPosition! Lati:S1.943835,E30.094658,Course:11,Speed:0.00,DateTime:2026-09-14 14:13:27"
    await conn.handle_command_response({"content": content})

    db_session.refresh(device)
    assert device.last_latitude == pytest.approx(-1.943835)
    assert device.last_longitude == pytest.approx(30.094658)

    locations = db_session.query(Location).filter(Location.device_id == device.id).all()
    assert len(locations) == 1
    assert locations[0].gps_valid is True
    assert locations[0].satellites == 0
    assert locations[0].latitude == pytest.approx(-1.943835)


async def test_unparseable_reply_writes_nothing(tcp_env, db_session):
    device = Device(imei="123456789012345", name="Test Device", lifecycle="sold")
    db_session.add(device)
    db_session.commit()

    conn = _make_connection()
    conn._pending_command = "WHERE#"

    await conn.handle_command_response({"content": "No Fix"})

    db_session.refresh(device)
    assert device.last_latitude is None
    assert db_session.query(Location).filter(Location.device_id == device.id).count() == 0


async def test_status_reply_is_never_parsed_as_location(tcp_env, db_session):
    device = Device(imei="123456789012345", name="Test Device", lifecycle="sold")
    db_session.add(device)
    db_session.commit()

    conn = _make_connection()
    conn._pending_command = "STATUS#"

    # Even if this happened to look location-shaped, a non-WHERE# pending
    # command must never trigger a location write.
    await conn.handle_command_response({"content": "LastPosition! Lati:S1.0,E30.0,Course:0,Speed:0,DateTime:2026-01-01 00:00:00"})

    assert db_session.query(Location).filter(Location.device_id == device.id).count() == 0


async def test_send_command_serializes_overlapping_calls(tcp_env, db_session):
    """Two overlapping send_command() calls on one connection must not
    cross-wire replies — the second caller waits for the lock instead of
    overwriting the first's pending future."""
    device = Device(imei="123456789012345", name="Test Device", lifecycle="sold")
    db_session.add(device)
    db_session.commit()

    conn = _make_connection()

    task1 = asyncio.create_task(conn.send_command("WHERE#", timeout=5.0))
    await asyncio.sleep(0)
    assert conn._pending_command == "WHERE#"

    task2 = asyncio.create_task(conn.send_command("STATUS#", timeout=5.0))
    await asyncio.sleep(0)
    # task2 must still be blocked on the lock — task1's command is unchanged.
    assert conn._pending_command == "WHERE#"

    await conn.handle_command_response({"content": "WHERE_REPLY"})
    result1 = await task1
    assert result1["response"] == "WHERE_REPLY"

    await asyncio.sleep(0)
    assert conn._pending_command == "STATUS#"

    await conn.handle_command_response({"content": "STATUS_REPLY"})
    result2 = await task2
    assert result2["response"] == "STATUS_REPLY"
