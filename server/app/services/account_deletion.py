"""
Account deletion: everything that happens to a user's data when their
account is deleted — whether from the app's own "Delete account" button
(DELETE /api/auth/me) or from Clerk's user.deleted webhook.

What is removed vs. kept follows the in-app Privacy Policy ("Data
Retention" / "Your Rights"): personal data and location history are
deleted; the financial record (payments, subscriptions, disbursements) is
kept, since that's the legal-retention carve-out the policy names.
"""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.contact_request import ContactRequest
from app.models.device import Device
from app.models.entitlement import EntitlementCheckLog
from app.models.geofence import Geofence
from app.models.geofence_device import GeofenceDevice
from app.models.geofence_device_state import GeofenceDeviceState
from app.models.geofence_version import GeofenceVersion
from app.models.location import Location
from app.models.location_quality_log import LocationQualityLog
from app.models.subscription import Subscription
from app.models.trip import Trip
from app.models.trip_settings import TripSettings
from app.models.user import User
from app.models.vehicle import Vehicle

logger = logging.getLogger(__name__)


def delete_user_data(db: Session, user: User) -> None:
    """
    Delete [user] and their personal data, and free their devices for
    re-pairing. Commits on success; rolls back and re-raises on failure.

    Every child table is cleared with an explicit bulk delete BEFORE
    db.delete(user) rather than relying on the DB's ON DELETE CASCADE: the
    User model's ORM backrefs (trips, geofences, trip_settings) have no
    delete cascade, so deleting the user while those rows still exist makes
    SQLAlchemy try to NULL their non-nullable user_id first — which is
    exactly how this used to fail for any user with a trip or a geofence.
    """
    clerk_user_id = user.clerk_user_id
    try:
        device_ids = [d.id for d in db.query(Device.id).filter(Device.user_id == user.id)]

        # Trips reference locations (start/end), so they go first — both the
        # user's own and any left on their devices by a previous owner.
        trip_filter = Trip.user_id == user.id
        if device_ids:
            trip_filter = trip_filter | Trip.device_id.in_(device_ids)
        db.query(Trip).filter(trip_filter).delete(synchronize_session=False)

        # Location history (alarms included — they're location rows too) of
        # the devices being freed, so the next owner never sees it.
        if device_ids:
            db.query(LocationQualityLog).filter(
                LocationQualityLog.device_id.in_(device_ids)
            ).delete(synchronize_session=False)
            db.query(Location).filter(
                Location.device_id.in_(device_ids)
            ).delete(synchronize_session=False)

        geofence_ids = [g.id for g in db.query(Geofence.id).filter(Geofence.user_id == user.id)]
        if geofence_ids:
            db.query(GeofenceDeviceState).filter(
                GeofenceDeviceState.geofence_id.in_(geofence_ids)
            ).delete(synchronize_session=False)
            db.query(GeofenceDevice).filter(
                GeofenceDevice.geofence_id.in_(geofence_ids)
            ).delete(synchronize_session=False)
        db.query(GeofenceVersion).filter(GeofenceVersion.user_id == user.id).delete(synchronize_session=False)
        db.query(Geofence).filter(Geofence.user_id == user.id).delete(synchronize_session=False)

        db.query(TripSettings).filter(TripSettings.user_id == user.id).delete(synchronize_session=False)
        db.query(EntitlementCheckLog).filter(
            EntitlementCheckLog.owner_user_id == user.id
        ).delete(synchronize_session=False)
        db.query(ContactRequest).filter(
            ContactRequest.clerk_user_id == clerk_user_id
        ).delete(synchronize_session=False)

        # Free the user's devices so they can be paired by someone else.
        # 'in_stock', not 'registered' — this device already proved TCP
        # connectivity to get to 'sold' in the first place; only its
        # ownership is being cleared. 'registered' would wrongly claim it has
        # never connected (see Device model's transition table: sold ->
        # in_stock on customer removal, never sold -> registered).
        for device in db.query(Device).filter(Device.user_id == user.id):
            device.user_id = None
            device.lifecycle = "in_stock"
            logger.info("Freed device %s from deleted user %s.", device.imei, user.id)

        db.query(Vehicle).filter(Vehicle.clerk_user_id == clerk_user_id).delete(synchronize_session=False)

        # Keep subscriptions and payments: they're the financial record of
        # money actually received (and possibly refunded — disbursements
        # reference payments.id, so deleting a refunded payment would fail
        # the whole deletion). Both are keyed by clerk_user_id, not a users
        # FK, so they survive the user row; the admin dashboard shows them as
        # a deleted account. Only end the subscription.
        db.query(Subscription).filter(
            Subscription.clerk_user_id == clerk_user_id,
            Subscription.status == "active",
        ).update(
            {Subscription.status: "cancelled", Subscription.updated_at: datetime.utcnow()},
            synchronize_session=False,
        )

        db.delete(user)
        db.commit()
        logger.info("Deleted user %s and freed %d device(s).", clerk_user_id, len(device_ids))
    except Exception:
        db.rollback()
        raise
