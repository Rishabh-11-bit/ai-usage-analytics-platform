"""One-shot migration: copy the SQLite database into Postgres.

Usage:
    pip install psycopg2-binary
    psql "$DATABASE_URL" -f schema_postgres.sql
    DATABASE_URL=postgres://user:pass@host/db python migrate_to_postgres.py

Copies rows table-by-table preserving IDs, then resets each sequence.
Idempotent-ish: uses ON CONFLICT DO NOTHING, so re-running skips
existing rows. The app itself still runs on SQLite; switching the
runtime to Postgres is the next infra step after this data move.
"""
import os
import sqlite3
import sys

TABLES = [  # dependency order
    ("tenants", ["id", "name", "created"]),
    ("employees", ["id", "tenant_id", "external_id", "name", "email", "team", "active"]),
    ("users", ["id", "tenant_id", "email", "name", "pw_hash", "role", "employee_id"]),
    ("sessions", ["token", "user_id", "expires"]),
    ("projects", ["id", "tenant_id", "name", "client"]),
    ("virtual_keys", ["id", "tenant_id", "employee_id", "key_prefix", "key_hash",
                      "monthly_budget_usd", "active", "created"]),
    ("usage_events", ["id", "tenant_id", "event_key", "employee_id", "project_id",
                      "provider", "model", "input_tokens", "cached_tokens",
                      "output_tokens", "cost_usd", "source", "virtual_key_id", "ts"]),
    ("seats", ["id", "tenant_id", "tool", "employee_id", "monthly_cost_usd", "last_active"]),
    ("prompt_logs", ["id", "tenant_id", "employee_id", "virtual_key_id", "project_id",
                     "model", "prompt_redacted", "input_tokens", "output_tokens",
                     "cost_usd", "ts"]),
    ("settings", ["tenant_id", "key", "value"]),
    ("billing", ["tenant_id", "plan", "trial_started", "stripe_customer_id",
                 "stripe_subscription_id"]),
]

SERIAL_TABLES = ["tenants", "employees", "users", "projects", "virtual_keys",
                 "usage_events", "seats", "prompt_logs"]


def main():
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL to the target Postgres instance.")
    try:
        import psycopg2
    except ImportError:
        sys.exit("pip install psycopg2-binary")

    sqlite_path = os.environ.get("TOKEN_TRACKER_DB", "./token_tracker.db")
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(url)
    cur = dst.cursor()

    total = 0
    for table, cols in TABLES:
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        if not rows:
            continue
        placeholders = ", ".join(["%s"] * len(cols))
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) "
               f"VALUES ({placeholders}) ON CONFLICT DO NOTHING")
        cur.executemany(sql, [tuple(r) for r in rows])
        print(f"{table}: {len(rows)} rows")
        total += len(rows)

    for table in SERIAL_TABLES:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1))")

    dst.commit()
    print(f"\nMigrated {total} rows from {sqlite_path}.")


if __name__ == "__main__":
    main()
