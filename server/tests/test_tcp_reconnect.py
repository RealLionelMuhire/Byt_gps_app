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
