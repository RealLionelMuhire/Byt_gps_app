"""Alert settings model - per-device alarm-type mute/severity/push preferences"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base


class AlertSettings(Base):
    """
    Per-device preferences for the alarm types tcp_server.py already knows
    about — see ALARM_LABELS in TCPConnection._send_push_notification and
    the six toggle endpoints (alarm/vibration, alarm/lowbattery, alarm/acc,
    alarm/overspeed, alarm/displacement, alarm/sos) in app/api/commands.py.

    One row per device. Absence of a row means "everything enabled" — same
    convention as TripSettings/CommandSettings.

    These flags are meant to be checked in
    TCPConnection._send_push_notification, before it calls
    send_push_notification() — they gate the push path only. They do NOT
    affect broadcast_alarm()'s WebSocket send: every alarm still reaches
    connected WS clients regardless of these settings, matching how alerts
    are surfaced live in the app vs. how they interrupt the user via push.
    """
    __tablename__ = "alert_settings"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    # Master switch: if False, no push fires for this device regardless of
    # the per-type flags or min_push_severity below.
    push_notifications_enabled = Column(Boolean, nullable=False, default=True)

    # Per-alarm-type push toggles — one per key in tcp_server.py's
    # ALARM_LABELS dict.
    sos_push_enabled = Column(Boolean, nullable=False, default=True)
    vibration_push_enabled = Column(Boolean, nullable=False, default=True)
    low_battery_push_enabled = Column(Boolean, nullable=False, default=True)
    acc_push_enabled = Column(Boolean, nullable=False, default=True)
    overspeed_push_enabled = Column(Boolean, nullable=False, default=True)
    displacement_push_enabled = Column(Boolean, nullable=False, default=True)

    # Coarse filter applied on top of the per-type flags above: only push
    # alarms whose severity is >= this value. Severity itself isn't stored
    # anywhere yet — it would be a fixed mapping in app code alongside
    # ALARM_LABELS (e.g. sos/overspeed/displacement=high, acc=medium,
    # vibration/low_battery=low). 'low' (default) means no extra filtering
    # beyond the per-type flags. Values: low | medium | high. String rather
    # than a DB enum, validated at the API layer — matches Device.lifecycle /
    # Device.marker_icon's existing convention in this codebase.
    min_push_severity = Column(String(10), nullable=False, default='low')

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    device = relationship("Device", backref="alert_settings", uselist=False)

    def __repr__(self):
        return f"<AlertSettings(device_id={self.device_id}, push_enabled={self.push_notifications_enabled}, min_severity='{self.min_push_severity}')>"
