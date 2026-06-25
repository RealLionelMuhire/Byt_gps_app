"""
Admin Dashboard for GPS Tracking - BYThron GPS Service
Includes:
  - GET  /dashboard            - device overview (read-only)
  - GET  /admin/login          - Clerk sign-in page
  - POST /admin/auth/verify    - exchange Clerk JWT for a signed admin session cookie
  - GET  /admin/logout         - clear session
  - GET  /admin/devices        - whitelisted device inventory
  - POST /admin/devices        - add a new device to inventory
  - POST /admin/devices/{imei}/delete - remove a device from inventory
"""

import secrets
import string
import hmac
import hashlib
from datetime import datetime

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from fastapi import Depends
import os
import csv
import io

from app.core.database import get_db
from app.core.config import settings
from app.core.auth import _verify_clerk_token
from app.models.device import Device
from app.models.location import Location
from app.models.user import User

router = APIRouter()

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


# ── Auth Helpers ─────────────────────────────────────────────────────────────

SESSION_COOKIE = "admin_session"


def _generate_pin(length: int = 6) -> str:
    """Generate a random alphanumeric PIN (uppercase)."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _sign_session(clerk_user_id: str) -> str:
    """
    Create a tamper-proof session token: HMAC-SHA256(SECRET_KEY, clerk_user_id).
    Stored as a cookie; verified on every protected request.
    """
    key = settings.SECRET_KEY.encode()
    sig = hmac.new(key, clerk_user_id.encode(), hashlib.sha256).hexdigest()
    return f"{clerk_user_id}:{sig}"


def _verify_session(cookie_value: str) -> str | None:
    """
    Verify the session cookie and return the embedded clerk_user_id,
    or None if the cookie is missing, tampered, or the user is not an admin.
    """
    if not cookie_value or ":" not in cookie_value:
        return None
    try:
        clerk_user_id, provided_sig = cookie_value.rsplit(":", 1)
    except ValueError:
        return None

    expected = _sign_session(clerk_user_id)
    # Constant-time comparison prevents timing attacks
    if not hmac.compare_digest(expected, f"{clerk_user_id}:{provided_sig}"):
        return None

    # Confirm the user is still in the admin whitelist
    if clerk_user_id not in settings.admin_user_ids:
        return None

    return clerk_user_id


def _get_admin(request: Request) -> str | None:
    """Return the admin's Clerk user ID from the session cookie, or None."""
    return _verify_session(request.cookies.get(SESSION_COOKIE, ""))


def _check_admin(request: Request) -> bool:
    return _get_admin(request) is not None



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
            "lifecycle": device.lifecycle,
            "sim_number": device.sim_number or "—",
            "hardware_model": device.hardware_model or "—",
            "sim_renewal_date": device.sim_renewal_date.strftime("%Y-%m-%d") if device.sim_renewal_date else None,
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


# ── Admin Auth Routes ─────────────────────────────────────────────────────────

@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    """Show the Clerk-powered sign-in page."""
    if _check_admin(request):
        return RedirectResponse(url="/admin/devices", status_code=302)
    return templates.TemplateResponse("admin_login.html", {
        "request": request,
        "clerk_publishable_key": settings.CLERK_PUBLISHABLE_KEY or "",
    })


@router.post("/admin/auth/verify")
async def admin_auth_verify(request: Request):
    """
    Called by the Clerk JS component after a successful sign-in.
    Expects JSON body: { "token": "<clerk_session_jwt>" }
    Validates the token, checks admin whitelist, and sets a signed session cookie.
    """
    try:
        body = await request.json()
        token = body.get("token", "")
    except Exception:
        return JSONResponse({"error": "Invalid request body."}, status_code=400)

    if not token:
        return JSONResponse({"error": "Missing token."}, status_code=400)

    # Validate the Clerk JWT
    clerk_user_id = await _verify_clerk_token(token)
    if not clerk_user_id:
        return JSONResponse({"error": "Invalid or expired Clerk token."}, status_code=401)

    # Check admin whitelist
    if not settings.admin_user_ids:
        return JSONResponse(
            {"error": "No admin users configured. Set ADMIN_CLERK_USER_IDS in your .env file."},
            status_code=403
        )
    if clerk_user_id not in settings.admin_user_ids:
        return JSONResponse(
            {"error": "Your account does not have admin access. Contact the system administrator."},
            status_code=403
        )

    # Issue signed session cookie
    session_value = _sign_session(clerk_user_id)
    response = JSONResponse({"ok": True, "redirect": "/admin/devices"})
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_value,
        httponly=True,
        secure=not settings.DEBUG,  # False on localhost (HTTP), True in production (HTTPS)
        samesite="lax",
        max_age=3600 * 8,  # 8-hour session
    )
    return response


