"""
Trip service - auto-end active trips when device stops sending.
"""

import asyncio
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.trip import Trip
from app.models.location import Location
from app.api.locations import compute_distance_for_device_time_range
from app.services.geocoding import build_trip_display_name

logger = logging.getLogger(__name__)

GEOCODING_TIMEOUT_SECONDS = 15


def end_active_trips_for_device(device_id: int, db: Session) -> int:
    """
    End all active trips (end_time=null) for a device.
    Called when device disconnects or stops sending.
    Sets end_time to last location timestamp, computes distance, geocodes display_name.
    Returns number of trips ended.
    """
    active = db.query(Trip).filter(
        Trip.device_id == device_id,
        Trip.end_time.is_(None),
    ).all()

    if not active:
        return 0

    # Get last GPS-valid location for this device
    last_loc = (
        db.query(Location)
        .filter(
            Location.device_id == device_id,
            Location.gps_valid == True,
        )
        .order_by(Location.timestamp.desc())
        .first()
    )

    end_time = last_loc.timestamp if last_loc else datetime.utcnow()

    for trip in active:
        try:
            total_distance, locations = compute_distance_for_device_time_range(
                device_id, trip.start_time, end_time, db
            )
            trip.end_time = end_time
            trip.total_distance_km = total_distance
            if locations:
                trip.end_location_id = locations[-1].id
                # Geocode display_name (sync, avoid blocking TCP handler too long)
                try:
                    display_name = build_trip_display_name(
                        locations[0].latitude,
                        locations[0].longitude,
                        locations[-1].latitude,
                        locations[-1].longitude,
                    )
                    trip.display_name = display_name
                except Exception as e:
                    logger.warning("Geocoding failed for trip %s: %s", trip.id, e)
                    trip.display_name = (
                        f"{locations[0].latitude:.4f}, {locations[0].longitude:.4f} → "
                        f"{locations[-1].latitude:.4f}, {locations[-1].longitude:.4f}"
                    )
        except Exception as e:
            logger.error("Error ending trip %s: %s", trip.id, e)
            trip.end_time = end_time
            trip.total_distance_km = 0.0

    db.commit()
    logger.info("Ended %d active trip(s) for device %s", len(active), device_id)
    return len(active)
