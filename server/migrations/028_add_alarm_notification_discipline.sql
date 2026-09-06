-- Migration 028: Alarm notification-delivery discipline
--
-- Adds dedup/acknowledgment/escalation/digest state on top of the existing
-- alarm pipeline (AlertSettings from migration 021, ALARM_SEVERITY,
-- broadcast_alarm in app/tcp_server.py) — see app/services/alarm_rules.py
-- and TCPServer._send_push_notification for how these are used.

BEGIN;

-- Location-level bookkeeping for a single alarm event's notification lifecycle.
ALTER TABLE locations ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMP NULL;
ALTER TABLE locations ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMP NULL;
ALTER TABLE locations ADD COLUMN IF NOT EXISTS digested_at TIMESTAMP NULL;

COMMENT ON COLUMN locations.acknowledged_at IS 'Set when the user views/acknowledges this alarm in the app (POST /api/locations/{id}/acknowledge). NULL = unacknowledged.';
COMMENT ON COLUMN locations.escalated_at IS 'Set once the one-time unacknowledged-CRITICAL-alarm push resend has fired (see scripts/cron_alarm_escalation.py) — prevents resending more than once.';
COMMENT ON COLUMN locations.digested_at IS 'Set once this alarm has been accounted for via push (sent immediately, explicitly muted, or folded into a digest). NULL means it is still awaiting the periodic low/medium digest job (scripts/cron_alarm_digest.py).';

-- Per (device, alarm_type) push-dedup bookkeeping — suppresses a duplicate
-- push for the same combination within a configurable window unless an
-- intervening "resolved" state is recorded (see app/models/alarm_push_state.py).
CREATE TABLE IF NOT EXISTS alarm_push_state (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    alarm_type VARCHAR(50) NOT NULL,
    last_push_at TIMESTAMP NULL,
    last_alarm_state VARCHAR(10) NOT NULL DEFAULT 'fired',
    CONSTRAINT uq_alarm_push_state_device_type UNIQUE (device_id, alarm_type)
);

CREATE INDEX IF NOT EXISTS idx_alarm_push_state_device_id ON alarm_push_state(device_id);

-- Same posture as migration 023: this app only ever connects as the
-- `postgres` role (which owns this table and bypasses RLS), so enabling RLS
-- with no policies just locks Supabase's auto-exposed PostgREST API out of
-- a new table, without affecting the app itself.
ALTER TABLE alarm_push_state ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE alarm_push_state IS 'Push-dedup bookkeeping per (device, alarm_type) — gates TCPServer._send_push_notification only, does not affect the WebSocket alarm broadcast.';

COMMIT;
