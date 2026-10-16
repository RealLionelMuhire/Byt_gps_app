"""
Command <-> reply correlation and the command audit log (migration 053).

Every command used to go out with GT06 server flag 1, so a reply could not
be matched to its command: one landing after the 10s wait was lost, or
shown as the answer to the NEXT command (seen live 2026-10-16 as
"inconsistent" cut/restore replies). Commands now carry a unique flag the
tracker echoes back. Reply samples below are real G900LS replies.
"""

import asyncio
import struct

import pytest
from sqlalchemy.orm import sessionmaker

import app.tcp_server as tcp_server_module
from app.models.command_log import CommandLog
from app.models.device import Device
from app.models.user import Role, User
from app.services.relay_state import parse_relay_reply
from app.tcp_server import GPSTrackerConnection

pytestmark = pytest.mark.anyio
IMEI = "123456789012345"


@pytest.fixture()
def anyio_backend():
    return "asyncio"


def test_parse_relay_reply_real_samples():
    assert parse_relay_reply("RELAY:0") is False
    assert parse_relay_reply("RELAY:1") is True
    assert parse_relay_reply("Stop engine succeed RELAY 1 OK") is True
    assert parse_relay_reply("Stop engine fail, RELAY 1") is None  # never infer success from a failure
    assert parse_relay_reply("LastPosition! Lati:S1.94,E30.09,Course:11,Speed:0.00") is None
    assert parse_relay_reply(None) is None


class _CapturingWriter:
    def __init__(self):
        self.packets = []

    def write(self, data):
        self.packets.append(bytes(data))

    async def drain(self):
        pass

    def last_flag(self):
        # 78 78 | len | 0x80 | cmdlen | server flag (4 bytes) | ...
        return struct.unpack(">I", self.packets[-1][5:9])[0]


class _FakeServer:
    def __init__(self):
        self.statuses = []

    async def broadcast_device_status(self, device_id, relay_state):
        self.statuses.append((device_id, relay_state))

    async def broadcast_location_update(self, device_id, data):
        pass


@pytest.fixture()
def env(db_session, monkeypatch):
    monkeypatch.setattr(tcp_server_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    device = Device(imei=IMEI, name="Haojue EX", lifecycle="sold", relay_cut=False)
    db_session.add(device)
    db_session.commit()
    writer = _CapturingWriter()
    conn = GPSTrackerConnection(reader=None, writer=writer, address=("1.2.3.4", 1), server=_FakeServer())
    conn.device_imei = IMEI
    conn.authenticated = True
    return conn, writer, device


async def _until_sent(writer, count):
    for _ in range(100):
        if len(writer.packets) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError("command was never written")


async def test_unique_flags_matched_reply_is_logged_and_updates_fuel_state(env, db_session):
    conn, writer, device = env
    task = asyncio.create_task(conn.send_command("RELAY,1#", timeout=2))
    await _until_sent(writer, 1)
    flag = writer.last_flag()
    assert flag not in (0, 1)

    await conn.handle_command_response({"content": "Stop engine succeed RELAY 1 OK", "server_flag": flag})
    result = await task

    assert result["response"] == "Stop engine succeed RELAY 1 OK"
    row = db_session.query(CommandLog).one()
    assert (row.command, row.outcome, row.server_flag, row.reply_server_flag) == ("RELAY,1#", "replied", flag, flag)
    db_session.refresh(device)
    assert device.relay_cut is True, "the reply itself confirms the cut — no heartbeat wait"
    assert conn.server.statuses and conn.server.statuses[-1][1]["relay_cut"] is True

    task2 = asyncio.create_task(conn.send_command("RELAY,0#", timeout=2))
    await _until_sent(writer, 2)
    assert writer.last_flag() != flag
    await conn.handle_command_response({"content": "RELAY:0", "server_flag": writer.last_flag()})
    await task2


async def test_late_reply_is_never_shown_as_the_next_commands_answer(env, db_session):
    conn, writer, device = env
    first = await conn.send_command("RELAY,1#", timeout=0.01)  # tracker too slow
    assert first["response"] is None
    cut_flag = writer.last_flag()

    task = asyncio.create_task(conn.send_command("RELAY#", timeout=0.5))
    await _until_sent(writer, 2)
    # The CUT's reply lands during the next command:
    await conn.handle_command_response({"content": "Stop engine succeed RELAY 1 OK", "server_flag": cut_flag})
    query_result = await task

    assert query_result["response"] is None, "the cut's late reply must not answer RELAY#"
    rows = {r.command: r for r in db_session.query(CommandLog).all()}
    assert rows["RELAY,1#"].outcome == "late_reply"
    assert rows["RELAY,1#"].reply == "Stop engine succeed RELAY 1 OK"
    assert rows["RELAY#"].outcome == "timeout"
    db_session.refresh(device)
    assert device.relay_cut is True, "a late but real confirmation still updates the fuel state"


async def test_firmware_that_does_not_echo_the_flag_falls_back_but_is_marked(env, db_session):
    conn, writer, _ = env
    task = asyncio.create_task(conn.send_command("RELAY#", timeout=2))
    await _until_sent(writer, 1)
    await conn.handle_command_response({"content": "RELAY:0", "server_flag": 1})
    assert (await task)["response"] == "RELAY:0"
    assert db_session.query(CommandLog).one().outcome == "replied_unverified"


async def test_reply_with_no_command_in_flight_is_logged_as_unmatched(env, db_session):
    conn, _, _ = env
    await conn.handle_command_response({"content": "RELAY:0", "server_flag": 12345})
    row = db_session.query(CommandLog).one()
    assert (row.command, row.outcome, row.reply) == (None, "unmatched", "RELAY:0")


def test_command_log_endpoint(client, db_session, current_clerk_id):
    admin = User(clerk_user_id="clerk_admin", email="a@example.com", first_name="A", last_name="B",
                 role=Role.ADMIN, onboarding_step=0, onboarding_complete=True)
    db_session.add(admin)
    db_session.commit()
    current_clerk_id["value"] = "clerk_admin"
    device = Device(imei=IMEI, name="Haojue EX", lifecycle="sold", user_id=admin.id)
    db_session.add(device)
    db_session.commit()
    db_session.add(CommandLog(device_id=device.id, imei=IMEI, command="RELAY,1#", server_flag=999,
                              reply="Stop engine succeed RELAY 1 OK", outcome="replied"))
    db_session.commit()

    res = client.get(f"/api/devices/{device.id}/command_log")
    assert res.status_code == 200, res.text
    assert res.json()[0]["reply"] == "Stop engine succeed RELAY 1 OK"
