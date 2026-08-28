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
import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from fastapi import Depends
import os
import csv
import io
from urllib.parse import quote

from app.core.database import get_db
from app.core.config import settings
from app.core.auth import _verify_clerk_token
from app.models.device import Device
from app.models.location import Location
from app.models.user import User, Role
from app.models.subscription import Subscription, SubscriptionPlan, Payment
from app.api.devices import _resolve_client_user, _apply_assignment, DeviceAssignRequest
from app.api.auth import invite_clerk_client_and_create_local

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
    or None if the cookie is missing or tampered.
    """
    if not cookie_value or ":" not in cookie_value:
        return None
    try:
        clerk_user_id, provided_sig = cookie_value.rsplit(":", 1)
    except ValueError:
        return None

    expected = _sign_session(clerk_user_id)
    if not hmac.compare_digest(expected, f"{clerk_user_id}:{provided_sig}"):
        return None

    return clerk_user_id


def _get_admin(request: Request) -> str | None:
    """Return the Clerk user ID from the session cookie, or None."""
    return _verify_session(request.cookies.get(SESSION_COOKIE, ""))


def _check_admin(request: Request, db: Session = None) -> bool:
    """
    Verify the admin session cookie is valid AND the user's role is still
    ADMIN or SUPER_ADMIN in the database. Pass `db` when you need to guard
    against stale sessions (demoted/deleted users). The fleet dashboard
    may omit `db` for a lightweight "is an admin signed in?" check.
    """
    clerk_user_id = _get_admin(request)
    if not clerk_user_id:
        return False
    if db is not None:
        user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
        if not user or user.role not in (Role.SUPER_ADMIN, Role.ADMIN):
            return False
    return True


async def _require_admin_dashboard(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """
    Dashboard auth dependency: verify session cookie and check DB role
    is ADMIN or SUPER_ADMIN. Redirects to login on failure.
    """
    clerk_user_id = _get_admin(request)
    if not clerk_user_id:
        raise HTTPException(status_code=302, detail="Redirecting to login")

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user or user.role not in (Role.SUPER_ADMIN, Role.ADMIN):
        raise HTTPException(status_code=302, detail="Redirecting to login")

    return user



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


def _client_options(db) -> list:
    """Client accounts (role USER) for the assign-to-client dropdown."""
    clients = (
        db.query(User)
        .filter(User.role == Role.USER)
        .order_by(User.first_name.asc())
        .all()
    )
    return [
        {
            "id": c.id,
            "email": c.email,
            "name": f"{c.first_name} {c.last_name}".strip() or c.email,
        }
        for c in clients
    ]


def _render_admin_devices(
    request: Request,
    db: Session,
    error=None,
    success=None,
    form=None,
    owner_filter: str = None,
):
    """
    Render admin_devices.html with the full standard context (device rows,
    rejected IMEIs, stats, client dropdown options, banners). Every admin
    route that renders this page goes through here so the context can't drift
    between success and error paths.

    `owner_filter` (name/email substring) narrows the device rows to those
    assigned to a matching client.
    """
    all_plans = db.query(SubscriptionPlan).order_by(SubscriptionPlan.price.asc()).all()
    device_data, plans_data = _build_device_data(devices, db, all_plans=all_plans)

    if owner_filter:
        needle = owner_filter.strip().lower()
        device_data = [
            d for d in device_data
            if (d.get("owner_name") or "").lower().find(needle) >= 0
            or (d.get("owner_email") or "").lower().find(needle) >= 0
        ]

    tcp_server = getattr(request.app.state, "tcp_server", None)
    raw_rejected = tcp_server.rejected_imeis if tcp_server else []
    registered_imeis = {d.imei for d in devices}
    rejected_imeis = []
    for r in raw_rejected:
        if r["imei"] in registered_imeis:
            continue
        diff = datetime.utcnow() - r["time"]
        r["duration"] = format_duration(int(diff.total_seconds()))
        rejected_imeis.append(r)

    return templates.TemplateResponse("admin_devices.html", {
        "request": request,
        "devices": device_data,
        "rejected_imeis": rejected_imeis,
        "total_devices": len(device_data),
        "online_devices": len([d for d in device_data if d["status"] == "online"]),
        "paired_devices": len([d for d in device_data if d["owner_email"]]),
        "clients": _client_options(db),
        "plans": plans_data,
        "error": error,
        "success": success,
        "form": form,
        "owner_filter": owner_filter or "",
    })


def _build_device_data(devices, db, all_plans=None):
    """
    Build the device data dict for the admin dashboard.
    `all_plans` may be passed from the caller (e.g. _render_admin_devices)
    to avoid an extra query when plans are already loaded.
    """
    if all_plans is None:
        all_plans = db.query(SubscriptionPlan).all()
    plans = {p.id: p for p in all_plans}
    plans_by_slug = {p.slug.lower(): p for p in all_plans}

    # Also return the plans list so the caller can reuse it for the template
    # dropdown without re-querying.
    result_plans = [
        {
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "billing_type": p.billing_type,
            "price": p.price,
            "currency": p.currency,
            "duration_value": p.duration_value,
            "duration_unit": p.duration_unit,
            "is_active": p.is_active,
        }
        for p in all_plans
    ]

    # Batch-load owners + their latest subscription so per-device payment
    # status (subscription mode) doesn't N+1 either.
    owner_ids = {d.user_id for d in devices if d.user_id}
    owners = db.query(User).filter(User.id.in_(owner_ids)).all() if owner_ids else []
    owner_by_id = {u.id: u for u in owners}
    clerk_ids = [u.clerk_user_id for u in owners if u.clerk_user_id]
    subs = (
        db.query(Subscription)
        .filter(Subscription.clerk_user_id.in_(clerk_ids))
        .order_by(Subscription.created_at.desc())
        .all()
        if clerk_ids else []
    )
    latest_sub_by_clerk = {}
    for s in subs:
        latest_sub_by_clerk.setdefault(s.clerk_user_id, s)

    now = datetime.utcnow()
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

        owner = owner_by_id.get(device.user_id)

        plan = plans.get(device.plan_id)

        # Payment scheme / subscription mode for this device:
        # status = active | expired | none, plus the subscription plan slug,
        # name and expiry so the inventory shows the client's real payment state.
        sub = latest_sub_by_clerk.get(owner.clerk_user_id) if owner else None
        if sub is not None and sub.status == "active" and sub.expires_at and sub.expires_at > now:
            subscription_status = "active"
        elif sub is not None:
            subscription_status = "expired"
        else:
            subscription_status = "none"
        sub_plan = plans_by_slug.get((sub.plan_id or "").lower()) if sub else None

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
            "plan_id": device.plan_id,
            "plan": {
                "name": plan.name,
                "slug": plan.slug,
                "billing_type": plan.billing_type,
                "price": plan.price,
                "currency": plan.currency,
                "duration_value": plan.duration_value,
                "duration_unit": plan.duration_unit,
                "is_active": plan.is_active,
            } if plan else None,
            "subscription_status": subscription_status,
            "subscription_plan_name": sub_plan.name if sub_plan else None,
            "subscription_expires_at": (
                sub.expires_at.strftime("%Y-%m-%d") if sub and sub.expires_at else None
            ),
            "billing_type": plan.billing_type if plan else None,
        })
    return result, result_plans


# ── Read-Only Fleet Dashboard ─────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/dashboard/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """Public fleet overview dashboard."""
    devices = db.query(Device).all()
    device_data, _ = _build_device_data(devices, db)
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
async def admin_auth_verify(request: Request, db: Session = Depends(get_db)):
    """
    Called by the Clerk JS component after a successful sign-in.
    Expects JSON body: { "token": "<clerk_session_jwt>" }
    Validates the token, checks DB role (must be ADMIN or SUPER_ADMIN),
    and sets a signed session cookie.
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

    # Check DB role — must be ADMIN or SUPER_ADMIN
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user or user.role not in (Role.SUPER_ADMIN, Role.ADMIN):
        return JSONResponse(
            {"error": "Your account does not have admin access. Contact the system administrator."},
            status_code=403,
        )

    # Issue signed session cookie
    session_value = _sign_session(clerk_user_id)
    response = JSONResponse({"ok": True, "redirect": "/admin/devices"})
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_value,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="lax",
        max_age=3600 * 8,
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
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    success = None
    if request.query_params.get("assigned") == "1":
        success = "Device assigned to client successfully."
    elif request.query_params.get("bulk_added"):
        success = f"Added {request.query_params['bulk_added']} device(s) from CSV."
    elif request.query_params.get("success"):
        success = request.query_params["success"]
    error = request.query_params.get("error", "")
    return _render_admin_devices(
        request, db,
        success=success,
        error=error or None,
        owner_filter=request.query_params.get("owner", ""),
    )


