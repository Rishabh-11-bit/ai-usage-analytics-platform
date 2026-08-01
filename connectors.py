"""Seat-license connectors: GitHub Copilot and Cursor.

Real mode (per connector, via env vars on the polling host):
  Copilot:  GITHUB_TOKEN (org admin, `manage_billing:copilot` scope) + GITHUB_ORG
            -> GET https://api.github.com/orgs/{org}/copilot/billing/seats
  Cursor:   CURSOR_API_KEY (team admin key)
            -> GET https://api.cursor.com/teams/members  (basic auth)

Demo mode (no env keys): refreshes the seeded seats' last_active
timestamps so the utilization panel stays alive.

Sync is idempotent: seats upsert on (tenant, tool, employee).
"""
import base64
import json
import os
import random
import urllib.request
from datetime import datetime, timedelta, timezone

import db

SEAT_PRICES = {"GitHub Copilot": 19.0, "Cursor": 20.0}


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _match_employee(tenant_id, login_or_email, display_name=None):
    """Match a seat holder to an employee by email, else create an
    unmapped placeholder the admin can merge in the directory panel."""
    if login_or_email and "@" in login_or_email:
        rows = db.query(
            "SELECT id FROM employees WHERE tenant_id=? AND lower(email)=lower(?)",
            (tenant_id, login_or_email))
        if rows:
            return rows[0]["id"]
    return db.upsert_employee(
        tenant_id, f"seat:{login_or_email}",
        display_name or login_or_email or "unknown")


def sync_copilot(tenant_id):
    token, org = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_ORG")
    if not (token and org):
        return _demo_refresh(tenant_id, "GitHub Copilot")
    n, page = 0, 1
    while True:
        data = _get(
            f"https://api.github.com/orgs/{org}/copilot/billing/seats?per_page=100&page={page}",
            {"Authorization": f"Bearer {token}",
             "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"})
        seats = data.get("seats", [])
        for s in seats:
            assignee = s.get("assignee", {}) or {}
            emp = _match_employee(tenant_id, assignee.get("email") or assignee.get("login"),
                                  assignee.get("login"))
            db.upsert_seat(tenant_id, "GitHub Copilot", emp,
                           SEAT_PRICES["GitHub Copilot"], s.get("last_activity_at"))
            n += 1
        if len(seats) < 100:
            break
        page += 1
    _mark_synced(tenant_id, "copilot", "real", n)
    return {"mode": "real", "seats": n}


def sync_cursor(tenant_id):
    key = os.environ.get("CURSOR_API_KEY")
    if not key:
        return _demo_refresh(tenant_id, "Cursor")
    basic = base64.b64encode(f"{key}:".encode()).decode()
    data = _get("https://api.cursor.com/teams/members",
                {"Authorization": f"Basic {basic}"})
    n = 0
    for m in data.get("teamMembers", []):
        emp = _match_employee(tenant_id, m.get("email"), m.get("name"))
        db.upsert_seat(tenant_id, "Cursor", emp, SEAT_PRICES["Cursor"], None)
        n += 1
    _mark_synced(tenant_id, "cursor", "real", n)
    return {"mode": "real", "seats": n}


def _demo_refresh(tenant_id, tool):
    """No credentials: nudge existing demo seats so the panel stays live."""
    now = datetime.now(timezone.utc)
    seats = db.query("SELECT id, last_active FROM seats WHERE tenant_id=? AND tool=?",
                     (tenant_id, tool))
    for s in seats:
        # active seats drift forward; idle seats stay idle
        if s["last_active"] and s["last_active"] > (now - timedelta(days=30)).isoformat():
            db.execute("UPDATE seats SET last_active=? WHERE id=?",
                       ((now - timedelta(hours=random.randint(1, 48))).isoformat(), s["id"]))
    name = "copilot" if tool == "GitHub Copilot" else "cursor"
    _mark_synced(tenant_id, name, "demo", len(seats))
    return {"mode": "demo", "seats": len(seats)}


def _mark_synced(tenant_id, name, mode, count):
    db.set_setting(tenant_id, f"connector:{name}:last_sync",
                   datetime.now(timezone.utc).isoformat())
    db.set_setting(tenant_id, f"connector:{name}:mode", mode)
    db.set_setting(tenant_id, f"connector:{name}:count", str(count))


SYNCS = {"copilot": sync_copilot, "cursor": sync_cursor}


def status(tenant_id):
    """Connector cards for the dashboard."""
    out = []
    defs = [
        ("openai", "OpenAI usage API", "OPENAI_ADMIN_KEY"),
        ("anthropic", "Anthropic usage API", "ANTHROPIC_ADMIN_KEY"),
        ("copilot", "GitHub Copilot seats", "GITHUB_TOKEN + GITHUB_ORG"),
        ("cursor", "Cursor seats", "CURSOR_API_KEY"),
    ]
    for name, label, env in defs:
        configured = all(os.environ.get(v.strip()) for v in env.split("+"))
        out.append({
            "name": name, "label": label, "env": env,
            "configured": configured,
            "syncable": name in SYNCS,
            "last_sync": db.get_setting(tenant_id, f"connector:{name}:last_sync"),
            "mode": db.get_setting(tenant_id, f"connector:{name}:mode",
                                   "real" if configured else "demo"),
        })
    return out
