"""
Tracker models (migration 057): each device's hardware model decides the
exact command text for a capability, customers only get verified
commands, and admins manage models from the portal.
"""

import asyncio
import json
import re
import struct
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

import app.tcp_server as tcp_server_module
from app.models.command_log import CommandLog
from app.models.command_settings import CommandSettings
from app.models.device import Device
from app.models.tracker_model import TrackerModel, TrackerModelCommand
from app.models.user import Role
from app.services.tracker_models import (
    BUILTIN_G900LS, G900LS_ROWS, TK903ELE_ROWS, CommandNotSupported, InvalidCommandParams,
    profile_for_device, template_errors,
)
from app.tcp_server import GPSTrackerConnection
from tests.test_plan_expiry_freshness import make_user

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "057_add_tracker_models.sql"


def seed(db):
    g900 = TrackerModel(name="G900LS J16-4G", protocol="gt06", is_default=True)
    tk = TrackerModel(name="TK903ELE", protocol="gt06", command_password="123456")
    for model, rows in ((g900, G900LS_ROWS), (tk, TK903ELE_ROWS)):
        for key, template, verified, label in rows:
            model.commands.append(TrackerModelCommand(key=key, template=template, verified=verified, label=label))
        db.add(model)
    db.commit()
    return g900, tk


class FakeTcp:
    def __init__(self, response="OK"):
        self.sent = []
        self.response = response

    async def send_command_to_device(self, imei, command, timeout=10.0, user_id=None):
        self.sent.append(command)
        return {"success": True, "response": self.response}


@pytest.fixture()
def env(client, db_session, current_clerk_id):
    g900, tk = seed(db_session)
    owner = make_user(db_session, "clerk_owner")
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    g900_device = Device(imei="111111111111111", name="Haojue", user_id=owner.id, lifecycle="sold")
    tk_device = Device(imei="222222222222222", name="Truck", user_id=owner.id, lifecycle="sold",
                       tracker_model_id=tk.id)
    db_session.add_all([g900_device, tk_device])
    db_session.commit()
    tcp = FakeTcp()
    client.app.state.tcp_server = tcp
    current_clerk_id["value"] = owner.clerk_user_id

    def as_admin():
        current_clerk_id["value"] = admin.clerk_user_id

    return {"g900": g900, "tk": tk, "g900_device": g900_device, "tk_device": tk_device,
            "tcp": tcp, "as_admin": as_admin, "owner": owner}


# --- Seed data --------------------------------------------------------------------


def _seeded_rows(sql, model):
    pattern = re.compile(
        r"\(\(SELECT id FROM tracker_models WHERE name = '" + re.escape(model) +
        r"'\), '([^']+)', (NULL|'[^']*'), '((?:[^']|'')*)', (TRUE|FALSE)\)")
    return [
        (key, template.replace("''", "'"), verified == "TRUE", None if label == "NULL" else label.strip("'"))
        for key, label, template, verified in pattern.findall(sql)
    ]


def test_the_migration_seeds_exactly_the_built_in_command_tables():
    sql = MIGRATION.read_text()
    assert _seeded_rows(sql, "G900LS J16-4G") == list(G900LS_ROWS)
    assert _seeded_rows(sql, "TK903ELE") == list(TK903ELE_ROWS)
    assert "ENABLE ROW LEVEL SECURITY" in sql.split("tracker_model_commands IS")[1]


def test_every_seeded_template_is_valid():
    for key, template, _, _ in G900LS_ROWS + TK903ELE_ROWS:
        assert template_errors(key, template) is None, (key, template)


# --- Rendering --------------------------------------------------------------------


def test_each_model_renders_its_own_command_for_the_same_capability(db_session):
    _, tk = seed(db_session)
    assert BUILTIN_G900LS.render("fuel.cut") == "RELAY,1#"
    tk_device = Device(imei="3", name="x", tracker_model_id=tk.id)
    db_session.add(tk_device)
    db_session.commit()
    assert profile_for_device(db_session, tk_device).render("fuel.cut") == "stop123456"
    assert BUILTIN_G900LS.render("alarm.power_cut.on", {"mode": 1}) == "POWERALM,ON,1,5,10,#"


def test_parameters_are_validated_and_unsupported_capabilities_refused():
    with pytest.raises(InvalidCommandParams):
        BUILTIN_G900LS.render("alarm.vibration.on", {"mode": 7})
    with pytest.raises(InvalidCommandParams):
        BUILTIN_G900LS.render("alarm.vibration.on", {"volume": 1})
    tk_like = BUILTIN_G900LS.__class__(model_id=1, name="TK903ELE", commands={})
    with pytest.raises(CommandNotSupported):
        tk_like.render("location.query")


def test_template_validation():
    assert template_errors("alarm.vibration.on", "SENALM,ON,{mode}#") is None
    assert "Unknown placeholder" in template_errors("fuel.cut", "RELAY,{mode}#")
    assert template_errors("fuel.cut", "") is not None
    assert template_errors("fuel.cut", "RELAY,1#é") is not None
    assert template_errors("nope.cut", "X#") is not None
    assert template_errors("extra.version", "VERSION#") is None


