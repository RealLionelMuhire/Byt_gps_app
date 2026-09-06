"""
Shared push notification sender — FCM (preferred) with an Expo fallback.

Extracted so every caller (app/tcp_server.py's alarm notifier,
scripts/cron_expiry.py's subscription-expiry notifier, and the alarm
escalation/digest cron scripts) shares one implementation and one non-fatal
failure contract: a failure here should never break the caller's own flow —
it only logs and returns False.

Migration note: the mobile client used to be Expo-only (User.expo_push_token).
It's moving to FCM (User.fcm_token) — see the auto-detecting PUT
/api/auth/push-token endpoint in app/api/auth.py. Both columns can be
populated at once during the transition (an old app build's Expo token
alongside a newer build's FCM token, across different users), so send_push_notification
picks FCM when available and only falls back to Expo when it isn't. Once
every client is confirmed on FCM, the Expo path and expo_push_token can be
deleted outright.
"""

import json
import logging
from typing import Optional

import httpx

from app.core.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

# Lazily initialized on first use so import doesn't crash servers/tests that
# have no Firebase credentials configured — FCM sending is then just skipped
# in favor of the Expo fallback (or a no-op if neither token is set).
_firebase_app = None
_firebase_init_attempted = False


def _get_firebase_app():
    """Initialize (once) and return the Firebase Admin app, or None if no
    credentials are configured / initialization failed. Never raises."""
    global _firebase_app, _firebase_init_attempted
    if _firebase_init_attempted:
        return _firebase_app
    _firebase_init_attempted = True

    if not settings.FIREBASE_SERVICE_ACCOUNT_JSON and not settings.FIREBASE_SERVICE_ACCOUNT_PATH:
        logger.info("No Firebase credentials configured (FIREBASE_SERVICE_ACCOUNT_JSON/_PATH) — FCM push disabled, Expo fallback only.")
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials

        if settings.FIREBASE_SERVICE_ACCOUNT_JSON:
            cred = credentials.Certificate(json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON))
        else:
            cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_PATH)

        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin SDK initialized — FCM push enabled.")
    except Exception as exc:
        logger.error("Failed to initialize Firebase Admin SDK: %s", exc)
        _firebase_app = None

    return _firebase_app


async def _send_fcm(user: User, title: str, body: str, data: dict, channel_id: Optional[str]) -> bool:
    from firebase_admin import messaging

    # FCM's `data` payload requires string values (unlike Expo's arbitrary JSON).
    string_data = {k: str(v) for k, v in data.items()}

    message = messaging.Message(
        token=user.fcm_token,
        notification=messaging.Notification(title=title, body=body),
        data=string_data,
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(channel_id=channel_id) if channel_id else None,
        ),
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
        ),
    )

    try:
        # messaging.send() is sync (blocking HTTP call) — firebase-admin has
        # no native async client, so this runs in the event loop's default
        # thread pool to avoid blocking other coroutines.
        import asyncio
        response = await asyncio.to_thread(messaging.send, message)
        logger.info("FCM push sent to user %d: %s (message_id=%s)", user.id, title, response)
        return True
    except Exception as exc:
        logger.error("Failed to send FCM push to user %d: %s", user.id, exc)
        return False


async def _send_expo(user: User, title: str, body: str, data: dict, channel_id: Optional[str]) -> bool:
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
            logger.info("Expo push sent to user %d: %s", user.id, title)
            return True
        logger.warning("Expo push API returned %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.error("Failed to send Expo push to user %d: %s", user.id, exc)
        return False


async def send_push_notification(
    user: User, title: str, body: str, data: dict, channel_id: Optional[str] = None
) -> bool:
    """
    Send a push notification to `user`, preferring FCM (user.fcm_token) and
    falling back to Expo (user.expo_push_token) — see the module docstring
    for why both exist. Returns True if the provider accepted it, False
    otherwise (including when the user has no usable token at all — logged
    at debug, not a real failure).

    `channel_id` maps to Android's notification channel (e.g. "gps-alarms");
    omit it for notification types that don't need a dedicated channel.
    """
    if user.fcm_token and _get_firebase_app() is not None:
        return await _send_fcm(user, title, body, data, channel_id)

    if user.expo_push_token:
        return await _send_expo(user, title, body, data, channel_id)

    logger.debug("No usable push token (fcm_token/expo_push_token) for user %d — skipping push", user.id)
    return False
