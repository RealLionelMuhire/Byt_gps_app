"""Device command API endpoints — send SMS-compatible commands over TCP (Protocol 0x80, doc §6.1)"""

import time
from collections import defaultdict
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import logging

from app.core.database import get_db
from app.core.auth import get_current_user, require_device_access
from app.models.command_settings import CommandSettings
from app.models.device import Device
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


# ── CommandSettings enforcement (confirmation, admin_only, fuel_cut_enabled, rate limit) ──

# The one destructive command in this file — DYD,000000# immobilizes the
# vehicle (doc §6.4) — matched against CommandSettings.fuel_cut_enabled.
_CUT_FUEL_COMMAND = "DYD,000000#"


class _DefaultCommandSettings:
    """Fallback used when a device has no command_settings row — mirrors the column defaults in app/models/command_settings.py."""
    require_confirmation_for_fuel_cut = True
    fuel_cut_enabled = True
    admin_only = False
    commands_per_hour = 30


_DEFAULT_COMMAND_SETTINGS = _DefaultCommandSettings()


def _get_command_settings(db: Session, device_id: int):
    return (
        db.query(CommandSettings).filter(CommandSettings.device_id == device_id).first()
        or _DEFAULT_COMMAND_SETTINGS
    )


# device_id -> [timestamps] of accepted commands in the current window
_command_attempts: Dict[int, List[float]] = defaultdict(list)
_COMMAND_WINDOW_SECONDS = 3600


def _check_command_rate_limit(device_id: int, limit: int) -> None:
    """Raise 429 if device_id has exceeded CommandSettings.commands_per_hour."""
    now = time.monotonic()
    cutoff = now - _COMMAND_WINDOW_SECONDS
    _command_attempts[device_id] = [t for t in _command_attempts[device_id] if t > cutoff]
    if len(_command_attempts[device_id]) >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Command rate limit exceeded for this device ({limit}/hour). Please wait and try again.",
        )
    _command_attempts[device_id].append(now)


def _enforce_command_policy(db: Session, device: Device, user: User, command: str) -> None:
    """
    Apply CommandSettings policy to an outgoing command for `device`. Raises
    HTTPException on violation. Called after require_device_access(), so
    ownership/admin access is already established — this adds the
    per-device policy layer on top (admin_only, fuel_cut_enabled, rate
    limit). require_confirmation_for_fuel_cut is checked separately in
    cut_fuel(), which is the only endpoint with a place to carry a
    `confirm` flag in its request body.
    """
    settings = _get_command_settings(db, device.id)

    if settings.admin_only and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only an admin may send commands to this device")

    if not settings.fuel_cut_enabled and command.strip().upper() == _CUT_FUEL_COMMAND:
        raise HTTPException(status_code=403, detail="Fuel cut is disabled for this device")

    _check_command_rate_limit(device.id, settings.commands_per_hour)


class CommandSettingsResponse(BaseModel):
    device_id: int
    require_confirmation_for_fuel_cut: bool
    fuel_cut_enabled: bool
    admin_only: bool
    commands_per_hour: int


@router.get("/{device_id}/command_settings", response_model=CommandSettingsResponse)
async def get_command_settings(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Read this device's command policy so a client can decide what to
    render *before* attempting a command — e.g. hide/disable the fuel
    control card when fuel_cut_enabled is false, skip the confirm step when
    require_confirmation_for_fuel_cut is false, or hide command actions
    entirely for a non-admin when admin_only is true.

    Returns the same hardcoded defaults _enforce_command_policy/cut_fuel
    already fall back to when the device has no command_settings row (see
    _DefaultCommandSettings) rather than 404ing on a missing row — the
    client shouldn't need a special case for "no row yet" vs "row exists".
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user)

    settings = _get_command_settings(db, device_id)
    return CommandSettingsResponse(
        device_id=device_id,
        require_confirmation_for_fuel_cut=settings.require_confirmation_for_fuel_cut,
        fuel_cut_enabled=settings.fuel_cut_enabled,
        admin_only=settings.admin_only,
        commands_per_hour=settings.commands_per_hour,
    )


class CommandRequest(BaseModel):
    command: str

    class Config:
        json_schema_extra = {
            "example": {"command": "STATUS#"}
        }


class AlarmToggleRequest(BaseModel):
    enabled: bool


class SpeedLimitRequest(BaseModel):
    enabled: bool
    speed_kmh: int = 120


class MovementAlarmRequest(BaseModel):
    enabled: bool
    radius_meters: int = 200


def _get_tcp_server(request: Request):
    tcp_server = getattr(request.app.state, 'tcp_server', None)
    if not tcp_server:
        raise HTTPException(status_code=503, detail="TCP server not available")
    return tcp_server


