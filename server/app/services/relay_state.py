"""Tracker-reported fuel/relay state (Device.relay_cut, migration 052).

Source of truth is the tracker itself: bit 7 of the GT06 terminal-
information byte ("oil/electricity disconnected") that every heartbeat
(0x13) and alarm (0x16) packet carries — see app/protocol_parser.py. A
RELAY,1#/RELAY,0# command only *asks* for a change; clients must treat the
vehicle as cut/restored only once this state says so, never because a
command was sent (a command can time out, be refused, or the relay may not
be wired).
"""

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
