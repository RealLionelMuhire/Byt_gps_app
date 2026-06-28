"""Device API endpoints"""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import secrets
import string
import time
from collections import defaultdict

from app.core.database import get_db
from app.core.auth import require_auth
from app.models.device import Device
from app.models.location import Location
from app.models.trip import Trip
from app.api.trips import TripResponse
from app.core.config import settings
from app.models.user import User
from pydantic import BaseModel

router = APIRouter()

# ── Simple in-memory rate limiter for pairing endpoint ────────────────────────
_pair_attempts: Dict[str, list] = defaultdict(list)  # ip -> [timestamps]
_PAIR_WINDOW_SECONDS = 60
_PAIR_MAX_ATTEMPTS   = 5

def _check_pair_rate_limit(ip: str):
    """Raise 429 if the IP has exceeded pairing attempt limits."""
    now = time.monotonic()
    cutoff = now - _PAIR_WINDOW_SECONDS
    # Keep only recent attempts
    _pair_attempts[ip] = [t for t in _pair_attempts[ip] if t > cutoff]
    if len(_pair_attempts[ip]) >= _PAIR_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail=f"Too many pairing attempts. Please wait {_PAIR_WINDOW_SECONDS} seconds and try again."
        )
    _pair_attempts[ip].append(now)


# Pydantic schemas
class DeviceBase(BaseModel):
    name: str
    description: Optional[str] = None
    sim_number: Optional[str] = None  # SIM card phone number (for SMS configuration)
    hardware_model: Optional[str] = None
    sim_renewal_date: Optional[datetime] = None


class DeviceCreate(DeviceBase):
    imei: str
    pairing_pin: Optional[str] = None  # Auto-generated if omitted


class DeviceUpdate(DeviceBase):
    pass


class DeviceResponse(DeviceBase):
    id: int
    imei: str
    pairing_pin: Optional[str]  # Shown on creation; used during mobile pairing
    lifecycle: str              # registered | in_stock | sold
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
    clerk_user_id: str = Depends(require_auth),
):
    """List GPS tracker devices."""
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    query = db.query(Device)
    
    # If not admin, only show devices paired to this user
    if not user.is_admin:
        query = query.filter(Device.user_id == user.id)

    if status:
        query = query.filter(Device.status == status)

    devices = query.offset(skip).limit(limit).all()
    return devices


@router.get("/rejected")
async def list_rejected_devices(
    request: Request,
    db: Session = Depends(get_db),
    clerk_user_id: str = Depends(require_auth),
):
    """Get the list of recently rejected unknown IMEI connections."""
    tcp_server = getattr(request.app.state, "tcp_server", None)
    if not tcp_server:
        return []
    
    # Get all currently registered IMEIs
    registered_imeis = {row[0] for row in db.query(Device.imei).all()}
    
    rejected = []
    # Iterate over a copy since we might mutate the original list later
    for r in list(tcp_server.rejected_imeis):
        if r["imei"] in registered_imeis:
            continue  # Skip devices that have already been registered!
            
        diff = int((datetime.utcnow() - r["time"]).total_seconds())
        rejected.append({
            "imei": r["imei"],
            "ip": r["ip"],
            "time": r["time"].isoformat(),
            "seconds_ago": diff
        })
    return rejected


