-- 026: Admin-created subscription flow
-- Admins create subscription records for companies.
-- Companies then choose/activate one.

-- Add created_by (the admin who created the subscription)
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id) ON DELETE SET NULL;

-- Add is_active flag: admin creates with is_active=false,
-- company activates by setting is_active=true
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

-- Migrate existing: mark current active subscriptions as is_active=true
UPDATE subscriptions SET is_active = TRUE WHERE status = 'active';
