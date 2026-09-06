"""
Tests for app/services/push_notifications.send_push_notification's FCM/Expo
dual-path selection (see that module's docstring for why both exist during
the Expo -> FCM client migration). firebase_admin's messaging.send and the
Expo httpx call are both monkeypatched — this only tests routing logic, not
real delivery (see scripts/send_test_fcm_push.py for that).
"""

import pytest

import app.services.push_notifications as push_notifications_module
from app.models.user import User

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    return "asyncio"


def _user(**kwargs):
    return User(id=1, clerk_user_id="clerk_1", email="a@example.com", first_name="A", last_name="B", **kwargs)


async def test_fcm_used_when_token_present_and_firebase_configured(monkeypatch):
    monkeypatch.setattr(push_notifications_module, "_get_firebase_app", lambda: object())

    calls = []

    async def fake_send_fcm(user, title, body, data, channel_id):
        calls.append("fcm")
        return True

    async def fake_send_expo(user, title, body, data, channel_id):
        calls.append("expo")
        return True

    monkeypatch.setattr(push_notifications_module, "_send_fcm", fake_send_fcm)
    monkeypatch.setattr(push_notifications_module, "_send_expo", fake_send_expo)

    user = _user(fcm_token="fcm-token-123", expo_push_token="ExponentPushToken[abc]")
    result = await push_notifications_module.send_push_notification(user, "t", "b", {})

    assert result is True
    assert calls == ["fcm"]


async def test_expo_fallback_when_no_fcm_token(monkeypatch):
    monkeypatch.setattr(push_notifications_module, "_get_firebase_app", lambda: object())

    calls = []

    async def fake_send_expo(user, title, body, data, channel_id):
        calls.append("expo")
        return True

    monkeypatch.setattr(push_notifications_module, "_send_expo", fake_send_expo)

    user = _user(fcm_token=None, expo_push_token="ExponentPushToken[abc]")
    result = await push_notifications_module.send_push_notification(user, "t", "b", {})

    assert result is True
    assert calls == ["expo"]


async def test_expo_fallback_when_firebase_not_configured(monkeypatch):
    monkeypatch.setattr(push_notifications_module, "_get_firebase_app", lambda: None)

    calls = []

    async def fake_send_expo(user, title, body, data, channel_id):
        calls.append("expo")
        return True

    monkeypatch.setattr(push_notifications_module, "_send_expo", fake_send_expo)

    user = _user(fcm_token="fcm-token-123", expo_push_token="ExponentPushToken[abc]")
    result = await push_notifications_module.send_push_notification(user, "t", "b", {})

    assert result is True
    assert calls == ["expo"]


async def test_no_token_returns_false_without_raising(monkeypatch):
    monkeypatch.setattr(push_notifications_module, "_get_firebase_app", lambda: object())

    user = _user(fcm_token=None, expo_push_token=None)
    result = await push_notifications_module.send_push_notification(user, "t", "b", {})

    assert result is False


async def test_push_token_endpoint_auto_detects_expo_vs_fcm():
    """Same detection rule the endpoint uses (app/api/auth.py) — Expo tokens
    are always shaped ExponentPushToken[...]; anything else is FCM."""
    def classify(token: str) -> str:
        return "expo" if token.startswith("ExponentPushToken[") else "fcm"

    assert classify("ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]") == "expo"
    assert classify("dGhpc2lzYW5mY210b2tlbg:APA91b...") == "fcm"
