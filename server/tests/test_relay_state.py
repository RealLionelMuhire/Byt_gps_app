"""
Tracker-reported fuel/relay state (Device.relay_cut, migration 052).

The source of truth is GT06 terminal-info bit 7 ("oil/electricity
disconnected"), carried by every heartbeat — previously decoded and then
dropped, so clients could only guess the fuel state from the last button
pressed. See app/services/relay_state.py.
"""

import pytest
from sqlalchemy.orm import sessionmaker

import app.tcp_server as tcp_server_module
from app.models.device import Device
from app.models.user import Role, User
from app.protocol_parser import ProtocolParser
from app.tcp_server import GPSTrackerConnection

pytestmark = pytest.mark.anyio

IMEI = "123456789012345"


@pytest.fixture()
def anyio_backend():
    return "asyncio"


def _heartbeat_packet(terminal_info: int) -> bytes:
    # 78 78 | len | 0x13 | terminal info | voltage | gsm | alarm | lang | serial(2) | crc(2) | 0D 0A
    return bytes([0x78, 0x78, 0x0A, 0x13, terminal_info, 0x04, 0x03, 0x00, 0x02, 0x00, 0x01, 0x00, 0x00, 0x0D, 0x0A])


def test_parser_reports_relay_bit():
    parser = ProtocolParser()
    assert parser.parse_heartbeat(_heartbeat_packet(0x80 | 0x46))["oil_elec_disconnected"] is True
    assert parser.parse_heartbeat(_heartbeat_packet(0x46))["oil_elec_disconnected"] is False


class _FakeWriter:
    def write(self, data):
        pass

    async def drain(self):
        pass


class _FakeServer:
    def __init__(self):
        self.statuses = []

    async def broadcast_device_status(self, device_id, relay_state):
        self.statuses.append((device_id, relay_state))


@pytest.fixture()
def tcp_env(db_session, monkeypatch):
    monkeypatch.setattr(tcp_server_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))


def _connection():
    conn = GPSTrackerConnection(reader=None, writer=_FakeWriter(), address=("1.2.3.4", 1234), server=_FakeServer())
    conn.device_imei = IMEI
    conn.authenticated = True
    return conn


def _heartbeat(cut):
    return {"battery_percent": 80, "gsm_signal": 3, "signal_bars": 3, "oil_elec_disconnected": cut}


async def test_heartbeat_records_relay_state_and_broadcasts_only_on_change(tcp_env, db_session):
    device = Device(imei=IMEI, name="Truck", lifecycle="sold")
    db_session.add(device)
    db_session.commit()
    conn = _connection()

    await conn.handle_heartbeat(_heartbeat(False))
    db_session.refresh(device)
    assert device.relay_cut is False
    assert device.relay_state_at is not None
    assert len(conn.server.statuses) == 1  # first report ever

    await conn.handle_heartbeat(_heartbeat(False))
    assert len(conn.server.statuses) == 1  # unchanged -> no broadcast

    await conn.handle_heartbeat(_heartbeat(True))
    db_session.refresh(device)
    assert device.relay_cut is True
    assert len(conn.server.statuses) == 2
    device_id, payload = conn.server.statuses[-1]
    assert device_id == device.id
    assert payload["relay_cut"] is True
    assert payload["relay_state_at"]


async def test_heartbeat_without_status_byte_leaves_state_alone(tcp_env, db_session):
    device = Device(imei=IMEI, name="Truck", lifecycle="sold", relay_cut=True)
    db_session.add(device)
    db_session.commit()
    conn = _connection()

    await conn.handle_heartbeat(_heartbeat(None))
    db_session.refresh(device)
    assert device.relay_cut is True
    assert conn.server.statuses == []


class _FakeTcp:
    def __init__(self, reply):
        self.reply = reply
        self.sent = []

    async def send_command_to_device(self, imei, command):
        self.sent.append(command)
        return {"success": True, "response": self.reply}


def _admin_with_device(db_session, current_clerk_id, **device_fields):
    admin = User(clerk_user_id="clerk_admin", email="a@example.com", first_name="A", last_name="B",
                 role=Role.ADMIN, onboarding_step=0, onboarding_complete=True)
    db_session.add(admin)
    db_session.commit()
    current_clerk_id["value"] = "clerk_admin"
    device = Device(imei=IMEI, name="Truck", lifecycle="sold", user_id=admin.id, **device_fields)
    db_session.add(device)
    db_session.commit()
    return device


def test_query_relay_sends_relay_query_and_returns_tracker_state(client, db_session, current_clerk_id):
    device = _admin_with_device(db_session, current_clerk_id, relay_cut=False)
    fake = _FakeTcp("RELAY:0")
    client.app.state.tcp_server = fake

    res = client.post(f"/api/devices/{device.id}/query/relay")

    assert res.status_code == 200, res.text
    body = res.json()
    assert fake.sent == ["RELAY#"]
    assert body["device_response"] == "RELAY:0"
    assert body["relay_cut"] is False
    assert "relay_state_at" in body


def test_fuel_cut_response_carries_tracker_state_not_an_assumption(client, db_session, current_clerk_id):
    # Tracker hasn't reported a cut yet: the response must say so (None),
    # so clients show "waiting for the tracker" instead of "Fuel cut".
    device = _admin_with_device(db_session, current_clerk_id)
    fake = _FakeTcp(None)
    client.app.state.tcp_server = fake

    res = client.post(f"/api/devices/{device.id}/fuel/cut", json={"confirm": True})

    assert res.status_code == 200, res.text
    assert fake.sent == ["RELAY,1#"]
    assert res.json()["relay_cut"] is None


def test_device_list_exposes_relay_state(client, db_session, current_clerk_id):
    _admin_with_device(db_session, current_clerk_id, relay_cut=True)
    res = client.get("/api/devices/")
    assert res.status_code == 200, res.text
    assert res.json()[0]["relay_cut"] is True
