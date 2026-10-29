"""
Tests for what a customer keeps once their plan ends (2026-10-22 product
decision), in ENTITLEMENT_MODE=enforce:

- live updates (WebSocket locations) and alarm pushes stop at once;
- the map keeps the vehicle's position from when the plan ended for
  POSITION_GRACE (3 days), then it's hidden;
- history only within the plan's history.retention_days;

and that log mode (the production default until enforce is switched on)
changes nothing. Also: /ws/locations/{device_id} now checks ownership.

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

import app.api.ws as ws_module
import app.tcp_server as tcp_module
from app.models.device import Device
from app.models.entitlement import EntitlementCheckLog
from app.models.location import Location
from app.models.subscription import Subscription, SubscriptionVehicle
from app.models.trip import Trip
from app.models.user import Role
from app.services.entitlements import POSITION_GRACE, feature_allowed
from tests.test_entitlements import catalog, full_plan, mode, set_limit  # noqa: F401
from tests.test_plan_expiry_freshness import make_user
from tests.test_vehicle_subscriptions import make_vehicles

NOW = datetime.utcnow()
LIVE = (-1.95, 30.06)      # where the tracker is now
AT_END = (-1.94, 30.05)    # where it was when the plan ended


@pytest.fixture
def owner_setup(db_session, current_clerk_id, catalog, mode):
    """A customer with one tracked vehicle on a full plan; returns a
    function that puts the plan in a given state."""
    owner = make_user(db_session, "clerk_owner")
    plan = full_plan(db_session)
    [vehicle] = make_vehicles(db_session, owner, 1, with_devices=True)
    device = db_session.query(Device).filter_by(id=vehicle.device_id).one()
    device.last_latitude, device.last_longitude = LIVE
    device.last_update = NOW
    db_session.add_all([
        Location(device_id=device.id, latitude=AT_END[0], longitude=AT_END[1],
                 timestamp=NOW - timedelta(days=2, hours=1), gps_valid=True),
        Location(device_id=device.id, latitude=LIVE[0], longitude=LIVE[1],
                 timestamp=NOW - timedelta(minutes=1), gps_valid=True),
    ])
    db_session.commit()
    current_clerk_id["value"] = owner.clerk_user_id
    mode("enforce")

    def plan_state(state, *, ended_days_ago=2.0, covered=True):
        sub = Subscription(clerk_user_id=owner.clerk_user_id, plan_id=plan.id, status="active",
                           price=plan.price, started_at=NOW - timedelta(days=40), quantity=1)
        if state == "active":
            sub.expires_at = NOW + timedelta(days=20)
        elif state == "ended":
            sub.expires_at = NOW - timedelta(days=ended_days_ago)
            sub.status = "completed"
        elif state == "cancelled":
            sub.expires_at = NOW + timedelta(days=20)
            sub.status = "cancelled"
        db_session.add(sub)
        db_session.flush()
        if state == "cancelled":
            sub.updated_at = NOW - timedelta(days=ended_days_ago)
        if covered:
            db_session.add(SubscriptionVehicle(subscription_id=sub.id, vehicle_id=vehicle.id))
        db_session.commit()
        return sub

    return owner, plan, vehicle, device, plan_state


def listed_position(client):
    [d] = client.get("/api/devices/").json()
    return d["last_latitude"], d["last_longitude"], d["last_update"]


# --- Positions ------------------------------------------------------------------


def test_active_plan_shows_the_live_position(client, owner_setup):
    *_, plan_state = owner_setup
    plan_state("active")
    assert listed_position(client)[:2] == LIVE


def test_within_three_days_of_ending_the_position_is_frozen_where_the_plan_ended(
    client, db_session, owner_setup,
):
    *_, device, plan_state = owner_setup
    plan_state("ended", ended_days_ago=2)
    lat, lon, last_update = listed_position(client)
    assert (lat, lon) == AT_END
    assert last_update.startswith((NOW - timedelta(days=2, hours=1)).strftime("%Y-%m-%dT%H"))
    # Masking is on the response only — the stored live position is untouched.
    db_session.expire_all()
    assert (db_session.get(Device, device.id).last_latitude) == LIVE[0]


def test_after_three_days_the_position_disappears(client, owner_setup):
    *_, plan_state = owner_setup
    plan_state("ended", ended_days_ago=POSITION_GRACE.days + 1)
    assert listed_position(client)[:2] == (None, None)


def test_a_cancelled_plan_freezes_from_the_cancellation(client, owner_setup):
    *_, plan_state = owner_setup
    plan_state("cancelled", ended_days_ago=2)
    assert listed_position(client)[:2] == AT_END


def test_never_subscribed_or_not_covered_shows_no_position(client, owner_setup):
    *_, plan_state = owner_setup
    assert listed_position(client)[:2] == (None, None)  # never subscribed
    plan_state("active", covered=False)
    assert listed_position(client)[:2] == (None, None)


def test_device_detail_status_and_vehicle_list_are_masked_too(client, owner_setup):
    _, _, vehicle, device, plan_state = owner_setup
    plan_state("ended", ended_days_ago=POSITION_GRACE.days + 1)
    detail = client.get(f"/api/devices/{device.id}").json()
    assert (detail["last_latitude"], detail["last_longitude"]) == (None, None)
    assert client.get(f"/api/devices/{device.id}/status").json()["location"] is None
    [v] = client.get("/api/vehicles").json()["vehicles"]
    assert (v["device"]["latitude"], v["device"]["longitude"]) == (None, None)


def test_admins_still_see_live_positions(client, db_session, current_clerk_id, owner_setup):
    *_, plan_state = owner_setup
    plan_state("ended", ended_days_ago=POSITION_GRACE.days + 1)
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    current_clerk_id["value"] = admin.clerk_user_id
    assert listed_position(client)[:2] == LIVE


def test_log_mode_changes_nothing(client, owner_setup, mode):
    *_, plan_state = owner_setup
    mode("log")
    plan_state("ended", ended_days_ago=POSITION_GRACE.days + 1)
    assert listed_position(client)[:2] == LIVE


# --- Live WebSocket feed ----------------------------------------------------------


@pytest.fixture
def ws_session(db_session, monkeypatch):
    monkeypatch.setattr(ws_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    ws_module._gate_cache.clear()
    yield
    ws_module._gate_cache.clear()


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_customers_stop_getting_live_locations_and_alarms_when_the_plan_ends(
    owner_setup, ws_session, anyio_backend,
):
    owner, _, _, device, plan_state = owner_setup
    plan_state("ended")
    manager = ws_module.ConnectionManager()
    customer, admin = FakeSocket(), FakeSocket()
    await manager.connect_fleet(owner.id, {device.id}, customer)
    await manager.connect_fleet(999, {device.id}, admin, is_admin=True)

    await manager.broadcast(device.id, {"type": "location", "device_id": device.id})
    await manager.broadcast(device.id, {"type": "alarm", "device_id": device.id})
    await manager.broadcast(device.id, {"type": "device_status", "device_id": device.id})

    assert [m["type"] for m in customer.sent] == ["device_status"]
    assert [m["type"] for m in admin.sent] == ["location", "alarm", "device_status"]


def test_an_ended_plan_stops_the_feed_and_log_mode_only_records(
    db_session, owner_setup, ws_session, mode,
):
    owner, _, _, device, plan_state = owner_setup
    plan_state("ended")
    assert ws_module._customer_may_receive(device.id, "tracking.live") is False

    mode("log")
    ws_module._gate_cache.clear()
    assert ws_module._customer_may_receive(device.id, "tracking.live") is True
    assert db_session.query(EntitlementCheckLog).filter_by(feature_key="tracking.live", mode="log").count() == 1


@pytest.fixture
def ws_client(db_session, monkeypatch, ws_session):
    async def fake_resolve(token):
        return token or None

    monkeypatch.setattr(ws_module, "_resolve_clerk_user_id", fake_resolve)
    app = FastAPI()
    app.include_router(ws_module.router)
    return TestClient(app)


def test_the_per_device_socket_only_streams_your_own_vehicle(ws_client, db_session, owner_setup):
    owner, _, _, device, plan_state = owner_setup
    plan_state("active")
    make_user(db_session, "clerk_stranger")

    with pytest.raises(WebSocketDisconnect) as stranger:
        with ws_client.websocket_connect(f"/ws/locations/{device.id}?token=clerk_stranger") as ws:
            ws.receive_text()
    assert stranger.value.code == 4003

    with pytest.raises(WebSocketDisconnect) as anonymous:
        with ws_client.websocket_connect(f"/ws/locations/{device.id}") as ws:
            ws.receive_text()
    assert anonymous.value.code == 4001

    with ws_client.websocket_connect(f"/ws/locations/{device.id}?token=clerk_owner") as ws:
        ws.send_text("ping")  # accepted and open


# --- Alarm pushes -----------------------------------------------------------------


@pytest.fixture
def pushes(db_session, monkeypatch):
    sent = []

    async def fake_send(user, title, body, data=None, channel_id=None):
        sent.append(data)
        return True

    monkeypatch.setattr(tcp_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(tcp_module, "send_push_notification", fake_send)
    return sent


def alarm_row(db, device, alarm_type):
    row = Location(device_id=device.id, latitude=LIVE[0], longitude=LIVE[1], timestamp=NOW,
                   gps_valid=True, is_alarm=True, alarm_type=alarm_type)
    db.add(row)
    db.commit()
    return row


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("alarm_type", ["vibration", "sos"])
async def test_alarm_pushes_stop_when_the_plan_ends_sos_included(
    db_session, owner_setup, pushes, alarm_type, anyio_backend,
):
    *_, device, plan_state = owner_setup
    plan_state("ended")
    row = alarm_row(db_session, device, alarm_type)

    await tcp_module.TCPServer._send_push_notification(None, device.id, {"alarm_type": alarm_type}, location_id=row.id)

    assert pushes == []
    db_session.expire_all()
    assert db_session.get(Location, row.id).digested_at is not None  # never sent later by the digest


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_alarm_pushes_continue_on_an_active_plan(db_session, owner_setup, pushes, anyio_backend):
    *_, device, plan_state = owner_setup
    plan_state("active")
    row = alarm_row(db_session, device, "sos")
    await tcp_module.TCPServer._send_push_notification(None, device.id, {"alarm_type": "sos"}, location_id=row.id)
    assert [p["alarm_type"] for p in pushes] == ["sos"]


def test_feature_allowed_never_denies_admins_or_unowned_devices(db_session, catalog, mode):
    mode("enforce")
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    assert feature_allowed(db_session, owner=admin, key="alerts.push", route="t") is True
    assert feature_allowed(db_session, owner=None, key="alerts.push", route="t") is True


# --- History retention -------------------------------------------------------------


def make_trip(db, owner, device, days_ago, name):
    trip = Trip(device_id=device.id, user_id=owner.id, name=name,
                start_time=NOW - timedelta(days=days_ago), end_time=NOW - timedelta(days=days_ago) + timedelta(hours=1))
    db.add(trip)
    db.commit()
    return trip


def test_history_is_limited_to_the_plans_retention_window(client, db_session, owner_setup):
    owner, plan, _, device, plan_state = owner_setup
    plan_state("active")
    set_limit(db_session, plan, "history.retention_days", 7)
    recent = make_trip(db_session, owner, device, 2, "recent")
    old = make_trip(db_session, owner, device, 30, "old")

    names = [t["name"] for t in client.get("/api/trips", params={"device_id": device.id}).json()]
    assert names == ["recent"]
    assert [t["name"] for t in client.get(f"/api/devices/{device.id}/trips").json()] == ["recent"]

    denied = client.get(f"/api/trips/{old.id}", params={"device_id": device.id})
    assert denied.status_code == 402
    assert denied.json()["detail"]["reason"] == "over_limit"
    assert client.get(f"/api/trips/{recent.id}", params={"device_id": device.id}).status_code != 402

    db_session.add(Location(device_id=device.id, latitude=-2.5, longitude=29.0,
                            timestamp=NOW - timedelta(days=30), gps_valid=True))
    db_session.commit()
    history = client.get(f"/api/locations/{device.id}/history",
                         params={"start_time": (NOW - timedelta(days=40)).isoformat()}).json()
    latitudes = {p["latitude"] for p in history["locations"]}
    assert -2.5 not in latitudes  # 30 days old: outside the 7-day window
    assert AT_END[0] in latitudes


def test_the_device_trip_list_is_now_plan_gated(client, owner_setup):
    *_, device, plan_state = owner_setup
    plan_state("ended")
    assert client.get(f"/api/devices/{device.id}/trips").status_code == 402


# --- History while unpaid ------------------------------------------------------------


def test_trips_recorded_while_unpaid_are_hidden_then_shown_once_paid(client, db_session, owner_setup):
    owner, _, _, device, plan_state = owner_setup
    plan_state("ended", ended_days_ago=5)
    before = make_trip(db_session, owner, device, 10, "before it ended")
    unpaid = make_trip(db_session, owner, device, 1, "while unpaid")
    window = {"start_time": (NOW - timedelta(days=2)).isoformat(), "end_time": NOW.isoformat()}
    period = {"start": window["start_time"], "end": window["end_time"]}

    locked = [
        client.get("/api/trips", params={"device_id": device.id}),
        client.get(f"/api/devices/{device.id}/trips"),
        client.get(f"/api/trips/{unpaid.id}", params={"device_id": device.id}),
        client.get(f"/api/locations/{device.id}/route", params=window),
        client.get(f"/api/locations/{device.id}/period-route", params=period),
    ]
    assert [r.status_code for r in locked] == [402] * len(locked)
    detail = locked[0].json()["detail"]
    assert detail["reason"] == "subscription_expired"
    assert "still being recorded" in detail["message"]
    # Hidden, not deleted.
    assert db_session.query(Trip).filter_by(device_id=device.id).count() == 2

    plan_state("active")
    names = {t["name"] for t in client.get("/api/trips", params={"device_id": device.id}).json()}
    assert names == {"before it ended", "while unpaid"}
    assert client.get(f"/api/trips/{unpaid.id}", params={"device_id": device.id}).status_code == 200
    assert client.get(f"/api/trips/{before.id}", params={"device_id": device.id}).status_code == 200
    route = client.get(f"/api/locations/{device.id}/period-route", params=period)
    assert route.status_code == 200


@pytest.mark.parametrize("role", [Role.SUPER_ADMIN, Role.ADMIN])
def test_admins_see_an_unpaid_customers_history(client, db_session, current_clerk_id, owner_setup, role):
    owner, _, _, device, plan_state = owner_setup
    plan_state("ended", ended_days_ago=30)
    trip = make_trip(db_session, owner, device, 1, "while unpaid")
    current_clerk_id["value"] = make_user(db_session, f"clerk_{role.value.lower()}", role=role).clerk_user_id
    window = {"start_time": (NOW - timedelta(days=3)).isoformat(), "end_time": NOW.isoformat()}
    period = {"start": window["start_time"], "end": window["end_time"]}

    assert [t["name"] for t in client.get(f"/api/devices/{device.id}/trips").json()] == ["while unpaid"]
    assert client.get(f"/api/trips/{trip.id}", params={"device_id": device.id}).status_code == 200
    assert client.get(f"/api/locations/{device.id}/route", params=window).status_code == 200
    assert client.get(f"/api/locations/{device.id}/period-route", params=period).status_code == 200
    assert client.get(f"/api/reports/{device.id}", params=period).status_code == 200
