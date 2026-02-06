"""Device API endpoints"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.orm import Session
from datetime import datetime

from app.core.database import get_db
from app.models.device import Device
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


def get_user_from_clerk_id(clerk_user_id: Optional[str], db: Session) -> Optional[User]:
    """Helper function to get user by Clerk ID"""
    if not clerk_user_id:
        return None
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    return user


@router.get("/", response_model=List[DeviceResponse])
async def list_devices(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[str] = Query(None, description="Filter by status: online, offline"),
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db)
):
    """
    List GPS tracker devices
    
    If X-Clerk-User-Id header is provided, only returns devices owned by that user.
    Otherwise, returns all devices (for backward compatibility).
    """
    query = db.query(Device)
    
    # Filter by user if clerk_user_id is provided
    if x_clerk_user_id:
        user = get_user_from_clerk_id(x_clerk_user_id, db)
        if user:
            query = query.filter(Device.user_id == user.id)
        else:
            # User not found, return empty list
            return []
    
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
async def create_device(
    device_data: DeviceCreate,
    x_clerk_user_id: Optional[str] = Header(None, alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db)
):
    """
    Create new device (manual registration)
    
    If X-Clerk-User-Id header is provided, associates the device with that user.
    """
    # Check if IMEI already exists
    existing = db.query(Device).filter(Device.imei == device_data.imei).first()
    if existing:
        raise HTTPException(status_code=400, detail="Device with this IMEI already exists")
    
    # Get user_id if clerk_user_id provided
    user_id = None
    if x_clerk_user_id:
        user = get_user_from_clerk_id(x_clerk_user_id, db)
        if user:
            user_id = user.id
    
    device = Device(
        imei=device_data.imei,
        name=device_data.name,
        description=device_data.description,
        user_id=user_id,
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
async def assign_device_to_user(
    device_id: int,
    x_clerk_user_id: str = Header(..., alias="X-Clerk-User-Id"),
    db: Session = Depends(get_db)
):
    """
    Assign a device to a user
    
    Requires X-Clerk-User-Id header to identify the user.
    """
    # Get device
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # Get user
    user = get_user_from_clerk_id(x_clerk_user_id, db)
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Please sync user first.")
    
    # Assign device to user
    device.user_id = user.id
    device.updated_at = datetime.utcnow()
    
    db.commit()
    db.refresh(device)
    
    return {
        "message": "Device assigned successfully",
        "device_id": device.id,
        "user_id": user.id,
        "clerk_user_id": user.clerk_user_id
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
