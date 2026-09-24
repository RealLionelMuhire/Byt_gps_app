"""Geofence CRUD API — user-owned circle or polygon zones evaluated server-side.

See app/services/geofencing.py for why zones are evaluated here instead of
on the device: neither supported hardware model (TK903ELE, G900LS J16-4G)
exposes a command to push a zone definition to it.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from geoalchemy2.elements import WKTElement
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user, require_device_access
from app.models.device import Device
from app.models.geofence import Geofence
from app.models.geofence_device import GeofenceDevice
from app.models.geofence_version import GeofenceVersion
from app.models.user import User
from app.services.entitlements import check_feature, check_limit, require_feature

logger = logging.getLogger(__name__)
router = APIRouter()

MIN_RADIUS_METERS = 10
MAX_RADIUS_METERS = 50_000  # 50km — a generous ceiling for a single circle zone
MIN_POLYGON_POINTS = 3
# Same ceiling as locations.py's period-route MAX_PERIOD_DAYS — history is
# only ever requested for a period Historical Routes can itself display.
MAX_HISTORY_DAYS = 30


# --- Field-level validation, shared between create (required fields) and
# update (optional fields) via field_validator(...)(fn) below, rather than
# inheritance — pydantic v2 validators don't unwrap cleanly across models. ---


def _validate_name(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip()
    if not v:
        raise ValueError("name cannot be empty")
    if len(v) > 100:
        raise ValueError("name must be at most 100 characters")
    return v


def _validate_description(v: Optional[str]) -> Optional[str]:
    if v is not None and len(v) > 500:
        raise ValueError("description must be at most 500 characters")
    return v


def _validate_latitude(v: Optional[float]) -> Optional[float]:
    if v is not None and (v < -90 or v > 90):
        raise ValueError("center_latitude must be between -90 and 90")
    return v


def _validate_longitude(v: Optional[float]) -> Optional[float]:
    if v is not None and (v < -180 or v > 180):
        raise ValueError("center_longitude must be between -180 and 180")
    return v


def _validate_radius(v: Optional[float]) -> Optional[float]:
    if v is not None and (v < MIN_RADIUS_METERS or v > MAX_RADIUS_METERS):
        raise ValueError(f"radius_meters must be between {MIN_RADIUS_METERS} and {MAX_RADIUS_METERS}")
    return v


def _validate_point_lat(v: float) -> float:
    if v < -90 or v > 90:
        raise ValueError("lat must be between -90 and 90")
    return v


def _validate_point_lng(v: float) -> float:
    if v < -180 or v > 180:
        raise ValueError("lng must be between -180 and 180")
    return v


# --- Schemas ---


class PointIn(BaseModel):
    lat: float
    lng: float

    _check_lat = field_validator("lat")(_validate_point_lat)
    _check_lng = field_validator("lng")(_validate_point_lng)


class PointOut(BaseModel):
    lat: float
    lng: float


class GeofenceCreate(BaseModel):
    name: str
    description: Optional[str] = None
    shape_type: Literal["circle", "polygon"] = "circle"
    center_latitude: Optional[float] = None
    center_longitude: Optional[float] = None
    radius_meters: Optional[float] = None
    points: Optional[List[PointIn]] = None
    # No devices linked by default — a geofence applies to none until
    # explicitly assigned (see GeofenceDevice), not implicitly to the
    # owner's whole fleet.
    device_ids: List[int] = []
    is_active: bool = True
    alert_on_enter: bool = True
    alert_on_exit: bool = True

    _check_name = field_validator("name")(_validate_name)
    _check_description = field_validator("description")(_validate_description)
    _check_latitude = field_validator("center_latitude")(_validate_latitude)
    _check_longitude = field_validator("center_longitude")(_validate_longitude)
    _check_radius = field_validator("radius_meters")(_validate_radius)

    @model_validator(mode="after")
    def _check_shape(self):
        if self.shape_type == "circle":
            if self.center_latitude is None or self.center_longitude is None or self.radius_meters is None:
                raise ValueError("circle geofences require center_latitude, center_longitude, and radius_meters")
            if self.points is not None:
                raise ValueError("circle geofences must not include points")
        else:
            if self.points is None or len(self.points) < MIN_POLYGON_POINTS:
                raise ValueError(f"polygon geofences require at least {MIN_POLYGON_POINTS} points")
            if self.center_latitude is not None or self.center_longitude is not None or self.radius_meters is not None:
                raise ValueError("polygon geofences must not include circle fields")
        return self


class GeofenceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    shape_type: Optional[Literal["circle", "polygon"]] = None
    center_latitude: Optional[float] = None
    center_longitude: Optional[float] = None
    radius_meters: Optional[float] = None
    points: Optional[List[PointIn]] = None
    # None = leave device assignments untouched (partial-update default).
    # [] explicitly clears all assignments. A non-empty list replaces the
    # full assigned set.
    device_ids: Optional[List[int]] = None
    is_active: Optional[bool] = None
    alert_on_enter: Optional[bool] = None
    alert_on_exit: Optional[bool] = None

    _check_name = field_validator("name")(_validate_name)
    _check_description = field_validator("description")(_validate_description)
    _check_latitude = field_validator("center_latitude")(_validate_latitude)
    _check_longitude = field_validator("center_longitude")(_validate_longitude)
    _check_radius = field_validator("radius_meters")(_validate_radius)

    @model_validator(mode="after")
    def _check_shape(self):
        if self.shape_type == "circle" and self.points is not None:
            raise ValueError("circle geofences must not include points")
        if self.shape_type == "polygon":
            if self.points is not None and len(self.points) < MIN_POLYGON_POINTS:
                raise ValueError(f"polygon geofences require at least {MIN_POLYGON_POINTS} points")
            if self.center_latitude is not None or self.center_longitude is not None or self.radius_meters is not None:
                raise ValueError("polygon geofences must not include circle fields")
        return self


class GeofenceResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    shape_type: str
    center_latitude: Optional[float] = None
    center_longitude: Optional[float] = None
    radius_meters: Optional[float] = None
    points: Optional[List[PointOut]] = None
    device_ids: List[int] = []
    is_active: bool
    alert_on_enter: bool
    alert_on_exit: bool

    class Config:
        from_attributes = True


class GeofenceActivePeriod(BaseModel):
    # Real (unclipped) bounds, so the client can label "active since 22 Jun
    # 14:00" even when that's before the requested range. active_to None =
    # still active now.
    active_from: datetime
    active_to: Optional[datetime] = None


class GeofenceHistoryEntry(BaseModel):
    geofence_id: int
    name: str
    shape_type: str
    center_latitude: Optional[float] = None
    center_longitude: Optional[float] = None
    radius_meters: Optional[float] = None
    points: Optional[List[PointOut]] = None
    periods: List[GeofenceActivePeriod]


# --- Helpers ---


def _get_owned_geofence(geofence_id: int, user: User, db: Session) -> Geofence:
    """Raise 404 unless the geofence exists and belongs to `user`.

    404 rather than 403 for a non-owned geofence, matching this codebase's
    existing convention (require_device_access) of not distinguishing
    "doesn't exist" from "not yours".
    """
    geofence = db.query(Geofence).filter(Geofence.id == geofence_id).first()
    if not geofence or geofence.user_id != user.id:
        raise HTTPException(status_code=404, detail="Geofence not found")
    return geofence


def _build_polygon_wkt(points: List[PointIn]) -> str:
    """Build a closed PostGIS POLYGON WKT ring from input points, auto-closing
    it (repeating the first point as the last) if the caller didn't already."""
    coords = [(p.lng, p.lat) for p in points]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    ring = ",".join(f"{lng} {lat}" for lng, lat in coords)
    return f"POLYGON(({ring}))"


