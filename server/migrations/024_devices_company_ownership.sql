-- 024: Shift device ownership from user to company
-- Devices now belong to a company, not an individual user.
-- user_id is kept nullable for backward compatibility during migration.

-- Add company_id column
ALTER TABLE devices ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_devices_company_id ON devices (company_id);

-- Migrate existing ownership: for each device with a user_id, find the user's
-- company via their membership and set company_id accordingly.
UPDATE devices d
SET company_id = m.company_id
FROM memberships m
WHERE d.user_id = m.user_id
  AND d.company_id IS NULL;

-- Note: user_id is kept as-is for now (nullable). It will be deprecated
-- in favor of company_id. New code should use company_id for ownership.
