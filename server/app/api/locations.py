"""Location API endpoints"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from pydantic import BaseModel
from math import radians, cos, sin, asin, sqrt

from app.core.database import get_db
from app.models.location import Location
from app.models.device import Device
from app.models.user import User

router = APIRouter()


def get_user_from_clerk_id(clerk_user_id: Optional[str], db: Session) -> Optional[User]:
    """Helper function to get user by Clerk ID"""
    if not clerk_user_id:
        return None
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    return user


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Calculate great-circle distance between two points in kilometers"""
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return 6371 * c


def verify_device_access(device_id: int, clerk_user_id: Optional[str], db: Session) -> Device:
    """Verify user has access to the device"""
    device = db.query(Device).filter(Device.id == device_id).first()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # If clerk_user_id provided, verify ownership
    if clerk_user_id:
        user = get_user_from_clerk_id(clerk_user_id, db)
        if user and device.user_id != user.id:
            raise HTTPException(status_code=403, detail="Access denied to this device")
    
    return device


def compute_distance_for_device_time_range(
    device_id: int, start_time: datetime, end_time: datetime, db: Session
) -> tuple[float, List[Location]]:
    """
    Compute total distance for device in time range. Reusable by trips API.
    Returns (total_distance_km, locations). Only GPS-valid points are used.
    """
    query = db.query(Location).filter(
        Location.device_id == device_id,
        Location.gps_valid == True
    )
    query = query.filter(Location.timestamp >= start_time)
    query = query.filter(Location.timestamp <= end_time)
    locations = query.order_by(Location.timestamp.asc()).all()

    total_distance = 0.0
    for i in range(1, len(locations)):
        prev = locations[i - 1]
        curr = locations[i]
        total_distance += haversine_km(prev.longitude, prev.latitude, curr.longitude, curr.latitude)

    return round(total_distance, 3), locations


def fetch_route_line_for_range(
    device_id: int, start_time: datetime, end_time: datetime, db: Session
) -> dict:
    """
    Fetch route line structure for device in time range. Reusable by trips API.
    Returns dict with type, coordinates, timestamps, speeds, courses, properties.
    """
    query = db.query(Location).filter(
        Location.device_id == device_id,
        Location.gps_valid == True
    )
    query = query.filter(Location.timestamp >= start_time)
    query = query.filter(Location.timestamp <= end_time)
    locations = query.order_by(Location.timestamp.asc()).all()

    coordinates = []
    timestamps = []
    speeds = []
    courses = []
    for loc in locations:
        coordinates.append([loc.longitude, loc.latitude])
        timestamps.append(loc.timestamp.isoformat())
        speeds.append(loc.speed)
        courses.append(loc.course)

    return {
        "type": "LineString",
        "coordinates": coordinates,
        "timestamps": timestamps,
        "speeds": speeds,
        "courses": courses,
        "properties": {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "point_count": len(coordinates)
        }
    }


# Pydantic schemas
class LocationResponse(BaseModel):
    id: int
    device_id: int
    latitude: float
    longitude: float
    speed: float
    course: int
    satellites: int
    gps_valid: bool
    is_alarm: bool
    alarm_type: Optional[str]
    timestamp: datetime
    received_at: datetime
    
    class Config:
        from_attributes = True


class LocationHistoryResponse(BaseModel):
    device_id: int
    device_name: str
    device_imei: str
    total_points: int
    locations: List[LocationResponse]


class DistanceResponse(BaseModel):
    device_id: int
    device_name: str
    device_imei: str
    start_time: datetime
    end_time: datetime
    point_count: int
    total_distance_km: float


class RouteLineStringResponse(BaseModel):
    type: str
    coordinates: List[List[float]]
    timestamps: List[str]
    speeds: List[float]
    courses: List[int]
    properties: dict


@router.get("/{device_id}/latest", response_model=LocationResponse)
async def get_latest_location(
    device_id: int,
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db)
):
    """
    Get latest location for a device
    
    If X-Clerk-User-Id header is provided, verifies user has access to the device.
    """
    # Verify device access
    verify_device_access(device_id, x_clerk_user_id, db)
    
    location = db.query(Location).filter(
        Location.device_id == device_id
    ).order_by(Location.timestamp.desc()).first()
    
    if not location:
        raise HTTPException(status_code=404, detail="No location data found for device")
    
    return location


@router.get("/{device_id}/history", response_model=LocationHistoryResponse)
async def get_location_history(
    device_id: int,
    start_time: Optional[datetime] = Query(None, description="Start time (UTC)"),
    end_time: Optional[datetime] = Query(None, description="End time (UTC)"),
    limit: int = Query(1000, ge=1, le=10000, description="Maximum number of points"),
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db)
):
    """
    Get location history for a device
    
    If X-Clerk-User-Id header is provided, verifies user has access to the device.
    """
    # Verify device access
    device = verify_device_access(device_id, x_clerk_user_id, db)
    
    # Build query
    query = db.query(Location).filter(Location.device_id == device_id)
    
    # Apply time filters
    if start_time:
        query = query.filter(Location.timestamp >= start_time)
    else:
        # Default to last 24 hours
        start_time = datetime.utcnow() - timedelta(hours=24)
        query = query.filter(Location.timestamp >= start_time)
    
    if end_time:
        query = query.filter(Location.timestamp <= end_time)
    
    # Get total count
    total = query.count()
    
    # Get locations
    locations = query.order_by(Location.timestamp.desc()).limit(limit).all()
    
    return LocationHistoryResponse(
        device_id=device.id,
        device_name=device.name,
        device_imei=device.imei,
        total_points=total,
        locations=locations
    )


