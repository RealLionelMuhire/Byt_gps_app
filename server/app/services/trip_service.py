"""
Trip service - auto-end active trips when device stops sending.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.trip import Trip
from app.models.location import Location
from app.api.locations import compute_distance_for_device_time_range, location_quality_filters
from app.services.geocoding import build_trip_display_name
from app.services.trip_settings_service import get_or_create_trip_settings

logger = logging.getLogger(__name__)

GEOCODING_TIMEOUT_SECONDS = 15


def trip_arrival_time(trip: Trip, stop_speed_threshold_kmh: float, db: Session) -> datetime:
    """When an active trip actually ended: the first quality fix after its
    last moving one (speed >= stop_speed_threshold_kmh) — i.e. the moment
    the vehicle arrived and parked.

    Deliberately not simply the device's latest fix: the stale checker only
    closes a still-pinging trip after the owner's
    stop_splits_trip_after_minutes of parking, and ending it at "now" would
    fold that whole wait into the trip's duration (inflating it, deflating
    average speed, and letting a short repositioning move pass the
    minimum-duration filter on parking time alone). Falls back to the latest
    fix when the trip never recorded a moving point, and to now when there
    are no fixes at all.
    """
    quality = location_quality_filters(trip.device_id)
    last_moving = (
        db.query(Location)
        .filter(
            *quality,
            Location.timestamp >= trip.start_time,
            Location.speed >= stop_speed_threshold_kmh,
        )
        .order_by(Location.timestamp.desc())
        .first()
    )
    if last_moving is not None:
        arrival = (
            db.query(Location)
            .filter(*quality, Location.timestamp > last_moving.timestamp)
            .order_by(Location.timestamp.asc())
            .first()
        )
        return (arrival or last_moving).timestamp

    last_loc = db.query(Location).filter(*quality).order_by(Location.timestamp.desc()).first()
    return last_loc.timestamp if last_loc else datetime.utcnow()


def end_active_trips_for_device(device_id: int, db: Session, discard_if_short: bool = True) -> int:
    """
    End all active trips (end_time=null) for a device.
    Called when device disconnects or stops sending.
    Sets end_time to the trip's arrival time (see trip_arrival_time), computes distance, geocodes
    display_name — unless discard_if_short and the resulting duration is
    under the owner's minimum_trip_duration_minutes, in which case the trip
    is discarded entirely (deleted) rather than kept as a real trip.
    Auto-start already requires sustained movement (see tcp_server.py), but
    a genuine short movement (e.g. repositioning a few meters) can still
    slip through that gate, so this is the backstop for the automatic
    close paths (stale checker, disconnect) — matches how
    detect_trip_segments already filters retrospective suggestions by the
    same setting. discard_if_short=False for a user-initiated manual stop
    (POST /api/trips/{id}/end) — that's a deliberate action, not noise, and
    the caller there expects the trip to still exist afterward.
    Returns number of trips actually ended (discarded trips don't count).
    """
    active = db.query(Trip).filter(
        Trip.device_id == device_id,
        Trip.end_time.is_(None),
    ).all()

    if not active:
        return 0

    ended = 0
    discarded = 0
    for trip in active:
        trip_settings = get_or_create_trip_settings(trip.user_id, db)
        end_time = trip_arrival_time(trip, trip_settings.stop_speed_threshold_kmh, db)
        min_duration = timedelta(minutes=trip_settings.minimum_trip_duration_minutes)
        if discard_if_short and (end_time - trip.start_time) < min_duration:
            logger.info(
                "Discarding trip %s for device %s — duration %.0fs under the %dmin minimum",
                trip.id, device_id, (end_time - trip.start_time).total_seconds(),
                trip_settings.minimum_trip_duration_minutes,
            )
            db.delete(trip)
            discarded += 1
            continue

        try:
            total_distance, locations = compute_distance_for_device_time_range(
                device_id, trip.start_time, end_time, db
            )
        except Exception as e:
            # Don't close the trip with a placeholder 0.0 km — once end_time
            # is set nothing ever recomputes it, so the trip would read
            # "0.0 km" forever. Roll back this whole pass (including any
            # short-trip discards above, which are re-derived identically)
            # and leave the trip active: the stale checker retries every
            # 60s, and reads compute an active trip's distance live.
            logger.error(
                "Error computing distance while ending trip %s for device %s; leaving it active for retry: %s",
                trip.id, device_id, e, exc_info=True,
            )
            db.rollback()
            return 0

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
        ended += 1

    db.commit()
    logger.info(
        "Ended %d active trip(s) for device %s (%d discarded as under minimum duration)",
        ended, device_id, discarded,
    )
    return ended
