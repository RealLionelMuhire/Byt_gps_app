"""
Route-level tests for the admin disbursement API (app/api/disbursements.py)
— POST/GET /api/admin/disbursements, wrapping app/services/intouchpay.py's
send_deposit(). intouch send_deposit is monkeypatched at the module level it
was imported under (app.api.disbursements.send_deposit) so these tests
never make a real IntouchPay call.

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

from datetime import datetime

from app.api import disbursements as disbursements_module
from app.models.user import User, Role
from app.models.subscription import Payment, SubscriptionPlan
from app.models.disbursement import Disbursement
from app.services.intouchpay import IntouchPayError, InvalidDepositAmountError


def make_user(db, clerk_id, role=Role.USER, email=None):
    user = User(
        clerk_user_id=clerk_id,
        email=email or f"{clerk_id}@example.com",
        first_name="Test",
        last_name="User",
        role=role,
        onboarding_step=0,
        onboarding_complete=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_plan(db, slug="basic"):
    plan = SubscriptionPlan(
        name=slug.capitalize(), slug=slug, billing_type="recurrent", billing_model="prepaid",
        charge_scope="flat", price=2450, currency="RWF", duration_value=1,
        duration_unit="month", max_devices=3, is_active=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def make_payment(db, user, plan, tx_ref="tx-orig"):
    payment = Payment(
        clerk_user_id=user.clerk_user_id, tx_ref=tx_ref, plan_id=plan.id,
        amount=2450, currency="RWF", status="successful", verified_at=datetime.utcnow(),
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def as_admin(current_clerk_id, admin):
    current_clerk_id["value"] = admin.clerk_user_id


def test_non_admin_forbidden(client, db_session, current_clerk_id):
    target = make_user(db_session, "clerk_target")
    current_clerk_id["value"] = target.clerk_user_id

    resp = client.post("/api/admin/disbursements", json={
        "user_id": target.id, "phone": "250781234567", "amount": 1000, "reason": "Refund",
    })
    assert resp.status_code == 403


def test_create_disbursement_successful(client, db_session, current_clerk_id, monkeypatch):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    as_admin(current_clerk_id, admin)

    async def fake_send_deposit(**kwargs):
        return {"success": True, "responsecode": "2001", "status": "Successfull", "transactionid": "PROV-1"}

    monkeypatch.setattr(disbursements_module, "send_deposit", fake_send_deposit)

    resp = client.post("/api/admin/disbursements", json={
        "user_id": target.id, "phone": "250781234567", "amount": 1000, "reason": "Refund",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "successful"
    assert body["provider_transaction_id"] == "PROV-1"
    assert body["initiated_by_clerk_user_id"] == admin.clerk_user_id
    assert body["clerk_user_id"] == target.clerk_user_id

    row = db_session.query(Disbursement).filter(Disbursement.tx_ref == body["tx_ref"]).one()
    assert row.status == "successful"
    assert row.verified_at is not None


def test_create_disbursement_rejected_by_intouchpay(client, db_session, current_clerk_id, monkeypatch):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    as_admin(current_clerk_id, admin)

    async def fake_send_deposit(**kwargs):
        return {"success": False, "responsecode": "1108", "message": "Insufficient Account Balance"}

    monkeypatch.setattr(disbursements_module, "send_deposit", fake_send_deposit)

    resp = client.post("/api/admin/disbursements", json={
        "user_id": target.id, "phone": "250781234567", "amount": 1000, "reason": "Refund",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "failed"

    row = db_session.query(Disbursement).filter(Disbursement.tx_ref == body["tx_ref"]).one()
    assert row.status == "failed"


def test_create_disbursement_left_pending_when_intouchpay_unreachable(client, db_session, current_clerk_id, monkeypatch):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    as_admin(current_clerk_id, admin)

    async def fake_send_deposit(**kwargs):
        raise IntouchPayError("connection refused")

    monkeypatch.setattr(disbursements_module, "send_deposit", fake_send_deposit)

    resp = client.post("/api/admin/disbursements", json={
        "user_id": target.id, "phone": "250781234567", "amount": 1000, "reason": "Refund",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"

    # The row exists even though IntouchPay was never confirmed reached —
    # this is the whole point of committing before the call.
    row = db_session.query(Disbursement).filter(Disbursement.tx_ref == body["tx_ref"]).one()
    assert row.status == "pending"


def test_create_disbursement_below_minimum_amount_marks_row_failed(client, db_session, current_clerk_id, monkeypatch):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    as_admin(current_clerk_id, admin)

    async def fake_send_deposit(**kwargs):
        raise InvalidDepositAmountError("Deposit amount must be at least 100 RWF (got 50).")

    monkeypatch.setattr(disbursements_module, "send_deposit", fake_send_deposit)

    resp = client.post("/api/admin/disbursements", json={
        "user_id": target.id, "phone": "250781234567", "amount": 50, "reason": "Too small",
    })
    assert resp.status_code == 400

    row = db_session.query(Disbursement).filter(Disbursement.clerk_user_id == target.clerk_user_id).one()
    assert row.status == "failed"


def test_create_disbursement_unknown_user_404(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    as_admin(current_clerk_id, admin)

    resp = client.post("/api/admin/disbursements", json={
        "user_id": 999999, "phone": "250781234567", "amount": 1000, "reason": "Refund",
    })
    assert resp.status_code == 404


def test_create_disbursement_with_reference_payment_from_another_user_rejected(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    other = make_user(db_session, "clerk_other")
    plan = make_plan(db_session)
    payment = make_payment(db_session, other, plan)
    as_admin(current_clerk_id, admin)

    resp = client.post("/api/admin/disbursements", json={
        "user_id": target.id, "phone": "250781234567", "amount": 1000,
        "reason": "Refund", "reference_payment_id": payment.id,
    })
    assert resp.status_code == 400


def test_create_disbursement_with_valid_reference_payment(client, db_session, current_clerk_id, monkeypatch):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    plan = make_plan(db_session)
    payment = make_payment(db_session, target, plan)
    as_admin(current_clerk_id, admin)

    async def fake_send_deposit(**kwargs):
        return {"success": True, "responsecode": "2001", "transactionid": "PROV-2"}

    monkeypatch.setattr(disbursements_module, "send_deposit", fake_send_deposit)

    resp = client.post("/api/admin/disbursements", json={
        "user_id": target.id, "phone": "250781234567", "amount": 2450,
        "reason": "Refund for cancelled plan", "reference_payment_id": payment.id,
    })
    assert resp.status_code == 201
    assert resp.json()["reference_payment_id"] == payment.id


def test_list_disbursements_filtered_by_user(client, db_session, current_clerk_id, monkeypatch):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    target = make_user(db_session, "clerk_target")
    other = make_user(db_session, "clerk_other")
    as_admin(current_clerk_id, admin)

    async def fake_send_deposit(**kwargs):
        return {"success": True, "responsecode": "2001", "transactionid": "PROV-3"}

    monkeypatch.setattr(disbursements_module, "send_deposit", fake_send_deposit)

    client.post("/api/admin/disbursements", json={
        "user_id": target.id, "phone": "250781234567", "amount": 1000, "reason": "A",
    })
    client.post("/api/admin/disbursements", json={
        "user_id": other.id, "phone": "250789999999", "amount": 2000, "reason": "B",
    })

    resp = client.get(f"/api/admin/disbursements?user_id={target.id}")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["clerk_user_id"] == target.clerk_user_id

    resp_all = client.get("/api/admin/disbursements")
    assert len(resp_all.json()) == 2


def test_list_disbursements_non_admin_forbidden(client, db_session, current_clerk_id):
    user = make_user(db_session, "clerk_regular")
    current_clerk_id["value"] = user.clerk_user_id

    resp = client.get("/api/admin/disbursements")
    assert resp.status_code == 403
