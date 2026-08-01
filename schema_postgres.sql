-- TokenTracker Postgres schema (mirror of db.py's SQLite schema v3).
-- Apply with: psql "$DATABASE_URL" -f schema_postgres.sql

CREATE TABLE IF NOT EXISTS tenants (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS employees (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT,
    team TEXT,
    active SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, external_id)
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    employee_id INTEGER REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    client TEXT,
    UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS virtual_keys (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    employee_id INTEGER REFERENCES employees(id),
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    monthly_budget_usd DOUBLE PRECISION NOT NULL DEFAULT 100,
    active SMALLINT NOT NULL DEFAULT 1,
    created TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usage_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    event_key TEXT NOT NULL,
    employee_id INTEGER REFERENCES employees(id),
    project_id INTEGER REFERENCES projects(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    cached_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'poller',
    virtual_key_id INTEGER REFERENCES virtual_keys(id),
    ts TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, event_key)
);

CREATE TABLE IF NOT EXISTS seats (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    tool TEXT NOT NULL,
    employee_id INTEGER REFERENCES employees(id),
    monthly_cost_usd DOUBLE PRECISION NOT NULL,
    last_active TIMESTAMPTZ,
    UNIQUE (tenant_id, tool, employee_id)
);

CREATE TABLE IF NOT EXISTS prompt_logs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    employee_id INTEGER REFERENCES employees(id),
    virtual_key_id INTEGER REFERENCES virtual_keys(id),
    project_id INTEGER REFERENCES projects(id),
    model TEXT,
    prompt_redacted TEXT,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    ts TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (tenant_id, key)
);

CREATE TABLE IF NOT EXISTS billing (
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id),
    plan TEXT NOT NULL DEFAULT 'trial',
    trial_started TIMESTAMPTZ NOT NULL DEFAULT now(),
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events (tenant_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_emp ON usage_events (tenant_id, employee_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_proj ON usage_events (tenant_id, project_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_events (tenant_id, virtual_key_id, ts);
CREATE INDEX IF NOT EXISTS idx_prompts_ts ON prompt_logs (tenant_id, ts);
