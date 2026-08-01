"""Auth: org signup, login, bearer sessions, role-based access.

Passwords: PBKDF2-HMAC-SHA256, 200k iterations, per-user random salt.
Sessions: opaque random bearer tokens, 7-day expiry, stored server-side
so they can be revoked. No JWTs — nothing user-decodable to tamper with.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import db

SESSION_DAYS = 7
PBKDF2_ITERS = 200_000


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ITERS
    ).hex()
    return f"{salt}${digest}"


def verify_password(password, stored):
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_password(password, salt), stored)


def signup(org_name, name, email, password):
    """Create tenant + its first (admin) user. Returns a session token."""
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if db.query("SELECT 1 FROM users WHERE email=?", (email.lower(),)):
        raise ValueError("An account with this email already exists")
    if db.query("SELECT 1 FROM tenants WHERE name=?", (org_name,)):
        raise ValueError("An organization with this name already exists")
    tenant_id = db.create_tenant(org_name)
    db.execute(
        "INSERT INTO users (tenant_id, email, name, pw_hash, role) VALUES (?,?,?,?,'admin')",
        (tenant_id, email.lower(), name, hash_password(password)),
    )
    return login(email, password)


def create_user(tenant_id, name, email, password, role="member", employee_id=None):
    if db.query("SELECT 1 FROM users WHERE email=?", (email.lower(),)):
        raise ValueError("An account with this email already exists")
    db.execute(
        """INSERT INTO users (tenant_id, email, name, pw_hash, role, employee_id)
           VALUES (?,?,?,?,?,?)""",
        (tenant_id, email.lower(), name, hash_password(password), role, employee_id),
    )


def login(email, password):
    rows = db.query("SELECT * FROM users WHERE email=?", (email.lower(),))
    if not rows or not verify_password(password, rows[0]["pw_hash"]):
        raise ValueError("Invalid email or password")
    user = rows[0]
    token = secrets.token_hex(24)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    db.execute("INSERT INTO sessions (token, user_id, expires) VALUES (?,?,?)",
               (token, user["id"], expires))
    return {"token": token, "user": _public(user)}


def logout(token):
    db.execute("DELETE FROM sessions WHERE token=?", (token,))


def resolve(token):
    """Token -> user dict (with tenant_id, role, employee_id) or None."""
    rows = db.query(
        """SELECT u.*, s.expires FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token=?""", (token,))
    if not rows:
        return None
    if rows[0]["expires"] < datetime.now(timezone.utc).isoformat():
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    return _public(rows[0])


def _public(u):
    tenant = db.query("SELECT name FROM tenants WHERE id=?", (u["tenant_id"],))
    return {
        "id": u["id"], "tenant_id": u["tenant_id"],
        "tenant_name": tenant[0]["name"] if tenant else "",
        "email": u["email"], "name": u["name"], "role": u["role"],
        "employee_id": u["employee_id"],
    }
