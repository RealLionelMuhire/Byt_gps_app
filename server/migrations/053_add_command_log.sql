-- Migration 053: command_log
--
-- Audit trail of every command sent to a tracker and every reply it sent
-- back — the exact reply text, when it arrived, and which command it
-- answered. Before this, a reply that arrived after the 10s wait was
-- dropped, or (worse) shown as the answer to the NEXT command, because
-- every command used the same GT06 server flag (1) and replies could not
-- be matched. Commands now carry a unique server_flag the tracker echoes
-- back; reply_server_flag records what it actually echoed.
--
--   outcome: pending | replied | replied_unverified (tracker did not echo
--            the flag; attributed to the in-flight command) | timeout |
--            late_reply (arrived after the wait, matched by flag) |
--            unmatched (a reply no recent command accounts for)
--
-- Purely additive. IDEMPOTENT — safe to re-run:
--   psql $DATABASE_URL -f migrations/053_add_command_log.sql

BEGIN;

CREATE TABLE IF NOT EXISTS command_log (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NULL REFERENCES devices(id) ON DELETE CASCADE,
    imei VARCHAR(20) NOT NULL,
    command VARCHAR(200) NULL,
    server_flag BIGINT NULL,
    sent_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    sent_at TIMESTAMP NULL,
    reply TEXT NULL,
    reply_server_flag BIGINT NULL,
    reply_at TIMESTAMP NULL,
    outcome VARCHAR(20) NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS ix_command_log_device_sent ON command_log (device_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS ix_command_log_server_flag ON command_log (server_flag);

COMMENT ON TABLE command_log IS 'Every tracker command and reply (see migration 053).';
-- Not exposed through Supabase's REST API (see migration 054).
ALTER TABLE command_log ENABLE ROW LEVEL SECURITY;

COMMIT;
