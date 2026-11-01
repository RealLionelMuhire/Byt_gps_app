"""
A device's command vocabulary: which capabilities its tracker model
supports and the exact command text for each (see
app/services/tracker_capabilities.py for the catalog, app/models/
tracker_model.py for the tables).

Everything that sends a command goes through a TrackerProfile, so the
same "cut fuel" request becomes `RELAY,1#` on a G900LS and `stop123456` on
a TK903ELE — and a capability a model doesn't have is refused instead of
sending another model's command.
"""

import json
import logging
import re
import string
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.tracker_model import TrackerModel
from app.services.tracker_capabilities import capability, is_extra

logger = logging.getLogger(__name__)

# Capabilities whose reply the server acts on (relay state, location fix).
# None take parameters, so a raw command can be recognized by its text.
RELAY_CAPABILITIES = {"fuel.cut": True, "fuel.restore": False}
RECOGNIZED_CAPABILITIES = ("fuel.cut", "fuel.restore", "fuel.query", "location.query")

REPLY_PATTERN_KEYS = ("relay_cut", "relay_restored", "relay_deferred")
MAX_TEMPLATE_LENGTH = 200  # command_log.command is VARCHAR(200)


class CommandNotSupported(Exception):
    def __init__(self, key: str, model_name: str):
        super().__init__(f"{model_name} trackers don't support this command.")
        self.key = key
        self.model_name = model_name


class InvalidCommandParams(ValueError):
    pass


@dataclass(frozen=True)
class ModelCommand:
    key: str
    template: str
    verified: bool
    label: Optional[str] = None


@dataclass(frozen=True)
class TrackerProfile:
    model_id: Optional[int]
    name: str
    password: Optional[str] = None
    commands: Dict[str, ModelCommand] = field(default_factory=dict)
    reply_patterns: Dict[str, str] = field(default_factory=dict)

    def command(self, key: str) -> Optional[ModelCommand]:
        return self.commands.get(key)

    def render(self, key: str, params: Optional[dict] = None) -> str:
        """The exact command text for `key`, with parameters validated
        against the catalog (missing ones take their default)."""
        cmd = self.commands.get(key)
        if cmd is None:
            raise CommandNotSupported(key, self.name)
        values = {"password": self.password or ""}
        values.update(_validated_params(key, params or {}))
        return cmd.template.format(**values)

    def classify(self, command_text: Optional[str]) -> Optional[str]:
        """Which recognized capability (fuel/location) a raw command text
        is, for this model — e.g. "relay,1#" is fuel.cut on a G900LS."""
        normalized = _normalize(command_text)
        if not normalized:
            return None
        for key in RECOGNIZED_CAPABILITIES:
            if key in self.commands and _normalize(self.render(key)) == normalized:
                return key
        return None


def _normalize(text: Optional[str]) -> str:
    return (text or "").replace(" ", "").upper()


