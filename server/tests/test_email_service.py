"""
Tests for app/services/email.py's EmailJS REST integration. httpx's
AsyncClient is monkeypatched to a fake implementation — this only tests
send_email's request shape and its non-fatal failure contract (mirroring
test_push_notifications.py's approach for FCM/Expo), not real EmailJS
delivery.
"""

import pytest

import app.services.email as email_module
from app.core.config import settings
from app.models.user import User
from app.models.subscription import Payment, Subscription

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _emailjs_configured(monkeypatch):
    monkeypatch.setattr(settings, "EMAILJS_SERVICE_ID", "service_test")
    monkeypatch.setattr(settings, "EMAILJS_PRIVATE_KEY", "private_test")
    monkeypatch.setattr(settings, "EMAILJS_PUBLIC_KEY", "public_test")
    monkeypatch.setattr(settings, "EMAILJS_TEMPLATE_ID_RECEIPT", "template_receipt")
    monkeypatch.setattr(settings, "EMAILJS_TEMPLATE_ID_EXPIRING", "template_expiring")
    monkeypatch.setattr(settings, "EMAILJS_TEMPLATE_ID_EXPIRED", "template_expired")
    monkeypatch.setattr(settings, "EMAILJS_TEMPLATE_ID_PAYMENT_FAILED", "template_failed")


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Records the last request, returns a status code fixed at construction
    of the recorder (see the `posts` list closed over by _make_fake_client)."""

    def __init__(self, posts, status_code, timeout=None):
        self._posts = posts
        self._status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self._posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._status_code)


def _install_fake_client(monkeypatch, status_code=200):
    posts = []

    def factory(*args, **kwargs):
        return _FakeAsyncClient(posts, status_code, **kwargs)

    monkeypatch.setattr(email_module.httpx, "AsyncClient", factory)
    return posts


def _user(**kwargs):
    return User(id=1, clerk_user_id="clerk_1", email="a@example.com", first_name="A", last_name="B", **kwargs)


async def test_send_email_posts_expected_payload(monkeypatch):
    posts = _install_fake_client(monkeypatch, status_code=200)

    result = await email_module.send_email(
        to_email="a@example.com",
        to_name="A B",
        template_id="template_receipt",
        template_params={"amount": "2450"},
    )

    assert result is True
    assert len(posts) == 1
    body = posts[0]["json"]
    assert body["service_id"] == "service_test"
    assert body["template_id"] == "template_receipt"
    assert body["user_id"] == "public_test"
    assert body["accessToken"] == "private_test"
    assert body["template_params"]["to_email"] == "a@example.com"
    assert body["template_params"]["to_name"] == "A B"
    assert body["template_params"]["amount"] == "2450"


async def test_send_email_returns_false_on_non_200(monkeypatch):
    _install_fake_client(monkeypatch, status_code=422)

    result = await email_module.send_email(
        to_email="a@example.com", to_name="A", template_id="template_receipt", template_params={},
    )

    assert result is False


async def test_send_email_skips_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "EMAILJS_SERVICE_ID", None)
    posts = _install_fake_client(monkeypatch, status_code=200)

    result = await email_module.send_email(
        to_email="a@example.com", to_name="A", template_id="template_receipt", template_params={},
    )

    assert result is False
    assert posts == []


async def test_send_email_skips_when_template_missing(monkeypatch):
    posts = _install_fake_client(monkeypatch, status_code=200)

    result = await email_module.send_email(
        to_email="a@example.com", to_name="A", template_id=None, template_params={},
    )

    assert result is False
    assert posts == []


async def test_send_email_returns_false_without_raising_on_network_error(monkeypatch):
    def factory(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(email_module.httpx, "AsyncClient", factory)

    result = await email_module.send_email(
        to_email="a@example.com", to_name="A", template_id="template_receipt", template_params={},
    )

    assert result is False


async def test_send_payment_receipt_email_uses_receipt_template(monkeypatch):
    posts = _install_fake_client(monkeypatch, status_code=200)
    user = _user()
    # plan_id is a real FK (migrations 041/042) but this Payment is never
    # persisted/queried here — plan_name is passed explicitly below — so an
    # arbitrary int is fine; no subscription_plans row is needed.
    payment = Payment(clerk_user_id="clerk_1", tx_ref="IPabc", plan_id=1, amount=2450, currency="RWF", status="successful")

    result = await email_module.send_payment_receipt_email(user, payment, "Basic")

    assert result is True
    assert posts[0]["json"]["template_id"] == "template_receipt"
    assert posts[0]["json"]["template_params"]["amount"] == "2,450"
    assert posts[0]["json"]["template_params"]["currency"] == "RWF"


async def test_send_subscription_expiring_email_uses_expiring_template(monkeypatch):
    from datetime import datetime, timedelta

    posts = _install_fake_client(monkeypatch, status_code=200)
    user = _user()
    # plan_id is a real FK (migrations 041/042) but this Subscription is
    # never persisted/queried here — plan_name is passed explicitly below —
    # so an arbitrary int is fine; no subscription_plans row is needed.
    sub = Subscription(clerk_user_id="clerk_1", plan_id=1, status="active", price=2450, expires_at=datetime.utcnow() + timedelta(days=3))

    result = await email_module.send_subscription_expiring_email(user, sub, "Basic", days_left=3)

    assert result is True
    assert posts[0]["json"]["template_id"] == "template_expiring"
    assert posts[0]["json"]["template_params"]["days_left"] == "3"
