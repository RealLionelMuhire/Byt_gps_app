-- Migration 012: Replace is_admin boolean with role enum
-- Creates user_role enum type (SUPER_ADMIN, ADMIN, TECHNICIAN, USER),
-- backfills existing users, and drops the is_admin column.

-- 1. Create the enum type (idempotent)
DO $$ BEGIN
    CREATE TYPE user_role AS ENUM ('SUPER_ADMIN', 'ADMIN', 'TECHNICIAN', 'USER');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- 2. Add a temporary column with the new enum type
ALTER TABLE users ADD COLUMN IF NOT EXISTS role_new user_role;

-- 3. Backfill: first user (by id) → SUPER_ADMIN
UPDATE users
SET role_new = 'SUPER_ADMIN'
WHERE id = (SELECT id FROM users ORDER BY id ASC LIMIT 1);

-- 4. Backfill: existing admins (besides first user) → ADMIN
UPDATE users
SET role_new = 'ADMIN'
WHERE is_admin = TRUE AND role_new IS NULL;

-- 5. Backfill: anyone with old role 'owner' or remaining → USER
UPDATE users
SET role_new = 'USER'
WHERE role_new IS NULL;

-- 6. Drop old columns
ALTER TABLE users DROP COLUMN IF EXISTS role;
ALTER TABLE users DROP COLUMN IF EXISTS is_admin;

-- 7. Rename new column
ALTER TABLE users RENAME COLUMN role_new TO role;

-- 8. Set NOT NULL
ALTER TABLE users ALTER COLUMN role SET NOT NULL;

-- 9. Set default for new rows
ALTER TABLE users ALTER COLUMN role SET DEFAULT 'USER';

-- 10. Add index for role lookups
CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);

COMMENT ON COLUMN users.role IS 'User role: SUPER_ADMIN, ADMIN, TECHNICIAN, or USER';