# ── Admin Clients (customer directory) ────────────────────────────────────────

@router.get("/admin/clients", response_class=HTMLResponse)
async def admin_clients(request: Request, db: Session = Depends(get_db)):
    """
    Client directory: every customer account (role USER) with their assigned
    devices and subscription status. Supports a name/email search filter via
    the `q` query param.
    """
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    q = (request.query_params.get("q") or "").strip().lower()

    query = db.query(User).filter(User.role == Role.USER)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                User.email.ilike(like),
                User.first_name.ilike(like),
                User.last_name.ilike(like),
            )
        )
    clients = query.order_by(User.created_at.desc()).all()

    # Batch-load all assigned devices and active subscriptions so the page
    # does not N+1 per client row.
    devices = db.query(Device).filter(Device.user_id.isnot(None)).all()
    by_owner = defaultdict(list)
    for dev in devices:
        by_owner[dev.user_id].append(dev)

    subs = db.query(Subscription).filter(Subscription.status == "active").all()
    sub_by_clerk = {s.clerk_user_id: s for s in subs}

    client_rows = []
    for u in clients:
        devs = by_owner.get(u.id, [])
        sub = sub_by_clerk.get(u.clerk_user_id)
        client_rows.append({
            "id": u.id,
            "name": f"{u.first_name} {u.last_name}".strip() or u.email,
            "email": u.email,
            "onboarding_complete": u.onboarding_complete,
            "created_at": u.created_at,
            "device_count": len(devs),
            "devices": [
                {
                    "imei": d.imei,
                    "name": d.name,
                    "status": d.status,
                    "lifecycle": d.lifecycle,
                    "battery_level": d.battery_level,
                }
                for d in devs
            ],
            "plan": sub.plan_id if sub else None,
            "expires_at": sub.expires_at if sub else None,
        })

    total_assigned = sum(c["device_count"] for c in client_rows)
    unassigned_count = (
        db.query(Device).filter(Device.user_id.is_(None)).count()
    )

    return templates.TemplateResponse("admin_clients.html", {
        "request": request,
        "clients": client_rows,
        "q": q,
        "total_clients": len(clients),
        "clients_with_devices": sum(1 for c in client_rows if c["device_count"] > 0),
        "total_assigned": total_assigned,
        "unassigned_devices": unassigned_count,
    })


