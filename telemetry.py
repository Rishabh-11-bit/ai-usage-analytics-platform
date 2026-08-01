"""Extension telemetry: per-employee reporting tokens and counter ingest.

The browser extension reports COUNTERS ONLY (tokens, model, site, ts) —
never prompt or response content. Each employee gets a reporting token
(shown once, stored hashed). Ingested rows land in usage_events with
source='extension' so they flow through the same analytics, but they are
ESTIMATES and are labelled as such everywhere they surface.
"""
import hashlib
import secrets
from datetime import datetime, timezone

import db
from pricing import cost_usd

TOKEN_PREFIX = "ttw_"   # TokenTracker web reporting token


def issue_token(tenant_id, employee_id):
    """Mint a reporting token bound to one employee. Returns it once."""
    raw = TOKEN_PREFIX + secrets.token_hex(20)
    db.execute(
        "INSERT INTO settings (tenant_id, key, value) VALUES (?,?,?) "
        "ON CONFLICT(tenant_id, key) DO UPDATE SET value=excluded.value",
        (tenant_id, f"webtoken:{hashlib.sha256(raw.encode()).hexdigest()}",
         str(employee_id)))
    return raw


def resolve_token(raw):
    """token -> (tenant_id, employee_id) or None."""
    if not raw or not raw.startswith(TOKEN_PREFIX):
        return None
    h = hashlib.sha256(raw.encode()).hexdigest()
    rows = db.query(
        "SELECT tenant_id, value FROM settings WHERE key=?", (f"webtoken:{h}",))
    if not rows:
        return None
    return rows[0]["tenant_id"], int(rows[0]["value"])


# provider inference from the model string the extension detected
def _provider(model):
    m = (model or "").lower()
    if "claude" in m or m in ("opus", "sonnet", "haiku"):
        return "anthropic"
    if "gemini" in m:
        return "google"
    return "openai"


def _canonical_model(model):
    """Map a UI label to a priced model id when we can, else keep as-is."""
    m = (model or "").lower()
    table = {
        "opus": "claude-opus-4-8", "sonnet": "claude-sonnet-5",
        "haiku": "claude-haiku-4-5",
    }
    for k, v in table.items():
        if k in m and "claude" not in m:
            return v
    if "claude" in m:
        if "opus" in m: return "claude-opus-4-8"
        if "haiku" in m: return "claude-haiku-4-5"
        return "claude-sonnet-5"
    if "mini" in m: return "gpt-4o-mini"
    if "gpt-4o" in m or "gpt-4" in m or "gpt-5" in m: return "gpt-4o"
    return model or "unknown"


def ingest(tenant_id, employee_id, events):
    """events: [{ts, site, model, input_tokens, output_tokens}]. Idempotent
    per (employee, ts, model) via a deterministic event_key."""
    n = 0
    for e in events:
        ts = e.get("ts") or datetime.now(timezone.utc).isoformat()
        raw_model = e.get("model") or "unknown"
        model = _canonical_model(raw_model)
        provider = _provider(raw_model)
        inp = int(e.get("input_tokens") or 0)
        out = int(e.get("output_tokens") or 0)
        if inp <= 0 and out <= 0:
            continue
        # estimated cost using the same pricing table (no cached tokens visible)
        cost = cost_usd(provider, model, inp, 0, out)
        key = "ext|" + hashlib.sha1(
            f"{employee_id}|{ts}|{raw_model}|{inp}|{out}".encode()).hexdigest()[:20]
        db.insert_event(tenant_id, key, employee_id, provider, model,
                        inp, 0, out, cost, "extension", ts)
        n += 1
    return n
