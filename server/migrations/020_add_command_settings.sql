-- Migration 020: Add command_settings table
--
-- Per-device policy for the remote-command endpoints in app/api/commands.py:
-- a confirmation requirement and hard enable/disable for the one destructive
-- command (fuel/cut), an admin-only lock, and a simple hourly rate limit.
-- Absence of a row for a device means "use the hardcoded defaults" (see
-- app/models/command_settings.py) — same convention as trip_settings.

BEGIN;

CREATE TABLE IF NOT EXISTS command_settings (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL UNIQUE REFERENCES devices(id) ON DELETE CASCADE,
    require_confirmation_for_fuel_cut BOOLEAN NOT NULL DEFAULT TRUE,
    fuel_cut_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    admin_only BOOLEAN NOT NULL DEFAULT FALSE,
    commands_per_hour INTEGER NOT NULL DEFAULT 30,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_command_settings_device_id ON command_settings(device_id);

COMMENT ON TABLE command_settings IS 'Per-device policy for app/api/commands.py: fuel-cut confirmation/enable, admin-only lock, hourly rate limit. Row absent = defaults from the CommandSettings model.';

COMMIT;
