"""Device command API endpoints — send SMS-compatible commands over TCP (Protocol 0x80, doc §6.1)"""

import time
from collections import defaultdict
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
import logging

from app.core.database import get_db
from app.core.auth import get_current_user, require_device_access
from app.models.command_settings import CommandSettings
from app.models.device import Device
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


# ── CommandSettings enforcement (confirmation, admin_only, fuel_cut_enabled, rate limit) ──

# The one destructive command in this file — RELAY,1# immobilizes the
# vehicle. Confirmed against the actual G900LS J16-4G command list (the
# production hardware — docs/usage/CONFIGURATION_GUIDE.md's "Device 2"
# section, Fuel/Relay Control): `RELAY,1#` = cut, `RELAY,0#` = resume. The
# previous DYD,000000#/HFYD,000000# pair came from a generic external GT06
# protocol spec appendix and does not match this hardware's actual firmware
# vocabulary — replaced 2026-09-01 after checking against the vendor's own
# G900LS command sheet.
_CUT_FUEL_COMMAND = "RELAY,1#"


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


# ── Alarm-mode request bodies ───────────────────────────────────────────────
# The G900LS J16-4G command list (docs/usage/CONFIGURATION_GUIDE.md's
# "Device 2" section) requires an alert-delivery `mode` on every alarm
# command below — 0/1/2(/3) selecting GPRS-only vs. also SMS and/or a phone
# call to the CENTER admin numbers. It is not optional in the wire command,
# so every request model here carries it (defaulted to 0 = GPRS-only, the
# cheapest option and the only one that's guaranteed to reach this backend
# — SMS/Call modes notify the CENTER numbers directly from the device,
# bypassing the server for that leg). No UI currently exposes changing it;
# raise that as a follow-up if per-alarm delivery-mode selection is wanted.


class VibrationAlarmRequest(BaseModel):
    enabled: bool
    mode: int = 0  # 0=GPRS, 1=SMS+GPRS, 2=GPRS+SMS+Call — SENALM's own range.

    @field_validator("mode")
    @classmethod
    def mode_valid(cls, v: int) -> int:
        if v not in (0, 1, 2):
            raise ValueError("mode must be 0 (GPRS), 1 (SMS+GPRS), or 2 (GPRS+SMS+Call)")
        return v


class IgnitionAlarmRequest(BaseModel):
    enabled: bool
    mode: int = 0  # 0=GPRS,1=GPRS+SMS,2=GPRS+Call,3=GPRS+SMS+Call — ACCALM/ACCOFFALM's range.

    @field_validator("mode")
    @classmethod
    def mode_valid(cls, v: int) -> int:
        if v not in (0, 1, 2, 3):
            raise ValueError("mode must be between 0 and 3")
        return v


class PowerCutAlarmRequest(BaseModel):
    enabled: bool
    mode: int = 0  # 0=GPRS, 1=SMS+GPRS, 2=GPRS+SMS+Call — POWERALM's own range.
    detect_seconds: int = 5  # T1: power-failure detection time, 2-60s.
    min_charge_seconds: int = 10  # T2: minimum charge time before re-arming, 1-3600s.

    @field_validator("mode")
    @classmethod
    def mode_valid(cls, v: int) -> int:
        if v not in (0, 1, 2):
            raise ValueError("mode must be 0 (GPRS), 1 (SMS+GPRS), or 2 (GPRS+SMS+Call)")
        return v

    @field_validator("detect_seconds")
    @classmethod
    def detect_seconds_valid(cls, v: int) -> int:
        if not (2 <= v <= 60):
            raise ValueError("detect_seconds must be between 2 and 60")
        return v

    @field_validator("min_charge_seconds")
    @classmethod
    def min_charge_seconds_valid(cls, v: int) -> int:
        if not (1 <= v <= 3600):
            raise ValueError("min_charge_seconds must be between 1 and 3600")
        return v


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
#
# All four below are confirmed against the G900LS J16-4G command list
# (docs/usage/CONFIGURATION_GUIDE.md's "Device 2" section — the actual
# production hardware). Three alarm types that used to live here —
# overspeed, displacement, and SOS — were removed 2026-09-01: none of them
# has a corresponding SMS command on this device.
#   - SOS has no software arm/disarm at all on the G900LS — the physical
#     SOS button always calls/texts the CENTER admin numbers regardless of
#     any setting here. (The device can still report an SOS alarm_type
#     byte over the wire protocol when the button is pressed — see
#     app/protocol_parser.py's alarm_names — so it's still meaningful to
#     mute its *push notification*; that's app/models/alert_settings.py's
#     concern, unaffected by this change.)
#   - Overspeed and displacement/geofence alarms are not in this device's
#     documented command set at all — nothing here can arm or disarm them.
#     (The GT06 wire protocol does define "Over speed"/"Enter fence"/"Exit
#     fence" alarm bytes, so the device may still report them via some
#     undocumented or firmware-default mechanism; alert_settings' mute
#     toggles for these are left in place speculatively for that reason,
#     but there's no known way to configure them from this backend.)


