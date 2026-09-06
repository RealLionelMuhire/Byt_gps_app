"""Geofence CRUD API — user-owned circle or polygon zones evaluated server-side.

See app/services/geofencing.py for why zones are evaluated here instead of
on the device: neither supported hardware model (TK903ELE, G900LS J16-4G)
exposes a command to push a zone definition to it.
"""

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from geoalchemy2.elements import WKTElement
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.device import Device
from app.models.geofence import Geofence
from app.models.geofence_device import GeofenceDevice
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()

MIN_RADIUS_METERS = 10
MAX_RADIUS_METERS = 50_000  # 50km — a generous ceiling for a single circle zone
MIN_POLYGON_POINTS = 3


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


@router.post("", response_model=GeofenceResponse, status_code=201)
async def create_geofence(
    body: GeofenceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _validate_device_ids(body.device_ids, user, db)

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
    db.commit()
    db.refresh(geofence)
    return _serialize(geofence, db)


@router.get("", response_model=List[GeofenceResponse])
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


@router.get("/{geofence_id}", response_model=GeofenceResponse)
async def get_geofence(
    geofence_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    geofence = _get_owned_geofence(geofence_id, user, db)
    return _serialize(geofence, db)


@router.put("/{geofence_id}", response_model=GeofenceResponse)
async def update_geofence(
    geofence_id: int,
    body: GeofenceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    geofence = _get_owned_geofence(geofence_id, user, db)
    if body.device_ids is not None:
        _validate_device_ids(body.device_ids, user, db)

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
    db.commit()
    db.refresh(geofence)
    return _serialize(geofence, db)


@router.delete("/{geofence_id}", status_code=204)
async def delete_geofence(
    geofence_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    geofence = _get_owned_geofence(geofence_id, user, db)
    db.delete(geofence)
    db.commit()
