-- Migration 030: Auto-create companies from users and group by shared GPS
-- 
-- This migration handles legacy data where users owned devices directly.
-- It:
--   1. Creates a company for each user who doesn't have one (name from user's first/last name)
--   2. Moves devices from user_id to company_id ownership
--   3. Groups users who share access to the same GPS into one company
--      (first user to reach = company owner, others join as USER role)
--   4. Does NOT modify global roles (user.role stays as-is)

-- Step 1: Create companies for users who don't have one yet
INSERT INTO companies (name, is_company, created_at, updated_at)
SELECT 
    CONCAT(u.first_name, ' ', u.last_name) AS name,
    FALSE AS is_company,
    NOW() AS created_at,
    NOW() AS updated_at
FROM users u
WHERE NOT EXISTS (
    SELECT 1 FROM memberships m WHERE m.user_id = u.id
)
AND u.first_name IS NOT NULL
AND u.last_name IS NOT NULL;

-- Step 2: Create OWNER memberships for users who just got companies
INSERT INTO memberships (user_id, company_id, company_role, created_at)
SELECT 
    u.id AS user_id,
    c.id AS company_id,
    'OWNER' AS company_role,
    NOW() AS created_at
FROM users u
JOIN companies c ON c.name = CONCAT(u.first_name, ' ', u.last_name)
    AND c.is_company = FALSE
WHERE NOT EXISTS (
    SELECT 1 FROM memberships m WHERE m.user_id = u.id
)
AND u.first_name IS NOT NULL
AND u.last_name IS NOT NULL;

-- Step 3: Move devices from user_id to company_id ownership
-- For devices that have a user_id but no company_id
UPDATE devices d
SET 
    company_id = (
        SELECT m.company_id 
        FROM memberships m 
        WHERE m.user_id = d.user_id 
        LIMIT 1
    ),
    lifecycle = 'sold',
    updated_at = NOW()
WHERE d.user_id IS NOT NULL 
AND d.company_id IS NULL
AND EXISTS (
    SELECT 1 FROM memberships m WHERE m.user_id = d.user_id
);

-- Step 4: Group users who share the same GPS into one company
-- Find users who have devices with the same IMEI (shouldn't happen normally,
-- but handles edge cases where multiple users had access to same device)
-- The first user (by user_id) keeps their company, others join it

-- Find duplicate device access (same IMEI assigned to different users)
CREATE TEMPORARY TABLE shared_devices AS
SELECT 
    d1.imei,
    d1.user_id AS primary_user_id,
    d2.user_id AS secondary_user_id
FROM devices d1
JOIN devices d2 ON d1.imei = d2.imei AND d1.user_id < d2.user_id
WHERE d1.user_id IS NOT NULL AND d2.user_id IS NOT NULL;

-- For secondary users, move their devices to the primary user's company
UPDATE devices d
SET 
    company_id = (
        SELECT sd.primary_user_id 
        FROM shared_devices sd 
        WHERE sd.secondary_user_id = d.user_id
        LIMIT 1
    ),
    lifecycle = 'sold',
    updated_at = NOW()
WHERE d.user_id IN (
    SELECT secondary_user_id FROM shared_devices
)
AND d.company_id IS NULL;

-- Add secondary users as members (USER role) to the primary user's company
INSERT INTO memberships (user_id, company_id, company_role, created_at)
SELECT 
    sd.secondary_user_id,
    m.company_id,
    'USER',
    NOW()
FROM shared_devices sd
JOIN memberships m ON m.user_id = sd.primary_user_id
WHERE NOT EXISTS (
    SELECT 1 FROM memberships m2 
    WHERE m2.user_id = sd.secondary_user_id AND m2.company_id = m.company_id
);

-- Clean up temporary table
DROP TABLE shared_devices;

-- Step 5: Ensure all devices with company_id have lifecycle = 'sold'
UPDATE devices 
SET lifecycle = 'sold', updated_at = NOW()
WHERE company_id IS NOT NULL AND lifecycle != 'sold';

-- Step 6: Create indexes if they don't exist
CREATE INDEX IF NOT EXISTS idx_devices_company_id ON devices(company_id);
CREATE INDEX IF NOT EXISTS idx_memberships_user_company ON memberships(user_id, company_id);
