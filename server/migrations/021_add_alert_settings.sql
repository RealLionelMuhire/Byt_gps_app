-- Migration 021: Add alert_settings table
--
-- Per-device notification preferences layered on top of the existing
-- unconditional WebSocket broadcast + Expo push in tcp_server.py's
-- handle_alarm / broadcast_alarm / _send_push_notification. These flags
-- gate the push path only — WebSocket broadcast is unaffected. Absence of a
-- row for a device means "everything enabled" (see
-- app/models/alert_settings.py).

BEGIN;

CREATE TABLE IF NOT EXISTS alert_settings (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL UNIQUE REFERENCES devices(id) ON DELETE CASCADE,
    push_notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    sos_push_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    vibration_push_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    low_battery_push_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    acc_push_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    overspeed_push_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    displacement_push_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    min_push_severity VARCHAR(10) NOT NULL DEFAULT 'low',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alert_settings_device_id ON alert_settings(device_id);

COMMENT ON TABLE alert_settings IS 'Per-device push-notification preferences: per-alarm-type toggles, master switch, min severity filter. Row absent = everything enabled, per the AlertSettings model. Does not affect the WebSocket alarm broadcast.';

COMMIT;
