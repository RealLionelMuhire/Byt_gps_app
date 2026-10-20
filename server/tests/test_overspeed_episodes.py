"""
Tests for overspeed episodes (migration 055): the OverspeedEpisode rows
app/services/speed_limit.py opens/closes alongside the "Over speed" alarm,
the noise safeguard, the history endpoints that return them (with episode
boundaries pinned through simplification), the alarm endpoints' new
`overspeed` field, and `location_id` on the WS/push alarm payloads.

Fixes are fed through tcp_server's real _apply_speed_limit_check in the
same order handle_location uses (add + flush Location, evaluate, commit),
so the boundaries asserted here are the ones the live server records.
"""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import app.api.locations as locations_api
import app.tcp_server as tcp_server_module
from app.api.locations import douglas_peucker_pinned
from app.models.device import Device
from app.models.location import Location
from app.models.overspeed_episode import OverspeedEpisode
from app.models.user import User, Role
from app.tcp_server import TCPServer, _apply_speed_limit_check

T0 = datetime(2026, 10, 1, 8, 0, 0)
STEP = 10  # seconds between fixes


@pytest.fixture(autouse=True)
def _no_real_geocoding(monkeypatch):
    """period-route geocodes driving-segment endpoints (cache + background
    warm-up against the real DB/Nominatim) — stub both, same as
    test_reports.py does for the report route."""
    monkeypatch.setattr(locations_api, "reverse_geocode_many_cached_only", lambda coords, results: list(coords))
    monkeypatch.setattr(locations_api, "reverse_geocode_many", lambda coords: {})


def _owner_with_device(db_session, clerk_id="clerk_os_owner", imei="222222222222222", limit=80):
    user = User(
        clerk_user_id=clerk_id, email=f"{clerk_id}@example.com",
        first_name="Test", last_name="Owner", role=Role.USER,
        onboarding_step=0, onboarding_complete=False,
    )
    db_session.add(user)
    db_session.commit()
    device = Device(imei=imei, name="Truck", user_id=user.id, lifecycle="sold", speed_limit_kmh=limit)
    db_session.add(device)
    db_session.commit()
    db_session.refresh(device)
    return user, device


def _fix(db_session, device, at, speed, lon, is_outlier=False, gps_valid=True, alarm_type=None):
    """One fix, processed exactly like TCPServer.handle_location does."""
    location = Location(
        device_id=device.id, latitude=-1.95, longitude=lon, speed=speed, course=90,
        satellites=8, gps_valid=gps_valid, is_outlier=is_outlier, timestamp=at,
        is_alarm=alarm_type is not None, alarm_type=alarm_type,
    )
    db_session.add(location)
    db_session.flush()
    _apply_speed_limit_check(db_session, device, location, speed, gps_valid, is_outlier)
    db_session.commit()
    return location


def _drive(db_session, device, speeds, start=T0, lon0=30.0):
    """Feed one fix per speed, STEP seconds apart, along a straight line
    heading east (straight on purpose: Douglas-Peucker would drop every
    interior point, which is what the pinning tests rely on)."""
    return [
        _fix(db_session, device, start + timedelta(seconds=i * STEP), speed, lon0 + i * 0.0005)
        for i, speed in enumerate(speeds)
    ]


def _episodes(db_session, device):
    return (
        db_session.query(OverspeedEpisode)
        .filter(OverspeedEpisode.device_id == device.id)
        .order_by(OverspeedEpisode.started_at)
        .all()
    )


def _alarms(db_session, device):
    return (
        db_session.query(Location)
        .filter(Location.device_id == device.id, Location.alarm_type == "Over speed")
        .order_by(Location.timestamp)
        .all()
    )


def _iso(dt):
    return dt.isoformat() + "Z"


# ---------------------------------------------------------------------------
# Episode lifecycle — boundaries must equal the alarm's own fixes.
# ---------------------------------------------------------------------------

