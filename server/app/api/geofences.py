"""Geofence CRUD API — user-owned circle zones evaluated server-side.

See app/services/geofencing.py for why zones are evaluated here instead of
on the device: neither supported hardware model (TK903ELE, G900LS J16-4G)
exposes a command to push a zone definition to it.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.geofence import Geofence
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()

MIN_RADIUS_METERS = 10
MAX_RADIUS_METERS = 50_000  # 50km — a generous ceiling for a single circle zone


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


# --- Schemas ---


class GeofenceCreate(BaseModel):
    name: str
    description: Optional[str] = None
    center_latitude: float
    center_longitude: float
    radius_meters: float
    is_active: bool = True
    alert_on_enter: bool = True
    alert_on_exit: bool = True

    _check_name = field_validator("name")(_validate_name)
    _check_description = field_validator("description")(_validate_description)
    _check_latitude = field_validator("center_latitude")(_validate_latitude)
    _check_longitude = field_validator("center_longitude")(_validate_longitude)
    _check_radius = field_validator("radius_meters")(_validate_radius)


class GeofenceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    center_latitude: Optional[float] = None
    center_longitude: Optional[float] = None
    radius_meters: Optional[float] = None
    is_active: Optional[bool] = None
    alert_on_enter: Optional[bool] = None
    alert_on_exit: Optional[bool] = None

    _check_name = field_validator("name")(_validate_name)
    _check_description = field_validator("description")(_validate_description)
    _check_latitude = field_validator("center_latitude")(_validate_latitude)
    _check_longitude = field_validator("center_longitude")(_validate_longitude)
    _check_radius = field_validator("radius_meters")(_validate_radius)


class GeofenceResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    center_latitude: Optional[float] = None
    center_longitude: Optional[float] = None
    radius_meters: Optional[float] = None
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


# --- Routes ---


@router.post("", response_model=GeofenceResponse, status_code=201)
async def create_geofence(
    body: GeofenceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    geofence = Geofence(
        user_id=user.id,
        name=body.name,
        description=body.description,
        center_latitude=body.center_latitude,
        center_longitude=body.center_longitude,
        radius_meters=body.radius_meters,
        is_active=body.is_active,
        alert_on_enter=body.alert_on_enter,
        alert_on_exit=body.alert_on_exit,
    )
    db.add(geofence)
    db.commit()
    db.refresh(geofence)
    return geofence


@router.get("", response_model=List[GeofenceResponse])
async def list_geofences(
    is_active: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Geofence).filter(Geofence.user_id == user.id)
    if is_active is not None:
        query = query.filter(Geofence.is_active == is_active)
    return query.order_by(Geofence.created_at.desc()).all()


@router.get("/{geofence_id}", response_model=GeofenceResponse)
async def get_geofence(
    geofence_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _get_owned_geofence(geofence_id, user, db)


@router.put("/{geofence_id}", response_model=GeofenceResponse)
async def update_geofence(
    geofence_id: int,
    body: GeofenceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    geofence = _get_owned_geofence(geofence_id, user, db)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(geofence, field, value)
    db.commit()
    db.refresh(geofence)
    return geofence


@router.delete("/{geofence_id}", status_code=204)
async def delete_geofence(
    geofence_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    geofence = _get_owned_geofence(geofence_id, user, db)
    db.delete(geofence)
    db.commit()
