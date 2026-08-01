"""AI gateway: per-employee virtual keys, budget enforcement, prompt-level
attribution, redacted prompt storage (opt-in), and coaching on real traffic.

Every request through the gateway is attributed to an employee and
optionally a project (X-Project header), so chargeback works at the
request level — no more shared-key blobs.

Real mode: set OPENAI_API_KEY / ANTHROPIC_API_KEY and requests are
forwarded to the provider. Without keys, a deterministic simulator
answers so the whole flow is demo-able.
"""
import hashlib
import json
import os
import re
import secrets
import urllib.request
from datetime import datetime, timezone

import coach
import db
from pricing import cost_usd

PROMPT_MAX_STORED = 4000

REDACTIONS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (re.compile(r"\b(?:sk|vk|ghp|gho|ghs|xoxb|xoxp|AKIA)[A-Za-z0-9_\-]{8,}\b"), "[key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\.?[A-Za-z0-9_\-]*\b"), "[token]"),
    (re.compile(r"\b\d{6,}\b"), "[number]"),
]


def redact(text):
    for pat, repl in REDACTIONS:
        text = pat.sub(repl, text)
    return text[:PROMPT_MAX_STORED]


# ---- virtual keys ----

def create_key(tenant_id, employee_id, monthly_budget_usd=100.0):
    """Returns the full key ONCE; only its hash is stored."""
    raw = "vk_" + secrets.token_hex(16)
    db.execute(
        """INSERT INTO virtual_keys
           (tenant_id, employee_id, key_prefix, key_hash, monthly_budget_usd)
           VALUES (?,?,?,?,?)""",
        (tenant_id, employee_id, raw[:11] + "…",
         hashlib.sha256(raw.encode()).hexdigest(), monthly_budget_usd),
    )
    return raw


def resolve_key(raw):
    rows = db.query(
        "SELECT * FROM virtual_keys WHERE key_hash=? AND active=1",
        (hashlib.sha256(raw.encode()).hexdigest(),))
    return rows[0] if rows else None


def list_keys(tenant_id):
    rows = db.query("""
        SELECT k.id, k.key_prefix, k.monthly_budget_usd, k.active, k.created,
               e.name AS employee, e.team,
               ROUND(COALESCE((
                   SELECT SUM(u.cost_usd) FROM usage_events u
                   WHERE u.virtual_key_id = k.id
                     AND strftime('%Y-%m', u.ts) = strftime('%Y-%m', 'now')
               ), 0), 2) AS spent_this_month
        FROM virtual_keys k LEFT JOIN employees e ON e.id = k.employee_id
        WHERE k.tenant_id = ? ORDER BY k.created""", (tenant_id,))
    for r in rows:
        r["over_budget"] = r["spent_this_month"] >= r["monthly_budget_usd"]
    return rows


def update_key(tenant_id, key_id, monthly_budget_usd=None, active=None):
    if monthly_budget_usd is not None:
        db.execute("UPDATE virtual_keys SET monthly_budget_usd=? WHERE id=? AND tenant_id=?",
                   (monthly_budget_usd, key_id, tenant_id))
    if active is not None:
        db.execute("UPDATE virtual_keys SET active=? WHERE id=? AND tenant_id=?",
                   (1 if active else 0, key_id, tenant_id))


def month_spend(key):
    rows = db.query("""
        SELECT COALESCE(SUM(cost_usd), 0) AS c FROM usage_events
        WHERE virtual_key_id=? AND strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')""",
        (key["id"],))
    return rows[0]["c"]


# ---- completion handling ----

def _provider_for(model):
    return "anthropic" if model.startswith("claude") else "openai"


def _forward_real(provider, model, messages, max_tokens):
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        body = json.dumps({"model": model, "max_tokens": max_tokens or 1024,
                           "messages": messages}).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return {
            "content": "".join(b.get("text", "") for b in data.get("content", [])),
            "input_tokens": data["usage"]["input_tokens"],
            "cached_tokens": data["usage"].get("cache_read_input_tokens", 0),
            "output_tokens": data["usage"]["output_tokens"],
        }
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": messages}).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    u = data.get("usage", {})
    return {
        "content": data["choices"][0]["message"]["content"],
        "input_tokens": u.get("prompt_tokens", 0),
        "cached_tokens": u.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        "output_tokens": u.get("completion_tokens", 0),
    }


