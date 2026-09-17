"""
Admin-only IntouchPay disbursement API (`POST /requestdeposit/` — B2C push).

Every route here requires require_admin. There is no self-service or
automatic trigger anywhere in this codebase (e.g. cancelling a subscription
does NOT create one) — see app/models/disbursement.py for why that's a
deliberate choice, not an oversight: whether a cancellation should imply a
refund is a business-policy question left open when self-service
cancellation was built, and this module only provides the mechanism for a
human to decide and act, not a policy for when to.
"""

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_admin
from app.core.serialization import UtcDateTime
from app.models.disbursement import Disbursement
from app.models.subscription import Payment
from app.models.user import User
from app.services.intouchpay import (
    send_deposit,
    IntouchPayError,
    InvalidDepositAmountError,
    RESPONSECODE_DEPOSIT_SUCCESSFUL,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateDisbursementRequest(BaseModel):
    user_id: int  # internal User.id — same convention as /admin/subscriptions/{user_id}
    phone: str
    amount: float
    reason: str
    # Optional refund traceability — the Payment this disbursement is paying
    # back. Not required: a disbursement can be payroll, a prize, a
    # commission, anything with no originating Payment at all.
    reference_payment_id: Optional[int] = None


class DisbursementResponse(BaseModel):
    id: int
    user_id: Optional[int] = None
    clerk_user_id: str
    phone: str
    tx_ref: str
    provider_transaction_id: Optional[str] = None
    amount: float
    currency: str
    reason: str
    status: str  # pending | successful | failed
    reference_payment_id: Optional[int] = None
    initiated_by_clerk_user_id: str
    created_at: UtcDateTime
    verified_at: Optional[UtcDateTime] = None

    class Config:
        from_attributes = True


def _serialize(d: Disbursement, user_id: Optional[int]) -> DisbursementResponse:
    return DisbursementResponse(
        id=d.id,
        user_id=user_id,
        clerk_user_id=d.clerk_user_id,
        phone=d.phone,
        tx_ref=d.tx_ref,
        provider_transaction_id=d.provider_transaction_id,
        amount=d.amount,
        currency=d.currency,
        reason=d.reason,
        status=d.status,
        reference_payment_id=d.reference_payment_id,
        initiated_by_clerk_user_id=d.initiated_by_clerk_user_id,
        created_at=d.created_at,
        verified_at=d.verified_at,
    )


@router.post("", response_model=DisbursementResponse, status_code=201)
async def create_disbursement(
    body: CreateDisbursementRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Send money to a user's mobile money wallet — admin only, immediate (no
    approval step, unlike a Payment collection).

    The Disbursement row is created with status="pending" and committed
    BEFORE calling IntouchPay, not after (the opposite order from
    initiate_payment's Payment row) — money is leaving the business here,
    so a crash between the IntouchPay call and persisting its result must
    never leave zero record that the attempt happened. The row is then
    updated in place once the call returns (or left "pending" if IntouchPay
    is unreachable, for scripts/cron_expiry.py-style reconciliation).
    """
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    phone = body.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")

    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")

    reference_payment_id = None
    if body.reference_payment_id is not None:
        payment = db.query(Payment).filter(Payment.id == body.reference_payment_id).first()
        if not payment:
            raise HTTPException(status_code=404, detail="reference_payment_id not found")
        if payment.clerk_user_id != user.clerk_user_id:
            raise HTTPException(
                status_code=400,
                detail="reference_payment_id does not belong to this user",
            )
        reference_payment_id = payment.id

    tx_ref = f"ID{uuid.uuid4().hex}"

    try:
        disbursement = Disbursement(
            clerk_user_id=user.clerk_user_id,
            phone=phone,
            tx_ref=tx_ref,
            amount=body.amount,
            currency="RWF",
            reason=reason,
            status="pending",
            reference_payment_id=reference_payment_id,
            initiated_by_clerk_user_id=admin.clerk_user_id,
        )
        db.add(disbursement)
        db.commit()
        db.refresh(disbursement)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating disbursement tx_ref=%s: %s", tx_ref, exc)
        raise HTTPException(status_code=500, detail="Database error")

    try:
        resp = await send_deposit(
            amount=body.amount,
            phone=phone,
            transaction_id=tx_ref,
            reason=reason,
        )
    except InvalidDepositAmountError as exc:
        disbursement.status = "failed"
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc))
    except IntouchPayError as exc:
        # Unreachable/non-JSON — the row stays "pending" with a reserved
        # tx_ref; scripts/cron_expiry.py-style reconciliation (or a manual
        # retry via get_transaction_status) resolves it later. Never assume
        # failure here: for all we know the request reached IntouchPay and
        # only the response was lost.
        logger.error("IntouchPay requestdeposit error for tx_ref=%s: %s", tx_ref, exc)
        return _serialize(disbursement, body.user_id)

    provider_tx_id = resp.get("transactionid")
    if provider_tx_id:
        disbursement.provider_transaction_id = provider_tx_id

    accepted = bool(resp.get("success")) and resp.get("responsecode") == RESPONSECODE_DEPOSIT_SUCCESSFUL
    if accepted:
        disbursement.status = "successful"
        disbursement.verified_at = datetime.utcnow()
        logger.info("IntouchPay disbursement successful: tx_ref=%s user=%s amount=%s", tx_ref, user.clerk_user_id, body.amount)
    elif resp.get("success") is False:
        # A structured rejection (bad number, insufficient funds, below
        # minimum, ...) — IntouchPay's own message is preserved on the row
        # implicitly via logs; the response here surfaces status="failed"
        # and the caller can re-check via GET if they need the detail.
        disbursement.status = "failed"
        logger.warning("IntouchPay disbursement rejected: tx_ref=%s response=%s", tx_ref, resp)
    else:
        # Ambiguous — neither a documented success nor an explicit
        # rejection. Leave pending for reconciliation rather than guess.
        logger.info("IntouchPay disbursement unresolved after immediate response: tx_ref=%s response=%s", tx_ref, resp)

    db.commit()
    db.refresh(disbursement)
    return _serialize(disbursement, body.user_id)


@router.get("", response_model=List[DisbursementResponse])
async def list_disbursements(
    user_id: Optional[int] = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Disbursement history, optionally filtered to one user — admin only."""
    query = db.query(Disbursement).order_by(Disbursement.created_at.desc())
    if user_id is not None:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        query = query.filter(Disbursement.clerk_user_id == user.clerk_user_id)
        rows = query.all()
        return [_serialize(d, user_id) for d in rows]

    rows = query.limit(200).all()
    # user_id isn't stored on Disbursement (clerk_user_id is the FK-ish
    # field, matching Subscription/Payment) — batch-resolve it for the
    # response shape without an N+1 lookup per row.
    clerk_ids = {d.clerk_user_id for d in rows}
    users_by_clerk = {
        u.clerk_user_id: u.id
        for u in db.query(User).filter(User.clerk_user_id.in_(clerk_ids)).all()
    } if clerk_ids else {}
    return [_serialize(d, users_by_clerk.get(d.clerk_user_id)) for d in rows]
