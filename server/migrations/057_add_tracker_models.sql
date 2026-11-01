-- Migration 057: tracker models (per-hardware command vocabularies)
--
-- Every command used to be hard-coded in G900LS J16-4G syntax (RELAY,1#,
-- SENALM,ON,0#, ...), so a TK903ELE — whose fuel cut is `stop123456` —
-- would have been sent commands it doesn't understand, and devices.
-- hardware_model was a free-text label nothing read. Now:
--   tracker_models          one row per hardware model (admin portal);
--   tracker_model_commands  the exact command text per capability
--                           (app/services/tracker_capabilities.py) or
--                           admin-only extra ("extra.<slug>"), with
--                           `verified` = tried on a real tracker;
--   devices.tracker_model_id which model a tracker is (NULL = default).
--
-- Seeds the G900LS (default; the commands the app already sent are
-- verified) and the TK903ELE (from its SMS sheet; all unverified). Seed
-- rows mirror G900LS_ROWS/TK903ELE_ROWS in app/services/tracker_models.py
-- (tests/test_tracker_models.py keeps them in sync). Existing devices are
-- linked by matching their hardware_model text; the rest use the default.
--
-- Purely additive. IDEMPOTENT — safe to re-run:
--   psql $DATABASE_URL -f migrations/057_add_tracker_models.sql
-- Then: INSERT INTO schema_migrations (filename) VALUES ('057_add_tracker_models.sql');

BEGIN;

CREATE TABLE IF NOT EXISTS tracker_models (
    id SERIAL PRIMARY KEY,
    name VARCHAR(80) NOT NULL UNIQUE,
    manufacturer VARCHAR(80) NULL,
    protocol VARCHAR(20) NOT NULL DEFAULT 'gt06',
    command_password VARCHAR(40) NULL,
    notes TEXT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    reply_patterns TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tracker_model_commands (
    id SERIAL PRIMARY KEY,
    tracker_model_id INTEGER NOT NULL REFERENCES tracker_models(id) ON DELETE CASCADE,
    key VARCHAR(64) NOT NULL,
    label VARCHAR(80) NULL,
    template VARCHAR(200) NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tracker_model_command_key UNIQUE (tracker_model_id, key)
);
CREATE INDEX IF NOT EXISTS ix_tracker_model_commands_tracker_model_id ON tracker_model_commands (tracker_model_id);

ALTER TABLE devices ADD COLUMN IF NOT EXISTS tracker_model_id INTEGER NULL
    REFERENCES tracker_models(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_devices_tracker_model_id ON devices (tracker_model_id);

COMMENT ON TABLE tracker_models IS 'GPS tracker hardware models (see migration 057).';
COMMENT ON TABLE tracker_model_commands IS 'Per-model command text for each capability (see migration 057).';
-- Not exposed through Supabase's REST API (see migration 054).
ALTER TABLE tracker_models ENABLE ROW LEVEL SECURITY;
ALTER TABLE tracker_model_commands ENABLE ROW LEVEL SECURITY;

INSERT INTO tracker_models (name, manufacturer, protocol, command_password, notes, is_default) VALUES
    ('G900LS J16-4G', NULL, 'gt06', NULL, 'Production hardware. Fuel cuts may be deferred until GPS speed < 20 km/h.', TRUE),
    ('TK903ELE', NULL, 'gt06', '123456', 'Commands from the SMS sheet (docs/usage/CONFIGURATION_GUIDE.md) — none tried over GPRS yet.', FALSE)
ON CONFLICT (name) DO NOTHING;

INSERT INTO tracker_model_commands (tracker_model_id, key, label, template, verified) VALUES
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'fuel.cut', NULL, 'RELAY,1#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'fuel.restore', NULL, 'RELAY,0#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'fuel.query', NULL, 'RELAY#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'location.query', NULL, 'WHERE#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'status.query', NULL, 'STATUS#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'alarm.vibration.on', NULL, 'SENALM,ON,{mode}#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'alarm.vibration.off', NULL, 'SENALM,OFF#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'alarm.power_cut.on', NULL, 'POWERALM,ON,{mode},{detect_seconds},{min_charge_seconds},#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'alarm.power_cut.off', NULL, 'POWERALM,OFF#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'alarm.ignition_on.on', NULL, 'ACCALM,ON,{mode}#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'alarm.ignition_on.off', NULL, 'ACCALM,OFF#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'alarm.ignition_off.on', NULL, 'ACCOFFALM,ON,{mode}#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'alarm.ignition_off.off', NULL, 'ACCOFFALM,OFF#', TRUE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'device.restart', NULL, 'RESET#', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'interval.set', NULL, 'TIMER,{moving_seconds},{parked_seconds}#', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'extra.params', 'All parameters', 'PARAM#', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G'), 'extra.version', 'Firmware version', 'VERSION#', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'TK903ELE'), 'fuel.cut', NULL, 'stop{password}', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'TK903ELE'), 'fuel.restore', NULL, 'resume{password}', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'TK903ELE'), 'status.query', NULL, 'STATUS#', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'TK903ELE'), 'alarm.vibration.on', NULL, 'vibrate{password} 1', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'TK903ELE'), 'alarm.vibration.off', NULL, 'vibrate{password} 0', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'TK903ELE'), 'device.restart', NULL, 'reset{password}', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'TK903ELE'), 'extra.check_settings', 'All settings', 'check{password}', FALSE),
    ((SELECT id FROM tracker_models WHERE name = 'TK903ELE'), 'extra.version', 'Firmware version', 'VERSION#', FALSE)
ON CONFLICT (tracker_model_id, key) DO NOTHING;

UPDATE devices SET tracker_model_id = (SELECT id FROM tracker_models WHERE name = 'TK903ELE')
    WHERE tracker_model_id IS NULL AND hardware_model ILIKE '%TK903%';
UPDATE devices SET tracker_model_id = (SELECT id FROM tracker_models WHERE name = 'G900LS J16-4G')
    WHERE tracker_model_id IS NULL AND hardware_model ILIKE '%G900%';

COMMIT;
