"""Device command API endpoints — send SMS-compatible commands over TCP (Protocol 0x80, doc §6.1)"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import logging

from app.core.database import get_db
from app.models.device import Device

logger = logging.getLogger(__name__)
router = APIRouter()


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
):
    """Send any SMS-compatible command to a connected device over TCP.

    The command is sent via Protocol 0x80 over the existing GPRS/TCP connection.
    No SMS balance is needed. The device replies via Protocol 0x15.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

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
):
    """Enable or disable the vibration/shock alarm."""
    cmd = "vibrate123456 1" if body.enabled else "vibrate123456 0"
    return await _send(device_id, cmd, "vibration alarm", request, db)


@router.post("/{device_id}/alarm/lowbattery")
async def toggle_low_battery_alarm(
    device_id: int,
    body: AlarmToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Enable or disable the low battery alarm."""
    cmd = "lowbattery123456 on" if body.enabled else "lowbattery123456 off"
    return await _send(device_id, cmd, "low battery alarm", request, db)


@router.post("/{device_id}/alarm/acc")
async def toggle_acc_alarm(
    device_id: int,
    body: AlarmToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Enable or disable the ACC (ignition) on/off alarm."""
    cmd = "acc123456" if body.enabled else "noacc123456"
    return await _send(device_id, cmd, "ACC alarm", request, db)


@router.post("/{device_id}/alarm/overspeed")
async def toggle_overspeed_alarm(
    device_id: int,
    body: SpeedLimitRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Enable or disable the overspeed alarm. Set speed_kmh for the threshold."""
    cmd = f"speed123456 {body.speed_kmh:03d}" if body.enabled else "nospeed123456"
    return await _send(device_id, cmd, "overspeed alarm", request, db)


@router.post("/{device_id}/alarm/displacement")
async def toggle_displacement_alarm(
    device_id: int,
    body: MovementAlarmRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Enable or disable the displacement/movement alarm. Set radius_meters for the trigger radius."""
    cmd = f"move123456 {body.radius_meters:04d}" if body.enabled else "nomove123456"
    return await _send(device_id, cmd, "displacement alarm", request, db)


@router.post("/{device_id}/alarm/sos")
async def configure_sos_alarm(
    device_id: int,
    body: AlarmToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Configure SOS alarm mode. 0=off, 1=GPRS only, 2=GPRS+SMS, 3=GPRS+SMS+Call."""
    level = "1" if body.enabled else "0"
    cmd = f"KC123456 {level}"
    return await _send(device_id, cmd, "SOS alarm", request, db)


@router.post("/{device_id}/fuel/cut")
async def cut_fuel(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Cut oil/electricity (immobilize vehicle). Only works when speed < 20 km/h and GPS is on (doc §6.4)."""
    return await _send(device_id, "DYD,000000#", "cut fuel", request, db)


@router.post("/{device_id}/fuel/restore")
async def restore_fuel(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Restore oil/electricity (re-enable vehicle) (doc §6.5)."""
    return await _send(device_id, "HFYD,000000#", "restore fuel", request, db)


@router.post("/{device_id}/query/location")
async def query_location(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Query current location from device (doc §6.3). Returns lat/lon/speed/course/datetime."""
    return await _send(device_id, "DWXX#", "query location", request, db)


@router.post("/{device_id}/query/status")
async def query_status(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Query current device status (battery, GPS, GSM, ACC)."""
    return await _send(device_id, "STATUS#", "query status", request, db)


# ── Helper ─────────────────────────────────────────────────────────────────


async def _send(
    device_id: int,
    command: str,
    label: str,
    request: Request,
    db: Session,
) -> dict:
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

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
