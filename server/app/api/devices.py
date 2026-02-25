"""Device API endpoints"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime

from app.core.database import get_db
from app.models.device import Device
from app.models.location import Location
from app.core.config import settings
from app.models.user import User
from pydantic import BaseModel

router = APIRouter()


# Pydantic schemas
class DeviceBase(BaseModel):
    name: str
    description: Optional[str] = None


class DeviceCreate(DeviceBase):
    imei: str


class DeviceUpdate(DeviceBase):
    pass


class DeviceResponse(DeviceBase):
    id: int
    imei: str
    status: str
    user_id: Optional[int]
    last_connect: Optional[datetime]
    last_update: Optional[datetime]
    last_latitude: Optional[float]
    last_longitude: Optional[float]
    battery_level: Optional[int]
    gsm_signal: Optional[int]
    created_at: datetime
    
    class Config:
        from_attributes = True


class LocationIntervalStats(BaseModel):
    samples: int
    avg_seconds: Optional[float]
    min_seconds: Optional[float]
    max_seconds: Optional[float]
    last_interval_seconds: Optional[float]


class DeviceDiagnosticsResponse(BaseModel):
    device_id: int
    imei: str
    status: str
    last_connect: Optional[datetime]
    last_update: Optional[datetime]
    last_location_timestamp: Optional[datetime]
    seconds_since_last_update: Optional[int]
    sending_status: str
    location_intervals: LocationIntervalStats


@router.get("/", response_model=List[DeviceResponse])
async def list_devices(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[str] = Query(None, description="Filter by status: online, offline"),
    db: Session = Depends(get_db),
):
    """List GPS tracker devices."""
    query = db.query(Device)
    if status:
        query = query.filter(Device.status == status)
    
    devices = query.offset(skip).limit(limit).all()
    return devices


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(device_id: int, db: Session = Depends(get_db)):
    """Get device by ID"""
    device = db.query(Device).filter(Device.id == device_id).first()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    return device


@router.get("/imei/{imei}", response_model=DeviceResponse)
async def get_device_by_imei(imei: str, db: Session = Depends(get_db)):
    """Get device by IMEI"""
    device = db.query(Device).filter(Device.imei == imei).first()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    return device


@router.post("/", response_model=DeviceResponse, status_code=201)
async def create_device(device_data: DeviceCreate, db: Session = Depends(get_db)):
    """Create new device (manual registration)."""
    existing = db.query(Device).filter(Device.imei == device_data.imei).first()
    if existing:
        raise HTTPException(status_code=400, detail="Device with this IMEI already exists")
    
    device = Device(
        imei=device_data.imei,
        name=device_data.name,
        description=device_data.description,
        user_id=None,
        status='offline'
    )
    
    db.add(device)
    db.commit()
    db.refresh(device)
    
    return device


@router.put("/{device_id}", response_model=DeviceResponse)
async def update_device(
    device_id: int,
    device_data: DeviceUpdate,
    db: Session = Depends(get_db)
):
    """Update device information"""
    device = db.query(Device).filter(Device.id == device_id).first()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    device.name = device_data.name
    if device_data.description is not None:
        device.description = device_data.description
    device.updated_at = datetime.utcnow()
    
    db.commit()
    db.refresh(device)
    
    return device


@router.delete("/{device_id}", status_code=204)
async def delete_device(device_id: int, db: Session = Depends(get_db)):
    """Delete device"""
    device = db.query(Device).filter(Device.id == device_id).first()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    db.delete(device)
    db.commit()
    
    return None


@router.post("/{device_id}/assign")
async def assign_device_to_user(device_id: int, db: Session = Depends(get_db)):
    """Assign a device to the default user."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    user = db.query(User).first()
    if not user:
        raise HTTPException(status_code=404, detail="No user found. Sync a user first via POST /api/auth/sync.")
    
    device.user_id = user.id
    device.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(device)
    
    return {
        "message": "Device assigned successfully",
        "device_id": device.id,
        "user_id": user.id,
    }


@router.get("/{device_id}/status")
async def get_device_status(device_id: int, db: Session = Depends(get_db)):
    """Get device current status"""
    device = db.query(Device).filter(Device.id == device_id).first()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    return {
        "id": device.id,
        "imei": device.imei,
        "name": device.name,
        "status": device.status,
        "last_update": device.last_update,
        "battery_level": device.battery_level,
        "gsm_signal": device.gsm_signal,
        "location": {
            "latitude": device.last_latitude,
            "longitude": device.last_longitude
        } if device.last_latitude and device.last_longitude else None
    }


@router.get("/{device_id}/diagnostics", response_model=DeviceDiagnosticsResponse)
async def get_device_diagnostics(
    device_id: int,
    samples: int = Query(20, ge=2, le=200, description="Number of recent location points to analyze"),
    db: Session = Depends(get_db)
):
    """
    Diagnostics for a device, including recent location packet intervals.

    Note: interval stats are based on location packets only (heartbeats are not persisted).
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Determine last seen and sending status
    last_seen = device.last_update or device.last_connect
    if last_seen:
        seconds_since_last_update = int((datetime.utcnow() - last_seen).total_seconds())
    else:
        seconds_since_last_update = None

    if seconds_since_last_update is None:
        sending_status = "No data"
    elif seconds_since_last_update <= settings.DEVICE_SENDING_STALE_SECONDS:
        sending_status = "Sending"
    elif seconds_since_last_update <= settings.DEVICE_OFFLINE_TIMEOUT_SECONDS:
        sending_status = "Stale"
    else:
        sending_status = "Offline (timed out)"

    # Fetch recent locations for interval analysis
    locations = db.query(Location).filter(
        Location.device_id == device_id
    ).order_by(Location.timestamp.desc()).limit(samples).all()

    last_location_timestamp = locations[0].timestamp if locations else None

    intervals = []
    for i in range(len(locations) - 1):
        dt = locations[i].timestamp - locations[i + 1].timestamp
        intervals.append(abs(dt.total_seconds()))

    if intervals:
        avg_seconds = sum(intervals) / len(intervals)
        min_seconds = min(intervals)
        max_seconds = max(intervals)
        last_interval_seconds = intervals[0]
    else:
        avg_seconds = min_seconds = max_seconds = last_interval_seconds = None

    return DeviceDiagnosticsResponse(
        device_id=device.id,
        imei=device.imei,
        status=device.status,
        last_connect=device.last_connect,
        last_update=device.last_update,
        last_location_timestamp=last_location_timestamp,
        seconds_since_last_update=seconds_since_last_update,
        sending_status=sending_status,
        location_intervals=LocationIntervalStats(
            samples=len(intervals),
            avg_seconds=avg_seconds,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            last_interval_seconds=last_interval_seconds
        )
    )