def _parse_polygon_wkt(wkt: str) -> List[PointOut]:
    """Parse a PostGIS WKT "POLYGON((lng lat,lng lat,...))" string (as
    returned by ST_AsText) back into an open ring of {lat, lng} points,
    dropping the redundant closing point that duplicates the first."""
    ring_text = wkt[wkt.index("((") + 2 : wkt.rindex("))")]
    coords = [tuple(map(float, pair.strip().split())) for pair in ring_text.split(",")]
    if len(coords) > 1 and coords[0] == coords[-1]:
        coords = coords[:-1]
    return [PointOut(lng=lng, lat=lat) for lng, lat in coords]


def _validate_device_ids(device_ids: List[int], user: User, db: Session) -> None:
    """Raise 400 unless every id in device_ids is a device owned by `user`.

    400 (not 404) since this is a validation failure on the request body,
    not a lookup of one resource — matching this file's use of 400 for
    other cross-field shape mismatches above.
    """
    unique_ids = set(device_ids)
    if not unique_ids:
        return
    owned_count = (
        db.query(Device.id)
        .filter(Device.id.in_(unique_ids), Device.user_id == user.id)
        .count()
    )
    if owned_count != len(unique_ids):
        raise HTTPException(status_code=400, detail="one or more device_ids are invalid or not owned by you")


