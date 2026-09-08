"""
Regression tests for app/services/alarm_rules.py's ALARM_LABELS/
ALARM_SEVERITY/ALARM_SETTING_FIELDS keys: they must match the lowercased
GT06 hardware alarm-byte strings from app/protocol_parser.py's
`alarm_names` (what TCPServer._send_push_notification actually looks them
up with), not the AlertSettings *column* name suffix — see alarm_rules.py's
module docstring for the two real bugs this app has already had from
conflating the two ("overspeed" vs "over speed", then "vibration"/
"low_battery"/"acc" vs "shock"/"power cut"/"ignition on"/"ignition off").

test_speed_limit.py separately covers the "over speed" fix (it lives
alongside speed_limit.py); this file covers the rest.
"""

from app.services.alarm_rules import ALARM_LABELS, ALARM_SETTING_FIELDS, get_severity

# Every string app/protocol_parser.py's `alarm_names` dict can actually
# produce, lowercased exactly as TCPServer._send_push_notification does.
REAL_HARDWARE_ALARM_KEYS = [
    "sos", "power cut", "shock", "enter fence", "exit fence",
    "over speed", "ignition on", "ignition off",
]


def test_every_real_hardware_alarm_key_has_its_own_label_not_the_fallback():
    for key in REAL_HARDWARE_ALARM_KEYS:
        assert key in ALARM_LABELS, f"{key!r} falls back to the generic '🚨 Device Alarm' label"


def test_shock_resolves_to_the_vibration_label_and_mute_toggle():
    """"Shock" is the real hardware string (protocol_parser.py alarm byte
    0x03) for what the app calls "Vibration" in the UI — it must map to
    vibration_push_enabled, not fall through unmuteable."""
    assert "Vibration" in ALARM_LABELS["shock"][0]
    assert ALARM_SETTING_FIELDS["shock"] == "vibration_push_enabled"
    assert get_severity("shock") == "low"


def test_power_cut_resolves_to_the_low_battery_mute_toggle():
    """"Power cut" (alarm byte 0x02, and the POWERALM SMS command's own
    alarm) maps to low_battery_push_enabled — that AlertSettings column's
    name predates the realization that this hardware has no actual
    battery-level alarm, only external-power-disconnected."""
    assert "Power Cut" in ALARM_LABELS["power cut"][0]
    assert ALARM_SETTING_FIELDS["power cut"] == "low_battery_push_enabled"


def test_ignition_on_and_off_both_resolve_to_the_acc_mute_toggle():
    """Two distinct hardware strings (alarm bytes 0x07/0x08), one shared
    AlertSettings column — muting "Ignition (ACC)" in Notification
    Preferences must silence both."""
    assert ALARM_SETTING_FIELDS["ignition on"] == "acc_push_enabled"
    assert ALARM_SETTING_FIELDS["ignition off"] == "acc_push_enabled"
    assert "Ignition On" in ALARM_LABELS["ignition on"][0]
    assert "Ignition Off" in ALARM_LABELS["ignition off"][0]


def test_none_of_the_old_pre_fix_keys_linger_in_any_dict():
    """The bug being regression-tested here: these three used to be the
    dict keys instead of the real hardware strings above, which meant a
    real alarm of that type always fell back to the generic label/severity
    and had its mute toggle silently ignored."""
    for stale_key in ("vibration", "low_battery", "acc"):
        assert stale_key not in ALARM_LABELS
        assert stale_key not in ALARM_SETTING_FIELDS
