"""
Admin Dashboard for GPS Tracking - BYThron GPS Service
Includes:
  - GET /dashboard          - device overview (read-only)
  - GET /admin/login        - login form
  - POST /admin/login       - authenticate with ADMIN_SECRET
  - GET /admin/devices      - whitelisted device inventory
  - POST /admin/devices     - add a new device to inventory
  - DELETE /admin/devices/{imei} - remove a device from inventory
"""

import secrets
import string
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import os

from app.core.database import get_db
from app.core.config import settings
from app.models.device import Device
from app.models.location import Location
from app.models.user import User

router = APIRouter()

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


# ── Auth Helpers ─────────────────────────────────────────────────────────────

def _generate_pin(length: int = 6) -> str:
    """Generate a random alphanumeric PIN (uppercase)."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _check_admin(request: Request) -> bool:
    """Return True if the current session has a valid admin cookie."""
    token = request.cookies.get("admin_token")
    return bool(token and settings.ADMIN_SECRET and token == settings.ADMIN_SECRET)


def require_admin(request: Request):
    """Dependency: redirect to login if not authenticated."""
    if not _check_admin(request):
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})


# ── Helpers (reused from original dashboard) ──────────────────────────────────

def format_duration(seconds):
    if seconds is None:
        return "N/A"
    seconds = max(0, seconds)
    if seconds >= 86400:
        d = seconds // 86400
        return f"{d} day{'s' if d != 1 else ''} ago"
    if seconds >= 3600:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    if seconds >= 60:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    return f"{seconds} second{'s' if seconds != 1 else ''} ago"


def get_last_movement(device_id: int, db: Session) -> dict:
    last_moving = (
        db.query(Location)
        .filter(Location.device_id == device_id, Location.speed > 0)
        .order_by(Location.timestamp.desc())
        .first()
    )
    if not last_moving:
        return {"status": "Never moved", "time": None, "duration": "N/A"}
    diff = datetime.utcnow() - last_moving.timestamp
    secs = int(diff.total_seconds())
    return {
        "status": "Stationary" if secs > 300 else "Recently moved",
        "time": last_moving.timestamp,
        "duration": format_duration(secs),
    }


def _build_device_data(devices, db):
    result = []
    for device in devices:
        latest_loc = (
            db.query(Location)
            .filter(Location.device_id == device.id)
            .order_by(Location.timestamp.desc())
            .first()
        )
        last_seen = device.last_update or device.last_connect
        now = datetime.utcnow()
        last_seen_seconds = int((now - last_seen).total_seconds()) if last_seen else None

        if last_seen_seconds is None:
            sending_status = "No data"
        elif last_seen_seconds <= settings.DEVICE_SENDING_STALE_SECONDS:
            sending_status = "Sending"
        elif last_seen_seconds <= settings.DEVICE_OFFLINE_TIMEOUT_SECONDS:
            sending_status = "Stale"
        else:
            sending_status = "Offline"

        owner = None
        if device.user_id:
            owner = db.query(User).filter(User.id == device.user_id).first()

        result.append({
            "id": device.id,
            "imei": device.imei,
            "name": device.name or f"Tracker-{device.imei[-6:]}",
            "description": device.description or "",
            "pairing_pin": device.pairing_pin or "—",
            "status": device.status,
            "latitude": device.last_latitude,
            "longitude": device.last_longitude,
            "battery_level": device.battery_level or 0,
            "gsm_signal": device.gsm_signal or 0,
            "last_seen": last_seen,
            "last_seen_duration": format_duration(last_seen_seconds),
            "sending_status": sending_status,
            "movement": get_last_movement(device.id, db),
            "speed": latest_loc.speed if latest_loc else 0,
            "satellites": latest_loc.satellites if latest_loc else 0,
            "owner_email": owner.email if owner else None,
            "owner_name": f"{owner.first_name} {owner.last_name}".strip() if owner else None,
        })
    return result


# ── Read-Only Fleet Dashboard ─────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/dashboard/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """Public fleet overview dashboard."""
    devices = db.query(Device).all()
    device_data = _build_device_data(devices, db)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "devices": device_data,
        "total_devices": len(device_data),
        "online_devices": len([d for d in device_data if d["status"] == "online"]),
        "is_admin": _check_admin(request),
    })


# ── Admin Login ───────────────────────────────────────────────────────────────

@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("admin_login.html", {
        "request": request,
        "error": error,
    })


@router.post("/admin/login")
async def admin_login(request: Request, secret: str = Form(...)):
    if not settings.ADMIN_SECRET:
        return templates.TemplateResponse("admin_login.html", {
            "request": request,
            "error": "ADMIN_SECRET is not configured on this server.",
        })
    if secret != settings.ADMIN_SECRET:
        return templates.TemplateResponse("admin_login.html", {
            "request": request,
            "error": "Incorrect admin secret. Please try again.",
        })
    response = RedirectResponse(url="/admin/devices", status_code=302)
    response.set_cookie(
        key="admin_token",
        value=settings.ADMIN_SECRET,
        httponly=True,
        samesite="lax",
        max_age=3600 * 8,   # 8-hour session
    )
    return response


@router.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("admin_token")
    return response


# ── Admin Device Inventory ────────────────────────────────────────────────────

@router.get("/admin/devices", response_class=HTMLResponse)
async def admin_devices(request: Request, db: Session = Depends(get_db)):
    if not _check_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    devices = db.query(Device).order_by(Device.created_at.desc()).all()
    device_data = _build_device_data(devices, db)
    return templates.TemplateResponse("admin_devices.html", {
        "request": request,
        "devices": device_data,
        "total_devices": len(device_data),
        "online_devices": len([d for d in device_data if d["status"] == "online"]),
        "paired_devices": len([d for d in device_data if d["owner_email"]]),
    })


@router.post("/admin/devices")
async def admin_add_device(
    request: Request,
    imei: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    pairing_pin: str = Form(""),
    db: Session = Depends(get_db),
):
    if not _check_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    imei = imei.strip()
    if not imei.isdigit() or len(imei) != 15:
        devices = db.query(Device).order_by(Device.created_at.desc()).all()
        device_data = _build_device_data(devices, db)
        return templates.TemplateResponse("admin_devices.html", {
            "request": request,
            "devices": device_data,
            "total_devices": len(device_data),
            "online_devices": len([d for d in device_data if d["status"] == "online"]),
            "paired_devices": len([d for d in device_data if d["owner_email"]]),
            "error": "Invalid IMEI — must be exactly 15 digits.",
            "form": {"imei": imei, "name": name, "description": description},
        })

    existing = db.query(Device).filter(Device.imei == imei).first()
    if existing:
        devices = db.query(Device).order_by(Device.created_at.desc()).all()
        device_data = _build_device_data(devices, db)
        return templates.TemplateResponse("admin_devices.html", {
            "request": request,
            "devices": device_data,
            "total_devices": len(device_data),
            "online_devices": len([d for d in device_data if d["status"] == "online"]),
            "paired_devices": len([d for d in device_data if d["owner_email"]]),
            "error": f"Device with IMEI {imei} already exists.",
            "form": {"imei": imei, "name": name, "description": description},
        })

    pin = pairing_pin.strip().upper() or _generate_pin()

    device = Device(
        imei=imei,
        name=name.strip() or f"Tracker-{imei[-6:]}",
        description=description.strip() or None,
        pairing_pin=pin,
        status="offline",
    )
    db.add(device)
    db.commit()

    return RedirectResponse(url="/admin/devices", status_code=302)


@router.post("/admin/devices/{imei}/delete")
async def admin_delete_device(
    imei: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not _check_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    device = db.query(Device).filter(Device.imei == imei).first()
    if device:
        db.delete(device)
        db.commit()
    return RedirectResponse(url="/admin/devices", status_code=302)
