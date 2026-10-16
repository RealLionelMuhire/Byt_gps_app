"""Tracker-reported fuel/relay state (Device.relay_cut, migration 052).

Source of truth is the tracker itself: bit 7 of the GT06 terminal-
information byte ("oil/electricity disconnected") that every heartbeat
(0x13) and alarm (0x16) packet carries — see app/protocol_parser.py. A
RELAY,1#/RELAY,0# command only *asks* for a change; clients must treat the
vehicle as cut/restored only once this state says so, never because a
command was sent (a command can time out, be refused, or the relay may not
be wired).
"""

import re
from datetime import datetime
from typing import Optional

from app.models.device import Device


def apply_relay_report(device: Device, cut: Optional[bool], at: Optional[datetime] = None) -> bool:
    """Record one relay report from the tracker on `device` (not committed).

    Returns True when the state CHANGED (including the first report ever),
    i.e. when clients should be told. `cut=None` (packet without the
    status byte) is ignored.
    """
    if cut is None:
        return False
    changed = device.relay_cut is None or device.relay_cut != cut
    device.relay_cut = cut
    device.relay_state_at = at or datetime.utcnow()
    return changed


def relay_state_payload(device: Device) -> dict:
    """Wire shape shared by the WS `device_status` event and REST responses."""
    at = device.relay_state_at
    return {
        "relay_cut": device.relay_cut,
        "relay_state_at": at.isoformat() if at else None,
    }


# Real G900LS replies seen 2026-10-16: RELAY# -> "RELAY:0";
# RELAY,1# -> "Stop engine succeed RELAY 1 OK". The digit after RELAY is
# the relay output state (1 = cut), in both the query and the command reply.
_RELAY_REPLY_RE = re.compile(r"\bRELAY\s*[:=,]?\s*([01])\b", re.IGNORECASE)
_FAILURE_WORDS = ("fail", "error", "invalid", "not support", "unsupport", "denied")


def parse_relay_reply(text: Optional[str]) -> Optional[bool]:
    """The relay state a tracker reply states, or None when it doesn't
    state one (or reports a failure — never infer success from those)."""
    if not text:
        return None
    lowered = text.lower()
    if any(word in lowered for word in _FAILURE_WORDS):
        return None
    match = _RELAY_REPLY_RE.search(text)
    return None if match is None else match.group(1) == "1"
