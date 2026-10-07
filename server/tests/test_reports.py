"""
Tests for the Reports feature: the pure aggregation logic in
app/services/reports.py (in-memory fake rows, no DB) and the
GET /api/reports/{device_id} and /api/reports/fleet routes (real router
against the conftest SQLite harness).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from app.api.locations import build_period_segments
from app.models.device import Device
from app.models.location import Location
from app.models.user import Role, User
from app.services.reports import (
    build_activity,
    build_geofence_visits,
    build_overspeed_episodes,
    count_alarms,
    split_seconds_by_day,
    summarize_geofence_visits,
)


@dataclass
class FakeLoc:
    timestamp: datetime
    longitude: float
    latitude: float
    speed: Optional[float] = 0.0
    course: int = 0


@dataclass
class FakeAlarm:
    timestamp: datetime
    alarm_type: str
    geofence_name: Optional[str] = None
    road_name: Optional[str] = None
    speed: Optional[float] = 0.0
    latitude: float = -1.95
    longitude: float = 30.06


T0 = datetime(2026, 9, 1, 8, 0, 0)  # 10:00 in Kigali (UTC+2)
UTC = timedelta(0)
KIGALI = timedelta(hours=2)
STOP_SPEED = 5.0
MIN_STOP = 600


def loc(offset_s, lon, lat=-1.95, speed=0.0):
    return FakeLoc(T0 + timedelta(seconds=offset_s), lon, lat, speed)


def drive(start_s, n, start_lon, speed=40.0, step_s=10, step_lon=0.001):
    return [loc(start_s + i * step_s, start_lon + i * step_lon, speed=speed) for i in range(n)]


def park(start_s, n, lon, step_s=60):
    return [loc(start_s + i * step_s, lon, speed=0.0) for i in range(n)]


def _activity(locations, start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=10), offset=UTC):
    return build_activity(locations, STOP_SPEED, MIN_STOP, start, end, offset)


# --- split_seconds_by_day ---


def test_split_seconds_within_one_day():
    result = split_seconds_by_day(T0, T0 + timedelta(minutes=30), UTC)
    assert result == {T0.date(): 1800.0}


def test_split_seconds_across_local_midnight():
    # 21:30Z -> 22:30Z is 23:30 -> 00:30 in Kigali: half on each local day.
    start = datetime(2026, 9, 1, 21, 30)
    result = split_seconds_by_day(start, start + timedelta(hours=1), KIGALI)
    assert result == {datetime(2026, 9, 1).date(): 1800.0, datetime(2026, 9, 2).date(): 1800.0}


def test_split_seconds_empty_interval():
    assert split_seconds_by_day(T0, T0, UTC) == {}


# --- build_activity ---


def test_empty_period_has_zeroed_summary_and_every_day():
    result = _activity([], start=T0, end=T0 + timedelta(days=2))
    assert result["summary"]["distance_km"] == 0
    assert result["summary"]["drive_count"] == 0
    assert result["summary"]["avg_moving_speed_kmh"] is None
    assert [d["date"] for d in result["daily"]] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert result["drives"] == [] and result["stops"] == []


def test_drive_park_drive_produces_two_drives_and_one_stop():
    locations = drive(0, 30, 30.0) + park(300, 15, 30.03) + drive(1200, 30, 30.03)
    result = _activity(locations)
    summary = result["summary"]

    assert summary["drive_count"] == 2
    assert summary["stop_count"] == 1
    assert summary["max_speed_kmh"] == 40.0
    assert summary["first_movement_at"] == T0
    assert result["stops"][0]["duration_seconds"] == 14 * 60
    assert summary["stopped_seconds"] == 14 * 60
    assert summary["avg_moving_speed_kmh"] > 0


def test_distance_matches_period_route_total():
    """The report's distance must equal period playback's for the same
    points — both come from classify_period_spans."""
    locations = drive(0, 30, 30.0) + park(300, 15, 30.03) + drive(1200, 30, 30.03)
    segments = build_period_segments(locations, STOP_SPEED, MIN_STOP, simplify=False)
    period_total = round(sum(s["distance_km"] for s in segments if s["type"] == "driving"), 3)
    assert _activity(locations)["summary"]["distance_km"] == period_total


def test_short_dip_is_not_a_stop():
    # A 2-minute stop (red light) is under the 10-minute minimum.
    locations = drive(0, 10, 30.0) + park(100, 3, 30.01) + drive(220, 10, 30.01)
    result = _activity(locations)
    assert result["summary"]["stop_count"] == 0
    assert result["stops"] == []


def test_offline_gap_is_reported_and_adds_no_distance():
    locations = drive(0, 10, 30.0) + drive(3 * 3600, 10, 31.0)  # 3h gap, far away
    result = _activity(locations)
    assert result["summary"]["offline_count"] == 1
    assert result["offline_periods"][0]["duration_seconds"] == 3 * 3600 - 90
    # Two short drives only; never the ~110km straight-line jump.
    assert result["summary"]["distance_km"] < 5


def test_daily_buckets_follow_local_calendar():
    # Drive at 21:50Z-22:10Z = 23:50-00:10 Kigali: split across two local days.
    start_s = int((datetime(2026, 9, 1, 21, 50) - T0).total_seconds())
    locations = drive(start_s, 121, 30.0)  # 20 minutes at 10s steps
    result = _activity(
        locations, start=datetime(2026, 9, 1, 0, 0), end=datetime(2026, 9, 2, 21, 0), offset=KIGALI,
    )
    by_day = {d["date"]: d for d in result["daily"]}
    assert by_day["2026-09-01"]["driving_seconds"] == 600
    assert by_day["2026-09-02"]["driving_seconds"] == 600
    assert by_day["2026-09-01"]["drive_count"] == 1
    assert by_day["2026-09-02"]["drive_count"] == 0
    total = by_day["2026-09-01"]["distance_km"] + by_day["2026-09-02"]["distance_km"]
    assert abs(total - result["summary"]["distance_km"]) < 0.01


def test_active_days_counts_days_with_a_drive():
    day2 = 24 * 3600
    locations = drive(0, 10, 30.0) + drive(day2, 10, 30.0)
    result = _activity(locations, start=T0, end=T0 + timedelta(days=3))
    assert result["summary"]["active_days"] == 2


# --- build_overspeed_episodes ---


def test_overspeed_episode_peak_and_duration_with_limit():
    locations = [loc(0, 30.0, speed=70), loc(10, 30.001, speed=85), loc(20, 30.002, speed=97),
                 loc(30, 30.003, speed=82), loc(40, 30.004, speed=74)]
    alarms = [FakeAlarm(T0 + timedelta(seconds=10), "Over speed", speed=85, road_name="KN 5 Rd")]
    [episode] = build_overspeed_episodes(alarms, locations, speed_limit_kmh=80)
    assert episode["speed_kmh"] == 85
    assert episode["max_speed_kmh"] == 97
    # 82 is above the re-arm floor (80 - 5), so the episode runs to t=30.
    assert episode["duration_seconds"] == 20
    assert episode["road_name"] == "KN 5 Rd"


def test_overspeed_episode_stops_at_next_alarm():
    locations = [loc(i * 10, 30.0 + i * 0.001, speed=90) for i in range(6)]
    alarms = [FakeAlarm(T0, "Over speed", speed=90), FakeAlarm(T0 + timedelta(seconds=30), "Over speed", speed=90)]
    first, second = build_overspeed_episodes(alarms, locations, speed_limit_kmh=80)
    assert first["duration_seconds"] == 20
    assert second["duration_seconds"] == 20


def test_overspeed_without_limit_has_no_duration():
    alarms = [FakeAlarm(T0, "Over speed", speed=101)]
    [episode] = build_overspeed_episodes(alarms, [loc(0, 30.0, speed=101)], speed_limit_kmh=None)
    assert episode["max_speed_kmh"] == 101
    assert episode["duration_seconds"] is None


def test_overspeed_ignores_other_alarm_types():
    alarms = [FakeAlarm(T0, "SOS"), FakeAlarm(T0, "Enter fence", geofence_name="Depot")]
    assert build_overspeed_episodes(alarms, [], speed_limit_kmh=80) == []


# --- build_geofence_visits ---


def _fence(offset_s, kind, name="Depot"):
    return FakeAlarm(T0 + timedelta(seconds=offset_s), kind, geofence_name=name)


def test_enter_then_exit_is_one_visit():
    start, end = T0 - timedelta(hours=1), T0 + timedelta(hours=5)
    [visit] = build_geofence_visits([_fence(0, "Enter fence"), _fence(1800, "Exit fence")], start, end)
    assert visit["enter_time"] == T0
    assert visit["duration_seconds"] == 1800


def test_exit_without_enter_was_inside_at_period_start():
    start, end = T0 - timedelta(hours=1), T0 + timedelta(hours=5)
    [visit] = build_geofence_visits([_fence(0, "Exit fence")], start, end)
    assert visit["enter_time"] is None
    assert visit["duration_seconds"] == 3600


def test_enter_without_exit_is_clamped_to_period_end():
    start, end = T0 - timedelta(hours=1), T0 + timedelta(hours=2)
    [visit] = build_geofence_visits([_fence(0, "Enter fence")], start, end)
    assert visit["exit_time"] is None
    assert visit["duration_seconds"] == 7200


def test_hardware_fence_alarms_without_zone_name_are_ignored():
    start, end = T0, T0 + timedelta(hours=1)
    alarms = [FakeAlarm(T0, "Enter fence"), FakeAlarm(T0 + timedelta(minutes=5), "Exit fence")]
    assert build_geofence_visits(alarms, start, end) == []
    assert count_alarms(alarms) == []


def test_zones_tracked_independently_and_summarized():
    start, end = T0 - timedelta(hours=1), T0 + timedelta(hours=5)
    alarms = [
        _fence(0, "Enter fence", "Depot"), _fence(600, "Exit fence", "Depot"),
        _fence(700, "Enter fence", "Client"), _fence(4300, "Exit fence", "Client"),
        _fence(5000, "Enter fence", "Depot"), _fence(5600, "Exit fence", "Depot"),
    ]
    visits = build_geofence_visits(alarms, start, end)
    assert [v["geofence_name"] for v in visits] == ["Depot", "Client", "Depot"]
    zones = summarize_geofence_visits(visits)
    assert zones[0] == {"geofence_name": "Client", "visit_count": 1, "total_seconds": 3600}
    assert zones[1] == {"geofence_name": "Depot", "visit_count": 2, "total_seconds": 1200}


def test_count_alarms_most_frequent_first():
    alarms = [FakeAlarm(T0, "Over speed"), FakeAlarm(T0, "Over speed"), FakeAlarm(T0, "SOS")]
    assert count_alarms(alarms) == [{"alarm_type": "Over speed", "count": 2}, {"alarm_type": "SOS", "count": 1}]


# --- routes ---


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_geocoding(monkeypatch):
    """The report route geocodes drive/stop endpoints via the geocode
    cache (its own SessionLocal, i.e. the configured real DB, not this
    harness's SQLite) and warms misses in a background task that
    TestClient runs inline (real Nominatim calls). Stub both so route
    tests never leave the process: every point is a cache miss, and the
    warm-up is a no-op, so rows carry the coordinate fallback."""
    import app.api.reports as reports_api

    monkeypatch.setattr(reports_api, "reverse_geocode_many_cached_only", lambda coords, results: list(coords))
    monkeypatch.setattr(reports_api, "reverse_geocode_many", lambda coords: {})


def _owner_with_device(db_session, clerk_id="clerk_report_owner", imei="111111111111111", name="Truck"):
    user = User(
        clerk_user_id=clerk_id, email=f"{clerk_id}@example.com",
        first_name="Test", last_name="Owner", role=Role.USER,
        onboarding_step=0, onboarding_complete=False,
    )
    db_session.add(user)
    db_session.commit()
    device = Device(imei=imei, name=name, user_id=user.id, lifecycle="sold", speed_limit_kmh=80)
    db_session.add(device)
    db_session.commit()
    db_session.refresh(device)
    return user, device


def _add_fix(db_session, device, offset_s, lon, speed, **extra):
    db_session.add(Location(
        device_id=device.id, latitude=-1.95, longitude=lon, speed=speed, course=0,
        satellites=8, gps_valid=True, is_outlier=False, timestamp=T0 + timedelta(seconds=offset_s),
        **extra,
    ))


def _seed_day(db_session, device):
    for i in range(30):
        _add_fix(db_session, device, i * 10, 30.0 + i * 0.001, 40 if i != 10 else 90,
                 **({"is_alarm": True, "alarm_type": "Over speed", "road_name": "KN 3 Rd"} if i == 10 else {}))
    for i in range(15):
        _add_fix(db_session, device, 300 + i * 60, 30.03, 0)
    db_session.commit()


def _period():
    return {"start": (T0 - timedelta(hours=1)).isoformat() + "Z", "end": (T0 + timedelta(hours=5)).isoformat() + "Z"}


def test_device_report_route_returns_all_sections(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    _seed_day(db_session, device)

    response = client.get(f"/api/reports/{device.id}", params={**_period(), "tz_offset_minutes": 120})

    assert response.status_code == 200
    body = response.json()
    assert body["device_name"] == "Truck"
    assert body["summary"]["drive_count"] == 1
    assert body["summary"]["stop_count"] == 1
    assert body["summary"]["overspeed_count"] == 1
    assert body["summary"]["distance_km"] > 0
    assert body["overspeed"][0]["road_name"] == "KN 3 Rd"
    assert body["overspeed"][0]["max_speed_kmh"] == 90
    assert body["alarm_counts"] == [{"alarm_type": "Over speed", "count": 1}]
    assert body["drives"][0]["start_place"] == "-1.9500, 30.0000"  # coordinate fallback
    assert body["stops"][0]["place"] == "-1.9500, 30.0300"
    assert [d["date"] for d in body["daily"]] == ["2026-09-01"]
    assert body["truncated"]["drives"] is False


def test_device_report_rejects_inverted_and_overlong_periods(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id

    inverted = client.get(f"/api/reports/{device.id}", params={"start": "2026-09-02T00:00:00Z", "end": "2026-09-01T00:00:00Z"})
    assert inverted.status_code == 400

    too_long = client.get(f"/api/reports/{device.id}", params={"start": "2026-08-01T00:00:00Z", "end": "2026-09-05T00:00:00Z"})
    assert too_long.status_code == 400
    assert "31 days" in too_long.json()["detail"]


def test_device_report_forbidden_for_someone_elses_device(client, db_session, current_clerk_id):
    _, device = _owner_with_device(db_session)
    other, _ = _owner_with_device(db_session, clerk_id="clerk_other", imei="222222222222222")
    current_clerk_id["value"] = other.clerk_user_id

    response = client.get(f"/api/reports/{device.id}", params=_period())
    assert response.status_code in (403, 404)


def test_fleet_report_lists_only_callers_devices(client, db_session, current_clerk_id):
    user, device = _owner_with_device(db_session)
    _seed_day(db_session, device)
    _owner_with_device(db_session, clerk_id="clerk_other", imei="222222222222222", name="Not mine")
    current_clerk_id["value"] = user.clerk_user_id

    response = client.get("/api/reports/fleet", params=_period())

    assert response.status_code == 200
    rows = response.json()["devices"]
    assert [r["device_name"] for r in rows] == ["Truck"]
    assert rows[0]["summary"]["overspeed_count"] == 1
    assert rows[0]["summary"]["distance_km"] > 0
