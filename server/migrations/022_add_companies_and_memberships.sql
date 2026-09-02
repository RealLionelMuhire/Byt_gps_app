-- 022: Add companies and memberships tables
-- Companies represent a workspace (solo or team). Every user gets one at
-- onboarding step 5. Memberships link users to companies with a role.

-- ── company_role enum ────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'company_role') THEN
        CREATE TYPE company_role AS ENUM ('OWNER', 'USER');
    END IF;
END
$$;

-- ── companies table ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    is_company  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT NOW(),
    updated_at  TIMESTAMP DEFAULT NOW()
);

-- ── memberships table ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memberships (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    company_role  company_role NOT NULL DEFAULT 'USER',
    created_at    TIMESTAMP DEFAULT NOW(),

    CONSTRAINT uq_user_company UNIQUE (user_id, company_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_user_id    ON memberships (user_id);
CREATE INDEX IF NOT EXISTS idx_memberships_company_id ON memberships (company_id);
