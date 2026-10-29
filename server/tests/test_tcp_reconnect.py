"""
A tracker reconnecting before its old socket is noticed dead.

Seen live 2026-10-19/20: fixes kept arriving and the route kept recording,
but every command failed "Device not connected" (and never reached
command_log) for ~24h. The G900LS had silently lost its mobile link and
logged in again on a new socket; when the server finally saw the OLD
socket close, its cleanup deleted the IMEI's registry entry — which by
then pointed at the NEW connection — marked the device offline and ended
its trip.
"""

import asyncio

import pytest
from sqlalchemy.orm import sessionmaker

import app.tcp_server as tcp_server_module
from app.models.device import Device
from app.tcp_server import GPSTrackerConnection, TCPServer

pytestmark = pytest.mark.anyio
IMEI = "123456789012345"


@pytest.fixture()
def anyio_backend():
    return "asyncio"


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


@pytest.fixture()
def env(db_session, monkeypatch):
    monkeypatch.setattr(tcp_server_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    device = Device(imei=IMEI, name="Haojue EX", lifecycle="sold", status="online")
    db_session.add(device)
    db_session.commit()
    return TCPServer(), device


def _logged_in(server, port, reader=None):
    conn = GPSTrackerConnection(reader=reader, writer=_Writer(), address=("1.2.3.4", port), server=server)
    conn.device_imei = IMEI
    conn.authenticated = True
    server.register_device(IMEI, conn)
    return conn


async def test_stale_socket_closing_keeps_the_new_connection(env, db_session):
    server, device = env
    old = _logged_in(server, 1)
    new = _logged_in(server, 2)

    assert server.device_connections[IMEI] is new
    assert old.writer.closed, "the half-open leftover is closed at once, not left to race"

    await old.close()  # the old socket's cleanup finally runs

    assert server.device_connections[IMEI] is new
    db_session.refresh(device)
    assert device.status == "online", "a stale socket must not mark a connected tracker offline"

    task = asyncio.create_task(server.send_command_to_device(IMEI, "RELAY#", timeout=0.05))
    result = await task
    assert result.get("error") != "Device not connected"
    assert new.writer.packets, "the command went out on the live connection"


async def test_current_socket_closing_still_marks_offline(env, db_session):
    server, device = env
    conn = _logged_in(server, 1)

    await conn.close()

    assert IMEI not in server.device_connections
    db_session.refresh(device)
    assert device.status == "offline"
    assert (await server.send_command_to_device(IMEI, "RELAY#"))["error"] == "Device not connected"


async def test_live_connection_reclaims_a_lost_registry_slot(env):
    server, _ = env
    seen_registered = []

    class _Reader:
        def __init__(self):
            self.chunks = [b"\x00\x01", b""]

        async def read(self, _n):
            if len(self.chunks) == 1:  # after the first chunk was processed
                seen_registered.append(server.device_connections.get(IMEI))
            return self.chunks.pop(0)

    conn = _logged_in(server, 1, reader=_Reader())
    del server.device_connections[IMEI]  # e.g. lost before this fix was deployed

    await conn.handle()

    assert seen_registered == [conn], "incoming data re-registers the connection"


async def test_a_reset_by_the_tracker_is_logged_as_info_not_an_error(env, db_session, caplog):
    server, device = env

    class _ResetReader:
        async def read(self, _n):
            raise ConnectionResetError(104, "Connection reset by peer")

    class _ResetWriter(_Writer):
        async def wait_closed(self):
            raise ConnectionResetError(104, "Connection reset by peer")

    conn = _logged_in(server, 1, reader=_ResetReader())
    conn.writer = _ResetWriter()

    with caplog.at_level("INFO", logger=tcp_server_module.logger.name):
        await conn.handle()

    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("Connection lost from" in r.getMessage() and r.levelname == "INFO" for r in caplog.records)
    assert any("Connection closed for" in r.getMessage() for r in caplog.records)
    # The cleanup still ran.
    assert IMEI not in server.device_connections
    db_session.refresh(device)
    assert device.status == "offline"


def _unauthenticated(server, reader):
    return GPSTrackerConnection(reader=reader, writer=_Writer(), address=("1.2.3.4", 9), server=server)


async def test_a_socket_that_never_logs_in_is_closed_and_says_what_it_sent(env, monkeypatch, caplog):
    server, _ = env
    monkeypatch.setattr(tcp_server_module.settings, "TCP_LOGIN_TIMEOUT_SECONDS", 0.05)

    class _UnknownThenSilent:
        def __init__(self):
            self.sent = False

        async def read(self, _n):
            if not self.sent:
                self.sent = True
                return bytes.fromhex("79790011") + b"\x00" * 4
            await asyncio.sleep(10)

    conn = _unauthenticated(server, _UnknownThenSilent())
    with caplog.at_level("INFO", logger=tcp_server_module.logger.name):
        await asyncio.wait_for(conn.handle(), timeout=2)

    assert conn.writer.closed
    [warning] = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "No login from" in warning
    assert "Received 8 byte(s), first: 7979001100000000" in warning


async def test_a_silent_socket_is_closed_too(env, monkeypatch, caplog):
    server, _ = env
    monkeypatch.setattr(tcp_server_module.settings, "TCP_LOGIN_TIMEOUT_SECONDS", 0.05)

    class _Silent:
        async def read(self, _n):
            await asyncio.sleep(10)

    conn = _unauthenticated(server, _Silent())
    with caplog.at_level("INFO", logger=tcp_server_module.logger.name):
        await asyncio.wait_for(conn.handle(), timeout=2)

    assert conn.writer.closed
    assert any("Received nothing." in r.getMessage() for r in caplog.records if r.levelname == "WARNING")


async def test_a_logged_in_tracker_is_never_timed_out(env, monkeypatch, caplog):
    server, _ = env
    monkeypatch.setattr(tcp_server_module.settings, "TCP_LOGIN_TIMEOUT_SECONDS", 0.01)

    class _SlowThenEOF:
        async def read(self, _n):
            await asyncio.sleep(0.1)  # quiet for longer than the login timeout
            return b""

    conn = _logged_in(server, 1, reader=_SlowThenEOF())
    with caplog.at_level("INFO", logger=tcp_server_module.logger.name):
        await conn.handle()

    messages = [r.getMessage() for r in caplog.records]
    assert not any("No login from" in m for m in messages)
    assert "Connection closed by ('1.2.3.4', 1)" in messages  # ended by the tracker, not the timeout