def test_no_overspeed_records_no_episode(db_session):
    _, device = _owner_with_device(db_session)
    _drive(db_session, device, [40, 60, 79, 80, 70])  # exactly 80 is not over
    assert _episodes(db_session, device) == []
    assert _alarms(db_session, device) == []


def test_one_episode_starts_on_the_alarm_fix_and_ends_on_the_first_fix_under_the_floor(db_session):
    _, device = _owner_with_device(db_session)
    fixes = _drive(db_session, device, [50, 60, 85, 90, 78, 77, 74, 60])

    [episode] = _episodes(db_session, device)
    [alarm] = _alarms(db_session, device)
    assert alarm.id == fixes[2].id
    assert episode.alarm_location_id == alarm.id
    assert episode.start_location_id == alarm.id
    assert episode.started_at == alarm.timestamp == fixes[2].timestamp
    # 78/77 sit inside the 5 km/h hysteresis band (> 75): still the same
    # episode. 74 is the first fix at/under 80 - 5, so it closes there.
    assert episode.ended_at == fixes[6].timestamp
    assert episode.end_reason == "speed_dropped"
    assert episode.threshold_kmh == 80
    assert episode.hysteresis_kmh == 5
    assert episode.max_speed_kmh == 90
    assert (episode.end_latitude, episode.end_longitude) == (fixes[6].latitude, fixes[6].longitude)
    assert device.is_overspeeding is False


def test_multiple_episodes_each_get_their_own_row_and_alarm(db_session):
    _, device = _owner_with_device(db_session)
    fixes = _drive(db_session, device, [60, 90, 95, 70, 60, 88, 92, 99, 50, 40])

    first, second = _episodes(db_session, device)
    alarms = _alarms(db_session, device)
    assert [a.id for a in alarms] == [fixes[1].id, fixes[5].id]
    assert (first.started_at, first.ended_at) == (fixes[1].timestamp, fixes[3].timestamp)
    assert (second.started_at, second.ended_at) == (fixes[5].timestamp, fixes[8].timestamp)
    assert first.alarm_location_id == fixes[1].id
    assert second.alarm_location_id == fixes[5].id
    assert second.max_speed_kmh == 99


def test_episode_still_in_progress_at_the_end_of_the_data_stays_open(db_session):
    _, device = _owner_with_device(db_session)
    fixes = _drive(db_session, device, [50, 85, 90, 95])

    [episode] = _episodes(db_session, device)
    assert episode.started_at == fixes[1].timestamp
    assert episode.ended_at is None
    assert episode.end_reason is None
    assert episode.last_fix_at == fixes[3].timestamp
    assert episode.max_speed_kmh == 95
    assert device.is_overspeeding is True


def test_hysteresis_blip_does_not_split_the_episode(db_session):
    _, device = _owner_with_device(db_session)
    fixes = _drive(db_session, device, [85, 79, 82, 76, 70])

    [episode] = _episodes(db_session, device)
    assert len(_alarms(db_session, device)) == 1
    assert (episode.started_at, episode.ended_at) == (fixes[0].timestamp, fixes[4].timestamp)


# ---------------------------------------------------------------------------
# Noise safeguard.
# ---------------------------------------------------------------------------

def test_a_single_outlier_fix_cannot_fire_an_alarm_or_open_an_episode(db_session):
    _, device = _owner_with_device(db_session)
    _fix(db_session, device, T0, 50, 30.0)
    _fix(db_session, device, T0 + timedelta(seconds=10), 150, 30.2, is_outlier=True)
    _fix(db_session, device, T0 + timedelta(seconds=20), 55, 30.001)

    assert _episodes(db_session, device) == []
    assert _alarms(db_session, device) == []


def test_an_implausible_reported_speed_is_ignored(db_session):
    _, device = _owner_with_device(db_session)
    _drive(db_session, device, [50, 240, 55])
    assert _episodes(db_session, device) == []
    assert _alarms(db_session, device) == []


