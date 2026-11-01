"""
Admin API for tracker hardware models (migration 057): the models, their
command tables, which model each device is, and sending a model's command
to a real tracker to verify it.

Mounted at /api/admin. Admin-only throughout.
"""

import json
import logging
from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.models.command_log import CommandLog
from app.models.device import Device
from app.models.tracker_model import TrackerModel, TrackerModelCommand
from app.models.user import User
from app.services.tracker_capabilities import capability, catalog_payload, is_extra
from app.services.tracker_models import (
    CommandNotSupported, InvalidCommandParams, profile_for_device, profile_from_model,
    reply_pattern_errors, template_errors,
)

logger = logging.getLogger(__name__)
router = APIRouter()

SUPPORTED_PROTOCOLS = ("gt06",)


# ── Schemas ──────────────────────────────────────────────────────────────────


class TrackerModelIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    manufacturer: Optional[str] = Field(default=None, max_length=80)
    protocol: str = "gt06"
    command_password: Optional[str] = Field(default=None, max_length=40)
    notes: Optional[str] = None
    is_active: bool = True
    is_default: bool = False
    reply_patterns: Dict[str, str] = Field(default_factory=dict)


class TrackerCommandIn(BaseModel):
    template: str
    verified: bool = False
    label: Optional[str] = Field(default=None, max_length=80)
    notes: Optional[str] = None


class DeviceTrackerModelIn(BaseModel):
    tracker_model_id: Optional[int] = None


class TestCommandIn(BaseModel):
    key: str
    params: Dict[str, int] = Field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _model_or_404(db: Session, model_id: int) -> TrackerModel:
    model = db.query(TrackerModel).filter(TrackerModel.id == model_id).first()
    if model is None:
        raise HTTPException(status_code=404, detail="Tracker model not found")
    return model


def _patterns(model: TrackerModel) -> Dict[str, str]:
    return profile_from_model(model).reply_patterns


def _command_payload(c: TrackerModelCommand) -> dict:
    cap = capability(c.key)
    return {
        "key": c.key,
        "label": c.label or (cap.label if cap else c.key),
        "template": c.template,
        "verified": c.verified,
        "notes": c.notes,
        "extra": is_extra(c.key),
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _model_payload(model: TrackerModel, device_count: int, with_commands: bool) -> dict:
    payload = {
        "id": model.id,
        "name": model.name,
        "manufacturer": model.manufacturer,
        "protocol": model.protocol,
        "command_password": model.command_password,
        "notes": model.notes,
        "is_active": model.is_active,
        "is_default": model.is_default,
        "reply_patterns": _patterns(model),
        "device_count": device_count,
        "command_count": len(model.commands),
        "verified_count": sum(1 for c in model.commands if c.verified),
    }
    if with_commands:
        payload["commands"] = [_command_payload(c) for c in model.commands]
    return payload


def _device_counts(db: Session) -> Dict[int, int]:
    rows = (
        db.query(Device.tracker_model_id, func.count(Device.id))
        .filter(Device.tracker_model_id.isnot(None))
        .group_by(Device.tracker_model_id)
        .all()
    )
    return {model_id: count for model_id, count in rows}


def _apply(model: TrackerModel, body: TrackerModelIn, db: Session) -> None:
    if body.protocol not in SUPPORTED_PROTOCOLS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported protocol '{body.protocol}'. This server parses: {', '.join(SUPPORTED_PROTOCOLS)}.",
        )
    error = reply_pattern_errors(body.reply_patterns)
    if error:
        raise HTTPException(status_code=422, detail=error)
    clash = (
        db.query(TrackerModel)
        .filter(func.lower(TrackerModel.name) == body.name.strip().lower(), TrackerModel.id != (model.id or 0))
        .first()
    )
    if clash:
        raise HTTPException(status_code=409, detail="A tracker model with this name already exists")
    if body.is_default and not body.is_active:
        raise HTTPException(status_code=422, detail="The default model must be active")

    model.name = body.name.strip()
    model.manufacturer = (body.manufacturer or "").strip() or None
    model.protocol = body.protocol
    model.command_password = body.command_password or None
    model.notes = body.notes or None
    model.is_active = body.is_active
    patterns = {k: v for k, v in body.reply_patterns.items() if v}
    model.reply_patterns = json.dumps(patterns) if patterns else None
    if body.is_default and not model.is_default:
        # Exactly one default: devices without a model use it.
        db.query(TrackerModel).filter(TrackerModel.is_default.is_(True)).update({"is_default": False})
    elif model.is_default and not body.is_default:
        raise HTTPException(status_code=422, detail="Make another model the default instead of unsetting it here")
    model.is_default = body.is_default
    model.updated_at = datetime.utcnow()


# ── Catalog + models ────────────────────────────────────────────────────────


@router.get("/tracker-capabilities")
async def list_capabilities(_: User = Depends(require_admin)):
    """The fixed capability catalog: what a model's commands can mean."""
    return {"capabilities": catalog_payload(), "protocols": list(SUPPORTED_PROTOCOLS)}