def _set_device_links(geofence: Geofence, device_ids: List[int], db: Session) -> None:
    """Replace the full set of devices this geofence is scoped to."""
    db.query(GeofenceDevice).filter(GeofenceDevice.geofence_id == geofence.id).delete()
    for device_id in set(device_ids):
        db.add(GeofenceDevice(geofence_id=geofence.id, device_id=device_id))


def _utcnow() -> datetime:
    """Naive UTC, matching every other timestamp column in this schema."""
    return datetime.utcnow()


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


_VERSIONED_FIELDS = (
    "name", "shape_type", "center_latitude", "center_longitude",
    "radius_meters", "points", "device_ids", "is_active",
)


def _version_snapshot(response: "GeofenceResponse") -> dict:
    """The subset of a geofence's state that Historical Routes draws.
    Description and alert_on_enter/exit are deliberately excluded — they
    don't change what's on the map, so editing them shouldn't split
    history into a new version."""
    return dict(
        name=response.name,
        shape_type=response.shape_type,
        center_latitude=response.center_latitude,
        center_longitude=response.center_longitude,
        radius_meters=response.radius_meters,
        points=[p.model_dump() for p in response.points] if response.points is not None else None,
        device_ids=sorted(response.device_ids),
        is_active=response.is_active,
    )


def _open_version(geofence_id: int, db: Session) -> Optional[GeofenceVersion]:
    return (
        db.query(GeofenceVersion)
        .filter(GeofenceVersion.geofence_id == geofence_id, GeofenceVersion.valid_to.is_(None))
        .order_by(GeofenceVersion.valid_from.desc())
        .first()
    )


def _record_version(geofence: Geofence, response: "GeofenceResponse", db: Session) -> None:
    """Close the geofence's current version and open a new one, unless
    nothing Historical Routes cares about changed. Call inside the same
    transaction as the geofence write itself, so the two can't diverge."""
    snapshot = _version_snapshot(response)
    current = _open_version(geofence.id, db)
    if current is not None:
        if all(getattr(current, f) == snapshot[f] for f in _VERSIONED_FIELDS):
            return
        now = _utcnow()
        current.valid_to = now
    else:
        now = _utcnow()
    db.add(GeofenceVersion(geofence_id=geofence.id, user_id=geofence.user_id, valid_from=now, **snapshot))


def _close_version(geofence: Geofence, db: Session) -> None:
    current = _open_version(geofence.id, db)
    if current is not None:
        current.valid_to = _utcnow()


def _merge_history(versions: List[GeofenceVersion], device_id: int) -> List[GeofenceHistoryEntry]:
    """Group the active, device-scoped versions of each geofence by shape,
    merging back-to-back windows (e.g. a device reassignment that kept this
    device, or a rename) into one period, so an unchanged zone is drawn
    once rather than once per version. Name is the latest one in range."""
    entries: dict = {}
    for v in sorted(versions, key=lambda v: v.valid_from):
        if not v.is_active or device_id not in (v.device_ids or []):
            continue
        shape_key = (
            v.geofence_id, v.shape_type, v.center_latitude, v.center_longitude,
            v.radius_meters, repr(v.points),
        )
        entry = entries.get(shape_key)
        if entry is None:
            entries[shape_key] = entry = dict(
                geofence_id=v.geofence_id,
                name=v.name,
                shape_type=v.shape_type,
                center_latitude=v.center_latitude,
                center_longitude=v.center_longitude,
                radius_meters=v.radius_meters,
                points=v.points,
                periods=[],
            )
        entry["name"] = v.name
        periods = entry["periods"]
        if periods and periods[-1]["active_to"] == v.valid_from:
            periods[-1]["active_to"] = v.valid_to
        else:
            periods.append(dict(active_from=v.valid_from, active_to=v.valid_to))

    def aware(value: Optional[datetime]) -> Optional[datetime]:
        # Stored naive-UTC; serialized with an explicit offset so clients
        # don't have to guess.
        return value.replace(tzinfo=timezone.utc) if value is not None else None

    for e in entries.values():
        e["periods"] = [
            dict(active_from=aware(p["active_from"]), active_to=aware(p["active_to"])) for p in e["periods"]
        ]
    return [GeofenceHistoryEntry(**e) for e in entries.values()]