@router.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── Admin Device Inventory ────────────────────────────────────────────────────

@router.get("/admin/devices", response_class=HTMLResponse)
async def admin_devices(request: Request, db: Session = Depends(get_db)):
    if not _check_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    devices = db.query(Device).order_by(Device.created_at.desc()).all()
    device_data = _build_device_data(devices, db)
    
    tcp_server = getattr(request.app.state, "tcp_server", None)
    rejected_imeis = tcp_server.rejected_imeis if tcp_server else []

    # Format time for rejected list
    for r in rejected_imeis:
        diff = datetime.utcnow() - r["time"]
        r["duration"] = format_duration(int(diff.total_seconds()))

    return templates.TemplateResponse("admin_devices.html", {
        "request": request,
        "devices": device_data,
        "rejected_imeis": rejected_imeis,
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
    sim_number: str = Form(""),
    pairing_pin: str = Form(""),
    hardware_model: str = Form(""),
    sim_renewal_date: str = Form(""),
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
            "error": "Invalid IMEI — must be exactly 15 or 16 digits.",
            "form": {"imei": imei, "name": name, "description": description, "sim_number": sim_number, "hardware_model": hardware_model},
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
            "form": {"imei": imei, "name": name, "description": description, "sim_number": sim_number, "hardware_model": hardware_model},
        })

    pin = pairing_pin.strip().upper() or _generate_pin()
    
    renewal_dt = None
    if sim_renewal_date:
        try:
            renewal_dt = datetime.strptime(sim_renewal_date, "%Y-%m-%d")
        except ValueError:
            pass

    device = Device(
        imei=imei,
        name=name.strip() or f"Tracker-{imei[-6:]}",
        description=description.strip() or None,
        sim_number=sim_number.strip() or None,
        hardware_model=hardware_model.strip() or None,
        sim_renewal_date=renewal_dt,
        pairing_pin=pin,
        lifecycle="registered",
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


@router.post("/admin/devices/{imei}/verify")
async def admin_verify_device(
    imei: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Manually mark a device as verified/in_stock, optionally sending a TCP test command.
    """
    if not _check_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    device = db.query(Device).filter(Device.imei == imei).first()
    if device and device.lifecycle == 'registered':
        device.lifecycle = 'in_stock'
        device.updated_at = datetime.utcnow()
        
        # Try to send a PARAM# command to test it if it's connected
        tcp_server = getattr(request.app.state, "tcp_server", None)
        if tcp_server and device.status == 'online':
            # This is async, we don't await the response here because 
            # we just want to trigger it and let the device process it.
            import asyncio
            asyncio.create_task(tcp_server.send_command_to_device(imei, "PARAM#"))

        db.commit()

    return RedirectResponse(url="/admin/devices", status_code=302)


@router.post("/admin/devices/{imei}/unpair")
async def admin_unpair_device(
    imei: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Reclaim a device from a user (transfer ownership back to the company).
    Resets user_id, lifecycle to in_stock, and generates a new pairing pin.
    """
    if not _check_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    device = db.query(Device).filter(Device.imei == imei).first()
    if device and device.user_id:
        device.user_id = None
        device.lifecycle = 'in_stock'
        device.pairing_pin = _generate_pin()
        device.updated_at = datetime.utcnow()
        db.commit()

    return RedirectResponse(url="/admin/devices", status_code=302)


@router.post("/admin/devices/bulk")
async def admin_add_device_bulk(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Bulk register devices from a CSV file.
    Expected CSV columns: imei, name, sim_number, hardware_model
    """
    if not _check_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    contents = await file.read()
    decoded = contents.decode("utf-8")
    reader = csv.DictReader(io.StringIO(decoded))

    added = 0
    for row in reader:
        imei = row.get("imei", "").strip()
        if not imei or not (15 <= len(imei) <= 16):
            continue

        existing = db.query(Device).filter(Device.imei == imei).first()
        if not existing:
            device = Device(
                imei=imei,
                name=row.get("name", "").strip() or f"Tracker-{imei[-6:]}",
                sim_number=row.get("sim_number", "").strip() or None,
                hardware_model=row.get("hardware_model", "").strip() or None,
                pairing_pin=_generate_pin(),
                lifecycle="registered",
                status="offline",
            )
            db.add(device)
            added += 1

    db.commit()
    return RedirectResponse(url=f"/admin/devices?bulk_added={added}", status_code=302)