def _validated_params(key: str, params: dict) -> dict:
    cap = capability(key)
    if cap is None:  # extras take no parameters
        if params:
            raise InvalidCommandParams("This command takes no parameters.")
        return {}
    known = {p.name: p for p in cap.params}
    unknown = set(params) - set(known)
    if unknown:
        raise InvalidCommandParams(f"Unknown parameter(s): {', '.join(sorted(unknown))}")
    out = {}
    for p in cap.params:
        raw = params.get(p.name, p.default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise InvalidCommandParams(f"{p.label} must be a whole number")
        if isinstance(raw, bool) or not (p.minimum <= value <= p.maximum):
            raise InvalidCommandParams(f"{p.label} must be between {p.minimum} and {p.maximum}")
        out[p.name] = value
    return out


def template_errors(key: str, template: str) -> Optional[str]:
    """Why `template` can't be saved for `key`, or None. Only {password}
    and the capability's own parameter names may appear."""
    if not template or not template.strip():
        return "The command can't be empty."
    if len(template) > MAX_TEMPLATE_LENGTH:
        return f"The command must be at most {MAX_TEMPLATE_LENGTH} characters."
    if not all(32 <= ord(ch) < 127 for ch in template):
        return "Use plain ASCII characters only (the tracker receives ASCII)."
    cap = capability(key)
    if cap is None and not is_extra(key):
        return f"Unknown capability '{key}'."
    allowed = {"password"} | ({p.name for p in cap.params} if cap else set())
    try:
        fields = {name for _, name, spec, conv in string.Formatter().parse(template) if name is not None}
    except ValueError:
        return "Unbalanced { } in the command."
    bad = fields - allowed
    if bad:
        return f"Unknown placeholder(s): {', '.join('{' + b + '}' for b in sorted(bad))}. Allowed: " + \
            ", ".join("{" + a + "}" for a in sorted(allowed))
    return None


def reply_pattern_errors(patterns: dict) -> Optional[str]:
    for name, pattern in patterns.items():
        if name not in REPLY_PATTERN_KEYS:
            return f"Unknown reply pattern '{name}'."
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                return f"{name}: invalid regular expression ({exc})."
    return None


def _parse_patterns(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in REPLY_PATTERN_KEYS and isinstance(v, str) and v} \
        if isinstance(data, dict) else {}


def profile_from_model(model: TrackerModel) -> TrackerProfile:
    return TrackerProfile(
        model_id=model.id,
        name=model.name,
        password=model.command_password,
        commands={c.key: ModelCommand(c.key, c.template, bool(c.verified), c.label) for c in model.commands},
        reply_patterns=_parse_patterns(model.reply_patterns),
    )


def _builtin(name: str, password: Optional[str], rows: Iterable[tuple]) -> TrackerProfile:
    return TrackerProfile(
        model_id=None, name=name, password=password,
        commands={key: ModelCommand(key, template, verified, label) for key, template, verified, label in rows},
    )


# The two models in service, exactly as migration 057 seeds them
# (tests/test_tracker_models.py keeps the two in sync). The G900LS is the
# fallback when no model is configured at all — e.g. code deployed before
# 057 ran — so behavior is unchanged until the tables exist.
#
# G900LS J16-4G: every command the app already sent before tracker models
# existed is verified (fuel/relay and WHERE# confirmed live in
# command_log; the alarm commands per the vendor sheet in
# docs/usage/CONFIGURATION_GUIDE.md). RESET#/TIMER were never sent from
# the server — unverified until tried from the portal.
G900LS_ROWS = (
    ("fuel.cut", "RELAY,1#", True, None),
    ("fuel.restore", "RELAY,0#", True, None),
    ("fuel.query", "RELAY#", True, None),
    ("location.query", "WHERE#", True, None),
    ("status.query", "STATUS#", True, None),
    ("alarm.vibration.on", "SENALM,ON,{mode}#", True, None),
    ("alarm.vibration.off", "SENALM,OFF#", True, None),
    ("alarm.power_cut.on", "POWERALM,ON,{mode},{detect_seconds},{min_charge_seconds},#", True, None),
    ("alarm.power_cut.off", "POWERALM,OFF#", True, None),
    ("alarm.ignition_on.on", "ACCALM,ON,{mode}#", True, None),
    ("alarm.ignition_on.off", "ACCALM,OFF#", True, None),
    ("alarm.ignition_off.on", "ACCOFFALM,ON,{mode}#", True, None),
    ("alarm.ignition_off.off", "ACCOFFALM,OFF#", True, None),
    ("device.restart", "RESET#", False, None),
    ("interval.set", "TIMER,{moving_seconds},{parked_seconds}#", False, None),
    ("extra.params", "PARAM#", False, "All parameters"),
    ("extra.version", "VERSION#", False, "Firmware version"),
)
# TK903ELE: from its SMS command sheet only — never sent over GPRS from
# this server, so all unverified (customers see none until an admin tests
# and verifies them in the portal).
TK903ELE_ROWS = (
    ("fuel.cut", "stop{password}", False, None),
    ("fuel.restore", "resume{password}", False, None),
    ("status.query", "STATUS#", False, None),
    ("alarm.vibration.on", "vibrate{password} 1", False, None),
    ("alarm.vibration.off", "vibrate{password} 0", False, None),
    ("device.restart", "reset{password}", False, None),
    ("extra.check_settings", "check{password}", False, "All settings"),
    ("extra.version", "VERSION#", False, "Firmware version"),
)
BUILTIN_G900LS = _builtin("G900LS J16-4G", None, G900LS_ROWS)


def default_model(db: Session) -> Optional[TrackerModel]:
    return (
        db.query(TrackerModel)
        .filter(TrackerModel.is_default.is_(True), TrackerModel.is_active.is_(True))
        .order_by(TrackerModel.id)
        .first()
    )


def profile_for_device(db: Session, device: Optional[Device]) -> TrackerProfile:
    """The device's own model, else the default model, else the built-in
    G900LS. Fails safe to the built-in if the tables can't be read (a
    deploy that went out before migration 057 — this project's recurring
    040/042/051 failure mode)."""
    try:
        model = device.tracker_model if device is not None and device.tracker_model_id else None
        if model is None:
            model = default_model(db)
        return profile_from_model(model) if model is not None else BUILTIN_G900LS
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Tracker model lookup failed for device %s — using built-in G900LS: %s",
                     getattr(device, "id", None), exc)
        return BUILTIN_G900LS


def profile_for_imei(db: Session, imei: Optional[str]) -> TrackerProfile:
    try:
        device = db.query(Device).filter(Device.imei == imei).first() if imei else None
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Device lookup failed for %s — using built-in G900LS: %s", imei, exc)
        return BUILTIN_G900LS
    return profile_for_device(db, device)
