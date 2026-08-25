"""
Shared helper for fetching/creating a user's trip segmentation settings.

Used by both the trips API (settings CRUD, suggested-trip detection) and the
locations API (period-route), so neither module has to import the other
just for this.
"""

from sqlalchemy.orm import Session

from app.models.trip_settings import TripSettings

DEFAULT_STOP_SPLITS_MINUTES = 60
DEFAULT_MIN_TRIP_MINUTES = 5
DEFAULT_STOP_SPEED_KMH = 5.0
DEFAULT_MIN_STOP_SEGMENT_MINUTES = 10


def get_or_create_trip_settings(user_id: int, db: Session) -> TripSettings:
    """Get user's trip settings, or create defaults."""
    settings = db.query(TripSettings).filter(TripSettings.user_id == user_id).first()
    if not settings:
        settings = TripSettings(
            user_id=user_id,
            stop_splits_trip_after_minutes=DEFAULT_STOP_SPLITS_MINUTES,
            minimum_trip_duration_minutes=DEFAULT_MIN_TRIP_MINUTES,
            stop_speed_threshold_kmh=DEFAULT_STOP_SPEED_KMH,
            min_stop_segment_minutes=DEFAULT_MIN_STOP_SEGMENT_MINUTES,
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings
