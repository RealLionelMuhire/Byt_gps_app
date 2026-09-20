"""
Tests for app/services/intouchpay.py's get_balance() — httpx's AsyncClient is
monkeypatched to a fake implementation, same pattern as
test_email_service.py's EmailJS tests. This only tests request shape and the
non-2xx/non-JSON failure contract, not real IntouchPay connectivity.
"""

import pytest

import app.services.intouchpay as intouchpay_module
from app.core.config import settings

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _intouch_configured(monkeypatch):
    monkeypatch.setattr(settings, "INTOUCH_USERNAME", "user_test")
    monkeypatch.setattr(settings, "INTOUCH_ACCOUNT_NO", "acct_test")
    monkeypatch.setattr(settings, "INTOUCH_PARTNER_PASSWORD", "secret_test")
    monkeypatch.setattr(settings, "INTOUCH_SANDBOX", True)


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body


class _FakeAsyncClient:
    def __init__(self, posts, response, timeout=None):
        self._posts = posts
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self._posts.append({"url": url, "json": json})
        return self._response


def _install_fake_client(monkeypatch, response):
    posts = []

    def factory(*args, **kwargs):
        return _FakeAsyncClient(posts, response, **kwargs)

    monkeypatch.setattr(intouchpay_module.httpx, "AsyncClient", factory)
    return posts


async def test_get_balance_posts_auth_fields_to_sandbox_url(monkeypatch):
    posts = _install_fake_client(
        monkeypatch,
        _FakeResponse(200, {"status": "success", "balance": 125000, "currency": "RWF"}),
    )

    result = await intouchpay_module.get_balance()

    assert result == {"status": "success", "balance": 125000, "currency": "RWF"}
    assert len(posts) == 1
    assert posts[0]["url"] == intouchpay_module._SANDBOX_BALANCE_URL
    body = posts[0]["json"]
    assert body["username"] == "user_test"
    assert body["accountno"] == "acct_test"
    assert "timestamp" in body and "password" in body
    # No endpoint-specific params beyond the four auth fields.
    assert set(body.keys()) == {"username", "accountno", "timestamp", "password"}


async def test_get_balance_uses_production_url_when_not_sandbox(monkeypatch):
    monkeypatch.setattr(settings, "INTOUCH_SANDBOX", False)
    posts = _install_fake_client(
        monkeypatch, _FakeResponse(200, {"status": "success", "balance": 5000, "currency": "RWF"})
    )

    await intouchpay_module.get_balance()

    assert posts[0]["url"] == intouchpay_module._PROD_BALANCE_URL


async def test_get_balance_returns_auth_error_body_without_raising(monkeypatch):
    """An auth failure (e.g. responsecode 0005) comes back as an ordinary
    dict with no "balance" key — not a raised exception — matching the
    same non-raise_for_status contract as request_payment/
    get_transaction_status."""
    _install_fake_client(
        monkeypatch, _FakeResponse(200, {"success": False, "responsecode": "0005", "message": "Invalid Password"})
    )

    result = await intouchpay_module.get_balance()

    assert result["responsecode"] == "0005"
    assert "balance" not in result


async def test_get_balance_raises_on_non_json_response(monkeypatch):
    _install_fake_client(monkeypatch, _FakeResponse(200, json_body=None, text="<html>error</html>"))

    with pytest.raises(intouchpay_module.IntouchPayError):
        await intouchpay_module.get_balance()


async def test_get_balance_raises_on_unreachable_host(monkeypatch):
    import httpx

    class _RaisingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(intouchpay_module.httpx, "AsyncClient", _RaisingClient)

    with pytest.raises(intouchpay_module.IntouchPayError):
        await intouchpay_module.get_balance()


# ── send_deposit ─────────────────────────────────────────────────────────────

async def test_send_deposit_posts_expected_fields_to_sandbox_url(monkeypatch):
    posts = _install_fake_client(
        monkeypatch,
        _FakeResponse(200, {
            "status": "Successfull", "success": True, "responsecode": "2001",
            "transactionid": "01KRP244POCW3042D849FTE4231CJ43FB",
        }),
    )

    result = await intouchpay_module.send_deposit(
        amount=1000, phone="250781234567", transaction_id="ID-1", reason="Refund",
    )

    assert result["responsecode"] == "2001"
    assert len(posts) == 1
    assert posts[0]["url"] == intouchpay_module._SANDBOX_DEPOSIT_URL
    body = posts[0]["json"]
    assert body["amount"] == 1000
    assert body["mobilephone"] == "250781234567"
    assert body["requesttransactionid"] == "ID-1"
    assert body["reason"] == "Refund"
    assert body["withdrawcharge"] == 1
    assert body["sid"] == 1
    # IntouchPay confirmed 2026-09-20 their production server only honors
    # callbackurl for request_payment, never requestdeposit — sending one
    # here would be silently ignored, so it must never be included.
    assert "callbackurl" not in body


async def test_send_deposit_uses_production_url_when_not_sandbox(monkeypatch):
    monkeypatch.setattr(settings, "INTOUCH_SANDBOX", False)
    posts = _install_fake_client(
        monkeypatch, _FakeResponse(200, {"success": True, "responsecode": "2001"})
    )

    await intouchpay_module.send_deposit(
        amount=1000, phone="250781234567", transaction_id="ID-2", reason="Payout",
    )

    assert posts[0]["url"] == intouchpay_module._PROD_DEPOSIT_URL


async def test_send_deposit_rejects_amount_below_minimum_without_calling_intouchpay(monkeypatch):
    posts = _install_fake_client(monkeypatch, _FakeResponse(200, {"success": True}))

    with pytest.raises(intouchpay_module.InvalidDepositAmountError):
        await intouchpay_module.send_deposit(
            amount=50, phone="250781234567", transaction_id="ID-3", reason="Too small",
        )

    assert posts == []  # never reached the network


async def test_send_deposit_returns_rejection_body_without_raising(monkeypatch):
    """A structured rejection (e.g. insufficient funds) is a normal dict
    with success=False, not a raised exception."""
    _install_fake_client(
        monkeypatch,
        _FakeResponse(200, {"success": False, "responsecode": "1108", "message": "Insufficient Account Balance"}),
    )

    result = await intouchpay_module.send_deposit(
        amount=1000, phone="250781234567", transaction_id="ID-4", reason="Refund",
    )

    assert result["success"] is False
    assert result["responsecode"] == "1108"


async def test_send_deposit_raises_on_non_json_response(monkeypatch):
    _install_fake_client(monkeypatch, _FakeResponse(200, json_body=None, text="<html>error</html>"))

    with pytest.raises(intouchpay_module.IntouchPayError):
        await intouchpay_module.send_deposit(
            amount=1000, phone="250781234567", transaction_id="ID-5", reason="Refund",
        )


async def test_classify_status_recognizes_deposit_success_code():
    assert intouchpay_module.classify_status({"success": True, "responsecode": "2001"}) == "successful"
    assert intouchpay_module.classify_status({"success": True, "responsecode": "01"}) == "successful"
    assert intouchpay_module.classify_status({"responsecode": "1000"}) == "pending"
    assert intouchpay_module.classify_status({"success": False, "responsecode": "3100"}) == "unknown"