def _serialize(geofence: Geofence, db: Session) -> GeofenceResponse:
    """Build the response, returning only the fields relevant to this row's
    shape_type rather than both shapes' fields populated meaninglessly."""
    data = dict(
        id=geofence.id,
        name=geofence.name,
        description=geofence.description,
        shape_type=geofence.shape_type,
        is_active=geofence.is_active,
        alert_on_enter=geofence.alert_on_enter,
        alert_on_exit=geofence.alert_on_exit,
    )
    if geofence.shape_type == "circle":
        data.update(
            center_latitude=geofence.center_latitude,
            center_longitude=geofence.center_longitude,
            radius_meters=geofence.radius_meters,
        )
    else:
        wkt = db.query(func.ST_AsText(Geofence.geom)).filter(Geofence.id == geofence.id).scalar()
        data["points"] = _parse_polygon_wkt(wkt)
    data["device_ids"] = [
        d for (d,) in db.query(GeofenceDevice.device_id).filter(GeofenceDevice.geofence_id == geofence.id).all()
    ]
    return GeofenceResponse(**data)


# --- Routes ---


@router.post("", response_model=GeofenceResponse, status_code=201, dependencies=[require_feature("geofences.enabled")])
async def create_geofence(
    body: GeofenceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _validate_device_ids(body.device_ids, user, db)
    zone_count = db.query(Geofence).filter(Geofence.user_id == user.id).count()
    check_limit(db, owner=user, actor=user, key="geofences.max_zones", current=zone_count,
                route="POST /api/geofences")
    if body.shape_type == "polygon":
        check_feature(db, owner=user, actor=user, key="geofences.polygon", route="POST /api/geofences")

    geofence = Geofence(
        user_id=user.id,
        name=body.name,
        description=body.description,
        shape_type=body.shape_type,
        is_active=body.is_active,
        alert_on_enter=body.alert_on_enter,
        alert_on_exit=body.alert_on_exit,
    )
    if body.shape_type == "circle":
        geofence.center_latitude = body.center_latitude
        geofence.center_longitude = body.center_longitude
        geofence.radius_meters = body.radius_meters
    else:
        geofence.geom = WKTElement(_build_polygon_wkt(body.points), srid=4326)
    db.add(geofence)
    db.flush()  # assigns geofence.id, needed by the device links below, before the single commit
    _set_device_links(geofence, body.device_ids, db)
    db.flush()
    response = _serialize(geofence, db)
    _record_version(geofence, response, db)
    db.commit()
    return response


@router.get("", response_model=List[GeofenceResponse], dependencies=[require_feature("geofences.enabled")])
async def list_geofences(
    is_active: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Geofence).filter(Geofence.user_id == user.id)
    if is_active is not None:
        query = query.filter(Geofence.is_active == is_active)
    geofences = query.order_by(Geofence.created_at.desc()).all()
    return [_serialize(g, db) for g in geofences]


# Declared before /{geofence_id} so "history" isn't parsed as an id.
@router.get("/history", response_model=List[GeofenceHistoryEntry], dependencies=[require_feature("geofences.enabled")])
async def get_geofence_history(
    device_id: int = Query(...),
    start: datetime = Query(..., description="Period start (UTC)"),
    end: datetime = Query(..., description="Period end (UTC)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Geofences that were active AND assigned to `device_id` at any point in
    [start, end], with the exact windows they were in effect — for drawing
    zones on a Historical Routes map. A zone created after `end`, or only
    active before `start`, is excluded; a zone disabled or deleted mid-period
    is included with its window ending at that moment. Scoped to the
    device's owner's zones, so an admin viewing someone's device sees that
    owner's zones, not their own.
    """
    start = _to_naive_utc(start)
    end = _to_naive_utc(end)
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if (end - start) > timedelta(days=MAX_HISTORY_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"Period exceeds max of {MAX_HISTORY_DAYS} days; narrow the start/end range.",
        )

    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user)

    # Only versions overlapping [start, end]. device_ids is filtered in
    # Python (JSON containment isn't portable across Postgres/SQLite, and a
    # user's version count is small).
    versions = (
        db.query(GeofenceVersion)
        .filter(
            GeofenceVersion.user_id == device.user_id,
            GeofenceVersion.valid_from < end,
            (GeofenceVersion.valid_to.is_(None)) | (GeofenceVersion.valid_to > start),
        )
        .all()
    )
    return _merge_history(versions, device.id)


@router.get("/{geofence_id}", response_model=GeofenceResponse, dependencies=[require_feature("geofences.enabled")])
async def get_geofence(
    geofence_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    geofence = _get_owned_geofence(geofence_id, user, db)
    return _serialize(geofence, db)


@router.put("/{geofence_id}", response_model=GeofenceResponse, dependencies=[require_feature("geofences.enabled")])
async def update_geofence(
    geofence_id: int,
    body: GeofenceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    geofence = _get_owned_geofence(geofence_id, user, db)
    if body.device_ids is not None:
        _validate_device_ids(body.device_ids, user, db)
    if body.shape_type == "polygon" or body.points is not None:
        check_feature(db, owner=user, actor=user, key="geofences.polygon",
                      route="PUT /api/geofences/{geofence_id}")

    data = body.model_dump(exclude_unset=True)
    data.pop("points", None)
    data.pop("device_ids", None)
    points = body.points
    new_shape = data.pop("shape_type", None)
    effective_shape = new_shape or geofence.shape_type

    circle_fields_sent = any(f in data for f in ("center_latitude", "center_longitude", "radius_meters"))
    if circle_fields_sent and effective_shape != "circle":
        raise HTTPException(status_code=400, detail="circle fields require shape_type='circle'")
    if points is not None and effective_shape != "polygon":
        raise HTTPException(status_code=400, detail="points require shape_type='polygon'")

    if new_shape == "circle":
        merged = {
            f: data.get(f, getattr(geofence, f) if geofence.shape_type == "circle" else None)
            for f in ("center_latitude", "center_longitude", "radius_meters")
        }
        if any(v is None for v in merged.values()):
            raise HTTPException(
                status_code=422,
                detail="switching to a circle geofence requires center_latitude, center_longitude, and radius_meters",
            )
        geofence.shape_type = "circle"
        geofence.geom = None
        geofence.center_latitude = merged["center_latitude"]
        geofence.center_longitude = merged["center_longitude"]
        geofence.radius_meters = merged["radius_meters"]
        data.pop("center_latitude", None)
        data.pop("center_longitude", None)
        data.pop("radius_meters", None)
    elif new_shape == "polygon" or points is not None:
        if not points or len(points) < MIN_POLYGON_POINTS:
            raise HTTPException(
                status_code=422,
                detail=f"polygon geofences require at least {MIN_POLYGON_POINTS} points",
            )
        geofence.shape_type = "polygon"
        geofence.geom = WKTElement(_build_polygon_wkt(points), srid=4326)
        geofence.center_latitude = None
        geofence.center_longitude = None
        geofence.radius_meters = None

    for field, value in data.items():
        setattr(geofence, field, value)
    if body.device_ids is not None:
        _set_device_links(geofence, body.device_ids, db)
    db.flush()
    response = _serialize(geofence, db)
    _record_version(geofence, response, db)
    db.commit()
    return response


@router.delete("/{geofence_id}", status_code=204, dependencies=[require_feature("geofences.enabled")])
async def delete_geofence(
    geofence_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    geofence = _get_owned_geofence(geofence_id, user, db)
    _close_version(geofence, db)
    db.delete(geofence)
    db.commit()