def test_devices_without_a_model_use_the_default_then_the_built_in(db_session):
    device = Device(imei="4", name="x")
    db_session.add(device)
    db_session.commit()
    assert profile_for_device(db_session, device) is BUILTIN_G900LS  # no models at all
    seed(db_session)
    profile = profile_for_device(db_session, device)
    assert profile.name == "G900LS J16-4G" and profile.model_id is not None


# --- Sending commands -------------------------------------------------------------


def test_a_g900ls_fuel_cut_sends_relay_and_a_tk903ele_cut_is_held_back_until_verified(client, env):
    res = client.post(f"/api/devices/{env['g900_device'].id}/fuel/cut", json={"confirm": True})
    assert res.status_code == 200, res.text
    assert res.json()["command_sent"] == "RELAY,1#"

    res = client.post(f"/api/devices/{env['tk_device'].id}/fuel/cut", json={"confirm": True})
    assert res.status_code == 403
    assert "hasn't been verified on TK903ELE" in res.json()["detail"]

    env["as_admin"]()
    res = client.post(f"/api/devices/{env['tk_device'].id}/fuel/cut", json={"confirm": True})
    assert res.status_code == 200
    assert res.json()["command_sent"] == "stop123456"
    assert env["tcp"].sent == ["RELAY,1#", "stop123456"]


def test_a_capability_the_model_lacks_is_refused_and_nothing_is_sent(client, env):
    env["as_admin"]()
    res = client.post(f"/api/devices/{env['tk_device'].id}/query/location")
    assert res.status_code == 422
    assert "TK903ELE trackers don't support" in res.json()["detail"]
    assert env["tcp"].sent == []


def test_alarm_parameters_reach_the_model_template(client, env):
    res = client.post(f"/api/devices/{env['g900_device'].id}/alarm/power-cut",
                      json={"enabled": True, "mode": 1, "detect_seconds": 7, "min_charge_seconds": 30})
    assert res.status_code == 200
    assert env["tcp"].sent == ["POWERALM,ON,1,7,30,#"]


def test_a_raw_command_that_is_the_models_fuel_cut_obeys_fuel_cut_disabled(client, db_session, env):
    db_session.add(CommandSettings(device_id=env["tk_device"].id, fuel_cut_enabled=False))
    db_session.commit()
    env["as_admin"]()
    res = client.post(f"/api/devices/{env['tk_device'].id}/command", json={"command": "stop123456"})
    assert res.status_code == 403
    assert env["tcp"].sent == []


def test_capabilities_customers_see_only_verified_ones_admins_see_everything(client, env):
    g900 = client.get(f"/api/devices/{env['g900_device'].id}/capabilities").json()
    keys = {c["key"] for c in g900["capabilities"]}
    assert g900["tracker_model"]["name"] == "G900LS J16-4G"
    assert {"fuel.cut", "fuel.query", "location.query", "alarm.vibration.on"} <= keys
    assert "device.restart" not in keys, "admin-only and unverified"
    assert g900["extras"] == []

    tk = client.get(f"/api/devices/{env['tk_device'].id}/capabilities").json()
    assert tk["capabilities"] == [], "nothing verified on the TK903ELE yet"

    env["as_admin"]()
    tk = client.get(f"/api/devices/{env['tk_device'].id}/capabilities").json()
    assert {c["key"] for c in tk["capabilities"]} == {
        "fuel.cut", "fuel.restore", "status.query", "alarm.vibration.on", "alarm.vibration.off", "device.restart"}
    assert all(c["verified"] is False for c in tk["capabilities"])
    assert {e["key"] for e in tk["extras"]} == {"extra.check_settings", "extra.version"}


# --- Admin API --------------------------------------------------------------------


def test_admin_routes_are_admin_only(client, env):
    assert client.get("/api/admin/tracker-models").status_code == 403


def test_admin_builds_a_new_model_tests_a_command_and_verifies_it(client, db_session, env):
    env["as_admin"]()
    caps = client.get("/api/admin/tracker-capabilities").json()
    assert any(c["key"] == "fuel.cut" and c["destructive"] for c in caps["capabilities"])

    res = client.post("/api/admin/tracker-models", json={"name": "Concox GT06N", "command_password": "000000"})
    assert res.status_code == 201, res.text
    model_id = res.json()["id"]
    assert res.json()["is_default"] is False

    bad = client.put(f"/api/admin/tracker-models/{model_id}/commands/fuel.cut", json={"template": "DYD,{pin}#"})
    assert bad.status_code == 422 and "{pin}" in bad.json()["detail"]
    ok = client.put(f"/api/admin/tracker-models/{model_id}/commands/fuel.cut", json={"template": "DYD,{password}#"})
    assert ok.status_code == 200 and ok.json()["verified"] is False
    assert client.put(f"/api/admin/tracker-models/{model_id}/commands/extra.gps",
                      json={"template": "GPSON#"}).status_code == 422, "extras need a label"

    device = env["g900_device"]
    assert client.put(f"/api/admin/devices/{device.id}/tracker-model",
                      json={"tracker_model_id": model_id}).json()["tracker_model"]["name"] == "Concox GT06N"
    env["tcp"].response = "DYD=Success!"
    res = client.post(f"/api/admin/devices/{device.id}/test-command", json={"key": "fuel.cut"})
    assert res.status_code == 200, res.text
    assert (res.json()["command_sent"], res.json()["device_response"]) == ("DYD,000000#", "DYD=Success!")

    client.put(f"/api/admin/tracker-models/{model_id}/commands/fuel.cut",
               json={"template": "DYD,{password}#", "verified": True})
    detail = client.get(f"/api/admin/tracker-models/{model_id}").json()
    assert detail["verified_count"] == 1 and detail["device_count"] == 1


