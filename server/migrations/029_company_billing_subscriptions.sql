-- Migration 029: Company-level billing subscriptions
-- Adds company_id, billing_type, pricing_model, device_count_snapshot,
-- amount_due, payment_status, due_date to subscriptions table.

ALTER TABLE subscriptions
ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id),
ADD COLUMN IF NOT EXISTS billing_type VARCHAR(20) NOT NULL DEFAULT 'prepaid',
ADD COLUMN IF NOT EXISTS pricing_model VARCHAR(20) NOT NULL DEFAULT 'flat',
ADD COLUMN IF NOT EXISTS device_count_snapshot INTEGER NOT NULL DEFAULT 0,
ADD COLUMN IF NOT EXISTS amount_due DOUBLE PRECISION NOT NULL DEFAULT 0.0,
ADD COLUMN IF NOT EXISTS payment_status VARCHAR(20) NOT NULL DEFAULT 'pending',
ADD COLUMN IF NOT EXISTS due_date TIMESTAMP;

-- Backfill company_id for existing subscriptions by looking up the user's membership
UPDATE subscriptions s
SET company_id = (
    SELECT m.company_id
    FROM memberships m
    JOIN users u ON u.id = m.user_id
    WHERE u.clerk_user_id = s.clerk_user_id
    LIMIT 1
)
WHERE s.company_id IS NULL;

-- Backfill billing fields from plan data
UPDATE subscriptions s
SET
    billing_type = COALESCE(sp.billing_type, 'prepaid'),
    pricing_model = COALESCE(sp.pricing_model, 'flat'),
    amount_due = s.price,
    payment_status = 'paid'
FROM subscription_plans sp
WHERE s.plan_id = sp.slug AND s.amount_due = 0.0;

-- Mark existing subscriptions as paid (they were already purchased)
UPDATE subscriptions
SET payment_status = 'paid'
WHERE payment_status = 'pending' AND amount_due = 0.0;

CREATE INDEX IF NOT EXISTS idx_subscriptions_company_id ON subscriptions(company_id);