def test_an_invalid_or_outlier_fix_mid_episode_neither_extends_nor_closes_it(db_session):
    _, device = _owner_with_device(db_session)
    start = _fix(db_session, device, T0, 85, 30.0)
    _fix(db_session, device, T0 + timedelta(seconds=10), 0, 30.5, is_outlier=True)
    _fix(db_session, device, T0 + timedelta(seconds=20), 0, 30.001, gps_valid=False)
    over = _fix(db_session, device, T0 + timedelta(seconds=30), 90, 30.002)
    end = _fix(db_session, device, T0 + timedelta(seconds=40), 60, 30.003)

    [episode] = _episodes(db_session, device)
    assert episode.started_at == start.timestamp
    assert episode.ended_at == end.timestamp
    assert episode.max_speed_kmh == 90
    assert over.alarm_type is None  # still the one episode, no second alarm


def test_signal_lost_mid_episode_ends_at_the_last_over_limit_fix(db_session):
    _, device = _owner_with_device(db_session)
    fixes = _drive(db_session, device, [85, 90])
    _fix(db_session, device, fixes[-1].timestamp + timedelta(minutes=20), 30, 30.05)

    [episode] = _episodes(db_session, device)
    assert episode.ended_at == fixes[1].timestamp
    assert episode.end_reason == "signal_lost"
    assert (episode.end_latitude, episode.end_longitude) == (fixes[1].latitude, fixes[1].longitude)
    assert device.is_overspeeding is False


def test_the_episode_is_recorded_even_when_another_alarm_holds_the_slot(db_session):
    _, device = _owner_with_device(db_session)
    shock = _fix(db_session, device, T0, 95, 30.0, alarm_type="Shock")

    [episode] = _episodes(db_session, device)
    assert shock.alarm_type == "Shock"
    assert episode.alarm_location_id is None
    assert episode.started_at == shock.timestamp


