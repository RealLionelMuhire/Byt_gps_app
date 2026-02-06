#!/bin/bash
# Fix production database - Add user_id column to devices table

echo "🔧 Fixing production database..."

# Check if we're on the production server
if [ ! -f "/root/gps_tracking/docker-compose.yml" ]; then
    echo "❌ This script should be run on the production server"
    echo "📝 SSH to production and run:"
    echo "   cd /root/gps_tracking"
    echo "   ./fix_production_db.sh"
    exit 1
fi

# Run migration on the PostgreSQL container
docker exec -i gps_postgres psql -U gps_user -d gps_tracking <<'EOF'
-- Drop old users table if it exists
DROP TABLE IF EXISTS users CASCADE;

-- Create new users table for Clerk
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    clerk_user_id VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Create indexes
CREATE INDEX idx_clerk_user_id ON users(clerk_user_id);
CREATE INDEX idx_email ON users(email);

-- Create trigger function
CREATE OR REPLACE FUNCTION update_users_updated_at()
RETURNS TRIGGER AS $trigger$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$trigger$ LANGUAGE plpgsql;

-- Create trigger
DROP TRIGGER IF EXISTS trigger_update_users_updated_at ON users;
CREATE TRIGGER trigger_update_users_updated_at
BEFORE UPDATE ON users
FOR EACH ROW
EXECUTE FUNCTION update_users_updated_at();

-- Add user_id to devices if not exists
ALTER TABLE devices ADD COLUMN IF NOT EXISTS user_id INTEGER;

-- Add foreign key
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

-- Create index
CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id);

-- Verify
SELECT 
    'Migration complete!' as status,
    (SELECT COUNT(*) FROM users) as user_count,
    (SELECT COUNT(*) FROM devices) as device_count;
EOF

echo ""
echo "✅ Migration complete!"
echo "🔄 Restarting GPS server..."
docker compose restart gps_server

echo ""
echo "✅ Done! Check the server:"
echo "   curl http://164.92.212.186:8000/dashboard"