@router.get("/{device_id}/trips", response_model=List[TripResponse])
async def list_device_trips(
    device_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
):
    """List trips for a device. Access trips via device_id."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    trips = (
        db.query(Trip)
        .filter(Trip.device_id == device_id)
        .order_by(Trip.created_at.desc())
        .all()
    )
    return trips


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Get device by ID"""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.get("/imei/{imei}", response_model=DeviceResponse)
async def get_device_by_imei(
    imei: str,
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Get device by IMEI"""
    device = db.query(Device).filter(Device.imei == imei).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.post("/", response_model=DeviceResponse, status_code=201)
async def create_device(
    device_data: DeviceCreate,
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
):
    """
    Whitelist a new GPS device in the inventory.
    If `pairing_pin` is not provided, a secure 6-character PIN is auto-generated.
    The PIN must be given to the end-user (e.g. printed inside the device box)
    so they can pair the device from the mobile app.
    """
    existing = db.query(Device).filter(Device.imei == device_data.imei).first()
    if existing:
        raise HTTPException(status_code=400, detail="Device with this IMEI already exists")

    # Auto-generate a pairing PIN if not supplied
    pin = (device_data.pairing_pin or "").strip().upper()
    if not pin:
        alphabet = string.ascii_uppercase + string.digits
        pin = "".join(secrets.choice(alphabet) for _ in range(6))

    device = Device(
        imei=device_data.imei,
        name=device_data.name or f"Tracker-{device_data.imei[-6:]}",
        description=device_data.description,
        sim_number=device_data.sim_number,
        pairing_pin=pin,
        lifecycle='registered',  # Starts as 'registered' until TCP handshake received
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
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Update device information"""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    device.name = device_data.name
    if device_data.description is not None:
        device.description = device_data.description
    if device_data.sim_number is not None:
        device.sim_number = device_data.sim_number
    if device_data.hardware_model is not None:
        device.hardware_model = device_data.hardware_model
    if device_data.sim_renewal_date is not None:
        device.sim_renewal_date = device_data.sim_renewal_date
    device.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(device)

    return device


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Delete device"""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    db.delete(device)
    db.commit()
    return None


# assign_device_to_user was removed — it was an unauthenticated legacy utility.
# Use POST /api/devices/pair from the mobile app for proper device ownership transfer.


@router.post("/{device_id}/verify")
async def verify_device(
    device_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
):
    """
    Admin: Manually mark a device as verified and ready to sell.

    This is an optional step after the automatic TCP handshake promotion.
    Use this when you want to explicitly confirm the device is ready for customer pairing
    (e.g. after sending a STATUS# or PARAM# command and confirming the response).

    Transition: registered → in_stock (if device has never connected, force it ready).
    Note: if device is already 'in_stock' or 'sold', this is a no-op.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if device.lifecycle == 'sold':
        raise HTTPException(
            status_code=409,
            detail="Device is already sold and paired to a customer. Cannot modify lifecycle."
        )

    previous = device.lifecycle
    device.lifecycle = 'in_stock'
    device.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(device)

    return {
        "device_id": device.id,
        "imei": device.imei,
        "previous_lifecycle": previous,
        "lifecycle": device.lifecycle,
        "message": "Device marked as verified and ready to sell."
    }


@router.get("/imei/{imei}/status")
async def get_device_status_by_imei_ownership(
    imei: str,
    db: Session = Depends(get_db),
    clerk_user_id: str = Depends(require_auth),
):
    """
    Get device status by IMEI — ownership-checked.
    Used by the mobile app's device-wait screen to poll for first signal.
    Returns 404 if the device doesn't exist or isn't owned by the requesting user.
    """
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    device = db.query(Device).filter(
        Device.imei == imei,
        Device.user_id == user.id,
    ).first()

    if not device:
        raise HTTPException(status_code=404, detail="Device not found or not paired to your account")

    return {"status": device.status}


@router.get("/{device_id}/status")
async def get_device_status(
    device_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Get device status by numeric device ID or by IMEI.

    - If device_id looks like an IMEI (15-16 digits), perform an ownership-aware
      lookup using the caller's Clerk JWT (required for mobile app polling).
    - Otherwise treat it as a numeric admin device ID (no auth required).
    """
    is_imei = device_id.isdigit() and len(device_id) in (15, 16)

    if is_imei:
        # Ownership-aware path — mobile app polls this after pairing
        from app.core.auth import _verify_clerk_token
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        clerk_user_id = await _verify_clerk_token(token) if token else None

        if not clerk_user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        device = db.query(Device).filter(
            Device.imei == device_id,
            Device.user_id == user.id,
        ).first()

        if not device:
            raise HTTPException(status_code=404, detail="Device not found or not paired to your account")

        return {"status": device.status}

    else:
        # Numeric device ID path — admin use, no ownership check
        try:
            dev_id = int(device_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="device_id must be numeric or a 15-16 digit IMEI")

        device = db.query(Device).filter(Device.id == dev_id).first()
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
                "longitude": device.last_longitude,
            } if device.last_latitude and device.last_longitude else None,
        }


@router.get("/{device_id}/diagnostics", response_model=DeviceDiagnosticsResponse)
async def get_device_diagnostics(
    device_id: int,
    samples: int = Query(20, ge=2, le=200, description="Number of recent location points to analyze"),
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
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
