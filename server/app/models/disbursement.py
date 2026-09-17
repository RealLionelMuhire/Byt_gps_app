from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import Base


class Disbursement(Base):
    """
    An outbound IntouchPay deposit (B2C push, `POST /requestdeposit/`) — the
    business sending money TO a customer's mobile money wallet. The mirror
    image of Payment (customer-to-business), never the same table: a
    disbursement needs its own status lifecycle, its own recipient phone
    snapshot (independent of whatever the user's profile phone is *today*),
    and an audit trail of which admin authorized it, none of which apply to
    an inbound Payment.

    Admin-initiated only (see app/api/disbursements.py) — there is no
    automatic trigger anywhere (e.g. subscription cancellation does NOT
    create one). Whether/when a cancellation should imply a refund is a
    business-policy question that was deliberately left open when
    self-service cancellation was built (see
    app/api/onboarding.py's _cancel_active_subscription docstring); this
    table only provides the mechanism, not a policy for when to use it.
    """
    __tablename__ = "disbursements"

    id = Column(Integer, primary_key=True, index=True)

    # Recipient — a real user account, so a payout always traces back to
    # somebody in the system. `phone` is snapshotted at creation time
    # (rather than always resolved fresh from the User row) so a later
    # phone-number change never re-targets an already-recorded disbursement.
    clerk_user_id = Column(String(255), nullable=False, index=True)
    phone = Column(String(20), nullable=False)

    tx_ref = Column(String(255), nullable=False, unique=True, index=True)
    # IntouchPay's own transaction id (from the immediate response/webhook),
    # used the same way Subscription/Payment reconciliation uses it for
    # gettransactionstatus lookups. Nullable — not known until the
    # requestdeposit call actually returns.
    provider_transaction_id = Column(String(255), nullable=True)

    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False, default="RWF")
    reason = Column(String(255), nullable=False)  # narration shown to the recipient
    status = Column(String(20), nullable=False, default="pending")  # pending | successful | failed

    # Optional traceability for the refund use case specifically — NULL for
    # a disbursement that isn't refunding any particular payment (payroll,
    # a prize, a commission, ...). Never enforced/required at the DB level
    # since disbursements aren't refund-only.
    reference_payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True)
    reference_payment = relationship("Payment", foreign_keys=[reference_payment_id])

    # Which admin authorized this — a real money movement always has an
    # accountable human on record, unlike Payment (customer self-service).
    initiated_by_clerk_user_id = Column(String(255), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)
