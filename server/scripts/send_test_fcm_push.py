"""
Standalone FCM smoke test — sends ONE real push notification via the
Firebase Admin SDK to a token you provide, independent of the DB/User model.

This is the closest thing to "proof FCM delivery works" that can be produced
from this backend: real Firebase infrastructure, not a mock. It does NOT
prove delivery to a killed app by itself — that observation has to happen on
the physical device this token came from. Get a real token by running the
(separately migrated) Flutter app on a device and having it call
FirebaseMessaging.instance.getToken(), or read it back from PUT
/api/auth/push-token once the client sends one.

Usage:
    python scripts/send_test_fcm_push.py <fcm_token>
    # or
    FCM_TEST_TOKEN=<token> python scripts/send_test_fcm_push.py

Requires FIREBASE_SERVICE_ACCOUNT_JSON or FIREBASE_SERVICE_ACCOUNT_PATH to
be set (see .env / app/core/config.py) — same credentials the server itself
uses.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.config import settings
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def send_test_push(token: str) -> None:
    import json
    import firebase_admin
    from firebase_admin import credentials, messaging

    if not settings.FIREBASE_SERVICE_ACCOUNT_JSON and not settings.FIREBASE_SERVICE_ACCOUNT_PATH:
        logger.error("No Firebase credentials configured — set FIREBASE_SERVICE_ACCOUNT_JSON or FIREBASE_SERVICE_ACCOUNT_PATH first.")
        sys.exit(1)

    if settings.FIREBASE_SERVICE_ACCOUNT_JSON:
        cred = credentials.Certificate(json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON))
    else:
        cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_PATH)

    firebase_admin.initialize_app(cred)

    message = messaging.Message(
        token=token,
        notification=messaging.Notification(
            title="🔔 FCM test push",
            body="If you see this with the app fully killed, FCM delivery works.",
        ),
        data={"type": "fcm_smoke_test"},
        android=messaging.AndroidConfig(priority="high"),
        apns=messaging.APNSConfig(payload=messaging.APNSPayload(aps=messaging.Aps(sound="default"))),
    )

    response = messaging.send(message)
    logger.info("✅  Sent. FCM message_id=%s — now check the device.", response)


if __name__ == "__main__":
    token = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FCM_TEST_TOKEN")
    if not token:
        print("Usage: python scripts/send_test_fcm_push.py <fcm_token>   (or set FCM_TEST_TOKEN)")
        sys.exit(1)
    send_test_push(token)