# ── Admin Device Billing Panel (payment scheme / subscription mode) ──────────

@router.get("/admin/devices/{imei}/billing", response_class=HTMLResponse)
async def admin_device_billing(request: Request, imei: str, db: Session = Depends(get_db)):
    """
    Per-device payment scheme / subscription mode panel.

    Shows the linked plan (one-time vs recurrent, price, currency, length),
    the owner's subscription state (active / expired / none with expiry), and
    the payment history that matches this device's plan. Read-only — payments
    are created via the IntouchPay payment flow in the mobile app, so
    the admin only views the scheme, never records payments manually.
    """
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    device = db.query(Device).filter(Device.imei == imei).first()
    if not device:
        return RedirectResponse(url="/admin/devices", status_code=302)

    owner = (
        db.query(User).filter(User.id == device.user_id).first()
        if device.user_id else None
    )
    plan = (
        db.query(SubscriptionPlan).filter(SubscriptionPlan.id == device.plan_id).first()
        if device.plan_id else None
    )

    # Latest subscription for the owner (any status) → current mode
    sub = None
    if owner:
        sub = (
            db.query(Subscription)
            .filter(Subscription.clerk_user_id == owner.clerk_user_id)
            .order_by(Subscription.created_at.desc())
            .first()
        )

    now = datetime.utcnow()
    if sub is not None and sub.status == "active" and sub.expires_at and sub.expires_at > now:
        subscription_status = "active"
    elif sub is not None:
        subscription_status = "expired"
    else:
        subscription_status = "none"

    # Payment history narrowed to this device's linked plan slug (all the
    # owner's payments when no plan is linked).
    payments = []
    if owner:
        q = db.query(Payment).filter(Payment.clerk_user_id == owner.clerk_user_id)
        if plan:
            q = q.filter(Payment.plan_id == plan.slug)
        payments = q.order_by(Payment.verified_at.desc()).limit(50).all()

    return templates.TemplateResponse("admin_device_billing.html", {
        "request": request,
        "device": device,
        "owner": owner,
        "plan": plan,
        "subscription_status": subscription_status,
        "subscription_plan_slug": sub.plan_id if sub else None,
        "subscription_started_at": sub.started_at if sub else None,
        "subscription_expires_at": sub.expires_at if sub else None,
        "payments": payments,
        "now": now,
    })


