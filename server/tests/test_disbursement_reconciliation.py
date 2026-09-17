"""
Tests for scripts/cron_expiry.py's _reconcile_pending_disbursements_async() —
the fallback path for a Disbursement (B2C deposit, app/api/disbursements.py)
stuck in status="pending" because the IntouchPay webhook was missed. Reuses
test_subscription_expiry_cron.py's `cron_env`/`user` fixtures (same
SessionLocal-monkeypatch pattern) from conftest via that file's fixtures
being module-local — duplicated minimally here rather than imported across
test files, matching this suite's existing convention of one self-contained
fixture set per test file.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import scripts.cron_expiry as cron_expiry_module
from app.models.user import User
from app.models.disbursement import Disbursement


@pytest.fixture()
def cron_env(db_session, monkeypatch):
    engine = db_session.get_bind()
    test_session_local = sessionmaker(bind=engine)
    monkeypatch.setattr(cron_expiry_module, "SessionLocal", test_session_local)
    return test_session_local


@pytest.fixture()
def user(db_session):
    u = User(clerk_user_id="clerk_1", email="owner@example.com", first_name="Test", last_name="Owner")
    db_session.add(u)
    db_session.commit()
    return u


def make_disbursement(db_session, *, created_minutes_ago, status="pending", tx_ref="ID-stuck-1"):
    d = Disbursement(
        clerk_user_id="clerk_1", phone="250781234567", tx_ref=tx_ref,
        amount=1000, currency="RWF", reason="Refund", status=status,
        initiated_by_clerk_user_id="clerk_admin",
        created_at=datetime.utcnow() - timedelta(minutes=created_minutes_ago),
    )
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


async def test_reconciles_stuck_pending_disbursement_to_successful(cron_env, user, db_session, monkeypatch):
    d = make_disbursement(db_session, created_minutes_ago=20)

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "successful", "responsecode": "2001"}

    monkeypatch.setattr(cron_expiry_module, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(cron_expiry_module, "classify_status", lambda resp: "successful")

    await cron_expiry_module._reconcile_pending_disbursements_async()

    db_session.refresh(d)
    assert d.status == "successful"
    assert d.verified_at is not None


async def test_leaves_recently_created_disbursement_untouched(cron_env, user, db_session, monkeypatch):
    d = make_disbursement(db_session, created_minutes_ago=5)  # under PENDING_RECONCILE_AFTER_MINUTES=15

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        raise AssertionError("should not check a disbursement inside the reconcile grace window")

    monkeypatch.setattr(cron_expiry_module, "get_transaction_status", fake_get_transaction_status)

    await cron_expiry_module._reconcile_pending_disbursements_async()

    db_session.refresh(d)
    assert d.status == "pending"


async def test_marks_disbursement_failed_after_hard_timeout(cron_env, user, db_session, monkeypatch):
    d = make_disbursement(db_session, created_minutes_ago=25 * 60)  # past PENDING_HARD_FAIL_AFTER_MINUTES=24h

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "pending"}

    monkeypatch.setattr(cron_expiry_module, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(cron_expiry_module, "classify_status", lambda resp: "unknown")

    await cron_expiry_module._reconcile_pending_disbursements_async()

    db_session.refresh(d)
    assert d.status == "failed"


async def test_leaves_unresolved_disbursement_pending_before_hard_timeout(cron_env, user, db_session, monkeypatch):
    d = make_disbursement(db_session, created_minutes_ago=20)

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        return {"status": "pending"}

    monkeypatch.setattr(cron_expiry_module, "get_transaction_status", fake_get_transaction_status)
    monkeypatch.setattr(cron_expiry_module, "classify_status", lambda resp: "unknown")

    await cron_expiry_module._reconcile_pending_disbursements_async()

    db_session.refresh(d)
    assert d.status == "pending"


async def test_ignores_already_resolved_disbursements(cron_env, user, db_session, monkeypatch):
    d = make_disbursement(db_session, created_minutes_ago=20, status="successful")

    async def fake_get_transaction_status(tx_ref, provider_tx_id=None):
        raise AssertionError("should not re-check an already-resolved disbursement")

    monkeypatch.setattr(cron_expiry_module, "get_transaction_status", fake_get_transaction_status)

    await cron_expiry_module._reconcile_pending_disbursements_async()

    db_session.refresh(d)
    assert d.status == "successful"


def test_reconcile_pending_disbursements_skips_when_unconfigured(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "INTOUCH_USERNAME", "")
    called = []
    monkeypatch.setattr(cron_expiry_module, "_reconcile_pending_disbursements_async", lambda: called.append(True))

    cron_expiry_module.reconcile_pending_disbursements()

    assert called == []


@pytest.fixture(autouse=True)
def anyio_backend():
    return "asyncio"


pytestmark = pytest.mark.anyio
