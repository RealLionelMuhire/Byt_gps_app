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


# Real G900LS replies, from the production command_log (2026-10-16/17):
#   RELAY#    -> "RELAY:0" / "RELAY:1"                       current output state
#   RELAY,1#  -> "Stop engine succeed RELAY 1 OK"            cut NOW
#             -> "it will be executed after speed less than 20KM/H RELAY 1 OK"
#                  DEFERRED: firmware safety rule — cuts only once its GPS
#                  speed reads < 20 km/h, which took 2.5-3.5 min on a
#                  bench unit with no fresh fixes. Output NOT switched yet.
#             -> "The state of power cut, no longer execute! RELAY 1 OK"
#                  a cut is already pending; says nothing about the output
#                  (RELAY# right after still read RELAY:0).
#   RELAY,0#  -> "Resume engine Succeed RELAY 0 OK"          restored NOW
# So a bare "RELAY 1 OK" only means "accepted" — treating it as "cut"
# (the first version did) showed "Fuel supply cut" minutes before it was.
_STATE_QUERY_RE = re.compile(r"\bRELAY\s*:\s*([01])\b", re.IGNORECASE)
_EXECUTED_RE = re.compile(r"\bsucc(?:eed|eeded|ess)\b.*?\bRELAY\s*([01])\s*OK\b", re.IGNORECASE | re.DOTALL)
_FAILURE_WORDS = ("fail", "error", "invalid", "not support", "unsupport", "denied")
_DEFERRED_WORDS = ("will be executed", "no longer execute", "after speed")


def is_deferred_relay_reply(text: Optional[str]) -> bool:
    """The tracker accepted a cut/restore but will apply it later."""
    return bool(text) and any(word in text.lower() for word in _DEFERRED_WORDS)


def parse_relay_reply(text: Optional[str]) -> Optional[bool]:
    """The relay output state a tracker reply STATES as current, or None
    when it doesn't (deferred, pending, failed, or unrelated replies)."""
    if not text:
        return None
    lowered = text.lower()
    if any(word in lowered for word in _FAILURE_WORDS) or is_deferred_relay_reply(text):
        return None
    match = _STATE_QUERY_RE.search(text) or _EXECUTED_RE.search(text)
    return None if match is None else match.group(1) == "1"
