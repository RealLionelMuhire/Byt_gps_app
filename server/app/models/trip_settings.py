"""Trip settings model - user preferences for trip segmentation"""

from sqlalchemy import Column, Integer, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class TripSettings(Base):
    """User preferences for automatic trip detection and segmentation"""
    __tablename__ = "trip_settings"
    __table_args__ = (UniqueConstraint("user_id", name="uq_trip_settings_user_id"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)

    # Stop duration (minutes): if vehicle stopped longer than this, start a new trip
    # e.g. 60 = 1 hour stop splits into two trips; 10 = 10 min stop = short stop, same trip
    stop_splits_trip_after_minutes = Column(Integer, nullable=False, default=60)

    # Minimum trip duration (minutes): segments shorter than this are ignored
    minimum_trip_duration_minutes = Column(Integer, nullable=False, default=5)

    # Speed threshold (km/h): below this = "stopped" for segmentation
    stop_speed_threshold_kmh = Column(Float, nullable=False, default=5.0)

    # Relationship
    user = relationship("User", backref="trip_settings")

    def __repr__(self):
        return f"<TripSettings(user_id={self.user_id}, stop_splits={self.stop_splits_trip_after_minutes}min)>"