# ── Admin Subscription Plans (schemes) ───────────────────────────────────────

@router.get("/admin/plans", response_class=HTMLResponse)
async def admin_plans(request: Request, db: Session = Depends(get_db)):
    """
    Subscription schemes page: create/edit/deactivate plans and see how many
    devices are linked to each one. The mobile pricing screen and billing
    flow read these plans (see app/api/subscriptions.py).
    """
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    plans = db.query(SubscriptionPlan).order_by(SubscriptionPlan.price.asc()).all()

    # How many devices are linked to each plan (one query, grouped in memory)
    device_counts = defaultdict(int)
    for dev in db.query(Device).filter(Device.plan_id.isnot(None)).all():
        device_counts[dev.plan_id] += 1

    plan_rows = []
    for p in plans:
        plan_rows.append({
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "billing_type": p.billing_type,
            "price": p.price,
            "currency": p.currency,
            "duration_value": p.duration_value,
            "duration_unit": p.duration_unit,
            "max_devices": p.max_devices,
            "description": p.description or "",
            "is_active": p.is_active,
            "linked_devices": device_counts.get(p.id, 0),
        })

    return templates.TemplateResponse("admin_plans.html", {
        "request": request,
        "plans": plan_rows,
        "total_plans": len(plan_rows),
        "active_plans": sum(1 for r in plan_rows if r["is_active"]),
        "linked_devices": sum(r["linked_devices"] for r in plan_rows),
        "error": request.query_params.get("error", ""),
        "success": request.query_params.get("success", ""),
    })


