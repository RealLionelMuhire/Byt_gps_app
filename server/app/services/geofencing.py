"""
Server-side geofence evaluation.

Runs on every incoming location fix, in place of relying on the device's
own GT06 fence alarm bytes (0x04 enter / 0x05 exit) — neither supported
hardware model (TK903ELE, G900LS J16-4G) exposes a command to push a zone
definition to the device, so those bytes can never be configured from this
backend (see docs/usage/CONFIGURATION_GUIDE.md's Alarms sections).

Two shapes, both feeding the same transition-detection/dedup logic below:
  - circle: distance-based (haversine) check against
    Geofence.center_latitude/longitude/radius_meters.
  - polygon: PostGIS ST_Contains(geom, point) — note this excludes the
    boundary itself (a point exactly on the polygon's edge is "outside"),
    matching PostGIS's containment semantics.
"""

from typing import List, NamedTuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.geofence import Geofence
from app.models.geofence_device_state import GeofenceDeviceState
from app.api.locations import haversine_km


class GeofenceTransition(NamedTuple):
    geofence: Geofence
    entered: bool  # True = enter, False = exit


def evaluate_geofences(
    db: Session, device_id: int, user_id: int, lon: float, lat: float,
) -> List[GeofenceTransition]:
    """
    Compare (lat, lon) against every active geofence owned by user_id
    (circle or polygon), using each geofence's *persisted* inside/outside
    state for
    this device (geofence_device_state) to fire only on transitions —
    not on every ping while the device stays inside or outside a zone.

    The first observation of a given (device, geofence) pair seeds the
    state without firing an event: otherwise a device already inside a
    brand-new geofence would fire a spurious "Enter fence" the moment
    it's first evaluated.

    Mutates `db` (adds/updates GeofenceDeviceState rows) but does not
    commit — the caller is expected to commit alongside its own writes
    (e.g. the Location row for this ping) in the same transaction.
    """
    if user_id is None:
        return []

    circle_geofences = (
        db.query(Geofence)
        .filter(
            Geofence.user_id == user_id,
            Geofence.is_active == True,  # noqa: E712
            Geofence.shape_type == "circle",
            Geofence.center_latitude.isnot(None),
            Geofence.center_longitude.isnot(None),
            Geofence.radius_meters.isnot(None),
        )
        .all()
    )
    polygon_geofences = (
        db.query(Geofence)
        .filter(
            Geofence.user_id == user_id,
            Geofence.is_active == True,  # noqa: E712
            Geofence.shape_type == "polygon",
            Geofence.geom.isnot(None),
        )
        .all()
    )
    geofences = circle_geofences + polygon_geofences
    if not geofences:
        return []

    # Batched, like the circle fetch above: one query covers every polygon
    # zone for this user rather than one ST_Contains round-trip per zone.
    inside_polygon_ids = set()
    if polygon_geofences:
        point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
        inside_polygon_ids = {
            gid
            for (gid,) in db.query(Geofence.id)
            .filter(
                Geofence.id.in_([g.id for g in polygon_geofences]),
                func.ST_Contains(Geofence.geom, point),
            )
            .all()
        }

    states = {
        s.geofence_id: s
        for s in db.query(GeofenceDeviceState)
        .filter(
            GeofenceDeviceState.device_id == device_id,
            GeofenceDeviceState.geofence_id.in_([g.id for g in geofences]),
        )
        .all()
    }

    transitions: List[GeofenceTransition] = []
    for gf in geofences:
        if gf.shape_type == "circle":
            distance_m = haversine_km(gf.center_longitude, gf.center_latitude, lon, lat) * 1000
            is_inside = distance_m <= gf.radius_meters
        else:
            is_inside = gf.id in inside_polygon_ids

        state = states.get(gf.id)
        if state is None:
            db.add(GeofenceDeviceState(device_id=device_id, geofence_id=gf.id, is_inside=is_inside))
            continue

        if is_inside != state.is_inside:
            # Ground truth is always kept current, even if the flag below
            # suppresses the alert — otherwise a muted enter would leave
            # state stale and the next real exit would never fire either.
            state.is_inside = is_inside
            if (is_inside and gf.alert_on_enter) or (not is_inside and gf.alert_on_exit):
                transitions.append(GeofenceTransition(geofence=gf, entered=is_inside))

    return transitions
