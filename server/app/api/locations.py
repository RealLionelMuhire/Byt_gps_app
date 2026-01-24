"""Location API endpoints"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from pydantic import BaseModel

from app.core.database import get_db
from app.models.location import Location
from app.models.device import Device

router = APIRouter()


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


@router.get("/{device_id}/latest", response_model=LocationResponse)
async def get_latest_location(device_id: int, db: Session = Depends(get_db)):
    """Get latest location for a device"""
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
    db: Session = Depends(get_db)
):
    """Get location history for a device"""
    # Get device
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
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
    db: Session = Depends(get_db)
):
    """Get device route (optimized for map display)"""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
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
