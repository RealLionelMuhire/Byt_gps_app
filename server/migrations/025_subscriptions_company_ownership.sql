-- 025: Shift subscriptions and payments from user to company
-- Subscriptions now belong to a company, not an individual user.

-- Add company_id to subscriptions
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_subscriptions_company_id ON subscriptions (company_id);

-- Add company_id to payments
ALTER TABLE payments ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_payments_company_id ON payments (company_id);

-- Migrate existing data: for each subscription, find the user's company
UPDATE subscriptions s
SET company_id = m.company_id
FROM memberships m
JOIN users u ON u.clerk_user_id = s.clerk_user_id
WHERE u.id = m.user_id
  AND s.company_id IS NULL;

UPDATE payments p
SET company_id = m.company_id
FROM memberships m
JOIN users u ON u.clerk_user_id = p.clerk_user_id
WHERE u.id = m.user_id
  AND p.company_id IS NULL;

-- Note: clerk_user_id is kept for backward compat during migration.
