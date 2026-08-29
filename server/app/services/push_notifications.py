"""
Shared Expo push notification sender.

Extracted from app/tcp_server.py's alarm notifier so subscription-expiry
notifications (and any future caller) don't duplicate the Expo Push API
call. Non-fatal by design: a failure here should never break the caller's
own flow (a TCP alarm handler, a cron job, etc.) — it only logs.
"""

import logging
from typing import Optional

import httpx

from app.models.user import User

logger = logging.getLogger(__name__)

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


async def send_push_notification(
    user: User, title: str, body: str, data: dict, channel_id: Optional[str] = None
) -> bool:
    """
    Send an Expo push notification to `user`. Returns True if Expo accepted
    it (HTTP 200), False otherwise (including when the user has no
    expo_push_token — logged at debug, not a real failure).

    `channel_id` maps to Android's notification channel (e.g. "gps-alarms");
    omit it for notification types that don't need a dedicated channel.
    """
    if not user.expo_push_token:
        logger.debug("No expo_push_token for user %d — skipping push", user.id)
        return False

    payload = {
        "to": user.expo_push_token,
        "title": title,
        "body": body,
        "sound": "default",
        "priority": "high",
        "data": data,
    }
    if channel_id:
        payload["channelId"] = channel_id

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(
                _EXPO_PUSH_URL,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        if resp.status_code == 200:
            logger.info("Push notification sent to user %d: %s", user.id, title)
            return True
        logger.warning("Expo push API returned %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.error("Failed to send push notification to user %d: %s", user.id, exc)
        return False
