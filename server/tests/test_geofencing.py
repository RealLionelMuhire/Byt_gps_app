"""
Unit tests for app/services/geofencing.py — server-side enter/exit
transition detection, replacing reliance on the device's own (unconfigurable)
GT06 fence alarm bytes.

Uses the `db_session` fixture from conftest.py (isolated in-memory SQLite)
directly, without going through the TCP server or HTTP API, to exercise
evaluate_geofences() against a realistic ping sequence for one device.
"""

from datetime import datetime, timedelta

from geoalchemy2.elements import WKTElement

from app.models.user import User, Role
from app.models.device import Device
from app.models.geofence import Geofence
from app.models.geofence_device import GeofenceDevice
from app.models.geofence_device_state import GeofenceDeviceState
from app.models.location import Location
from app.services.geofencing import evaluate_geofences
from app.tcp_server import _apply_geofence_transitions


def make_user(db, clerk_id="clerk_geofence_owner"):
    user = User(
        clerk_user_id=clerk_id,
        email=f"{clerk_id}@example.com",
        first_name="Test",
        last_name="User",
        role=Role.USER,
        onboarding_step=0,
        onboarding_complete=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_device(db, owner: User, imei="123456789012345"):
    # -1.9, 30.05 is the Kigali coordinate already used as this device's
    # last-known location by the fixtures in test_vehicles_api.py.
    device = Device(
        imei=imei,
        name="Device 1",
        lifecycle="sold",
        user_id=owner.id,
        status="online",
        last_latitude=-1.9,
        last_longitude=30.05,
        last_update=datetime.utcnow(),
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def link_device(db, geofence: Geofence, device: Device):
    """Scope `geofence` to `device` — required for evaluate_geofences to
    consider it at all; a geofence with no GeofenceDevice row applies to
    no devices (see GeofenceDevice's docstring)."""
    db.add(GeofenceDevice(geofence_id=geofence.id, device_id=device.id))
    db.commit()


def make_geofence(db, owner: User, device: Device = None, **overrides):
    """`device`, if given, is linked via GeofenceDevice so the geofence
    actually evaluates against it — matching how a real zone must be
    explicitly assigned before it fires for anyone."""
    defaults = dict(
        user_id=owner.id,
        name="Home",
        center_latitude=-1.9,
        center_longitude=30.05,
        radius_meters=200,
        is_active=True,
        alert_on_enter=True,
        alert_on_exit=True,
    )
    defaults.update(overrides)
    geofence = Geofence(**defaults)
    db.add(geofence)
    db.commit()
    db.refresh(geofence)
    if device is not None:
        link_device(db, geofence, device)
    return geofence


# Well outside the 200m radius geofence centered on (-1.9, 30.05).
OUTSIDE = (30.10, -1.95)
# Exactly the geofence center — well inside.
INSIDE = (30.05, -1.9)
# ~40m from center (well inside a 200m radius).
INSIDE_NEARBY = (30.0505, -1.9003)


def test_first_observation_seeds_state_without_firing(db_session):
    """A device already inside a brand-new geofence the first time it's
    evaluated must not fire a spurious 'Enter fence' — only real
    transitions after the baseline should alarm."""
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    geofence = make_geofence(db_session, owner, device=device)

    transitions = evaluate_geofences(db_session, device.id, owner.id, *INSIDE)
    db_session.commit()

    assert transitions == []
    state = db_session.query(GeofenceDeviceState).filter_by(
        device_id=device.id, geofence_id=geofence.id
    ).first()
    assert state is not None
    assert state.is_inside is True


def test_enter_and_exit_fire_once_per_transition_not_per_ping(db_session):
    """The core requirement: repeated pings while inside (or outside) must
    not re-fire the alarm — only the boundary crossings themselves."""
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    make_geofence(db_session, owner, device=device)

    def ping(lon, lat):
        transitions = evaluate_geofences(db_session, device.id, owner.id, lon, lat)
        db_session.commit()
        return transitions

    # 1: baseline (outside) — no event.
    assert ping(*OUTSIDE) == []
    # 2: still outside — no event.
    assert ping(*OUTSIDE) == []
    # 3: crosses into the zone — exactly one "enter" event.
    t = ping(*INSIDE)
    assert len(t) == 1
    assert t[0].entered is True
    # 4, 5: still inside (different points within the radius) — no re-fire.
    assert ping(*INSIDE) == []
    assert ping(*INSIDE_NEARBY) == []
    # 6: crosses back out — exactly one "exit" event.
    t = ping(*OUTSIDE)
    assert len(t) == 1
    assert t[0].entered is False
    # 7: still outside — no event.
    assert ping(*OUTSIDE) == []


def test_inactive_geofence_never_fires(db_session):
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    make_geofence(db_session, owner, device=device, is_active=False)

    assert evaluate_geofences(db_session, device.id, owner.id, *OUTSIDE) == []
    db_session.commit()
    assert evaluate_geofences(db_session, device.id, owner.id, *INSIDE) == []


def test_alert_on_enter_false_suppresses_enter_but_state_still_tracks(db_session):
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    make_geofence(db_session, owner, device=device, alert_on_enter=False, alert_on_exit=True)

    def ping(lon, lat):
        transitions = evaluate_geofences(db_session, device.id, owner.id, lon, lat)
        db_session.commit()
        return transitions

    assert ping(*OUTSIDE) == []          # baseline
    assert ping(*INSIDE) == []           # enter suppressed by alert_on_enter=False
    t = ping(*OUTSIDE)                   # exit still fires — state wasn't left stale
    assert len(t) == 1
    assert t[0].entered is False


# --- Device scoping (GeofenceDevice) ---


def test_unscoped_geofence_never_fires(db_session):
    """A geofence with zero GeofenceDevice rows applies to no devices —
    not implicitly to every device the owner has."""
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    make_geofence(db_session, owner)  # no device= — deliberately unlinked

    assert evaluate_geofences(db_session, device.id, owner.id, *INSIDE) == []
    db_session.commit()
    # Even a hard crossing never fires — the zone was never assigned to
    # this device, unlike test_enter_and_exit_fire_once_per_transition_not_per_ping.
    assert evaluate_geofences(db_session, device.id, owner.id, *OUTSIDE) == []


def test_geofence_only_fires_for_linked_device_not_other_devices(db_session):
    """Two devices owned by the same user: a geofence linked to one must
    not evaluate for the other, even though both belong to the same
    owner and the same user_id filter would otherwise pass both."""
    owner = make_user(db_session)
    linked_device = make_device(db_session, owner, imei="222222222222222")
    other_device = make_device(db_session, owner, imei="333333333333333")
    make_geofence(db_session, owner, device=linked_device)

    linked_transitions = evaluate_geofences(db_session, linked_device.id, owner.id, *INSIDE)
    db_session.commit()
    assert linked_transitions == []  # first observation, seeds state

    other_transitions = evaluate_geofences(db_session, other_device.id, owner.id, *INSIDE)
    db_session.commit()
    assert other_transitions == []
    # Confirm it's exclusion, not "still seeding": no state row was ever
    # created for the unlinked device.
    assert db_session.query(GeofenceDeviceState).filter_by(device_id=other_device.id).first() is None


def test_polygon_geofence_scoping_matches_circle(db_session):
    """Same GeofenceDevice scoping applies identically on the polygon
    branch — confirms neither evaluation path can drift out of sync."""
    owner = make_user(db_session)
    linked_device = make_device(db_session, owner, imei="444444444444444")
    other_device = make_device(db_session, owner, imei="555555555555555")
    make_polygon_geofence(db_session, owner, device=linked_device)

    assert evaluate_geofences(db_session, linked_device.id, owner.id, *POLY_INSIDE) == []
    db_session.commit()
    assert evaluate_geofences(db_session, other_device.id, owner.id, *POLY_INSIDE) == []


def test_only_owner_geofences_are_evaluated(db_session):
    owner = make_user(db_session, clerk_id="clerk_owner")
    other = make_user(db_session, clerk_id="clerk_other")
    device = make_device(db_session, owner, imei="111111111111111")
    # Linked to `device` anyway, to prove the user_id filter (not device
    # scoping) is what excludes it — belongs to a different user.
    make_geofence(db_session, other, device=device)

    transitions = evaluate_geofences(db_session, device.id, owner.id, *INSIDE)
    assert transitions == []


def test_circle_fields_missing_is_ignored(db_session):
    """A geofence with no circle fields set (e.g. a polygon row) must not
    blow up circle evaluation — it's just skipped by the circle branch."""
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    geofence = Geofence(
        user_id=owner.id, name="Polygon placeholder", shape_type="polygon",
        center_latitude=None, center_longitude=None, radius_meters=None,
        geom=WKTElement(SQUARE_WKT, srid=4326),
    )
    db_session.add(geofence)
    db_session.commit()
    link_device(db_session, geofence, device)

    # POLY_OUTSIDE, not INSIDE: this geofence is the polygon below, not a
    # circle — INSIDE (the circle fixtures' center) is outside the square.
    assert evaluate_geofences(db_session, device.id, owner.id, *POLY_OUTSIDE) == []


# --- Polygon geofences ---
#
# Square ring covering lng [30.04, 30.06] x lat [-1.91, -1.89] — chosen to
# sit next to (not overlap) the circle fixtures above (centered on
# 30.05, -1.9 with a 200m radius, i.e. roughly within +-0.002 degrees of
# that point), so circle and polygon fixtures in the same test file can't
# accidentally overlap and mask a bug in either.
SQUARE_WKT = "POLYGON((30.04 -1.91,30.06 -1.91,30.06 -1.89,30.04 -1.89,30.04 -1.91))"

POLY_INSIDE = (30.05, -1.90)     # square's center — well inside.
POLY_OUTSIDE = (30.20, -1.90)    # well outside.
POLY_BOUNDARY = (30.04, -1.90)   # exactly on the square's left edge.


def make_polygon_geofence(db, owner: User, device: Device = None, wkt=SQUARE_WKT, **overrides):
    defaults = dict(
        user_id=owner.id,
        name="Polygon zone",
        shape_type="polygon",
        geom=WKTElement(wkt, srid=4326),
        is_active=True,
        alert_on_enter=True,
        alert_on_exit=True,
    )
    defaults.update(overrides)
    geofence = Geofence(**defaults)
    db.add(geofence)
    db.commit()
    db.refresh(geofence)
    if device is not None:
        link_device(db, geofence, device)
    return geofence


def test_polygon_inside_point_is_detected_as_inside(db_session):
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    geofence = make_polygon_geofence(db_session, owner, device=device)

    transitions = evaluate_geofences(db_session, device.id, owner.id, *POLY_INSIDE)
    db_session.commit()

    assert transitions == []  # first observation seeds state without firing
    state = db_session.query(GeofenceDeviceState).filter_by(
        device_id=device.id, geofence_id=geofence.id
    ).first()
    assert state.is_inside is True


def test_polygon_outside_point_is_detected_as_outside(db_session):
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    geofence = make_polygon_geofence(db_session, owner, device=device)

    transitions = evaluate_geofences(db_session, device.id, owner.id, *POLY_OUTSIDE)
    db_session.commit()

    assert transitions == []
    state = db_session.query(GeofenceDeviceState).filter_by(
        device_id=device.id, geofence_id=geofence.id
    ).first()
    assert state.is_inside is False


def test_polygon_boundary_point_is_treated_as_outside(db_session):
    """PostGIS ST_Contains excludes the boundary itself: a point exactly on
    a polygon's edge is in neither the interior nor the exterior, so
    containment is false. A device riding the fence line is "outside"."""
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    geofence = make_polygon_geofence(db_session, owner, device=device)

    transitions = evaluate_geofences(db_session, device.id, owner.id, *POLY_BOUNDARY)
    db_session.commit()

    assert transitions == []
    state = db_session.query(GeofenceDeviceState).filter_by(
        device_id=device.id, geofence_id=geofence.id
    ).first()
    assert state.is_inside is False


def test_polygon_enter_and_exit_fire_once_per_transition_not_per_ping(db_session):
    """Same requirement as the circle version above, adapted to a polygon
    zone: repeated pings while inside (or outside) must not re-fire — only
    the boundary crossings themselves, exactly once each."""
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    make_polygon_geofence(db_session, owner, device=device)

    def ping(lon, lat):
        transitions = evaluate_geofences(db_session, device.id, owner.id, lon, lat)
        db_session.commit()
        return transitions

    # 1: baseline (outside) — no event.
    assert ping(*POLY_OUTSIDE) == []
    # 2: still outside — no event.
    assert ping(*POLY_OUTSIDE) == []
    # 3: crosses into the zone — exactly one "enter" event.
    t = ping(*POLY_INSIDE)
    assert len(t) == 1
    assert t[0].entered is True
    # 4: still inside — no re-fire.
    assert ping(*POLY_INSIDE) == []
    # 5: sitting on the boundary counts as outside (ST_Contains excludes
    # it), so this does NOT re-fire "enter" — it's already "outside" from
    # step 3's perspective... except step 3 made it inside, so touching the
    # boundary now is itself an exit.
    t = ping(*POLY_BOUNDARY)
    assert len(t) == 1
    assert t[0].entered is False
    # 6: crosses back in.
    t = ping(*POLY_INSIDE)
    assert len(t) == 1
    assert t[0].entered is True
    # 7: crosses back out — exactly one "exit" event.
    t = ping(*POLY_OUTSIDE)
    assert len(t) == 1
    assert t[0].entered is False
    # 8: still outside — no event.
    assert ping(*POLY_OUTSIDE) == []


# ---------------------------------------------------------------------------
# _apply_geofence_transitions — the tcp_server.py wiring: claims the
# Location row's alarm_type slot and snapshots the fence's name onto it
# (migration 030). Same "device already committed" precondition
# evaluate_geofences itself has, since this calls straight through to it.
# ---------------------------------------------------------------------------

def _location(device, is_alarm=False, alarm_type=None):
    return Location(
        device_id=device.id, latitude=-1.9, longitude=30.05,
        is_alarm=is_alarm, alarm_type=alarm_type, timestamp=datetime.utcnow(),
    )


def test_apply_geofence_transitions_snapshots_the_fence_name_on_enter(db_session):
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    geofence = make_geofence(db_session, owner, device=device, name="Warehouse")

    # Baseline observation (outside) — seeds state, no alarm yet.
    baseline = _location(device)
    db_session.add(baseline)
    _apply_geofence_transitions(db_session, device, baseline, *OUTSIDE)
    db_session.commit()

    location = _location(device)
    db_session.add(location)
    transitions = _apply_geofence_transitions(db_session, device, location, *INSIDE)
    db_session.commit()

    assert len(transitions) == 1
    assert location.is_alarm is True
    assert location.alarm_type == "Enter fence"
    assert location.geofence_name == "Warehouse"


def test_apply_geofence_transitions_snapshots_the_fence_name_on_exit(db_session):
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    make_geofence(db_session, owner, device=device, name="Depot")

    baseline = _location(device)
    db_session.add(baseline)
    _apply_geofence_transitions(db_session, device, baseline, *INSIDE)
    db_session.commit()

    location = _location(device)
    db_session.add(location)
    _apply_geofence_transitions(db_session, device, location, *OUTSIDE)
    db_session.commit()

    assert location.alarm_type == "Exit fence"
    assert location.geofence_name == "Depot"


def test_apply_geofence_transitions_does_not_override_an_existing_alarm(db_session):
    """A hardware alarm already claimed this fix's one alarm_type slot —
    the geofence crossing must not clobber it, and geofence_name must stay
    unset since the persisted alarm isn't actually a fence event."""
    owner = make_user(db_session)
    device = make_device(db_session, owner)
    make_geofence(db_session, owner, device=device, name="Warehouse")

    baseline = _location(device)
    db_session.add(baseline)
    _apply_geofence_transitions(db_session, device, baseline, *OUTSIDE)
    db_session.commit()

    location = _location(device, is_alarm=True, alarm_type="Shock")
    db_session.add(location)
    transitions = _apply_geofence_transitions(db_session, device, location, *INSIDE)
    db_session.commit()

    # The transition is still reported (state tracking must not be
    # suppressed just because another alarm won the slot this time), but
    # the Location row itself keeps the hardware alarm's identity.
    assert len(transitions) == 1
    assert location.alarm_type == "Shock"
    assert location.geofence_name is None