@router.post("/admin/plans/create")
async def admin_create_plan(
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    billing_type: str = Form("recurrent"),
    price: float = Form(0.0),
    currency: str = Form("RWF"),
    duration_value: int = Form(1),
    duration_unit: str = Form("month"),
    max_devices: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a new subscription scheme (one-time or recurrent) from the admin UI."""
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    name = name.strip()
    slug = slug.strip().lower()
    if not name or not slug:
        return RedirectResponse(url="/admin/plans?error=" + quote("Name and slug are required"), status_code=302)
    if billing_type not in ("one_time", "recurrent"):
        return RedirectResponse(url="/admin/plans?error=" + quote("Invalid billing type"), status_code=302)
    if duration_unit not in ("day", "week", "month", "year"):
        return RedirectResponse(url="/admin/plans?error=" + quote("Invalid duration unit"), status_code=302)
    if price < 0:
        return RedirectResponse(url="/admin/plans?error=" + quote("Price cannot be negative"), status_code=302)
    if duration_value < 1:
        return RedirectResponse(url="/admin/plans?error=" + quote("Length must be at least 1"), status_code=302)

    existing = db.query(SubscriptionPlan).filter(
        SubscriptionPlan.slug == slug
    ).first()
    if existing:
        return RedirectResponse(
            url=f"/admin/plans?error={quote('A plan with slug ' + slug + ' already exists')}",
            status_code=302,
        )

    plan = SubscriptionPlan(
        name=name,
        slug=slug,
        billing_type=billing_type,
        price=max(0.0, price),
        currency=currency.strip().upper() or "RWF",
        duration_value=max(1, duration_value),
        duration_unit=duration_unit,
        max_devices=int(max_devices) if max_devices.strip() else None,
        description=description.strip() or None,
        is_active=True,
    )
    db.add(plan)
    db.commit()
    logger.info("Admin created subscription plan %s (%s %s / %s %s)",
                plan.slug, plan.price, plan.currency, plan.duration_value, plan.duration_unit)
    return RedirectResponse(url="/admin/plans?success=" + quote("Plan created"), status_code=302)


@router.post("/admin/plans/{plan_id}/toggle")
async def admin_toggle_plan(
    plan_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Activate / deactivate a plan. Deactivated plans hide from the mobile
    pricing screen but stay attached to linked devices."""
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()
    if plan:
        plan.is_active = not plan.is_active
        db.commit()
        state = "activated" if plan.is_active else "deactivated"
        return RedirectResponse(
            url=f"/admin/plans?success={quote('Plan ' + state)}",
            status_code=302,
        )
    return RedirectResponse(url="/admin/plans?error=" + quote("Plan not found"), status_code=302)


@router.post("/admin/devices/{imei}/plan")
async def admin_set_device_plan(
    imei: str,
    request: Request,
    plan_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """Link (or unlink) a subscription scheme to a device from the inventory page."""
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    device = db.query(Device).filter(Device.imei == imei).first()
    if not device:
        return RedirectResponse(url="/admin/devices", status_code=302)

    if plan_id.strip():
        try:
            pid = int(plan_id)
        except ValueError:
            return RedirectResponse(url="/admin/devices?error=Invalid plan", status_code=302)
        plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == pid).first()
        if not plan:
            return RedirectResponse(url="/admin/devices?error=Plan not found", status_code=302)
        device.plan_id = pid
    else:
        device.plan_id = None  # unlink

    device.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(
        url="/admin/devices?success=" + quote("Plan updated for device"),
        status_code=302,
    )


@router.post("/admin/devices/{imei}/assign")
async def admin_assign_device(
    imei: str,
    request: Request,
    client_email: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Assign an inventory device to a client account, by the client's email.
    Reuses the same resolution + assignment logic as the REST API
    (POST /api/devices/{device_id}/assign), which enforces the one-owner
    rule and lifecycle transitions.
    """
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    device = db.query(Device).filter(Device.imei == imei).first()
    if not device:
        return RedirectResponse(url="/admin/devices", status_code=302)

    try:
        target = _resolve_client_user(db, DeviceAssignRequest(email=client_email))
        # Shared with the REST API — enforces the one-owner rule (409) and
        # the lifecycle transition, so both entry points can't drift.
        _apply_assignment(device, target, db)
        logger.info("Admin dashboard: assigned device %s to client %s", imei, client_email)
        return RedirectResponse(url="/admin/devices?assigned=1", status_code=302)
    except HTTPException as exc:
        return _render_admin_devices(
            request, db, error=f"Could not assign to {client_email}: {exc.detail}"
        )


@router.post("/admin/devices/{imei}/assign-new")
async def admin_register_client_and_assign(
    imei: str,
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Invite a NEW client and assign them the device in one action.

    Sends a Clerk email invitation (the client sets their own password via the
    invite link — the admin never handles a password), pre-provisions the
    local client row (role USER) via the shared helper in api/auth.py, and
    assigns the device using the same _apply_assignment helper as the REST
    API. When the client accepts the invitation, their local row is claimed
    by the webhook / sync flow, keeping the assignment intact.
    """
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    device = db.query(Device).filter(Device.imei == imei).first()
    if not device:
        return _render_admin_devices(request, db, error="Device not found.")

    # Quick client-side-level validation before hitting Clerk
    email = (email or "").strip()
    if not email or "@" not in email:
        return _render_admin_devices(request, db, error="A valid client email is required.")

    # Check the device is actually unassigned BEFORE creating the client, so
    # a failed assignment never strands an orphan client account with no device.
    if device.user_id is not None:
        return _render_admin_devices(
            request, db,
            error="Device is already assigned to another client. Unassign it first.",
        )

    try:
        client = await invite_clerk_client_and_create_local(
            db,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )
        _apply_assignment(device, client, db)
        logger.info(
            "Admin dashboard: invited new client %s and assigned device %s",
            email, imei,
        )
        return RedirectResponse(url="/admin/devices?assigned=1", status_code=302)
    except HTTPException as exc:
        return _render_admin_devices(
            request, db, error=f"Could not invite {email}: {exc.detail}"
        )


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
    if not _check_admin(request, db):
        return RedirectResponse(url="/admin/login", status_code=302)

    imei = imei.strip()
    if not imei.isdigit() or len(imei) != 15:
        return _render_admin_devices(
            request, db,
            error="Invalid IMEI — must be exactly 15 or 16 digits.",
            form={"imei": imei, "name": name, "description": description, "sim_number": sim_number, "hardware_model": hardware_model},
        )

    existing = db.query(Device).filter(Device.imei == imei).first()
    if existing:
        return _render_admin_devices(
            request, db,
            error=f"Device with IMEI {imei} already exists.",
            form={"imei": imei, "name": name, "description": description, "sim_number": sim_number, "hardware_model": hardware_model},
        )

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
    if not _check_admin(request, db):
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
    if not _check_admin(request, db):
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
    if not _check_admin(request, db):
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
    if not _check_admin(request, db):
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
