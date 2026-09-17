"""
Tests for the user-configurable speed-limit feature: evaluate_speed_limit
(app/services/speed_limit.py), its wiring into TCPServer.handle_location
via _apply_speed_limit_check (app/tcp_server.py), the
GET/PUT /api/devices/{device_id}/speed_limit endpoints, and the
alarm_rules.py "over speed" key fix this feature depends on for correct
severity/label/mute behavior.

Uses the `db_session`/`client`/`current_clerk_id` fixtures from
conftest.py for the model-level and route-level tests, matching
test_geofencing.py and test_vehicles_api.py's own patterns.
"""

import asyncio
from datetime import datetime

import app.tcp_server as tcp_server_module
from app.models.device import Device
from app.models.location import Location
from app.models.user import User, Role
from app.services.geocoding import format_fallback_location
from app.services.speed_limit import evaluate_speed_limit
from app.services.alarm_rules import ALARM_LABELS, ALARM_SETTING_FIELDS, get_severity
from app.tcp_server import _apply_speed_limit_check, _resolve_overspeed_road_name


# ---------------------------------------------------------------------------
# evaluate_speed_limit — pure edge-detection logic, no DB needed beyond a
# plain (unpersisted) Device instance.
# ---------------------------------------------------------------------------

def _device(speed_limit_kmh=None, is_overspeeding=False):
    return Device(
        imei="123456789012345", name="Test", speed_limit_kmh=speed_limit_kmh,
        is_overspeeding=is_overspeeding,
    )


def test_no_threshold_configured_never_fires():
    device = _device(speed_limit_kmh=None)
    assert evaluate_speed_limit(device, speed_kmh=200, gps_valid=True) is False
    assert device.is_overspeeding is False


def test_under_the_limit_does_not_fire():
    device = _device(speed_limit_kmh=80)
    assert evaluate_speed_limit(device, speed_kmh=60, gps_valid=True) is False
    assert device.is_overspeeding is False


def test_crossing_above_the_limit_fires_exactly_once():
    device = _device(speed_limit_kmh=80, is_overspeeding=False)
    assert evaluate_speed_limit(device, speed_kmh=95, gps_valid=True) is True
    assert device.is_overspeeding is True

    # Still over the limit on the next fix — must not fire again, or a
    # vehicle cruising over the limit would synthesize a new alarm on
    # nearly every single fix.
    assert evaluate_speed_limit(device, speed_kmh=90, gps_valid=True) is False
    assert device.is_overspeeding is True


def test_dropping_back_under_resolves_without_firing_and_a_later_crossing_fires_again():
    device = _device(speed_limit_kmh=80, is_overspeeding=True)
    assert evaluate_speed_limit(device, speed_kmh=70, gps_valid=True) is False
    assert device.is_overspeeding is False

    # Crossing again after resolving is a genuinely new event.
    assert evaluate_speed_limit(device, speed_kmh=85, gps_valid=True) is True
    assert device.is_overspeeding is True


def test_hysteresis_blip_back_toward_the_limit_does_not_disarm():
    """A fix that dips back under the bare limit, but not all the way to
    limit - HYSTERESIS_KMH, must not re-arm -- this is the noisy-GPS-speed
    chatter case: without the margin, this same blip would silently
    disarm and the very next over-limit fix would fire a spurious repeat
    alarm for what is really one continuous overspeed episode."""
    device = _device(speed_limit_kmh=80, is_overspeeding=True)
    assert evaluate_speed_limit(device, speed_kmh=78, gps_valid=True) is False
    assert device.is_overspeeding is True

    # Still armed, so climbing back over the limit fires nothing new.
    assert evaluate_speed_limit(device, speed_kmh=90, gps_valid=True) is False
    assert device.is_overspeeding is True


def test_hysteresis_drop_past_the_floor_disarms_and_a_later_crossing_fires_again():
    device = _device(speed_limit_kmh=80, is_overspeeding=True)
    # 75 == 80 - HYSTERESIS_KMH(5) -- right at the floor, must disarm.
    assert evaluate_speed_limit(device, speed_kmh=75, gps_valid=True) is False
    assert device.is_overspeeding is False

    assert evaluate_speed_limit(device, speed_kmh=85, gps_valid=True) is True
    assert device.is_overspeeding is True


def test_first_ever_fix_already_over_the_limit_still_fires():
    """Unlike evaluate_geofences' "don't fire on first observation" rule —
    an owner setting a threshold while already speeding should be told
    immediately, not have that first crossing silently swallowed."""
    device = _device(speed_limit_kmh=80, is_overspeeding=False)
    assert evaluate_speed_limit(device, speed_kmh=120, gps_valid=True) is True


def test_invalid_gps_fix_is_ignored_entirely():
    device = _device(speed_limit_kmh=80, is_overspeeding=False)
    assert evaluate_speed_limit(device, speed_kmh=150, gps_valid=False) is False
    assert device.is_overspeeding is False