@router.post("/{device_id}/command")
async def send_raw_command(
    device_id: int,
    body: CommandRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Send any SMS-compatible command to a connected device over TCP.

    The command is sent via Protocol 0x80 over the existing GPRS/TCP connection.
    No SMS balance is needed. The device replies via Protocol 0x15.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user)
    _enforce_command_policy(db, device, user, body.command)

    tcp = _get_tcp_server(request)
    result = await tcp.send_command_to_device(device.imei, body.command)

    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("error", "Command failed"))

    return {
        "device_id": device.id,
        "imei": device.imei,
        "command_sent": body.command,
        "device_response": result.get("response"),
        "note": result.get("note"),
    }


# ── Convenience endpoints for common alarm operations ──────────────────────


@router.post("/{device_id}/alarm/vibration")
async def toggle_vibration_alarm(
    device_id: int,
    body: AlarmToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enable or disable the vibration/shock alarm."""
    cmd = "vibrate123456 1" if body.enabled else "vibrate123456 0"
    return await _send(device_id, cmd, "vibration alarm", request, db, user)


@router.post("/{device_id}/alarm/lowbattery")
async def toggle_low_battery_alarm(
    device_id: int,
    body: AlarmToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enable or disable the low battery alarm."""
    cmd = "lowbattery123456 on" if body.enabled else "lowbattery123456 off"
    return await _send(device_id, cmd, "low battery alarm", request, db, user)


@router.post("/{device_id}/alarm/acc")
async def toggle_acc_alarm(
    device_id: int,
    body: AlarmToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enable or disable the ACC (ignition) on/off alarm."""
    cmd = "acc123456" if body.enabled else "noacc123456"
    return await _send(device_id, cmd, "ACC alarm", request, db, user)


@router.post("/{device_id}/alarm/overspeed")
async def toggle_overspeed_alarm(
    device_id: int,
    body: SpeedLimitRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enable or disable the overspeed alarm. Set speed_kmh for the threshold."""
    cmd = f"speed123456 {body.speed_kmh:03d}" if body.enabled else "nospeed123456"
    return await _send(device_id, cmd, "overspeed alarm", request, db, user)


@router.post("/{device_id}/alarm/displacement")
async def toggle_displacement_alarm(
    device_id: int,
    body: MovementAlarmRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enable or disable the displacement/movement alarm. Set radius_meters for the trigger radius."""
    cmd = f"move123456 {body.radius_meters:04d}" if body.enabled else "nomove123456"
    return await _send(device_id, cmd, "displacement alarm", request, db, user)


@router.post("/{device_id}/alarm/sos")
async def configure_sos_alarm(
    device_id: int,
    body: AlarmToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Configure SOS alarm mode. 0=off, 1=GPRS only, 2=GPRS+SMS, 3=GPRS+SMS+Call."""
    level = "1" if body.enabled else "0"
    cmd = f"KC123456 {level}"
    return await _send(device_id, cmd, "SOS alarm", request, db, user)


class FuelCutRequest(BaseModel):
    confirm: bool = False

    class Config:
        json_schema_extra = {"example": {"confirm": True}}


@router.post("/{device_id}/fuel/cut")
async def cut_fuel(
    device_id: int,
    request: Request,
    body: FuelCutRequest = FuelCutRequest(),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Cut oil/electricity (immobilize vehicle). Only works when speed < 20 km/h and GPS is on (doc §6.4).

    Requires {"confirm": true} in the body unless CommandSettings.require_confirmation_for_fuel_cut
    is disabled for this device (default: required).
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user)

    settings = _get_command_settings(db, device_id)
    if settings.require_confirmation_for_fuel_cut and not body.confirm:
        raise HTTPException(
            status_code=409,
            detail='Fuel cut requires confirmation — resend with {"confirm": true}',
        )

    return await _send(device_id, "DYD,000000#", "cut fuel", request, db, user)


@router.post("/{device_id}/fuel/restore")
async def restore_fuel(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Restore oil/electricity (re-enable vehicle) (doc §6.5)."""
    return await _send(device_id, "HFYD,000000#", "restore fuel", request, db, user)


@router.post("/{device_id}/query/location")
async def query_location(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Query current location from device (doc §6.3). Returns lat/lon/speed/course/datetime."""
    return await _send(device_id, "DWXX#", "query location", request, db, user)


@router.post("/{device_id}/query/status")
async def query_status(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Query current device status (battery, GPS, GSM, ACC)."""
    return await _send(device_id, "STATUS#", "query status", request, db, user)


# ── Helper ─────────────────────────────────────────────────────────────────


async def _send(
    device_id: int,
    command: str,
    label: str,
    request: Request,
    db: Session,
    user: User,
) -> dict:
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user)
    _enforce_command_policy(db, device, user, command)

    tcp = _get_tcp_server(request)
    result = await tcp.send_command_to_device(device.imei, command)

    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("error", f"Failed to send {label}"))

    return {
        "device_id": device.id,
        "imei": device.imei,
        "action": label,
        "command_sent": command,
        "device_response": result.get("response"),
        "note": result.get("note"),
    }
