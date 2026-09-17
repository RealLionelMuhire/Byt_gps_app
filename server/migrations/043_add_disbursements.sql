-- Migration 043: disbursements table (IntouchPay requestdeposit / B2C push)
--
-- The mirror image of `payments` (customer-to-business) — money the
-- business sends OUT to a customer's mobile money wallet, via IntouchPay's
-- POST /requestdeposit/. See app/models/disbursement.py for the full
-- rationale on why this is a separate table rather than reusing `payments`
-- with a signed amount: different status lifecycle, a recipient phone
-- snapshot independent of the user's current profile phone, and a required
-- audit trail of which admin authorized the payout.
--
-- Admin-initiated only — nothing (e.g. subscription cancellation) creates
-- one automatically. Run with:
--   psql $DATABASE_URL -f migrations/043_add_disbursements.sql

CREATE TABLE IF NOT EXISTS disbursements (
    id SERIAL PRIMARY KEY,
    clerk_user_id VARCHAR(255) NOT NULL,
    phone VARCHAR(20) NOT NULL,
    tx_ref VARCHAR(255) NOT NULL UNIQUE,
    provider_transaction_id VARCHAR(255),
    amount DOUBLE PRECISION NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'RWF',
    reason VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    reference_payment_id INTEGER REFERENCES payments(id),
    initiated_by_clerk_user_id VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    verified_at TIMESTAMP WITHOUT TIME ZONE
);

CREATE INDEX IF NOT EXISTS ix_disbursements_clerk_user_id ON disbursements (clerk_user_id);
CREATE INDEX IF NOT EXISTS ix_disbursements_tx_ref ON disbursements (tx_ref);
CREATE INDEX IF NOT EXISTS ix_disbursements_reference_payment_id ON disbursements (reference_payment_id);
CREATE INDEX IF NOT EXISTS ix_disbursements_status ON disbursements (status);

COMMENT ON COLUMN disbursements.phone IS 'Recipient phone at disbursement time — snapshotted, not re-resolved from the user profile later.';
COMMENT ON COLUMN disbursements.reference_payment_id IS 'Optional: the Payment this disbursement refunds. NULL for a non-refund payout (payroll, prize, commission, ...).';
COMMENT ON COLUMN disbursements.initiated_by_clerk_user_id IS 'The admin who authorized this payout — always required, unlike payments which are customer self-service.';
