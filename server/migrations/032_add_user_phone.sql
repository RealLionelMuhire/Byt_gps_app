-- Add optional phone number to users, populated from the mobile app's
-- sign-up/sync payload when the client collects it.
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number VARCHAR(32);
