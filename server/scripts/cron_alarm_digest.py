import asyncio
import sys
import os
from collections import defaultdict
from datetime import datetime

# Add the server directory to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.database import SessionLocal
from app.models.location import Location
from app.models.device import Device
from app.models.user import User
from app.services.push_notifications import send_push_notification
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _digest_body(alarm_counts: dict, device_names: set) -> str:
    parts = ", ".join(f"{count} {alarm_type}" for alarm_type, count in sorted(alarm_counts.items()))
    total = sum(alarm_counts.values())
    devices = ", ".join(sorted(device_names))
    plural = "alert" if total == 1 else "alerts"
    return f"{total} new {plural}: {parts} — {devices}"


async def _send_digest_pushes(digest_targets: list) -> None:
    """Send one summary push per user. Runs after the DB commit so a
    slow/failing push never blocks or rolls back the digested_at
    bookkeeping itself — matches cron_expiry.py's pattern. Re-queries User
    in a fresh session rather than reusing an ORM object from the closed
    session that did the bookkeeping (that object is detached and
    unusable here)."""
    if not digest_targets:
        return
    db = SessionLocal()
    try:
        for user_id, alarm_counts, device_names in digest_targets:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                continue
            await send_push_notification(
                user,
                title="📋 Alert summary",
                body=_digest_body(alarm_counts, device_names),
                data={"type": "alarm_digest"},
            )
    finally:
        db.close()


def send_alarm_digests():
    """
    Batch every alarm still awaiting a decision (Location.digested_at IS
    NULL) into one summary push per user. Only alarms that
    TCPServer._send_push_notification (app/tcp_server.py) left un-stamped
    land here — that's specifically alarms suppressed purely by a device's
    min_push_severity threshold (an explicit mute, or an alarm that already
    pushed immediately, is stamped at fire time and never reaches this
    query). See app/services/alarm_rules.py for the severity/critical rules
    this whole pipeline shares.

    digested_at is stamped regardless of whether the push actually succeeds
    — a failed digest push isn't retried forever, it just won't be
    double-sent next run, same non-fatal-push philosophy as cron_expiry.py.
    Intended to run every 15-30 minutes via crontab.
    """
    db = SessionLocal()
    digest_targets = []
    try:
        pending = (
            db.query(Location, Device)
            .join(Device, Device.id == Location.device_id)
            .filter(
                Location.is_alarm == True,
                Location.digested_at.is_(None),
            )
            .all()
        )

        if not pending:
            logger.info("No pending low/medium alarms to digest.")
            return

        by_user = defaultdict(list)
        for location, device in pending:
            if device.user_id:
                by_user[device.user_id].append((location, device))

        for user_id, rows in by_user.items():
            alarm_counts = defaultdict(int)
            device_names = set()
            for location, device in rows:
                alarm_counts[str(location.alarm_type or "unknown").lower()] += 1
                device_names.add(device.name)

            digest_targets.append((user_id, dict(alarm_counts), device_names))

            for location, _ in rows:
                location.digested_at = datetime.utcnow()

        db.commit()
        logger.info(f"Sent digest to {len(digest_targets)} user(s) covering {len(pending)} alarm(s).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error while building alarm digests: {e}")
        return
    finally:
        db.close()

    asyncio.run(_send_digest_pushes(digest_targets))


if __name__ == "__main__":
    send_alarm_digests()
