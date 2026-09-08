"""
Shared alarm classification constants.

Extracted from what used to be locals inside TCPServer._send_push_notification
(app/tcp_server.py) so the escalation/digest cron scripts can share the exact
same severity/label/critical rules instead of redefining them and risking
drift.

Keys here must match what TCPServer._send_push_notification actually looks
them up with: `str(data["alarm_type"]).lower()` — i.e. the lowercased GT06
hardware alarm-byte label from app/protocol_parser.py's `alarm_names`
("SOS", "Power cut", "Shock", "Enter fence", "Exit fence", "Over speed",
"Ignition on", "Ignition off", ...), NOT the AlertSettings *column* name
suffix (vibration_push_enabled, low_battery_push_enabled, acc_push_enabled,
...) despite those looking like the more natural key to reach for — the
column names describe what a mute toggle is *for* in the UI, while these
dict keys must match what the device actually says over the wire, and the
two vocabularies don't line up 1:1. Confirmed two rounds of this exact bug,
both fixed here:

  - "over speed" (with the space) was originally "overspeed" — fixed
    alongside adding app/services/speed_limit.py, whose synthesized alarms
    (which also use the two-word "Over speed") would otherwise have
    inherited the same bug.
  - "shock" was originally "vibration", "power cut" was originally
    "low_battery", and "ignition on"/"ignition off" didn't exist at all
    (only a single "acc" key, which never matches either hardware string) —
    fixed together after a real "Shock" alarm was reported showing the
    generic fallback text instead of a real label.

Every one of these mismatches silently produced the same three failures at
once: the push fell back to the generic "🚨 Device Alarm — Alarm triggered:
<key>" text instead of ALARM_LABELS' real one, severity defaulted to
"medium" instead of the type's real value, and — since a missing
ALARM_SETTING_FIELDS entry skips the per-type-mute check entirely in
_send_push_notification — the matching per-type toggle in Notification
Preferences had no effect on it at all, regardless of what the user set.
"ignition on"/"ignition off" share one AlertSettings column
(acc_push_enabled) same as before; "displacement" has no wire alarm byte
on this hardware at all (see app/api/commands.py's note on the endpoints
removed 2026-09-01) and is left as-is, unreachable but harmless.
"""

from typing import Dict, Set, Tuple

ALARM_LABELS: Dict[str, Tuple[str, str]] = {
    "sos":           ("🆘 SOS Alert",          "Emergency SOS triggered"),
    "shock":         ("📳 Vibration Detected", "Unusual movement detected on your vehicle"),
    "power cut":     ("🔌 Power Cut",          "Vehicle's external power was disconnected"),
    "ignition on":   ("🔑 Ignition On",        "Vehicle ignition turned on"),
    "ignition off":  ("🔑 Ignition Off",       "Vehicle ignition turned off"),
    "over speed":    ("⚡ Overspeed Alert",     "Vehicle exceeded the speed limit"),
    "displacement":  ("📍 Displacement Alert", "Vehicle moved outside the allowed radius"),
    "enter fence":   ("🚧 Geofence Entered",   "Vehicle entered a geofence zone"),
    "exit fence":    ("🚧 Geofence Exited",    "Vehicle exited a geofence zone"),
}

# Fixed severity per alarm type — not stored anywhere, just used to compare
# against AlertSettings.min_push_severity. Unknown alarm types (not in this
# dict) default to "medium" so a new alarm type isn't silently swallowed by
# a "high"-only filter nor always able to bypass a "medium" filter.
ALARM_SEVERITY: Dict[str, str] = {
    "sos": "critical",
    "over speed": "high",
    "displacement": "high",
    "ignition on": "medium",
    "ignition off": "medium",
    "shock": "low",
    "power cut": "low",
    "enter fence": "medium",
    "exit fence": "medium",
}
_SEVERITY_RANK: Dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

ALARM_SETTING_FIELDS: Dict[str, str] = {
    "sos": "sos_push_enabled",
    "shock": "vibration_push_enabled",
    "power cut": "low_battery_push_enabled",
    "ignition on": "acc_push_enabled",
    "ignition off": "acc_push_enabled",
    "over speed": "overspeed_push_enabled",
    "displacement": "displacement_push_enabled",
}

# Alarm types that bypass min_push_severity, per-type mute, the master
# push_notifications_enabled switch, AND push deduplication entirely.
# Deliberate safety decision: a user must not be able to accidentally (or
# otherwise) mute a safety-critical alert via per-device settings, and a
# repeated SOS press must never be suppressed as a "duplicate" — that could
# hide that the user is still in danger. Kept as an explicit set rather than
# derived from ALARM_SEVERITY == "critical" so promoting an alarm type to
# "critical" severity later doesn't silently grant it this bypass too.
CRITICAL_ALARM_TYPES: Set[str] = {"sos"}


def get_severity(alarm_key: str) -> str:
    return ALARM_SEVERITY.get(alarm_key, "medium")


def get_severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(severity, 0)


def is_critical(alarm_key: str) -> bool:
    return alarm_key in CRITICAL_ALARM_TYPES
