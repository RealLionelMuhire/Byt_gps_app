"""Per (device, alarm_type) push-dedup bookkeeping"""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class AlarmPushState(Base):
    """
    Tracks the last push sent for a given device+alarm_type combination, so
    app.tcp_server.TCPServer._send_push_notification can suppress a
    duplicate push within PUSH_DEDUP_WINDOW_MINUTES without an intervening
    "resolved" state. One row per (device_id, alarm_type); created on first
    push, updated (never inserted again) after that.

    This only gates the push path — it has no effect on broadcast_alarm()'s
    WebSocket send, same as AlertSettings.

    last_alarm_state exists for forward-compatibility: no alarm type in this
    codebase currently emits a distinct "resolved" event (geofence enter/exit
    are two separate alarm_type keys, not a fired/resolved pair of the same
    key), so in practice this stays "fired" and dedup is a pure time-window
    check today.
    """
    __tablename__ = "alarm_push_state"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    alarm_type = Column(String(50), nullable=False)

    last_push_at = Column(DateTime, nullable=True)
    last_alarm_state = Column(String(10), nullable=False, default="fired")  # 'fired' | 'resolved'

    __table_args__ = (UniqueConstraint("device_id", "alarm_type", name="uq_alarm_push_state_device_type"),)

    device = relationship("Device")

    def __repr__(self):
        return f"<AlarmPushState(device_id={self.device_id}, alarm_type='{self.alarm_type}', last_push_at={self.last_push_at})>"
