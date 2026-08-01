"""Directory sync: SCIM 2.0 server (Okta / Microsoft Entra push),
CSV import, and manual mapping of unmatched provider IDs.

SCIM: the IdP pushes Users to /scim/v2/Users with a per-tenant bearer
token (generate in the dashboard; only its hash is stored). userName is
the email; the enterprise extension's department becomes the team.
Deactivation (active: false) soft-disables the employee.
"""
import hashlib
import secrets

import db

ENTERPRISE_EXT = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


# ---- token management ----

def issue_token(tenant_id):
    raw = "scim_" + secrets.token_hex(20)
    db.set_setting(tenant_id, "scim_token_hash",
                   hashlib.sha256(raw.encode()).hexdigest())
    return raw


def resolve_token(raw):
    """SCIM bearer token -> tenant_id or None."""
    h = hashlib.sha256(raw.encode()).hexdigest()
    rows = db.query(
        "SELECT tenant_id FROM settings WHERE key='scim_token_hash' AND value=?", (h,))
    return rows[0]["tenant_id"] if rows else None


# ---- SCIM resource mapping ----

def _to_scim(emp):
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": str(emp["id"]),
        "userName": emp["email"] or emp["external_id"],
        "name": {"formatted": emp["name"]},
        "active": bool(emp["active"]),
        ENTERPRISE_EXT: {"department": emp["team"] or ""},
    }


def list_users(tenant_id, filter_expr=None):
    emps = db.query("SELECT * FROM employees WHERE tenant_id=?", (tenant_id,))
    if filter_expr:
        # Okta/Entra probe: userName eq "someone@x.com"
        parts = filter_expr.split('"')
        if len(parts) >= 2 and filter_expr.lower().startswith("username eq"):
            needle = parts[1].lower()
            emps = [e for e in emps if (e["email"] or "").lower() == needle]
    return {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": len(emps),
        "startIndex": 1, "itemsPerPage": len(emps),
        "Resources": [_to_scim(e) for e in emps],
    }


def create_user(tenant_id, payload):
    email = (payload.get("userName") or "").lower()
    name = (payload.get("name") or {}).get("formatted") \
        or (payload.get("displayName")) \
        or email.split("@")[0].replace(".", " ").title()
    team = (payload.get(ENTERPRISE_EXT) or {}).get("department")
    emp_id = db.upsert_employee(tenant_id, f"scim:{email}", name, email, team)
    emp = db.query("SELECT * FROM employees WHERE id=?", (emp_id,))[0]
    return _to_scim(emp)


def get_user(tenant_id, emp_id):
    rows = db.query("SELECT * FROM employees WHERE tenant_id=? AND id=?",
                    (tenant_id, emp_id))
    return _to_scim(rows[0]) if rows else None


def patch_user(tenant_id, emp_id, payload):
    """Handles the deactivation PATCH IdPs send on deprovisioning."""
    for op in payload.get("Operations", []):
        if op.get("op", "").lower() == "replace":
            val = op.get("value", {})
            if isinstance(val, dict) and "active" in val:
                db.execute("UPDATE employees SET active=? WHERE tenant_id=? AND id=?",
                           (1 if val["active"] else 0, tenant_id, emp_id))
            if op.get("path") == "active":
                db.execute("UPDATE employees SET active=? WHERE tenant_id=? AND id=?",
                           (1 if op.get("value") else 0, tenant_id, emp_id))
    return get_user(tenant_id, emp_id)


# ---- CSV import ----

def import_csv(tenant_id, text):
    """Rows: name,email,team (header optional). Returns imported count."""
    n = 0
    for line in text.strip().splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2 or parts[1].lower() in ("email", ""):
            continue
        name, email = parts[0], parts[1].lower()
        team = parts[2] if len(parts) > 2 else None
        db.upsert_employee(tenant_id, f"csv:{email}", name, email, team)
        n += 1
    return n


# ---- manual mapping of unmatched provider IDs ----

def unmapped(tenant_id):
    """Employees auto-created by pollers/connectors with no real identity."""
    return db.query("""
        SELECT id, external_id, name FROM employees
        WHERE tenant_id=? AND email IS NULL
          AND (external_id LIKE 'openai:%' OR external_id LIKE 'anthropic:%'
               OR external_id LIKE 'seat:%')""", (tenant_id,))


def map_to(tenant_id, unmapped_id, target_employee_id):
    """Merge an auto-created placeholder into a real employee: reassign
    usage, seats, prompts, then delete the placeholder."""
    ok = db.query("SELECT 1 FROM employees WHERE tenant_id=? AND id=?",
                  (tenant_id, unmapped_id)) and \
         db.query("SELECT 1 FROM employees WHERE tenant_id=? AND id=?",
                  (tenant_id, target_employee_id))
    if not ok:
        raise ValueError("Employee not found in this tenant")
    for table in ("usage_events", "prompt_logs", "virtual_keys"):
        db.execute(f"UPDATE {table} SET employee_id=? WHERE tenant_id=? AND employee_id=?",
                   (target_employee_id, tenant_id, unmapped_id))
    # seats are unique per (tenant, tool, employee): merge what can move,
    # drop duplicates the target already holds
    db.execute("UPDATE OR IGNORE seats SET employee_id=? WHERE tenant_id=? AND employee_id=?",
               (target_employee_id, tenant_id, unmapped_id))
    db.execute("DELETE FROM seats WHERE tenant_id=? AND employee_id=?",
               (tenant_id, unmapped_id))
    db.execute("DELETE FROM employees WHERE tenant_id=? AND id=?",
               (tenant_id, unmapped_id))
