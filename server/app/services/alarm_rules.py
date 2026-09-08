"""
Shared alarm classification constants.

Extracted from what used to be locals inside TCPServer._send_push_notification
(app/tcp_server.py) so the escalation/digest cron scripts can share the exact
same severity/label/critical rules instead of redefining them and risking
drift.

Keys here must match what TCPServer._send_push_notification actually looks
them up with: `str(data["alarm_type"]).lower()`. Every real alarm_type this
backend ever produces for overspeed is the two-word "Over speed" (the GT06
hardware alarm-byte label in app/protocol_parser.py, and
app/services/speed_limit.py's synthesized alarms both use it) — so the key
below is "over speed" (with the space), not "overspeed". It was originally
"overspeed" (matching the AlertSettings.overspeed_push_enabled *column*
name, not the actual runtime string), which meant every real overspeed
alarm silently missed all three dicts below: it fell back to the generic
"🚨 Device Alarm" label instead of the one meant for it, defaulted to
"medium" severity instead of "high", and — since a missing
ALARM_SETTING_FIELDS entry skips the per-type-mute check entirely in
_send_push_notification — the overspeed_push_enabled toggle had no effect
on it at all. Fixed alongside adding speed_limit.py, whose alarms would
otherwise have inherited the exact same bug.
"""

from typing import Dict, Set, Tuple

ALARM_LABELS: Dict[str, Tuple[str, str]] = {
    "sos":          ("🆘 SOS Alert",          "Emergency SOS triggered"),
    "vibration":    ("📳 Vibration Detected", "Unusual movement detected on your vehicle"),
    "low_battery":  ("🪫 Low Battery",        "GPS tracker battery is running low"),
    "acc":          ("🔑 Ignition Change",    "Vehicle ignition changed state"),
    "over speed":   ("⚡ Overspeed Alert",     "Vehicle exceeded the speed limit"),
    "displacement": ("📍 Displacement Alert", "Vehicle moved outside the allowed radius"),
    "enter fence":  ("🚧 Geofence Entered",   "Vehicle entered a geofence zone"),
    "exit fence":   ("🚧 Geofence Exited",    "Vehicle exited a geofence zone"),
}

# Fixed severity per alarm type — not stored anywhere, just used to compare
# against AlertSettings.min_push_severity. Unknown alarm types (not in this
# dict) default to "medium" so a new alarm type isn't silently swallowed by
# a "high"-only filter nor always able to bypass a "medium" filter.
ALARM_SEVERITY: Dict[str, str] = {
    "sos": "critical",
    "over speed": "high",
    "displacement": "high",
    "acc": "medium",
    "vibration": "low",
    "low_battery": "low",
    "enter fence": "medium",
    "exit fence": "medium",
}
_SEVERITY_RANK: Dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

ALARM_SETTING_FIELDS: Dict[str, str] = {
    "sos": "sos_push_enabled",
    "vibration": "vibration_push_enabled",
    "low_battery": "low_battery_push_enabled",
    "acc": "acc_push_enabled",
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
