import asyncio
import sys
import os
from datetime import datetime, timedelta

# Add the server directory to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import func

from app.core.database import SessionLocal
from app.models.location import Location
from app.models.device import Device
from app.models.user import User
from app.services.alarm_rules import CRITICAL_ALARM_TYPES, ALARM_LABELS
from app.services.push_notifications import send_push_notification
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# No multi-contact escalation chain in v1 — a single resend to the same
# device owner is the whole escalation story. This is deliberately simple;
# revisit only if the app's scale ever justifies more.
ESCALATION_WINDOW_MINUTES = 5


async def _resend_escalation_pushes(device_ids_and_alarm_types: list) -> None:
    """Resend the alarm push for each (device_id, alarm_type) pair.
    Runs after the DB commit so a slow/failing push never blocks or rolls
    back the escalated_at bookkeeping itself — re-queries Device/User in a
    fresh session rather than reusing ORM objects from the closed session
    that did the bookkeeping (those are detached and unusable here)."""
    if not device_ids_and_alarm_types:
        return
    db = SessionLocal()
    try:
        for device_id, alarm_type in device_ids_and_alarm_types:
            device = db.query(Device).filter(Device.id == device_id).first()
            if not device or not device.user_id:
                continue
            user = db.query(User).filter(User.id == device.user_id).first()
            if not user:
                continue

            _, body_text = ALARM_LABELS.get(
                alarm_type, ("🚨 Device Alarm", f"Alarm triggered: {alarm_type}")
            )
            await send_push_notification(
                user,
                title="⏰ Still unresolved",
                body=f"{device.name} • {body_text} (not yet acknowledged)",
                data={"type": "alarm_escalation", "device_id": device.id, "alarm_type": alarm_type},
                channel_id="gps-alarms",
            )
    finally:
        db.close()


def escalate_unacknowledged_critical_alarms():
    """
    Resend the push for any CRITICAL alarm (see
    app.services.alarm_rules.CRITICAL_ALARM_TYPES, currently just "sos")
    that's still unacknowledged ESCALATION_WINDOW_MINUTES after it fired.
    Exactly one resend per alarm event, ever — escalated_at being set is
    what prevents this from firing again on the next run. Intended to run
    every minute via crontab, matching cron_expiry.py's pattern.
    """
    db = SessionLocal()
    targets = []  # (device_id, alarm_type) pairs — see _resend_escalation_pushes
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=ESCALATION_WINDOW_MINUTES)

        stuck = (
            db.query(Location)
            .filter(
                Location.is_alarm == True,
                func.lower(Location.alarm_type).in_(CRITICAL_ALARM_TYPES),
                Location.acknowledged_at.is_(None),
                Location.escalated_at.is_(None),
                Location.timestamp <= cutoff,
            )
            .all()
        )

        if not stuck:
            logger.info("No unacknowledged critical alarms past the escalation window.")
            return

        for location in stuck:
            device = db.query(Device).filter(Device.id == location.device_id).first()
            if not device or not device.user_id:
                continue

            location.escalated_at = datetime.utcnow()
            targets.append((device.id, str(location.alarm_type or "").lower()))

        db.commit()
        logger.info(f"Escalated {len(targets)} unacknowledged critical alarm(s).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error while escalating unacknowledged critical alarms: {e}")
        return
    finally:
        db.close()

    asyncio.run(_resend_escalation_pushes(targets))


if __name__ == "__main__":
    escalate_unacknowledged_critical_alarms()