@router.get("/tracker-models")
async def list_tracker_models(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    counts = _device_counts(db)
    models = db.query(TrackerModel).order_by(TrackerModel.is_default.desc(), TrackerModel.name).all()
    return [_model_payload(m, counts.get(m.id, 0), with_commands=False) for m in models]


@router.get("/tracker-models/{model_id}")
async def get_tracker_model(model_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    model = _model_or_404(db, model_id)
    return _model_payload(model, _device_counts(db).get(model.id, 0), with_commands=True)


@router.post("/tracker-models", status_code=201)
async def create_tracker_model(body: TrackerModelIn, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    model = TrackerModel(is_default=False)
    _apply(model, body, db)
    if db.query(TrackerModel).count() == 0:
        model.is_default = True  # the first model is the default
    db.add(model)
    db.commit()
    db.refresh(model)
    return _model_payload(model, 0, with_commands=True)


@router.put("/tracker-models/{model_id}")
async def update_tracker_model(
    model_id: int, body: TrackerModelIn, db: Session = Depends(get_db), _: User = Depends(require_admin),
):
    model = _model_or_404(db, model_id)
    _apply(model, body, db)
    db.commit()
    db.refresh(model)
    return _model_payload(model, _device_counts(db).get(model.id, 0), with_commands=True)


@router.delete("/tracker-models/{model_id}", status_code=204)
async def delete_tracker_model(model_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    model = _model_or_404(db, model_id)
    if model.is_default:
        raise HTTPException(status_code=409, detail="Make another model the default before deleting this one")
    in_use = db.query(Device).filter(Device.tracker_model_id == model.id).count()
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"{in_use} device(s) use this model. Move them to another model or deactivate it instead.",
        )
    db.delete(model)
    db.commit()


# ── Commands ────────────────────────────────────────────────────────────────


@router.put("/tracker-models/{model_id}/commands/{key}")
async def upsert_tracker_command(
    model_id: int, key: str, body: TrackerCommandIn,
    db: Session = Depends(get_db), _: User = Depends(require_admin),
):
    """Set the command text a model uses for `key` — a catalog capability,
    or an admin-only extra "extra.<slug>" (label required). Changing the
    text of a verified command un-verifies it unless `verified` is sent
    again: it has to be re-tested."""
    model = _model_or_404(db, model_id)
    if is_extra(key):
        slug = key[len("extra."):]
        if not slug or not all(ch.isalnum() or ch in "_-" for ch in slug) or len(key) > 64:
            raise HTTPException(status_code=422, detail="Extra keys look like extra.check_settings (letters, digits, _ and -)")
        if not (body.label or "").strip():
            raise HTTPException(status_code=422, detail="Give the extra command a label")
    error = template_errors(key, body.template)
    if error:
        raise HTTPException(status_code=422, detail=error)

    row = next((c for c in model.commands if c.key == key), None)
    if row is None:
        row = TrackerModelCommand(key=key)
        model.commands.append(row)
    row.template = body.template.strip()
    row.verified = body.verified
    row.label = (body.label or "").strip() or None
    row.notes = body.notes or None
    row.updated_at = datetime.utcnow()
    model.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _command_payload(row)


@router.delete("/tracker-models/{model_id}/commands/{key}", status_code=204)
async def delete_tracker_command(
    model_id: int, key: str, db: Session = Depends(get_db), _: User = Depends(require_admin),
):
    model = _model_or_404(db, model_id)
    row = next((c for c in model.commands if c.key == key), None)
    if row is None:
        raise HTTPException(status_code=404, detail="This model has no such command")
    model.commands.remove(row)
    model.updated_at = datetime.utcnow()
    db.commit()


# ── Devices ─────────────────────────────────────────────────────────────────


def _device_or_404(db: Session, device_id: int) -> Device:
    device = db.query(Device).filter(Device.id == device_id).first()
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.put("/devices/{device_id}/tracker-model")
async def set_device_tracker_model(
    device_id: int, body: DeviceTrackerModelIn,
    db: Session = Depends(get_db), _: User = Depends(require_admin),
):
    """Which model this tracker is. null = the default model."""
    device = _device_or_404(db, device_id)
    if body.tracker_model_id is not None:
        model = _model_or_404(db, body.tracker_model_id)
        if not model.is_active and device.tracker_model_id != model.id:
            raise HTTPException(status_code=422, detail="This model is deactivated")
    device.tracker_model_id = body.tracker_model_id
    device.updated_at = datetime.utcnow()
    db.commit()
    profile = profile_for_device(db, device)
    return {"device_id": device.id, "tracker_model_id": device.tracker_model_id,
            "tracker_model": {"id": profile.model_id, "name": profile.name}}


@router.post("/devices/{device_id}/test-command")
async def test_tracker_command(
    device_id: int, body: TestCommandIn, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_admin),
):
    """Send one of the device's model commands (verified or not, extras
    included) to the real tracker and return its raw reply — how an admin
    checks a command before marking it verified. Logged in command_log like
    any other command. Bypasses the customer-facing policy (rate limit,
    confirmation): admins only, one command at a time."""
    device = _device_or_404(db, device_id)
    profile = profile_for_device(db, device)
    try:
        command = profile.render(body.key, body.params)
    except CommandNotSupported as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except InvalidCommandParams as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    tcp = getattr(request.app.state, "tcp_server", None)
    if tcp is None:
        raise HTTPException(status_code=503, detail="TCP server not available")
    result = await tcp.send_command_to_device(device.imei, command, user_id=user.id)
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("error", "Command failed"))

    log = (
        db.query(CommandLog)
        .filter(CommandLog.device_id == device.id, CommandLog.command == command)
        .order_by(CommandLog.id.desc())
        .first()
    )
    return {
        "device_id": device.id,
        "tracker_model": {"id": profile.model_id, "name": profile.name},
        "key": body.key,
        "command_sent": command,
        "device_response": result.get("response"),
        "note": result.get("note"),
        "command_log_id": log.id if log else None,
        "outcome": log.outcome if log else None,
    }