def _simulate(model, messages, max_tokens):
    """Deterministic demo response with realistic token accounting."""
    prompt_text = "\n".join(m.get("content", "") for m in messages)
    inp = coach.estimate_tokens(prompt_text) + 8 * len(messages)
    out = min(max_tokens or 300, max(60, inp // 3))
    return {
        "content": (f"[simulated {model} response — set OPENAI_API_KEY / "
                    "ANTHROPIC_API_KEY on the gateway host for real completions]"),
        "input_tokens": inp, "cached_tokens": 0, "output_tokens": out,
    }


def complete(raw_key, body, project_name=None):
    """The gateway request path. Raises GatewayError with an HTTP status."""
    key = resolve_key(raw_key)
    if not key:
        raise GatewayError(401, "Invalid or revoked virtual key")

    spent = month_spend(key)
    if spent >= key["monthly_budget_usd"]:
        raise GatewayError(429, f"Monthly budget exhausted "
                                f"(${spent:.2f} of ${key['monthly_budget_usd']:.2f}). "
                                "Ask your admin to raise it.")

    model = body.get("model", "claude-haiku-4-5")
    messages = body.get("messages", [])
    max_tokens = body.get("max_tokens")
    if not messages:
        raise GatewayError(400, "messages[] required")

    provider = _provider_for(model)
    result = _forward_real(provider, model, messages, max_tokens) \
        or _simulate(model, messages, max_tokens)

    cost = cost_usd(provider, model, result["input_tokens"],
                    result["cached_tokens"], result["output_tokens"])
    ts = datetime.now(timezone.utc).isoformat()
    tenant_id = key["tenant_id"]

    project_id = None
    if project_name:
        rows = db.query("SELECT id FROM projects WHERE tenant_id=? AND name=?",
                        (tenant_id, project_name))
        project_id = rows[0]["id"] if rows else None

    event_key = "gw|" + secrets.token_hex(8)
    db.insert_event(tenant_id, event_key, key["employee_id"], provider, model,
                    result["input_tokens"], result["cached_tokens"],
                    result["output_tokens"], cost, "gateway", ts,
                    project_id=project_id)
    db.execute("UPDATE usage_events SET virtual_key_id=? WHERE tenant_id=? AND event_key=?",
               (key["id"], tenant_id, event_key))

    if db.get_setting(tenant_id, "store_prompts", "0") == "1":
        prompt_text = "\n".join(m.get("content", "") for m in messages)
        db.execute(
            """INSERT INTO prompt_logs (tenant_id, employee_id, virtual_key_id,
               project_id, model, prompt_redacted, input_tokens, output_tokens,
               cost_usd, ts) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (tenant_id, key["employee_id"], key["id"], project_id, model,
             redact(prompt_text), result["input_tokens"],
             result["output_tokens"], cost, ts))

    budget_left = key["monthly_budget_usd"] - spent - cost
    return {
        "model": model,
        "content": result["content"],
        "usage": {"input_tokens": result["input_tokens"],
                  "cached_tokens": result["cached_tokens"],
                  "output_tokens": result["output_tokens"],
                  "cost_usd": round(cost, 6)},
        "budget": {"monthly_usd": key["monthly_budget_usd"],
                   "spent_usd": round(spent + cost, 4),
                   "remaining_usd": round(max(budget_left, 0), 4)},
    }


class GatewayError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(detail)


# ---- coaching on real traffic ----

def recent_prompts(tenant_id, limit=25):
    return db.query("""
        SELECT p.ts, p.model, p.prompt_redacted, p.input_tokens, p.output_tokens,
               p.cost_usd, e.name AS employee
        FROM prompt_logs p LEFT JOIN employees e ON e.id = p.employee_id
        WHERE p.tenant_id = ? ORDER BY p.ts DESC LIMIT ?""", (tenant_id, limit))


def traffic_coach(tenant_id, days=7):
    """Run the prompt coach over stored traffic; aggregate by distinct prompt."""
    rows = db.query(f"""
        SELECT p.prompt_redacted, p.model, COUNT(*) AS runs,
               MAX(e.name) AS employee
        FROM prompt_logs p LEFT JOIN employees e ON e.id = p.employee_id
        WHERE p.tenant_id = ? AND p.ts >= datetime('now', '-{int(days)} days')
          AND length(p.prompt_redacted) > 40
        GROUP BY p.prompt_redacted, p.model
        ORDER BY runs DESC LIMIT 100""", (tenant_id,))

    findings, total_monthly, issue_counts = [], 0.0, {}
    for r in rows:
        provider = _provider_for(r["model"] or "")
        # fractional runs/day so a weekly prompt isn't billed as daily
        runs_per_day = r["runs"] / days
        a = coach.analyze(r["prompt_redacted"], provider,
                          r["model"] or "claude-sonnet-5", runs_per_day)
        if not a["issues"]:
            continue
        total_monthly += a["est_monthly_savings_usd"]
        for i in a["issues"]:
            issue_counts[i["type"]] = issue_counts.get(i["type"], 0) + 1
        findings.append({
            "employee": r["employee"], "model": r["model"], "runs": r["runs"],
            "reduction_pct": a["reduction_pct"],
            "est_monthly_savings_usd": a["est_monthly_savings_usd"],
            "top_issues": [i["type"] for i in a["issues"][:3]],
            "excerpt": r["prompt_redacted"][:140],
        })

    findings.sort(key=lambda f: -f["est_monthly_savings_usd"])
    return {
        "days": days,
        "prompts_analyzed": len(rows),
        "prompts_with_findings": len(findings),
        "est_monthly_savings_usd": round(total_monthly, 2),
        "issue_counts": dict(sorted(issue_counts.items(), key=lambda x: -x[1])),
        "worst": findings[:5],
    }