def test_exactly_at_the_limit_does_not_count_as_over():
    device = _device(speed_limit_kmh=80, is_overspeeding=False)
    assert evaluate_speed_limit(device, speed_kmh=80, gps_valid=True) is False
    assert device.is_overspeeding is False


# ---------------------------------------------------------------------------
# _apply_speed_limit_check — the tcp_server.py wiring: claims the Location
# row's single alarm_type slot only when nothing else already has, but the
# underlying is_overspeeding state always updates regardless.
# ---------------------------------------------------------------------------

def _location(device, is_alarm=False, alarm_type=None):
    return Location(
        device_id=device.id, latitude=-1.9, longitude=30.05,
        is_alarm=is_alarm, alarm_type=alarm_type, timestamp=datetime.utcnow(),
    )


def test_apply_speed_limit_check_claims_the_alarm_slot_on_a_fresh_location(db_session):
    device = Device(imei="1", name="Test", speed_limit_kmh=80)
    db_session.add(device)
    db_session.commit()

    location = _location(device)
    fired = _apply_speed_limit_check(device, location, speed_kmh=100, gps_valid=True)

    assert fired is True
    assert location.is_alarm is True
    assert location.alarm_type == "Over speed"
    assert device.is_overspeeding is True


def test_apply_speed_limit_check_does_not_override_an_existing_alarm(db_session):
    """A hardware alarm or geofence transition already claimed this fix's
    one alarm_type slot — speed_limit must not clobber it, even though the
    vehicle really is also over the limit."""
    device = Device(imei="1", name="Test", speed_limit_kmh=80)
    db_session.add(device)
    db_session.commit()

    location = _location(device, is_alarm=True, alarm_type="Shock")
    fired = _apply_speed_limit_check(device, location, speed_kmh=100, gps_valid=True)

    assert fired is False
    assert location.alarm_type == "Shock"
    # State still updates for the next fix, regardless of who won the slot.
    assert device.is_overspeeding is True


# ---------------------------------------------------------------------------
# _resolve_overspeed_road_name — a bounded, cache-first-then-live
# reverse-geocode lookup for a just-fired overspeed alarm's position (see
# the function's docstring in tcp_server.py and the design note on the
# overspeed block in handle_location). Never raises and never returns
# None: a resolved name wins, anything else falls back to formatted
# coordinates.
# ---------------------------------------------------------------------------

def test_resolve_overspeed_road_name_returns_the_resolved_name(monkeypatch):
    device = Device(imei="1", name="Test", speed_limit_kmh=80)
    location = _location(device)

    monkeypatch.setattr(tcp_server_module, "reverse_geocode", lambda lat, lon: "KN 247 Street")

    road_name = asyncio.run(_resolve_overspeed_road_name(location))

    assert road_name == "KN 247 Street"


def test_location_road_name_column_round_trips(db_session):
    """Migration 039: road_name persists on the Location row (mirroring
    geofence_name/migration 030), so a historical "Over speed" alarm reopened
    later in GET /{device_id}/alarms can still show the road, not just the
    live WS/push alert at fire time."""
    device = Device(imei="1", name="Test", speed_limit_kmh=80)
    db_session.add(device)
    db_session.commit()

    location = _location(device, is_alarm=True, alarm_type="Over speed")
    location.road_name = "KN 247 Street"
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    assert location.road_name == "KN 247 Street"


def test_resolve_overspeed_road_name_falls_back_to_coordinates_when_unresolved(monkeypatch):
    """A genuine no-address-found response (or any other non-exception
    miss) must fall back to a formatted coordinate string, never None —
    every overspeed alert has to carry *some* location text."""
    device = Device(imei="1", name="Test", speed_limit_kmh=80)
    location = _location(device)

    monkeypatch.setattr(tcp_server_module, "reverse_geocode", lambda lat, lon: None)

    road_name = asyncio.run(_resolve_overspeed_road_name(location))

    assert road_name == format_fallback_location(location.latitude, location.longitude)


def test_resolve_overspeed_road_name_falls_back_on_timeout(monkeypatch):
    """A Nominatim call that hangs past OVERSPEED_GEOCODE_TIMEOUT_SECONDS
    must not block the alert — it falls back to coordinates instead of
    propagating the timeout."""
    import time

    device = Device(imei="1", name="Test", speed_limit_kmh=80)
    location = _location(device)

    def slow_lookup(lat, lon):
        # Short, not the real hang duration -- asyncio.run()'s shutdown
        # still waits for this background thread to actually finish (a
        # timed-out wait_for cancels the *await*, not the thread), so
        # keeping this small keeps the test fast.
        time.sleep(0.3)
        return "Should never be seen"

    monkeypatch.setattr(tcp_server_module, "reverse_geocode", slow_lookup)
    monkeypatch.setattr(tcp_server_module, "OVERSPEED_GEOCODE_TIMEOUT_SECONDS", 0.05)

    road_name = asyncio.run(_resolve_overspeed_road_name(location))

    assert road_name == format_fallback_location(location.latitude, location.longitude)


