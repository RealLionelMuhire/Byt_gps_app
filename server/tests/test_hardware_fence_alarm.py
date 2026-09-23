"""
Tests for handle_alarm's treatment of the GT06 hardware fence alarm bytes
(app/tcp_server.py's HARDWARE_FENCE_ALARM_TYPES): a tracker-reported
"Enter fence"/"Exit fence" is stored as a plain position fix and never
surfaced as an alarm, while real hardware alarms (SOS, ...) and real
server-side geofence crossings are unaffected.

Uses tests/test_tcp_where_reply.py's harness (fake server/writer, SessionLocal
bound to the shared in-memory db_session engine).
"""

from datetime import datetime

import pytest

from app.models.device import Device
from app.models.geofence import Geofence
from app.models.geofence_device import GeofenceDevice
from app.models.geofence_device_state import GeofenceDeviceState
from app.models.location import Location
from app.models.user import User
from tests.test_tcp_where_reply import _make_connection, tcp_env, anyio_backend  # noqa: F401

pytestmark = pytest.mark.anyio

ZONE_LAT, ZONE_LON = -1.9479, 30.1169


@pytest.fixture()
def owned_device(db_session):
    user = User(clerk_user_id="clerk_1", email="o@example.com", first_name="T", last_name="O")
    db_session.add(user)
    db_session.commit()
    device = Device(imei="123456789012345", name="Car", user_id=user.id, lifecycle="sold")
    db_session.add(device)
    db_session.commit()
    return device


def _alarm_packet(alarm_type, lat=ZONE_LAT, lon=ZONE_LON, speed=0):
    return {
        "alarm_type": alarm_type,
        "latitude": lat,
        "longitude": lon,
        "speed": speed,
        "course": 0,
        "satellites": 8,
        "gps_valid": True,
        "timestamp": datetime.utcnow(),
    }


def _add_zone(db_session, device, *, inside, pending=None):
    zone = Geofence(user_id=device.user_id, name="Home", center_latitude=ZONE_LAT,
                    center_longitude=ZONE_LON, radius_meters=200)
    db_session.add(zone)
    db_session.flush()
    db_session.add(GeofenceDevice(geofence_id=zone.id, device_id=device.id))
    db_session.add(GeofenceDeviceState(device_id=device.id, geofence_id=zone.id,
                                       is_inside=inside, pending_is_inside=pending))
    db_session.commit()
    return zone


@pytest.mark.parametrize("alarm_type", ["Enter fence", "Exit fence"])
async def test_hardware_fence_byte_is_stored_as_a_plain_fix_not_an_alarm(
    tcp_env, db_session, owned_device, alarm_type,
):
    conn = _make_connection()
    await conn.handle_alarm(_alarm_packet(alarm_type))

    assert conn.server.alarms == []
    location = db_session.query(Location).filter_by(device_id=owned_device.id).one()
    assert location.is_alarm is False
    assert location.alarm_type is None
    # Still a real GPS report — the device's position is updated from it.
    db_session.refresh(owned_device)
    assert owned_device.last_latitude == pytest.approx(ZONE_LAT)


async def test_parked_inside_zone_hardware_enter_fires_nothing(tcp_env, db_session, owned_device):
    # The reported bug: vehicle already parked inside the zone, tracker
    # sends its own "Enter fence" as it's about to move.
    _add_zone(db_session, owned_device, inside=True)

    conn = _make_connection()
    await conn.handle_alarm(_alarm_packet("Enter fence"))

    assert conn.server.alarms == []
    location = db_session.query(Location).filter_by(device_id=owned_device.id).one()
    assert location.is_alarm is False


async def test_real_server_side_crossing_on_a_hardware_fence_packet_still_alerts(
    tcp_env, db_session, owned_device,
):
    # Previously outside, with one fix already pending "inside" — this fix
    # corroborates it, so evaluate_geofences fires a genuine enter.
    _add_zone(db_session, owned_device, inside=False, pending=True)

    conn = _make_connection()
    await conn.handle_alarm(_alarm_packet("Enter fence"))

    assert len(conn.server.alarms) == 1
    _, data, _ = conn.server.alarms[0]
    assert data["alarm_type"] == "Enter fence"
    assert data["geofence_name"] == "Home"
    location = db_session.query(Location).filter_by(device_id=owned_device.id).one()
    assert location.is_alarm is True
    assert location.alarm_type == "Enter fence"
    assert location.geofence_name == "Home"


async def test_other_hardware_alarms_are_still_surfaced(tcp_env, db_session, owned_device):
    conn = _make_connection()
    await conn.handle_alarm(_alarm_packet("SOS"))

    assert len(conn.server.alarms) == 1
    assert conn.server.alarms[0][1]["alarm_type"] == "SOS"
    location = db_session.query(Location).filter_by(device_id=owned_device.id).one()
    assert location.is_alarm is True
    assert location.alarm_type == "SOS"
