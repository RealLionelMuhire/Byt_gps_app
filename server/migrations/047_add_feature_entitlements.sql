-- Migration 047: feature catalog + plan_features + entitlement_check_log
--
-- Until now a plan only limited how many vehicles an account could add
-- (max_devices); every other feature was available to everyone, paid,
-- expired or not. This adds the pieces to say which plan includes what:
--
--   features               the catalog (keys are defined in code, in
--                          app/services/entitlements.py's FEATURES — this
--                          seed must match it; tests/test_entitlements.py
--                          checks that)
--   plan_features          which plan includes which feature, with a limit
--                          for "limit" features (NULL = unlimited)
--   entitlement_check_log  daily counters of would-be (log mode) or actual
--                          (enforce mode) denials
--
-- SEED: every existing plan gets EVERY feature, unlimited — so nothing
-- changes for anyone until an admin deliberately removes something.
-- vehicles.max gets a catalog row but no plan_features rows: it stays
-- backed by subscription_plans.max_devices (see PlanFeature's docstring).
--
-- Checks start in log-only mode (settings.ENTITLEMENT_MODE="log"): nothing
-- is blocked, would-be denials are counted in entitlement_check_log.
--
-- Purely additive. IDEMPOTENT — safe to re-run. Run with:
--   psql $DATABASE_URL -f migrations/047_add_feature_entitlements.sql

BEGIN;

CREATE TABLE IF NOT EXISTS features (
    key VARCHAR(64) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description VARCHAR(500),
    group_name VARCHAR(50) NOT NULL,
    kind VARCHAR(20) NOT NULL DEFAULT 'boolean' CHECK (kind IN ('boolean', 'limit')),
    unit VARCHAR(20),
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS plan_features (
    id SERIAL PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES subscription_plans(id) ON DELETE CASCADE,
    feature_key VARCHAR(64) NOT NULL REFERENCES features(key) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    limit_value INTEGER CHECK (limit_value IS NULL OR limit_value >= 0),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    CONSTRAINT uq_plan_features_plan_feature UNIQUE (plan_id, feature_key)
);
CREATE INDEX IF NOT EXISTS idx_plan_features_plan_id ON plan_features(plan_id);

CREATE TABLE IF NOT EXISTS entitlement_check_log (
    id SERIAL PRIMARY KEY,
    day DATE NOT NULL,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    actor_user_id INTEGER,
    feature_key VARCHAR(64) NOT NULL,
    reason VARCHAR(40) NOT NULL,
    route VARCHAR(200) NOT NULL,
    mode VARCHAR(10) NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    first_seen TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    last_seen TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT uq_entitlement_check_log_bucket UNIQUE (day, owner_user_id, feature_key, reason, route)
);
CREATE INDEX IF NOT EXISTS idx_entitlement_check_log_owner ON entitlement_check_log(owner_user_id);

ALTER TABLE features ENABLE ROW LEVEL SECURITY;
ALTER TABLE plan_features ENABLE ROW LEVEL SECURITY;
ALTER TABLE entitlement_check_log ENABLE ROW LEVEL SECURITY;

INSERT INTO features (key, name, description, group_name, kind, unit, sort_order) VALUES
    ('vehicles.max', 'Vehicles', 'How many vehicles the account can register. Backed by the plan''s max_devices.', 'Fleet', 'limit', 'vehicles', 0),
    ('tracking.live', 'Live tracking', 'Real-time vehicle position, live road name and the live trail.', 'Tracking', 'boolean', NULL, 10),
    ('history.trips', 'Trip history', 'Recorded trips, route playback and raw location history.', 'History', 'boolean', NULL, 20),
    ('history.period', 'Period history', 'Everything a vehicle did over a chosen date range.', 'History', 'boolean', NULL, 30),
    ('history.retention_days', 'History retention', 'How far back history can be viewed.', 'History', 'limit', 'days', 40),
    ('geofences.enabled', 'Geofencing', 'Create zones and get enter/exit alerts.', 'Geofencing', 'boolean', NULL, 50),
    ('geofences.max_zones', 'Number of zones', 'How many geofence zones the account can have.', 'Geofencing', 'limit', 'zones', 60),
    ('geofences.polygon', 'Polygon zones', 'Zones drawn as any shape, not only circles.', 'Geofencing', 'boolean', NULL, 70),
    ('alerts.push', 'Push notifications', 'Alarm notifications on the phone.', 'Alerts', 'boolean', NULL, 80),
    ('alerts.history', 'Alert history', 'The list of past alarms and acknowledging them.', 'Alerts', 'boolean', NULL, 90),
    ('alerts.overspeed', 'Overspeed alerts', 'Set a speed limit and get alerted when it''s exceeded.', 'Alerts', 'boolean', NULL, 100),
    ('alerts.rules', 'Alert rules', 'Choose which alarm types notify and how.', 'Alerts', 'boolean', NULL, 110),
    ('commands.fuel_cut', 'Fuel cut / restore', 'Remotely cut or restore the vehicle''s fuel supply.', 'Commands', 'boolean', NULL, 120),
    ('commands.alarm_config', 'Device alarm settings', 'Turn the tracker''s vibration, power-cut and ignition alarms on or off.', 'Commands', 'boolean', NULL, 130),
    ('commands.query', 'Device queries', 'Ask the tracker for its current location or status.', 'Commands', 'boolean', NULL, 140),
    ('commands.raw', 'Advanced commands', 'Send any supported command to the tracker directly.', 'Commands', 'boolean', NULL, 150),
    ('diagnostics', 'Diagnostics', 'Device health and GPS quality details.', 'Diagnostics', 'boolean', NULL, 160)
ON CONFLICT (key) DO NOTHING;

INSERT INTO plan_features (plan_id, feature_key, enabled, limit_value)
SELECT p.id, f.key, TRUE, NULL
FROM subscription_plans p
CROSS JOIN features f
WHERE f.key <> 'vehicles.max'
ON CONFLICT (plan_id, feature_key) DO NOTHING;

COMMIT;
