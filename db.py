"""SQLite storage layer for TokenTracker (multi-tenant schema v2).

DB path is configurable via TOKEN_TRACKER_DB (default: ./token_tracker.db).
Every data row is tenant-scoped; queries must always filter by tenant_id.
"""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("TOKEN_TRACKER_DB", "./token_tracker.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    pw_hash TEXT NOT NULL,              -- pbkdf2: salt$hexdigest
    role TEXT NOT NULL DEFAULT 'member',-- admin | member
    employee_id INTEGER REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    external_id TEXT NOT NULL,          -- provider user_id / api key hash
    name TEXT NOT NULL,
    email TEXT,
    team TEXT,
    active INTEGER NOT NULL DEFAULT 1,  -- SCIM deprovisioning sets 0
    UNIQUE(tenant_id, external_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    client TEXT,                        -- client the work is billed to
    UNIQUE(tenant_id, name)
);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    event_key TEXT NOT NULL,            -- idempotency key: provider|user|model|hour
    employee_id INTEGER REFERENCES employees(id),
    project_id INTEGER REFERENCES projects(id),
    provider TEXT NOT NULL,             -- openai | anthropic
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'poller',  -- poller | gateway
    virtual_key_id INTEGER REFERENCES virtual_keys(id),
    ts TEXT NOT NULL,                   -- ISO-8601 UTC
    UNIQUE(tenant_id, event_key)
);

CREATE TABLE IF NOT EXISTS seats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    tool TEXT NOT NULL,                 -- e.g. GitHub Copilot, Cursor, Claude Code
    employee_id INTEGER REFERENCES employees(id),
    monthly_cost_usd REAL NOT NULL,
    last_active TEXT,                   -- ISO-8601; NULL = never used
    UNIQUE(tenant_id, tool, employee_id)
);

CREATE TABLE IF NOT EXISTS virtual_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    employee_id INTEGER REFERENCES employees(id),
    key_prefix TEXT NOT NULL,           -- first chars, for display only
    key_hash TEXT NOT NULL UNIQUE,      -- sha256 of the full key
    monthly_budget_usd REAL NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prompt_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    employee_id INTEGER REFERENCES employees(id),
    virtual_key_id INTEGER REFERENCES virtual_keys(id),
    project_id INTEGER REFERENCES projects(id),
    model TEXT,
    prompt_redacted TEXT,               -- redacted before storage; opt-in
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (tenant_id, key)
);

CREATE TABLE IF NOT EXISTS billing (
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id),
    plan TEXT NOT NULL DEFAULT 'trial', -- trial | pro | expired
    trial_started TEXT NOT NULL DEFAULT (datetime('now')),
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events (tenant_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_emp ON usage_events (tenant_id, employee_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_proj ON usage_events (tenant_id, project_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_events (tenant_id, virtual_key_id, ts);
CREATE INDEX IF NOT EXISTS idx_prompts_ts ON prompt_logs (tenant_id, ts);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as c:
        c.executescript(SCHEMA)


def create_tenant(name):
    with get_conn() as c:
        c.execute("INSERT INTO tenants (name) VALUES (?)", (name,))
        return c.execute("SELECT id FROM tenants WHERE name=?", (name,)).fetchone()["id"]


def upsert_employee(tenant_id, external_id, name, email=None, team=None):
    with get_conn() as c:
        c.execute(
            """INSERT INTO employees (tenant_id, external_id, name, email, team)
               VALUES (?,?,?,?,?)
               ON CONFLICT(tenant_id, external_id) DO UPDATE SET
                 name=excluded.name, email=excluded.email, team=excluded.team""",
            (tenant_id, external_id, name, email, team),
        )
        row = c.execute(
            "SELECT id FROM employees WHERE tenant_id=? AND external_id=?",
            (tenant_id, external_id),
        ).fetchone()
        return row["id"]


def upsert_project(tenant_id, name, client=None):
    with get_conn() as c:
        c.execute(
            """INSERT INTO projects (tenant_id, name, client) VALUES (?,?,?)
               ON CONFLICT(tenant_id, name) DO UPDATE SET client=excluded.client""",
            (tenant_id, name, client),
        )
        row = c.execute(
            "SELECT id FROM projects WHERE tenant_id=? AND name=?", (tenant_id, name)
        ).fetchone()
        return row["id"]


def insert_event(tenant_id, event_key, employee_id, provider, model, input_tokens,
                 cached_tokens, output_tokens, cost_usd, source, ts, project_id=None):
    """Idempotent insert — re-polling overlapping windows is safe."""
    with get_conn() as c:
        c.execute(
            """INSERT OR IGNORE INTO usage_events
               (tenant_id, event_key, employee_id, project_id, provider, model,
                input_tokens, cached_tokens, output_tokens, cost_usd, source, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tenant_id, event_key, employee_id, project_id, provider, model,
             input_tokens, cached_tokens, output_tokens, cost_usd, source, ts),
        )


def upsert_seat(tenant_id, tool, employee_id, monthly_cost_usd, last_active=None):
    """Connector-safe: re-syncing seats never duplicates them."""
    with get_conn() as c:
        c.execute(
            """INSERT INTO seats (tenant_id, tool, employee_id, monthly_cost_usd, last_active)
               VALUES (?,?,?,?,?)
               ON CONFLICT(tenant_id, tool, employee_id) DO UPDATE SET
                 monthly_cost_usd=excluded.monthly_cost_usd,
                 last_active=excluded.last_active""",
            (tenant_id, tool, employee_id, monthly_cost_usd, last_active),
        )


# Back-compat alias (older seeders call add_seat)
add_seat = upsert_seat


def get_setting(tenant_id, key, default=None):
    rows = query("SELECT value FROM settings WHERE tenant_id=? AND key=?", (tenant_id, key))
    return rows[0]["value"] if rows else default


def set_setting(tenant_id, key, value):
    execute(
        """INSERT INTO settings (tenant_id, key, value) VALUES (?,?,?)
           ON CONFLICT(tenant_id, key) DO UPDATE SET value=excluded.value""",
        (tenant_id, key, value),
    )


def query(sql, params=()):
    with get_conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def execute(sql, params=()):
    with get_conn() as c:
        c.execute(sql, params)