def test_model_rules_default_name_patterns_and_delete(client, db_session, env):
    env["as_admin"]()
    g900, tk = env["g900"], env["tk"]
    assert client.post("/api/admin/tracker-models", json={"name": "g900ls j16-4g"}).status_code == 409
    assert client.post("/api/admin/tracker-models", json={"name": "X", "protocol": "teltonika"}).status_code == 422
    assert client.put(f"/api/admin/tracker-models/{tk.id}", json={
        "name": "TK903ELE", "command_password": "123456", "reply_patterns": {"relay_cut": "("}}).status_code == 422

    res = client.put(f"/api/admin/tracker-models/{tk.id}", json={
        "name": "TK903ELE", "command_password": "123456", "is_default": True,
        "reply_patterns": {"relay_cut": r"stop\s+ok"}})
    assert res.status_code == 200 and res.json()["is_default"] is True
    db_session.expire_all()
    assert db_session.get(TrackerModel, g900.id).is_default is False, "exactly one default"
    assert json.loads(db_session.get(TrackerModel, tk.id).reply_patterns) == {"relay_cut": r"stop\s+ok"}

    assert client.delete(f"/api/admin/tracker-models/{tk.id}").status_code == 409, "default"
    client.put(f"/api/admin/tracker-models/{g900.id}", json={"name": "G900LS J16-4G", "is_default": True})
    assert client.delete(f"/api/admin/tracker-models/{tk.id}").status_code == 409, "a device uses it"


def test_inventory_shows_each_devices_model(client, env):
    env["as_admin"]()
    rows = client.get("/api/admin/dashboard/inventory").json()
    rows = rows.get("items", rows) if isinstance(rows, dict) else rows
    names = {r["imei"]: r["tracker_model_name"] for r in rows}
    assert names["111111111111111"] == "G900LS J16-4G", "no model -> the default"
    assert names["222222222222222"] == "TK903ELE"


# --- TCP layer --------------------------------------------------------------------


class _Writer:
    def __init__(self):
        self.packets = []

    def write(self, data):
        self.packets.append(bytes(data))

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


class _Server:
    def __init__(self):
        self.statuses = []

    async def broadcast_device_status(self, device_id, relay_state):
        self.statuses.append((device_id, relay_state))

    async def broadcast_location_update(self, device_id, data):
        pass


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def tk_connection(db_session, monkeypatch):
    monkeypatch.setattr(tcp_server_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    _, tk = seed(db_session)
    tk.reply_patterns = json.dumps({"relay_cut": r"stop\s+ok", "relay_deferred": r"later"})
    device = Device(imei="222222222222222", name="Truck", lifecycle="sold", relay_cut=False, tracker_model_id=tk.id)
    db_session.add(device)
    db_session.commit()
    writer = _Writer()
    conn = GPSTrackerConnection(reader=None, writer=writer, address=("1.2.3.4", 1), server=_Server())
    conn.device_imei = device.imei
    conn.authenticated = True
    return conn, writer, device


async def _reply(conn, writer, text):
    for _ in range(100):
        if writer.packets:
            break
        await asyncio.sleep(0)
    flag = struct.unpack(">I", writer.packets[-1][5:9])[0]
    await conn.handle_command_response({"content": text, "server_flag": flag})


@pytest.mark.anyio
async def test_a_models_reply_pattern_confirms_its_fuel_cut(tk_connection, db_session):
    conn, writer, device = tk_connection
    task = asyncio.create_task(conn.send_command("stop123456", timeout=2))
    await _reply(conn, writer, "Stop OK")
    await task
    db_session.refresh(device)
    assert device.relay_cut is True
    assert db_session.query(CommandLog).one().command == "stop123456"


@pytest.mark.anyio
async def test_a_deferred_cut_on_a_model_without_a_status_query_waits_for_the_heartbeat(
        tk_connection, db_session, monkeypatch):
    conn, writer, device = tk_connection
    monkeypatch.setattr(GPSTrackerConnection, "RELAY_CONFIRM_INTERVAL_S", 0.01)
    task = asyncio.create_task(conn.send_command("stop123456", timeout=2))
    await _reply(conn, writer, "will cut later")
    await task
    await asyncio.wait_for(conn._relay_confirm_task, timeout=2)
    db_session.refresh(device)
    assert device.relay_cut is False
    assert len(writer.packets) == 1, "no RELAY# — the TK903ELE has no fuel.query"
