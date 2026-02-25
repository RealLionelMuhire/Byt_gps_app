"""
Simple Web Dashboard for GPS Tracking - BYThron GPS Service
Shows device list, info, battery, location, and last activity
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from app.core.database import get_db
from app.core.config import settings
from app.models.device import Device
from app.models.location import Location
import os

router = APIRouter()

# Templates directory
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


def get_last_movement(device_id: int, db: Session) -> dict:
    """Calculate when device last moved"""
    # Get last location with speed > 0
    last_moving = db.query(Location).filter(
        Location.device_id == device_id,
        Location.speed > 0
    ).order_by(Location.timestamp.desc()).first()
    
    if not last_moving:
        return {
            "status": "Never moved",
            "time": None,
            "duration": "N/A"
        }
    
    now = datetime.utcnow()
    diff = now - last_moving.timestamp
    
    # Format duration
    if diff.days > 0:
        duration = f"{diff.days} day{'s' if diff.days > 1 else ''} ago"
    elif diff.seconds >= 3600:
        hours = diff.seconds // 3600
        duration = f"{hours} hour{'s' if hours > 1 else ''} ago"
    elif diff.seconds >= 60:
        minutes = diff.seconds // 60
        duration = f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    else:
        duration = f"{diff.seconds} second{'s' if diff.seconds != 1 else ''} ago"
    
    return {
        "status": "Stationary" if diff.seconds > 300 else "Recently moved",
        "time": last_moving.timestamp,
        "duration": duration
    }


def format_duration(seconds: int) -> str:
    """Format duration in seconds into human-friendly string"""
    if seconds is None:
        return "N/A"
    if seconds < 0:
        seconds = 0
    if seconds >= 86400:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"
    if seconds >= 3600:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if seconds >= 60:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    return f"{seconds} second{'s' if seconds != 1 else ''} ago"


def get_battery_icon(level: int) -> str:
    """Get battery icon based on level"""
    if level >= 80:
        return "🔋"  # Full
    elif level >= 60:
        return "🔋"  # Good
    elif level >= 40:
        return "🪫"  # Medium
    elif level >= 20:
        return "🪫"  # Low
    else:
        return "🪫"  # Critical


def get_signal_bars(csq: int) -> str:
    """Get signal strength bars"""
    if csq >= 20:
        return "📶"  # Excellent
    elif csq >= 15:
        return "📶"  # Good
    elif csq >= 10:
        return "📶"  # Fair
    elif csq >= 5:
        return "📶"  # Poor
    else:
        return "📶"  # No signal


@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/dashboard/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """Main dashboard page"""
    
    # Get all devices
    devices = db.query(Device).all()
    
    # Prepare device data
    device_data = []
    for device in devices:
        # Get latest location
        latest_loc = db.query(Location).filter(
            Location.device_id == device.id
        ).order_by(Location.timestamp.desc()).first()
        
        # Get movement info
        movement = get_last_movement(device.id, db)

        # Determine last seen time (last update or last connect)
        last_seen = device.last_update or device.last_connect
        now = datetime.utcnow()
        last_seen_seconds = int((now - last_seen).total_seconds()) if last_seen else None

        # Determine sending status based on freshness thresholds
        if last_seen_seconds is None:
            sending_status = "No data"
        elif last_seen_seconds <= settings.DEVICE_SENDING_STALE_SECONDS:
            sending_status = "Sending"
        elif last_seen_seconds <= settings.DEVICE_OFFLINE_TIMEOUT_SECONDS:
            sending_status = "Stale"
        else:
            sending_status = "Offline (timed out)"
        
        device_info = {
            "id": device.id,
            "name": device.name or f"Tracker-{device.imei[-6:]}",
            "imei": device.imei,
            "status": device.status,
            "battery_level": device.battery_level or 0,
            "battery_icon": get_battery_icon(device.battery_level or 0),
            "gsm_signal": device.gsm_signal or 0,
            "signal_icon": get_signal_bars(device.gsm_signal or 0),
            "latitude": device.last_latitude,
            "longitude": device.last_longitude,
            "last_update": device.last_update,
            "last_seen": last_seen,
            "last_seen_duration": format_duration(last_seen_seconds),
            "sending_status": sending_status,
            "movement": movement,
            "speed": latest_loc.speed if latest_loc else 0,
            "satellites": latest_loc.satellites if latest_loc else 0,
        }
        device_data.append(device_info)
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "devices": device_data,
        "total_devices": len(device_data),
        "online_devices": len([d for d in device_data if d["status"] == "online"])
    })
