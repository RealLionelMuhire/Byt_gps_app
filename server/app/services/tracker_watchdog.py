"""Tracker watchdog: tell admins when a tracker that was reporting goes quiet.

Why (2026-11-04..08, Haojue EX): the tracker's link died at 10:17 and for
almost four days nobody knew. The server logs showed what was going on: the
tracker opened a socket every ~52s that sent nothing ("No login ... Received
nothing."), and for a while it logged in again every 1-2 minutes without ever
sending a fix. That looks like the SIM's data being blocked (bundle/airtime
used up) rather than a server fault, and the tracker's 1000-fix memory filled
up after four hours, so the rest of the outage was lost.

Every TRACKER_WATCHDOG_INTERVAL_SECONDS this looks at every installed device
whose last packet (Device.last_update) is older than:
  - TRACKER_SILENT_WITH_EVIDENCE_MINUTES when the TCP server has seen signs
    of a broken link in that window (sockets that opened and sent nothing,
    or this tracker logging in 2+ times with no data), or
  - TRACKER_SILENT_MINUTES otherwise (a tracker can simply be asleep while
    parked, and that shouldn't page anyone).
It pushes each admin once per silence, and once more when the tracker
reports again. Silences longer than TRACKER_WATCHDOG_MAX_SILENCE_DAYS are
skipped.

Which devices were alerted on is kept in memory, so a restart can repeat at
most one alert per silent device. No migration needed for that.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.device import Device
from app.models.user import Role, User
from app.services.push_notifications import send_push_notification

logger = logging.getLogger(__name__)


@dataclass
class TrackerNotice:
    """One alert or recovery notice the watchdog decided to send."""

    kind: str  # "silent" | "recovered"
    device_id: int
    device_name: str
    imei: str
    last_update: datetime
    title: str
    body: str


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def _duration_text(delta: timedelta) -> str:
    minutes = _minutes(delta)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {rest}m" if rest else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def diagnose(silent_sockets: int, logins_without_data: int) -> str:
    """What the TCP server's evidence says about a silent tracker's link."""
    if logins_without_data >= 2:
        return (
            f"It logged in {logins_without_data} times recently but sent no data: "
            "the mobile link is up but data isn't getting through. Check the SIM's "
            "data bundle and airtime."
        )
    if silent_sockets > 0:
        return (
            f"{silent_sockets} connection(s) from the mobile network opened and sent "
            "nothing: likely this tracker, with its SIM's data blocked. Check the "
            "SIM's data bundle and airtime."
        )
    return (
        "No connection attempts reached the server: the tracker may be powered off, "
        "out of coverage, or its SIM blocked."
    )


class TrackerWatchdog:
    def __init__(
        self,
        server,
        session_factory: Callable = SessionLocal,
        push: Callable = send_push_notification,
        clock: Callable[[], datetime] = datetime.utcnow,
    ):
        # The TCPServer: provides silent_sockets_since() and
        # logins_without_data_since() (connection evidence it records).
        self._server = server
        self._session_factory = session_factory
        self._push = push
        self._clock = clock
        # device_id -> the last_update value it was alerted on.
        self._alerted: Dict[int, datetime] = {}

    # -- DB (runs in a worker thread) ------------------------------------

    def _load(self, oldest: datetime):
        db = self._session_factory()
        try:
            devices = [
                SimpleNamespace(id=d.id, name=d.name, imei=d.imei, last_update=d.last_update)
                for d in db.query(Device)
                .filter(Device.lifecycle != "registered", Device.last_update.isnot(None), Device.last_update >= oldest)
                .all()
            ]
            admins = [
                SimpleNamespace(id=u.id, fcm_token=u.fcm_token, expo_push_token=u.expo_push_token)
                for u in db.query(User).filter(User.role.in_([Role.SUPER_ADMIN, Role.ADMIN])).all()
                if u.fcm_token or u.expo_push_token
            ]
            return devices, admins
        finally:
            db.close()

    # -- decision ----------------------------------------------------------

    def evaluate(self, devices, now: datetime) -> List[TrackerNotice]:
        """Decide which notices to send now, and remember them."""
        evidence_window = timedelta(minutes=settings.TRACKER_SILENT_WITH_EVIDENCE_MINUTES)
        silent_sockets = self._server.silent_sockets_since(now - evidence_window)
        notices: List[TrackerNotice] = []
        for device in devices:
            silence = now - device.last_update
            alerted_at = self._alerted.get(device.id)

            if alerted_at is not None and device.last_update > alerted_at:
                # It reported again after an alert: recovered.
                del self._alerted[device.id]
                gap = device.last_update - alerted_at
                notices.append(TrackerNotice(
                    kind="recovered", device_id=device.id, device_name=device.name, imei=device.imei,
                    last_update=device.last_update,
                    title=f"{device.name} is reporting again",
                    body=f"Back after {_duration_text(gap)} without data.",
                ))
                alerted_at = None

            if alerted_at is not None:
                continue  # already alerted on this silence

            logins = self._server.logins_without_data_since(device.imei, now - evidence_window)
            has_evidence = silent_sockets > 0 or logins >= 2
            threshold = timedelta(
                minutes=settings.TRACKER_SILENT_WITH_EVIDENCE_MINUTES if has_evidence else settings.TRACKER_SILENT_MINUTES
            )
            if silence < threshold:
                continue

            self._alerted[device.id] = device.last_update
            notices.append(TrackerNotice(
                kind="silent", device_id=device.id, device_name=device.name, imei=device.imei,
                last_update=device.last_update,
                title=f"{device.name} stopped reporting",
                body=(
                    f"No data for {_duration_text(silence)} (last heard "
                    f"{device.last_update:%Y-%m-%d %H:%M} UTC). {diagnose(silent_sockets, logins)}"
                ),
            ))

        # Forget devices that dropped out of the window (silent > max days).
        known = {d.id for d in devices}
        for device_id in [d for d in self._alerted if d not in known]:
            del self._alerted[device_id]
        return notices

    # -- loop --------------------------------------------------------------

    async def check_once(self) -> List[TrackerNotice]:
        now = self._clock()
        oldest = now - timedelta(days=settings.TRACKER_WATCHDOG_MAX_SILENCE_DAYS)
        devices, admins = await asyncio.to_thread(self._load, oldest)
        notices = self.evaluate(devices, now)
        for notice in notices:
            log = logger.warning if notice.kind == "silent" else logger.info
            log("Tracker watchdog: %s (IMEI %s): %s. %s",
                notice.device_name, notice.imei, notice.title, notice.body)
            data = {"type": "tracker_health", "kind": notice.kind, "device_id": notice.device_id}
            for admin in admins:
                await self._push(admin, notice.title, notice.body, data)
        return notices

    async def run(self) -> None:
        while True:
            try:
                await asyncio.sleep(settings.TRACKER_WATCHDOG_INTERVAL_SECONDS)
                await self.check_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Tracker watchdog error: %s", e)
