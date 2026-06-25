-- Migration 010: Device Management Upgrades
-- Adds hardware model and sim renewal date for fleet management.

ALTER TABLE devices ADD COLUMN IF NOT EXISTS hardware_model VARCHAR(50) DEFAULT NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS sim_renewal_date TIMESTAMP DEFAULT NULL;

COMMENT ON COLUMN devices.hardware_model IS 'The hardware model of the tracker (e.g., G900LS, TK903ELE)';
COMMENT ON COLUMN devices.sim_renewal_date IS 'When the SIM airtime/data expires';
