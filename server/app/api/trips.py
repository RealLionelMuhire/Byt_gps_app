"""Trip API endpoints - saved trips"""

import asyncio
import logging
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator, model_validator

from app.core.database import get_db
from app.models.trip import Trip
from app.models.trip_settings import TripSettings
from app.models.user import User
from app.api.locations import (
    verify_device_access,
    compute_distance_for_device_time_range,
    fetch_route_line_for_range,
)
from app.services.geocoding import build_trip_display_name
from app.services.trip_detection import detect_trip_segments, SuggestedTrip

logger = logging.getLogger(__name__)
router = APIRouter()

# Max time for geocoding (2 calls × ~5s + 1s delay + buffer); fallback if exceeded
GEOCODING_TIMEOUT_SECONDS = 15

# Default trip settings (used when user has none)
DEFAULT_STOP_SPLITS_MINUTES = 60
DEFAULT_MIN_TRIP_MINUTES = 5
DEFAULT_STOP_SPEED_KMH = 5.0


def get_default_user(db: Session) -> User:
    """Get or create default user (no Clerk auth)."""
    user = db.query(User).first()
    if not user:
        user = User(
            clerk_user_id="default",
            email="default@local",
            name="Default User",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_or_create_trip_settings(user_id: int, db: Session) -> TripSettings:
    """Get user's trip settings, or create defaults."""
    settings = db.query(TripSettings).filter(TripSettings.user_id == user_id).first()
    if not settings:
        settings = TripSettings(
            user_id=user_id,
            stop_splits_trip_after_minutes=DEFAULT_STOP_SPLITS_MINUTES,
            minimum_trip_duration_minutes=DEFAULT_MIN_TRIP_MINUTES,
            stop_speed_threshold_kmh=DEFAULT_STOP_SPEED_KMH,
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


# --- Schemas ---


class TripSettingsResponse(BaseModel):
    stop_splits_trip_after_minutes: int
    minimum_trip_duration_minutes: int
    stop_speed_threshold_kmh: float

    class Config:
        from_attributes = True


class TripSettingsUpdate(BaseModel):
    stop_splits_trip_after_minutes: Optional[int] = None
    minimum_trip_duration_minutes: Optional[int] = None
    stop_speed_threshold_kmh: Optional[float] = None

    @field_validator("stop_splits_trip_after_minutes")
    @classmethod
    def stop_splits_valid(cls, v):
        if v is not None and (v < 1 or v > 10080):  # 1 min to 1 week
            raise ValueError("Must be between 1 and 10080 minutes")
        return v

    @field_validator("minimum_trip_duration_minutes")
    @classmethod
    def min_trip_valid(cls, v):
        if v is not None and (v < 0 or v > 1440):  # 0 to 24h
            raise ValueError("Must be between 0 and 1440 minutes")
        return v

    @field_validator("stop_speed_threshold_kmh")
    @classmethod
    def speed_valid(cls, v):
        if v is not None and (v < 0 or v > 200):
            raise ValueError("Must be between 0 and 200 km/h")
        return v


class SuggestedTripResponse(BaseModel):
    start_time: datetime
    end_time: datetime
    point_count: int
    total_distance_km: float
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float


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
# Note: /settings and /suggested must be defined before /{trip_id}


@router.get("/settings", response_model=TripSettingsResponse)
async def get_trip_settings(db: Session = Depends(get_db)):
    """Get trip segmentation settings (uses default user)."""
    user = get_default_user(db)
    settings = get_or_create_trip_settings(user.id, db)
    return TripSettingsResponse(
        stop_splits_trip_after_minutes=settings.stop_splits_trip_after_minutes,
        minimum_trip_duration_minutes=settings.minimum_trip_duration_minutes,
        stop_speed_threshold_kmh=settings.stop_speed_threshold_kmh,
    )


@router.put("/settings", response_model=TripSettingsResponse)
async def update_trip_settings(
    body: TripSettingsUpdate,
    db: Session = Depends(get_db),
):
    """Update trip segmentation settings (uses default user)."""
    user = get_default_user(db)
    settings = get_or_create_trip_settings(user.id, db)
    if body.stop_splits_trip_after_minutes is not None:
        settings.stop_splits_trip_after_minutes = body.stop_splits_trip_after_minutes
    if body.minimum_trip_duration_minutes is not None:
        settings.minimum_trip_duration_minutes = body.minimum_trip_duration_minutes
    if body.stop_speed_threshold_kmh is not None:
        settings.stop_speed_threshold_kmh = body.stop_speed_threshold_kmh
    db.commit()
    db.refresh(settings)
    return TripSettingsResponse(
        stop_splits_trip_after_minutes=settings.stop_splits_trip_after_minutes,
        minimum_trip_duration_minutes=settings.minimum_trip_duration_minutes,
        stop_speed_threshold_kmh=settings.stop_speed_threshold_kmh,
    )


@router.get("/suggested", response_model=List[SuggestedTripResponse])
async def get_suggested_trips(
    device_id: int = Query(..., description="Device ID"),
    start_time: Optional[datetime] = Query(None, description="Start of time range (UTC)"),
    end_time: Optional[datetime] = Query(None, description="End of time range (UTC)"),
    db: Session = Depends(get_db),
):
    """
    Get suggested trip segments based on settings.

    Segments location history by stop duration: a stop longer than
    stop_splits_trip_after_minutes splits into a new trip.
    """
    user = get_default_user(db)
    device = verify_device_access(device_id, None, db)
    settings = get_or_create_trip_settings(user.id, db)

    if not start_time:
        from datetime import timedelta
        start_time = datetime.utcnow() - timedelta(hours=24)
    if not end_time:
        end_time = datetime.utcnow()

    segments = detect_trip_segments(device_id, start_time, end_time, settings, db)
    return [
        SuggestedTripResponse(
            start_time=s.start_time,
            end_time=s.end_time,
            point_count=s.point_count,
            total_distance_km=s.total_distance_km,
            start_lat=s.start_lat,
            start_lon=s.start_lon,
            end_lat=s.end_lat,
            end_lon=s.end_lon,
        )
        for s in segments
    ]


@router.post("", response_model=TripResponse, status_code=201)
async def create_trip(body: TripCreate, db: Session = Depends(get_db)):
    """
    Create a saved trip from a device's location history.
    """
    user = get_default_user(db)
    verify_device_access(body.device_id, None, db)

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
    db: Session = Depends(get_db),
):
    """
    List saved trips. Optional device_id filter.
    """
    user = get_default_user(db)
    query = db.query(Trip).filter(Trip.user_id == user.id)

    if device_id is not None:
        verify_device_access(device_id, None, db)
        query = query.filter(Trip.device_id == device_id)

    trips = query.order_by(Trip.created_at.desc()).all()
    return trips


@router.get("/{trip_id}", response_model=TripDetailResponse)
async def get_trip(trip_id: int, db: Session = Depends(get_db)):
    """
    Get trip metadata and route geometry.
    """
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

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
async def delete_trip(trip_id: int, db: Session = Depends(get_db)):
    """
    Delete a saved trip. Location data is not affected.
    """
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    db.delete(trip)
    db.commit()
    return None
