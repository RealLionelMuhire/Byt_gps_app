-- 027: Subscriptions are per-user entities, independent of companies.
-- A user subscribes to a plan. That gives them access to features.
-- Companies don't own subscriptions — users do.

-- Remove company_id from subscriptions (revert 025)
ALTER TABLE subscriptions DROP COLUMN IF EXISTS company_id;

-- Remove company_id from payments (revert 025)
ALTER TABLE payments DROP COLUMN IF EXISTS company_id;

-- Drop the indexes too
DROP INDEX IF EXISTS idx_subscriptions_company_id;
DROP INDEX IF EXISTS idx_payments_company_id;

-- Drop subscription relationship from companies table (handled in model)
-- The back_populates in Company model will be removed in code.