# ---------------------------------------------------------------------------
# GET/PUT /api/devices/{device_id}/speed_limit
# ---------------------------------------------------------------------------

def _make_owner_and_device(db_session, clerk_id="clerk_speed_owner"):
    user = User(
        clerk_user_id=clerk_id, email=f"{clerk_id}@example.com",
        first_name="Test", last_name="Owner", role=Role.USER,
        onboarding_step=0, onboarding_complete=False,
    )
    db_session.add(user)
    db_session.commit()

    device = Device(imei="123456789012345", name="Test Device", user_id=user.id, lifecycle="sold")
    db_session.add(device)
    db_session.commit()
    db_session.refresh(device)
    return user, device


def test_get_speed_limit_defaults_to_null(client, db_session, current_clerk_id):
    user, device = _make_owner_and_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id

    response = client.get(f"/api/devices/{device.id}/speed_limit")

    assert response.status_code == 200
    assert response.json() == {"device_id": device.id, "speed_limit_kmh": None}


def test_put_speed_limit_sets_it_and_get_reflects_it(client, db_session, current_clerk_id):
    user, device = _make_owner_and_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id

    put_response = client.put(f"/api/devices/{device.id}/speed_limit", json={"speed_limit_kmh": 100})
    assert put_response.status_code == 200
    assert put_response.json()["speed_limit_kmh"] == 100

    get_response = client.get(f"/api/devices/{device.id}/speed_limit")
    assert get_response.json()["speed_limit_kmh"] == 100


def test_put_speed_limit_null_clears_it(client, db_session, current_clerk_id):
    user, device = _make_owner_and_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    client.put(f"/api/devices/{device.id}/speed_limit", json={"speed_limit_kmh": 100})

    response = client.put(f"/api/devices/{device.id}/speed_limit", json={"speed_limit_kmh": None})

    assert response.status_code == 200
    assert response.json()["speed_limit_kmh"] is None


def test_put_speed_limit_rejects_zero_or_negative(client, db_session, current_clerk_id):
    user, device = _make_owner_and_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id

    for bad_value in (0, -5):
        response = client.put(f"/api/devices/{device.id}/speed_limit", json={"speed_limit_kmh": bad_value})
        assert response.status_code == 422

    # Never persisted.
    assert client.get(f"/api/devices/{device.id}/speed_limit").json()["speed_limit_kmh"] is None


def test_speed_limit_endpoints_are_owner_or_admin_only(client, db_session, current_clerk_id):
    """require_device_access answers a non-owner with 404, not 403 — same
    "don't reveal whether the device even exists" convention every other
    device endpoint uses (see require_device_access's own docstring)."""
    _, device = _make_owner_and_device(db_session, clerk_id="clerk_owner")
    other = User(
        clerk_user_id="clerk_other", email="other@example.com",
        first_name="Other", last_name="User", role=Role.USER,
        onboarding_step=0, onboarding_complete=False,
    )
    db_session.add(other)
    db_session.commit()

    current_clerk_id["value"] = "clerk_other"
    assert client.get(f"/api/devices/{device.id}/speed_limit").status_code == 404
    assert client.put(f"/api/devices/{device.id}/speed_limit", json={"speed_limit_kmh": 100}).status_code == 404


def test_speed_limit_404s_for_an_unknown_device(client, db_session, current_clerk_id):
    user, _device = _make_owner_and_device(db_session)
    current_clerk_id["value"] = user.clerk_user_id
    assert client.get("/api/devices/999999/speed_limit").status_code == 404


# ---------------------------------------------------------------------------
# alarm_rules.py "over speed" key fix — the exact bug this feature would
# otherwise have inherited (see alarm_rules.py's module docstring). Every
# real alarm_type this backend produces for overspeed (hardware GT06 byte
# 0x06 and this feature's own synthesized alarms) is the two-word
# "Over speed", which TCPServer._send_push_notification lowercases before
# looking it up — so the dict keys must be "over speed", not "overspeed".
# ---------------------------------------------------------------------------

def test_over_speed_resolves_to_high_severity_not_the_medium_default():
    assert get_severity("over speed") == "high"


def test_over_speed_has_its_own_label_not_the_generic_fallback():
    assert "over speed" in ALARM_LABELS
    title, body = ALARM_LABELS["over speed"]
    assert "Overspeed" in title


def test_over_speed_maps_to_the_real_mute_toggle_column():
    assert ALARM_SETTING_FIELDS["over speed"] == "overspeed_push_enabled"
