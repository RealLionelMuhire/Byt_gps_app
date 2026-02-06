-- Migration Script for Clerk Authentication Integration
-- Run this on existing databases to add user management support
-- Date: 2026-02-06

BEGIN;

-- ============================================================================
-- 1. CREATE USERS TABLE
-- ============================================================================

-- Create users table if it doesn't exist
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    clerk_user_id VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Create indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_clerk_user_id ON users(clerk_user_id);
CREATE INDEX IF NOT EXISTS idx_email ON users(email);

-- Create trigger function to auto-update updated_at
CREATE OR REPLACE FUNCTION update_users_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger
DROP TRIGGER IF EXISTS trigger_update_users_updated_at ON users;
CREATE TRIGGER trigger_update_users_updated_at
BEFORE UPDATE ON users
FOR EACH ROW
EXECUTE FUNCTION update_users_updated_at();

COMMIT;

BEGIN;

-- ============================================================================
-- 2. UPDATE DEVICES TABLE
-- ============================================================================

-- Add user_id column to devices table
ALTER TABLE devices 
  ADD COLUMN IF NOT EXISTS user_id INTEGER;

-- Add foreign key constraint
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints 
        WHERE constraint_name = 'fk_devices_user' 
        AND table_name = 'devices'
    ) THEN
        ALTER TABLE devices 
        ADD CONSTRAINT fk_devices_user 
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE SET NULL;
    END IF;
END $$;

-- Create index for fast lookups
CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id);

COMMIT;

-- ============================================================================
-- 3. VERIFICATION QUERIES
-- ============================================================================

-- Verify users table structure
SELECT 
    column_name, 
    data_type, 
    is_nullable,
    column_default
FROM information_schema.columns
WHERE table_name = 'users'
ORDER BY ordinal_position;

-- Verify devices table has user_id column
SELECT 
    column_name, 
    data_type, 
    is_nullable
FROM information_schema.columns
WHERE table_name = 'devices' AND column_name = 'user_id';

-- Verify indexes exist
SELECT 
    tablename, 
    indexname, 
    indexdef
FROM pg_indexes
WHERE schemaname = 'public'
AND (
    indexname LIKE '%clerk_user%' 
    OR indexname LIKE '%devices_user%'
    OR indexname LIKE '%email%'
)
ORDER BY tablename, indexname;

-- Verify foreign key constraint
SELECT
    tc.constraint_name,
    tc.table_name,
    kcu.column_name,
    ccu.table_name AS foreign_table_name,
    ccu.column_name AS foreign_column_name
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage AS ccu
    ON ccu.constraint_name = tc.constraint_name
    AND ccu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
AND tc.table_name = 'devices'
AND kcu.column_name = 'user_id';

-- ============================================================================
-- 4. DATA MIGRATION (OPTIONAL)
-- ============================================================================

-- If you need to migrate existing data, uncomment and customize:

/*
-- Example: Create a default user for existing devices
INSERT INTO users (clerk_user_id, email, name, created_at, updated_at)
VALUES ('user_default_migration', 'admin@yourdomain.com', 'System Admin', NOW(), NOW())
ON CONFLICT (clerk_user_id) DO NOTHING;

-- Assign all unassigned devices to this default user
UPDATE devices 
SET user_id = (SELECT id FROM users WHERE clerk_user_id = 'user_default_migration')
WHERE user_id IS NULL;
*/

-- ============================================================================
-- MIGRATION COMPLETE
-- ============================================================================

SELECT '✅ Migration completed successfully!' AS status,
       (SELECT COUNT(*) FROM users) AS user_count,
       (SELECT COUNT(*) FROM devices) AS device_count,
       (SELECT COUNT(*) FROM devices WHERE user_id IS NOT NULL) AS assigned_devices,
       (SELECT COUNT(*) FROM devices WHERE user_id IS NULL) AS unassigned_devices;
