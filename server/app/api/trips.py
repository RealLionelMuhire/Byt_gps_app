"""Trip API endpoints - saved trips"""

import asyncio
import logging
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator, model_validator

from app.core.database import get_db
from app.models.trip import Trip
from app.api.locations import (
    verify_device_access,
    get_user_from_clerk_id,
    compute_distance_for_device_time_range,
    fetch_route_line_for_range,
)
from app.services.geocoding import build_trip_display_name

logger = logging.getLogger(__name__)
router = APIRouter()

# Max time for geocoding (2 calls × ~5s + 1s delay + buffer); fallback if exceeded
GEOCODING_TIMEOUT_SECONDS = 15


def require_authenticated_user(x_clerk_user_id: Optional[str], db: Session):
    """Require Clerk auth; return User or raise 401."""
    if not x_clerk_user_id:
        raise HTTPException(status_code=401, detail="Authentication required (X-Clerk-User-Id)")
    user = get_user_from_clerk_id(x_clerk_user_id, db)
    if not user:
        raise HTTPException(status_code=401, detail="User not found. Ensure user is synced from Clerk.")
    return user


# --- Schemas ---


class TripCreate(BaseModel):
    device_id: int
    name: str
    start_time: datetime
    end_time: datetime

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name cannot be empty")
        return v

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class TripResponse(BaseModel):
    id: int
    device_id: int
    user_id: int
    name: str
    display_name: Optional[str] = None
    start_time: datetime
    end_time: datetime
    total_distance_km: float
    created_at: datetime

    class Config:
        from_attributes = True


class TripDetailResponse(TripResponse):
    device_name: str
    device_imei: str
    route: dict


# --- Endpoints ---


@router.post("", response_model=TripResponse, status_code=201)
async def create_trip(
    body: TripCreate,
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db),
):
    """
    Create a saved trip from a device's location history.

    Requires authentication. Validates device ownership, time range, and presence of location data.
    """
    user = require_authenticated_user(x_clerk_user_id, db)

    # Verify device access
    device = verify_device_access(body.device_id, x_clerk_user_id, db)

    # Compute distance and ensure locations exist
    total_distance, locations = compute_distance_for_device_time_range(
        body.device_id, body.start_time, body.end_time, db
    )

    if not locations:
        raise HTTPException(
            status_code=400,
            detail="No GPS-valid location points found in the specified time range",
        )

    # Optional: store first/last location IDs
    start_location_id = locations[0].id if locations else None
    end_location_id = locations[-1].id if locations else None

    # Reverse-geocode start/end for human-readable display name (max 2 API calls)
    display_name = None
    try:
        start_loc = locations[0]
        end_loc = locations[-1]
        display_name = await asyncio.wait_for(
            asyncio.to_thread(
                build_trip_display_name,
                start_loc.latitude,
                start_loc.longitude,
                end_loc.latitude,
                end_loc.longitude,
            ),
            timeout=GEOCODING_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning("Geocoding failed, using coordinate fallback: %s", e)
        display_name = (
            f"{locations[0].latitude:.4f}, {locations[0].longitude:.4f} → "
            f"{locations[-1].latitude:.4f}, {locations[-1].longitude:.4f}"
        )

    trip = Trip(
        device_id=body.device_id,
        user_id=user.id,
        name=body.name,
        display_name=display_name,
        start_time=body.start_time,
        end_time=body.end_time,
        total_distance_km=total_distance,
        start_location_id=start_location_id,
        end_location_id=end_location_id,
    )
    db.add(trip)
    db.commit()
    db.refresh(trip)

    return trip


@router.get("", response_model=List[TripResponse])
async def list_trips(
    device_id: Optional[int] = Query(None, description="Filter by device"),
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db),
):
    """
    List saved trips for the authenticated user.

    Optional device_id filter.
    """
    user = require_authenticated_user(x_clerk_user_id, db)

    query = db.query(Trip).filter(Trip.user_id == user.id)

    if device_id is not None:
        # Verify user has access to this device
        verify_device_access(device_id, x_clerk_user_id, db)
        query = query.filter(Trip.device_id == device_id)

    trips = query.order_by(Trip.created_at.desc()).all()
    return trips


@router.get("/{trip_id}", response_model=TripDetailResponse)
async def get_trip(
    trip_id: int,
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db),
):
    """
    Get trip metadata and route geometry.
    """
    user = require_authenticated_user(x_clerk_user_id, db)

    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    if trip.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied to this trip")

    # Reuse route-line logic
    route = fetch_route_line_for_range(
        trip.device_id, trip.start_time, trip.end_time, db
    )
    route["properties"]["device_id"] = trip.device.id
    route["properties"]["device_name"] = trip.device.name
    route["properties"]["device_imei"] = trip.device.imei

    return TripDetailResponse(
        id=trip.id,
        device_id=trip.device_id,
        user_id=trip.user_id,
        name=trip.name,
        display_name=trip.display_name,
        start_time=trip.start_time,
        end_time=trip.end_time,
        total_distance_km=trip.total_distance_km,
        created_at=trip.created_at,
        device_name=trip.device.name,
        device_imei=trip.device.imei,
        route=route,
    )


@router.delete("/{trip_id}", status_code=204)
async def delete_trip(
    trip_id: int,
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db),
):
    """
    Delete a saved trip. Only the trip owner can delete.
    Location data is not affected.
    """
    user = require_authenticated_user(x_clerk_user_id, db)

    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    if trip.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied to this trip")

    db.delete(trip)
    db.commit()
    return None
