-- Migration 012: Replace is_admin boolean with role enum
-- Creates user_role enum type (SUPER_ADMIN, ADMIN, TECHNICIAN, USER),
-- backfills existing users, and drops the is_admin column.
--
-- IDEMPOTENT — safe to re-run. The first run ends with `role_new` renamed to
-- `role`, so a naive re-run would recreate a fresh all-NULL `role_new`, re-run
-- the backfill against it, and then DROP COLUMN role — wiping every user's
-- role. This version:
--   * detects "already applied" by checking users.role is actually the
--     user_role enum type, and then only normalises + fills NULL roles;
--   * guards every step of the conversion so a partially-applied previous run
--     resumes where it stopped instead of corrupting data;
--   * never overwrites a role that is already set.

DO $$
BEGIN
    -- 1. Create the enum type (idempotent)
    BEGIN
        CREATE TYPE user_role AS ENUM ('SUPER_ADMIN', 'ADMIN', 'TECHNICIAN', 'USER');
    EXCEPTION
        WHEN duplicate_object THEN NULL;
    END;

    -- Already fully applied: users.role is already the user_role enum
    -- (role_new was renamed to role). Normalise defaults/index and repair
    -- any roles a botched earlier run left NULL, then stop. This branch
    -- NEVER overwrites an existing role value.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'role'
          AND udt_name = 'user_role'
    ) THEN
        EXECUTE 'ALTER TABLE users ALTER COLUMN role SET DEFAULT ''USER''';
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_users_role ON users (role)';
        -- Repair: fill any roles a botched earlier run left NULL. Only fills
        -- NULLs — done BEFORE SET NOT NULL so a damaged all-NULL column still
        -- passes the constraint.
        EXECUTE 'UPDATE users SET role = ''SUPER_ADMIN''
                 WHERE role IS NULL
                   AND id = (SELECT id FROM users ORDER BY id ASC LIMIT 1)';
        EXECUTE 'UPDATE users SET role = ''USER'' WHERE role IS NULL';
        EXECUTE 'ALTER TABLE users ALTER COLUMN role SET NOT NULL';
        RETURN;
    END IF;

    -- ── Conversion (fresh install, or resuming a partially-applied run) ──

    -- 2. Temporary enum column (no-op if a previous run already created it)
    EXECUTE 'ALTER TABLE users ADD COLUMN IF NOT EXISTS role_new user_role';

    -- 3. Backfill: first user (by id) → SUPER_ADMIN
    EXECUTE 'UPDATE users
             SET role_new = ''SUPER_ADMIN''
             WHERE role_new IS NULL
               AND id = (SELECT id FROM users ORDER BY id ASC LIMIT 1)';

    -- 4. Backfill: existing admins (besides first user) → ADMIN.
    --    Guarded: is_admin may already be gone if a previous run got further.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'is_admin'
    ) THEN
        EXECUTE 'UPDATE users SET role_new = ''ADMIN'' WHERE is_admin = TRUE AND role_new IS NULL';
    END IF;

    -- 5. Backfill: anyone remaining → USER
    EXECUTE 'UPDATE users SET role_new = ''USER'' WHERE role_new IS NULL';

    -- 6. Drop old columns (IF EXISTS → safe in any partial state)
    EXECUTE 'ALTER TABLE users DROP COLUMN IF EXISTS role';
    EXECUTE 'ALTER TABLE users DROP COLUMN IF EXISTS is_admin';

    -- 7. Rename the temp column (only if it still exists)
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'role_new'
    ) THEN
        EXECUTE 'ALTER TABLE users RENAME COLUMN role_new TO role';
    END IF;

    -- 8. NOT NULL + default + index (safe: backfill above left no NULLs)
    EXECUTE 'ALTER TABLE users ALTER COLUMN role SET NOT NULL';
    EXECUTE 'ALTER TABLE users ALTER COLUMN role SET DEFAULT ''USER''';
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_users_role ON users (role)';
END $$;

COMMENT ON COLUMN users.role IS 'User role: SUPER_ADMIN, ADMIN, TECHNICIAN, or USER';
