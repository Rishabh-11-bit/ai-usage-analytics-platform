"""Provider pollers.

Real mode: set OPENAI_ADMIN_KEY / ANTHROPIC_ADMIN_KEY env vars and call
poll_openai() / poll_anthropic(). Without keys, both raise so you can fall
back to seed_demo.py for demo data.

- OpenAI: GET https://api.openai.com/v1/organization/usage/completions
  (group_by=user_id) and /v1/organization/costs
- Anthropic: GET https://api.anthropic.com/v1/organizations/usage_report/messages
  and /v1/organizations/cost_report
"""
import hashlib
import os
import urllib.request
import json
from datetime import datetime, timezone, timedelta

import db
from pricing import cost_usd


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _event_key(provider, user, model, bucket):
    return hashlib.sha1(f"{provider}|{user}|{model}|{bucket}".encode()).hexdigest()


def poll_openai(tenant_id, days=1):
    key = os.environ.get("OPENAI_ADMIN_KEY")
    if not key:
        raise RuntimeError("OPENAI_ADMIN_KEY not set — use seed_demo.py for demo data")
    start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    url = ("https://api.openai.com/v1/organization/usage/completions"
           f"?start_time={start}&bucket_width=1h&group_by=user_id&group_by=model")
    data = _get(url, {"Authorization": f"Bearer {key}"})
    n = 0
    for bucket in data.get("data", []):
        for r in bucket.get("results", []):
            user = r.get("user_id") or "unknown"
            model = r.get("model") or "unknown"
            emp_id = db.upsert_employee(tenant_id, f"openai:{user}", user)
            inp = r.get("input_tokens", 0)
            cached = r.get("input_cached_tokens", 0)
            out = r.get("output_tokens", 0)
            ts = datetime.fromtimestamp(bucket["start_time"], timezone.utc).isoformat()
            db.insert_event(
                tenant_id, _event_key("openai", user, model, bucket["start_time"]),
                emp_id, "openai", model, inp, cached, out,
                cost_usd("openai", model, inp, cached, out), "poller", ts)
            n += 1
    return n


def poll_anthropic(tenant_id, days=1):
    key = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_ADMIN_KEY not set — use seed_demo.py for demo data")
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:00:00Z")
    url = ("https://api.anthropic.com/v1/organizations/usage_report/messages"
           f"?starting_at={start}&bucket_width=1h&group_by[]=api_key_id&group_by[]=model")
    data = _get(url, {"x-api-key": key, "anthropic-version": "2023-06-01"})
    n = 0
    for bucket in data.get("data", []):
        for r in bucket.get("results", []):
            user = r.get("api_key_id") or "unknown"
            model = r.get("model") or "unknown"
            emp_id = db.upsert_employee(tenant_id, f"anthropic:{user}", user)
            inp = r.get("uncached_input_tokens", 0) + r.get("cache_read_input_tokens", 0)
            cached = r.get("cache_read_input_tokens", 0)
            out = r.get("output_tokens", 0)
            ts = bucket.get("starting_at")
            db.insert_event(
                tenant_id, _event_key("anthropic", user, model, ts),
                emp_id, "anthropic", model, inp, cached, out,
                cost_usd("anthropic", model, inp, cached, out), "poller", ts)
            n += 1
    return n


if __name__ == "__main__":
    db.init_db()
    tenant_id = int(os.environ.get("TOKEN_TRACKER_TENANT_ID", "1"))
    for fn in (poll_openai, poll_anthropic):
        try:
            print(fn.__name__, "->", fn(tenant_id), "events")
        except RuntimeError as e:
            print(fn.__name__, "skipped:", e)
