"""
The fixed catalog of tracker capabilities — what the apps and this backend
understand a command to MEAN (cut fuel, ask for location, arm an alarm).

Which capabilities a tracker supports, and the exact command text for
each, is data: one TrackerModel row per hardware model, edited in the
admin portal (see app/models/tracker_model.py). The catalog itself stays
in code because behavior hangs off each key — fuel.cut needs a
confirmation and the relay-state follow-up, location.query's reply is
parsed into a fix, and the apps render a specific control per key.

A model can also carry admin-only "extra" commands (key "extra.<slug>")
that are not in this catalog; those are sent from the admin portal only
and never shown to customers.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    minimum: int
    maximum: int
    default: int


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    group: str
    description: str = ""
    params: Tuple[Param, ...] = field(default_factory=tuple)
    # Immobilizes the vehicle — needs the confirmation step and the
    # per-device fuel_cut_enabled switch (CommandSettings).
    destructive: bool = False
    # False: admins only, even once verified (device housekeeping).
    customer_visible: bool = True


# Alert-delivery mode shared by the G900LS alarm commands: 0 = GPRS only
# (the only mode guaranteed to reach this backend), higher values also
# SMS/call the tracker's CENTER numbers.
_MODE_0_2 = Param("mode", "Delivery mode", 0, 2, 0)
_MODE_0_3 = Param("mode", "Delivery mode", 0, 3, 0)

CAPABILITIES: List[Capability] = [
    Capability("fuel.cut", "Cut fuel", "Fuel",
               "Immobilize the vehicle (relay on).", destructive=True),
    Capability("fuel.restore", "Restore fuel", "Fuel", "Re-enable the vehicle (relay off)."),
    Capability("fuel.query", "Check fuel status", "Fuel",
               "Ask the tracker whether the relay is cut. Also used to confirm a deferred cut."),
    Capability("location.query", "Locate now", "Queries", "Ask the tracker for its current position."),
    Capability("status.query", "Device status", "Queries", "Battery, GPS, GSM and ignition status."),
    Capability("alarm.vibration.on", "Vibration alarm on", "Alarms", params=(_MODE_0_2,)),
    Capability("alarm.vibration.off", "Vibration alarm off", "Alarms"),
    Capability("alarm.power_cut.on", "Power-cut alarm on", "Alarms", params=(
        _MODE_0_2,
        Param("detect_seconds", "Detection time (s)", 2, 60, 5),
        Param("min_charge_seconds", "Minimum charge time (s)", 1, 3600, 10),
    )),
    Capability("alarm.power_cut.off", "Power-cut alarm off", "Alarms"),
    Capability("alarm.ignition_on.on", "Ignition-on alarm on", "Alarms", params=(_MODE_0_3,)),
    Capability("alarm.ignition_on.off", "Ignition-on alarm off", "Alarms"),
    Capability("alarm.ignition_off.on", "Ignition-off alarm on", "Alarms", params=(_MODE_0_3,)),
    Capability("alarm.ignition_off.off", "Ignition-off alarm off", "Alarms"),
    Capability("device.restart", "Restart tracker", "Device", customer_visible=False),
    Capability("interval.set", "Set reporting interval", "Device", params=(
        Param("moving_seconds", "While moving (s)", 5, 3600, 20),
        Param("parked_seconds", "While parked (s)", 10, 86400, 300),
    ), customer_visible=False),
]
CAPABILITIES_BY_KEY: Dict[str, Capability] = {c.key: c for c in CAPABILITIES}

EXTRA_PREFIX = "extra."


def is_extra(key: str) -> bool:
    return key.startswith(EXTRA_PREFIX)


def capability(key: str) -> Optional[Capability]:
    return CAPABILITIES_BY_KEY.get(key)


def catalog_payload() -> List[dict]:
    """The catalog as JSON for the admin portal and the apps."""
    return [capability_payload(c) for c in CAPABILITIES]


def capability_payload(c: Capability) -> dict:
    return {
        "key": c.key, "label": c.label, "group": c.group, "description": c.description,
        "destructive": c.destructive, "customer_visible": c.customer_visible,
        "params": [
            {"name": p.name, "label": p.label, "min": p.minimum, "max": p.maximum, "default": p.default}
            for p in c.params
        ],
    }
