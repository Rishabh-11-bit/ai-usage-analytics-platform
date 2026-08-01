"""Billing: 14-day trial, per-employee pricing, Stripe integration.

Pricing: $2 per active tracked employee per month, $29 minimum — small
next to the savings the suggestions engine identifies, and the dashboard
shows that ROI comparison explicitly (find money first, charge second).

Stripe activates when STRIPE_SECRET_KEY is set: checkout creates a
subscription session; the webhook (signature-verified) flips the plan
to 'pro'. Without the key, everything still works as an explicit demo.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import db
import insights

TRIAL_DAYS = 14
PER_EMPLOYEE_USD = 2.0
MINIMUM_USD = 29.0


def ensure_row(tenant_id):
    if not db.query("SELECT 1 FROM billing WHERE tenant_id=?", (tenant_id,)):
        db.execute("INSERT INTO billing (tenant_id) VALUES (?)", (tenant_id,))


def state(tenant_id):
    ensure_row(tenant_id)
    row = db.query("SELECT * FROM billing WHERE tenant_id=?", (tenant_id,))[0]
    started = datetime.fromisoformat(row["trial_started"]).replace(tzinfo=timezone.utc)
    trial_left = max(0, (started + timedelta(days=TRIAL_DAYS)
                         - datetime.now(timezone.utc)).days)

    employees = insights.summary(tenant_id, 30)["active_employees"] or 0
    price = max(MINIMUM_USD, employees * PER_EMPLOYEE_USD)
    savings = sum(s["est_monthly_savings_usd"]
                  for s in insights.suggestions(tenant_id, 30))

    plan = row["plan"]
    if plan == "trial" and trial_left == 0:
        plan = "expired"

    return {
        "plan": plan,
        "trial_days_left": trial_left if plan == "trial" else 0,
        "active_employees": employees,
        "per_employee_usd": PER_EMPLOYEE_USD,
        "minimum_usd": MINIMUM_USD,
        "monthly_price_usd": round(price, 2),
        "identified_savings_usd": round(savings, 2),
        "roi": round(savings / price, 1) if price else 0,
        "stripe_configured": bool(os.environ.get("STRIPE_SECRET_KEY")),
    }


# ---- Stripe (real when STRIPE_SECRET_KEY is set) ----

def checkout_url(tenant_id, base_url):
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise ValueError("Stripe is not configured on this deployment "
                         "(set STRIPE_SECRET_KEY). This demo build tracks "
                         "the trial without charging.")
    s = state(tenant_id)
    qty = max(s["active_employees"], int(MINIMUM_USD / PER_EMPLOYEE_USD))
    form = {
        "mode": "subscription",
        "success_url": f"{base_url}/?billing=success",
        "cancel_url": f"{base_url}/?billing=cancelled",
        "client_reference_id": str(tenant_id),
        "line_items[0][quantity]": str(qty),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(int(PER_EMPLOYEE_USD * 100)),
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": "TokenTracker (per tracked employee)",
    }
    req = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=urllib.parse.urlencode(form).encode(),
        headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["url"]


def verify_webhook(payload: bytes, sig_header: str, secret: str, tolerance=300):
    """Stripe-Signature: t=<ts>,v1=<hmac>. HMAC-SHA256 over '<ts>.<payload>'."""
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    ts, v1 = parts.get("t"), parts.get("v1")
    if not (ts and v1):
        return False
    if abs(time.time() - int(ts)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def handle_webhook(payload: bytes, sig_header: str):
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET not set")
    if not verify_webhook(payload, sig_header or "", secret):
        raise ValueError("Invalid webhook signature")
    event = json.loads(payload)
    obj = event.get("data", {}).get("object", {})
    if event.get("type") == "checkout.session.completed":
        tenant_id = int(obj.get("client_reference_id", 0))
        ensure_row(tenant_id)
        db.execute("""UPDATE billing SET plan='pro', stripe_customer_id=?,
                      stripe_subscription_id=? WHERE tenant_id=?""",
                   (obj.get("customer"), obj.get("subscription"), tenant_id))
    elif event.get("type") == "customer.subscription.deleted":
        db.execute("UPDATE billing SET plan='expired' WHERE stripe_subscription_id=?",
                   (obj.get("id"),))
    return {"handled": event.get("type")}