@router.post("/{device_id}/alarm/vibration")
async def toggle_vibration_alarm(
    device_id: int,
    body: VibrationAlarmRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enable or disable the vibration/motion-sensor alarm (SENALM)."""
    cmd = f"SENALM,ON,{body.mode}#" if body.enabled else "SENALM,OFF#"
    return await _send(device_id, cmd, "vibration alarm", request, db, user)


@router.post("/{device_id}/alarm/power-cut")
async def toggle_power_cut_alarm(
    device_id: int,
    body: PowerCutAlarmRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enable or disable the power-cut alarm (POWERALM) — fires when the
    device's external power is disconnected. Not a battery-level alarm
    (the G900LS command list has no such command); previously exposed here
    as "low battery", which was the wrong description for this command.
    """
    cmd = (
        f"POWERALM,ON,{body.mode},{body.detect_seconds},{body.min_charge_seconds},#"
        if body.enabled
        else "POWERALM,OFF#"
    )
    return await _send(device_id, cmd, "power cut alarm", request, db, user)


@router.post("/{device_id}/alarm/ignition-on")
async def toggle_ignition_on_alarm(
    device_id: int,
    body: IgnitionAlarmRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enable or disable the ignition-ON alarm (ACCALM) — fires when ACC
    turns on. See toggle_ignition_off_alarm for the independent OFF alarm;
    the G900LS models these as two separate, separately-armable alarms,
    not one "ignition changed" alarm.
    """
    cmd = f"ACCALM,ON,{body.mode}#" if body.enabled else "ACCALM,OFF#"
    return await _send(device_id, cmd, "ignition-on alarm", request, db, user)


@router.post("/{device_id}/alarm/ignition-off")
async def toggle_ignition_off_alarm(
    device_id: int,
    body: IgnitionAlarmRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enable or disable the ignition-OFF alarm (ACCOFFALM) — fires when
    ACC turns off. See toggle_ignition_on_alarm's doc for why this is a
    separate endpoint rather than a second field on that one.
    """
    cmd = f"ACCOFFALM,ON,{body.mode}#" if body.enabled else "ACCOFFALM,OFF#"
    return await _send(device_id, cmd, "ignition-off alarm", request, db, user)


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
    """Cut fuel/relay (immobilize vehicle) via RELAY,1# — the G900LS J16-4G
    command list's Fuel/Relay Control section.

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

    return await _send(device_id, _CUT_FUEL_COMMAND, "cut fuel", request, db, user)


@router.post("/{device_id}/fuel/restore")
async def restore_fuel(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Restore fuel/relay (re-enable vehicle) via RELAY,0#."""
    return await _send(device_id, "RELAY,0#", "restore fuel", request, db, user)


@router.post("/{device_id}/query/location")
async def query_location(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Query current location from device via WHERE# (G900LS command list — returns latitude/longitude by SMS reply)."""
    return await _send(device_id, "WHERE#", "query location", request, db, user)


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