def test_changing_the_limit_mid_episode_closes_it_with_its_own_threshold(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    fixes = _drive(db_session, device, [85, 90])

    assert client.put(f"/api/devices/{device.id}/speed_limit", json={"speed_limit_kmh": 100}).status_code == 200
    db_session.refresh(device)
    [closed] = _episodes(db_session, device)
    assert closed.end_reason == "limit_changed"
    assert closed.ended_at == fixes[1].timestamp
    assert closed.threshold_kmh == 80
    assert device.is_overspeeding is False

    # Next fix is judged fresh against the new limit.
    _fix(db_session, device, T0 + timedelta(seconds=30), 105, 30.002)
    closed_again, reopened = _episodes(db_session, device)
    assert reopened.threshold_kmh == 100
    assert reopened.ended_at is None


def test_saving_the_same_limit_does_not_close_the_episode(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    _drive(db_session, device, [85, 90])

    client.put(f"/api/devices/{device.id}/speed_limit", json={"speed_limit_kmh": 80})
    [episode] = _episodes(db_session, device)
    assert episode.ended_at is None


def test_clearing_the_limit_mid_episode_closes_it(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    fixes = _drive(db_session, device, [85, 90])

    client.put(f"/api/devices/{device.id}/speed_limit", json={"speed_limit_kmh": None})
    [episode] = _episodes(db_session, device)
    assert episode.end_reason == "limit_changed"
    assert episode.ended_at == fixes[1].timestamp


# ---------------------------------------------------------------------------
# Simplification keeps episode boundaries.
# ---------------------------------------------------------------------------

def test_douglas_peucker_pinned_keeps_pinned_points_on_a_straight_line():
    points = [
        Location(latitude=-1.95, longitude=30.0 + i * 0.001, timestamp=T0 + timedelta(seconds=i))
        for i in range(10)
    ]
    pins = frozenset({points[3].timestamp, points[7].timestamp})

    kept = douglas_peucker_pinned(points, epsilon=0.0001, pins=pins)

    assert [p.timestamp for p in kept] == [points[i].timestamp for i in (0, 3, 7, 9)]


# ---------------------------------------------------------------------------
# History endpoints.
# ---------------------------------------------------------------------------

def _route(client, device, **params):
    return client.get(
        f"/api/locations/{device.id}/route",
        params={"start_time": _iso(T0 - timedelta(minutes=5)), "end_time": _iso(T0 + timedelta(hours=1)),
                "simplify": "true", **params},
    )


def test_route_returns_episodes_and_keeps_their_boundary_points(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    fixes = _drive(db_session, device, [60, 90, 95, 70, 60, 88, 92, 99, 50, 40])

    body = _route(client, device).json()

    episodes = body["overspeed_episodes"]
    assert [(e["started_at"], e["ended_at"]) for e in episodes] == [
        (fixes[1].timestamp.isoformat(), fixes[3].timestamp.isoformat()),
        (fixes[5].timestamp.isoformat(), fixes[8].timestamp.isoformat()),
    ]
    assert episodes[0]["alarm_location_id"] == fixes[1].id
    assert episodes[0]["threshold_kmh"] == 80
    assert episodes[0]["in_progress"] is False
    # A straight line simplifies to its two endpoints — except the pinned
    # episode boundaries, which must survive.
    timestamps = [f["properties"]["timestamp"] for f in body["features"]]
    assert timestamps == [fixes[i].timestamp.isoformat() for i in (0, 1, 3, 5, 8, 9)]


def test_route_with_no_overspeed_returns_an_empty_list(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    _drive(db_session, device, [40, 50, 60])

    assert _route(client, device).json()["overspeed_episodes"] == []


def test_route_reports_an_in_progress_episode(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    fixes = _drive(db_session, device, [50, 85, 90, 95])

    [episode] = _route(client, device).json()["overspeed_episodes"]
    assert episode["in_progress"] is True
    assert episode["ended_at"] is None
    assert episode["last_fix_at"] == fixes[3].timestamp.isoformat()


def test_route_includes_an_episode_that_started_before_the_window(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    fixes = _drive(db_session, device, [85, 90, 95, 99, 60])

    body = _route(client, device, start_time=_iso(fixes[2].timestamp)).json()
    [episode] = body["overspeed_episodes"]
    assert episode["started_at"] == fixes[0].timestamp.isoformat()


def test_period_route_returns_episodes_and_pins_boundaries_in_driving_segments(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    fixes = _drive(db_session, device, [60, 90, 95, 70, 60, 60])

    response = client.get(
        f"/api/locations/{device.id}/period-route",
        params={"start": _iso(T0 - timedelta(minutes=5)), "end": _iso(T0 + timedelta(hours=1))},
    )
    assert response.status_code == 200
    body = response.json()
    [episode] = body["overspeed_episodes"]
    assert (episode["started_at"], episode["ended_at"]) == (
        fixes[1].timestamp.isoformat(), fixes[3].timestamp.isoformat(),
    )
    driving_timestamps = [
        p["timestamp"] for s in body["segments"] if s["type"] == "driving" for p in s["points"]
    ]
    assert fixes[1].timestamp.isoformat() in driving_timestamps
    assert fixes[3].timestamp.isoformat() in driving_timestamps


def test_history_endpoints_404_for_a_device_the_caller_cannot_see(client, db_session, current_clerk_id):
    _, device = _owner_with_device(db_session)
    _drive(db_session, device, [60, 90, 60])
    other, _ = _owner_with_device(db_session, clerk_id="clerk_os_other", imei="333333333333333")
    current_clerk_id["value"] = other.clerk_user_id

    assert _route(client, device).status_code == 404
    period = client.get(
        f"/api/locations/{device.id}/period-route",
        params={"start": _iso(T0 - timedelta(minutes=5)), "end": _iso(T0 + timedelta(hours=1))},
    )
    assert period.status_code == 404


# ---------------------------------------------------------------------------
# Alarm endpoints carry the episode.
# ---------------------------------------------------------------------------

def test_alarms_carry_their_episode_and_legacy_alarms_carry_null(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    # An overspeed alarm from before migration 055 (or the tracker's own
    # firmware alarm): a plain alarm row with no episode.
    legacy = Location(
        device_id=device.id, latitude=-1.95, longitude=30.0, speed=95, course=0, satellites=8,
        gps_valid=True, is_outlier=False, timestamp=T0 - timedelta(minutes=30),
        is_alarm=True, alarm_type="Over speed", road_name="KN 3 Rd",
    )
    db_session.add(legacy)
    db_session.commit()
    fixes = _drive(db_session, device, [60, 90, 95, 70])

    response = client.get(
        f"/api/locations/{device.id}/alarms",
        params={"start_time": _iso(T0 - timedelta(hours=1)), "end_time": _iso(T0 + timedelta(hours=1))},
    )
    assert response.status_code == 200
    by_id = {a["id"]: a for a in response.json()}
    assert by_id[legacy.id]["overspeed"] is None
    overspeed = by_id[fixes[1].id]["overspeed"]
    assert overspeed["started_at"] == fixes[1].timestamp.isoformat()
    assert overspeed["ended_at"] == fixes[3].timestamp.isoformat()
    assert overspeed["threshold_kmh"] == 80
    assert overspeed["max_speed_kmh"] == 95

    acknowledged = client.post(f"/api/locations/{fixes[1].id}/acknowledge").json()
    assert acknowledged["overspeed"]["id"] == overspeed["id"]
    assert client.post(f"/api/locations/{legacy.id}/acknowledge").json()["overspeed"] is None


def test_device_report_uses_the_recorded_episode(client, db_session, current_clerk_id, monkeypatch):
    import app.api.reports as reports_api

    monkeypatch.setattr(reports_api, "reverse_geocode_many_cached_only", lambda coords, results: list(coords))
    monkeypatch.setattr(reports_api, "reverse_geocode_many", lambda coords: {})
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    fixes = _drive(db_session, device, [60, 90, 95, 78, 70])
    # The owner raises the limit afterwards: the report must still show the
    # episode as it was judged then (ended at the 70 fix), not re-judge it.
    device.speed_limit_kmh = 120
    db_session.commit()

    body = client.get(
        f"/api/reports/{device.id}",
        params={"start": _iso(T0 - timedelta(hours=1)), "end": _iso(T0 + timedelta(hours=1))},
    ).json()
    [row] = body["overspeed"]
    assert row["max_speed_kmh"] == 95
    assert row["duration_seconds"] == (fixes[4].timestamp - fixes[1].timestamp).total_seconds()


# ---------------------------------------------------------------------------
# WS + push payloads carry the alarm's location_id.
# ---------------------------------------------------------------------------

def test_broadcast_alarm_includes_location_id_on_ws_and_push(db_session, monkeypatch):
    user, device = _owner_with_device(db_session)
    user.fcm_token = "fcm-test-token"
    db_session.commit()
    [_, alarm] = _drive(db_session, device, [60, 90])

    monkeypatch.setattr(tcp_server_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    pushed = []

    async def fake_send_push_notification(user, title, body, data, channel_id=None):
        pushed.append(data)
        return True

    monkeypatch.setattr(tcp_server_module, "send_push_notification", fake_send_push_notification)
    broadcasts = []

    class FakeWsManager:
        async def broadcast(self, device_id, payload):
            broadcasts.append(payload)

    server = TCPServer()
    server.ws_manager = FakeWsManager()

    asyncio.run(server.broadcast_alarm(
        device.id,
        {"alarm_type": "Over speed", "timestamp": alarm.timestamp, "latitude": alarm.latitude,
         "longitude": alarm.longitude, "road_name": "KN 3 Rd"},
        location_id=alarm.id,
    ))

    assert broadcasts[0]["location_id"] == alarm.id
    assert pushed[0]["location_id"] == alarm.id
