"""Models package"""

from app.models.device import Device
from app.models.location import Location
from app.models.user import User
from app.models.geofence import Geofence
from app.models.trip import Trip

__all__ = ['Device', 'Location', 'User', 'Geofence', 'Trip']
