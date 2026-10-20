"""Overspeed episode — one continuous over-the-limit stretch (migration 055)."""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from app.core.database import Base


class OverspeedEpisode(Base):
    """Opened and closed by app/services/speed_limit.py on the same fixes
    that drive the "Over speed" alarm, so the alarm and the red route
    segment the history endpoints draw from this row are one record.

    end_latitude/end_longitude track the episode's most recent fix while
    it's open (ended_at NULL) and are final once it closes.
    """

    __tablename__ = "overspeed_episodes"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    # The "Over speed" alarm row — NULL when a hardware/geofence alarm
    # already held that fix's single alarm_type slot.
    alarm_location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    start_location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    started_at = Column(DateTime, nullable=False)
    start_latitude = Column(Float, nullable=False)
    start_longitude = Column(Float, nullable=False)
    # Snapshots at open time — the owner may change the limit later.
    threshold_kmh = Column(Float, nullable=False)
    hysteresis_kmh = Column(Float, nullable=False)
    max_speed_kmh = Column(Float, nullable=False)
    last_fix_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    end_latitude = Column(Float, nullable=True)
    end_longitude = Column(Float, nullable=True)
    # speed_dropped | signal_lost | limit_changed
    end_reason = Column(String(20), nullable=True)