@router.get("/{device_id}/route")
async def get_device_route(
    device_id: int,
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    simplify: bool = Query(False, description="Simplify route to reduce points"),
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db)
):
    """
    Get device route (optimized for map display)
    
    If X-Clerk-User-Id header is provided, verifies user has access to the device.
    """
    # Verify device access
    device = verify_device_access(device_id, x_clerk_user_id, db)
    
    query = db.query(Location).filter(
        Location.device_id == device_id,
        Location.gps_valid == True
    )
    
    if start_time:
        query = query.filter(Location.timestamp >= start_time)
    else:
        start_time = datetime.utcnow() - timedelta(hours=24)
        query = query.filter(Location.timestamp >= start_time)
    
    if end_time:
        query = query.filter(Location.timestamp <= end_time)
    
    locations = query.order_by(Location.timestamp.asc()).all()
    
    # Format as GeoJSON for easy map display
    features = []
    for loc in locations:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [loc.longitude, loc.latitude]
            },
            "properties": {
                "timestamp": loc.timestamp.isoformat(),
                "speed": loc.speed,
                "course": loc.course,
                "is_alarm": loc.is_alarm
            }
        })
    
    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "device_id": device.id,
            "device_name": device.name,
            "start_time": start_time.isoformat(),
            "end_time": (end_time or datetime.utcnow()).isoformat(),
            "point_count": len(features)
        }
    }


@router.get("/{device_id}/distance", response_model=DistanceResponse)
async def get_device_distance(
    device_id: int,
    start_time: Optional[datetime] = Query(None, description="Start time (UTC)"),
    end_time: Optional[datetime] = Query(None, description="End time (UTC)"),
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db)
):
    """
    Get total distance covered by a device within a time range.

    Distance is calculated using the Haversine formula across consecutive GPS points.
    Only GPS-valid points are used.
    """
    device = verify_device_access(device_id, x_clerk_user_id, db)

    if not start_time:
        start_time = datetime.utcnow() - timedelta(hours=24)
    if not end_time:
        end_time = datetime.utcnow()

    total_distance, locations = compute_distance_for_device_time_range(
        device_id, start_time, end_time, db
    )

    return DistanceResponse(
        device_id=device.id,
        device_name=device.name,
        device_imei=device.imei,
        start_time=start_time,
        end_time=end_time,
        point_count=len(locations),
        total_distance_km=total_distance
    )


@router.get("/{device_id}/route-line", response_model=RouteLineStringResponse)
async def get_device_route_line(
    device_id: int,
    start_time: Optional[datetime] = Query(None, description="Start time (UTC)"),
    end_time: Optional[datetime] = Query(None, description="End time (UTC)"),
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db)
):
    """
    Get device route as a LineString with timestamps aligned to coordinates.

    Returns GeoJSON-like structure with coordinates and timestamps arrays.
    """
    device = verify_device_access(device_id, x_clerk_user_id, db)

    if not start_time:
        start_time = datetime.utcnow() - timedelta(hours=24)
    if not end_time:
        end_time = datetime.utcnow()

    result = fetch_route_line_for_range(device_id, start_time, end_time, db)
    result["properties"]["device_id"] = device.id
    result["properties"]["device_name"] = device.name
    result["properties"]["device_imei"] = device.imei
    return result


@router.get("/{device_id}/alarms", response_model=List[LocationResponse])
async def get_device_alarms(
    device_id: int,
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db)
):
    """Get alarm events for a device"""
    query = db.query(Location).filter(
        Location.device_id == device_id,
        Location.is_alarm == True
    )
    
    if start_time:
        query = query.filter(Location.timestamp >= start_time)
    else:
        start_time = datetime.utcnow() - timedelta(days=7)
        query = query.filter(Location.timestamp >= start_time)
    
    if end_time:
        query = query.filter(Location.timestamp <= end_time)
    
    alarms = query.order_by(Location.timestamp.desc()).limit(limit).all()
    
    return alarms


@router.get("/nearby")
async def get_nearby_devices(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(10, ge=0.1, le=100, description="Search radius in kilometers"),
    db: Session = Depends(get_db)
):
    """Find devices near a location"""
    # This would use PostGIS spatial queries in production
    # For now, simple distance calculation
    from math import radians, cos, sin, asin, sqrt
    
    def haversine(lon1, lat1, lon2, lat2):
        """Calculate distance between two points on Earth"""
        lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        km = 6371 * c
        return km
    
    devices = db.query(Device).filter(
        Device.last_latitude.isnot(None),
        Device.last_longitude.isnot(None)
    ).all()
    
    nearby = []
    for device in devices:
        distance = haversine(longitude, latitude, device.last_longitude, device.last_latitude)
        if distance <= radius_km:
            nearby.append({
                "device_id": device.id,
                "device_name": device.name,
                "imei": device.imei,
                "latitude": device.last_latitude,
                "longitude": device.last_longitude,
                "distance_km": round(distance, 2),
                "last_update": device.last_update
            })
    
    nearby.sort(key=lambda x: x['distance_km'])
    
    return {
        "center": {"latitude": latitude, "longitude": longitude},
        "radius_km": radius_km,
        "devices_found": len(nearby),
        "devices": nearby
    }
