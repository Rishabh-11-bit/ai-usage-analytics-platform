"""Tests: ingestion idempotency, tenant isolation, auth, estimator ranges.

Run:  python -m pytest tests.py -q     (or: python tests.py)
Uses a throwaway DB file; the real one is untouched.
"""
import os
import tempfile

os.environ["TOKEN_TRACKER_DB"] = os.path.join(tempfile.gettempdir(), "tt_test.db")
if os.path.exists(os.environ["TOKEN_TRACKER_DB"]):
    os.remove(os.environ["TOKEN_TRACKER_DB"])

import auth          # noqa: E402
import billing       # noqa: E402
import db            # noqa: E402
import estimator     # noqa: E402
import gateway       # noqa: E402
import insights      # noqa: E402
import scim          # noqa: E402
import telemetry      # noqa: E402

db.init_db()
T1 = db.create_tenant("Tenant One")
T2 = db.create_tenant("Tenant Two")
E1 = db.upsert_employee(T1, "x:1", "Alice", team="Eng")
E2 = db.upsert_employee(T2, "x:1", "Bob", team="Eng")  # same external_id, other tenant


def test_ingestion_idempotency():
    for _ in range(3):  # re-polling the same window 3x
        db.insert_event(T1, "k1", E1, "openai", "gpt-4o",
                        1000, 100, 500, 0.01, "poller", "2026-07-12T10:00:00")
    rows = db.query("SELECT COUNT(*) AS n FROM usage_events WHERE tenant_id=?", (T1,))
    assert rows[0]["n"] == 1, "duplicate events on re-poll"


def test_same_event_key_isolated_across_tenants():
    db.insert_event(T2, "k1", E2, "openai", "gpt-4o",
                    2000, 0, 900, 0.02, "poller", "2026-07-12T10:00:00")
    n1 = db.query("SELECT COUNT(*) AS n FROM usage_events WHERE tenant_id=?", (T1,))[0]["n"]
    n2 = db.query("SELECT COUNT(*) AS n FROM usage_events WHERE tenant_id=?", (T2,))[0]["n"]
    assert (n1, n2) == (1, 1), "event_key uniqueness must be per-tenant"


def test_tenant_isolation_in_queries():
    s1 = insights.summary(T1, days=3650)
    s2 = insights.summary(T2, days=3650)
    assert s1["input_tokens"] == 1000, "tenant 1 sees foreign data"
    assert s2["input_tokens"] == 2000, "tenant 2 sees foreign data"


def test_member_scope_filters_to_own_employee():
    other = db.upsert_employee(T1, "x:2", "Carol", team="Eng")
    db.insert_event(T1, "k2", other, "openai", "gpt-4o",
                    5000, 0, 100, 0.05, "poller", "2026-07-12T11:00:00")
    mine = insights.summary(T1, days=3650, employee_id=E1)
    assert mine["input_tokens"] == 1000, "member sees other employees' usage"


def test_password_hash_and_verify():
    h = auth.hash_password("hunter22")
    assert auth.verify_password("hunter22", h)
    assert not auth.verify_password("wrong", h)
    assert "hunter22" not in h


def test_signup_login_and_role():
    res = auth.signup("Acme", "Ann", "ann@acme.io", "longenough")
    assert res["user"]["role"] == "admin"
    assert auth.resolve(res["token"])["email"] == "ann@acme.io"
    res2 = auth.login("ann@acme.io", "longenough")
    assert res2["token"] != res["token"]
    auth.logout(res["token"])
    assert auth.resolve(res["token"]) is None


def test_estimator_ranges_bracket_base():
    r = estimator.estimate({"simple": 5, "medium": 3, "complex": 1})
    t = r["totals"]
    assert t["ai_assisted_low_usd"] < t["ai_assisted_cost_usd"] < t["ai_assisted_high_usd"]
    assert t["savings_low_usd"] < t["savings_usd"] < t["savings_high_usd"]
    assert t["dev_only_cost_usd"] > 0


def test_estimator_empty():
    r = estimator.estimate({})
    assert r["breakdown"] == [] and r["totals"]["dev_only_cost_usd"] == 0


def test_gateway_key_and_budget_enforcement():
    raw = gateway.create_key(T1, E1, monthly_budget_usd=0.05)
    assert raw.startswith("vk_")
    assert gateway.resolve_key(raw)["employee_id"] == E1
    assert gateway.resolve_key("vk_bogus") is None
    db.set_setting(T1, "store_prompts", "1")
    body = {"model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "word " * 3000}]}
    r = gateway.complete(raw, body)  # simulated; opus makes it pricey
    assert r["usage"]["cost_usd"] > 0.05, "test setup: single call should bust budget"
    try:
        gateway.complete(raw, body)
        assert False, "expected budget 429"
    except gateway.GatewayError as e:
        assert e.status == 429


def test_gateway_records_attributed_event_and_prompt():
    events = db.query(
        "SELECT * FROM usage_events WHERE tenant_id=? AND source='gateway'", (T1,))
    assert len(events) == 1 and events[0]["employee_id"] == E1
    assert events[0]["virtual_key_id"] is not None
    logs = db.query("SELECT * FROM prompt_logs WHERE tenant_id=?", (T1,))
    assert len(logs) == 1


def test_redaction():
    red = gateway.redact("mail bob@x.io key sk-abcdefgh1234567890 num 1234567890")
    assert "bob@x.io" not in red and "[email]" in red
    assert "sk-abcdefgh" not in red and "[key]" in red
    assert "1234567890" not in red and "[number]" in red


def test_scim_create_and_deactivate():
    token = scim.issue_token(T1)
    assert scim.resolve_token(token) == T1
    assert scim.resolve_token("scim_bogus") is None
    u = scim.create_user(T1, {
        "userName": "dev@one.io", "name": {"formatted": "Dev One"},
        scim.ENTERPRISE_EXT: {"department": "Platform"}})
    emp = db.query("SELECT * FROM employees WHERE tenant_id=? AND email='dev@one.io'", (T1,))
    assert emp and emp[0]["team"] == "Platform" and emp[0]["active"] == 1
    scim.patch_user(T1, int(u["id"]), {
        "Operations": [{"op": "replace", "value": {"active": False}}]})
    emp = db.query("SELECT active FROM employees WHERE id=?", (u["id"],))
    assert emp[0]["active"] == 0
    listed = scim.list_users(T1, 'userName eq "dev@one.io"')
    assert listed["totalResults"] == 1


def test_directory_csv_and_mapping():
    n = scim.import_csv(T1, "Asha Rao,asha@one.io,Data\nname,email,team\n")
    assert n == 1
    ghost = db.upsert_employee(T1, "openai:user_xyz", "user_xyz")
    db.insert_event(T1, "k-map", ghost, "openai", "gpt-4o",
                    100, 0, 50, 0.001, "poller", "2026-07-12T09:00:00")
    target = db.query("SELECT id FROM employees WHERE email='asha@one.io'")[0]["id"]
    scim.map_to(T1, ghost, target)
    assert not db.query("SELECT 1 FROM employees WHERE id=?", (ghost,))
    moved = db.query("SELECT employee_id FROM usage_events WHERE event_key='k-map'")
    assert moved[0]["employee_id"] == target


def test_billing_trial_state():
    s = billing.state(T1)
    assert s["plan"] == "trial" and 0 < s["trial_days_left"] <= billing.TRIAL_DAYS
    assert s["monthly_price_usd"] >= billing.MINIMUM_USD
    db.execute("UPDATE billing SET trial_started=datetime('now', '-30 days') WHERE tenant_id=?", (T1,))
    assert billing.state(T1)["plan"] == "expired"


def test_stripe_webhook_signature():
    import hashlib as h, hmac as hm, time as t
    payload = b'{"type":"checkout.session.completed","data":{"object":{"client_reference_id":"1"}}}'
    ts = str(int(t.time()))
    sig = hm.new(b"whsec_test", f"{ts}.".encode() + payload, h.sha256).hexdigest()
    assert billing.verify_webhook(payload, f"t={ts},v1={sig}", "whsec_test")
    assert not billing.verify_webhook(payload, f"t={ts},v1=deadbeef", "whsec_test")


def test_connector_demo_sync_idempotent():
    import connectors
    db.upsert_seat(T1, "GitHub Copilot", E1, 19.0, "2026-07-10T00:00:00+00:00")
    before = len(db.query("SELECT * FROM seats WHERE tenant_id=?", (T1,)))
    connectors.sync_copilot(T1)
    connectors.sync_copilot(T1)
    after = len(db.query("SELECT * FROM seats WHERE tenant_id=?", (T1,)))
    assert before == after, "demo sync must not duplicate seats"


def test_telemetry_token_and_ingest_idempotent():
    tok = telemetry.issue_token(T1, E1)
    assert tok.startswith("ttw_")
    assert telemetry.resolve_token(tok) == (T1, E1)
    assert telemetry.resolve_token("ttw_bogus") is None
    assert telemetry.resolve_token("scim_x") is None  # wrong prefix
    events = [{"ts": "2026-07-12T10:00:00", "site": "chatgpt.com",
               "model": "gpt-4o", "input_tokens": 500, "output_tokens": 1500}]
    telemetry.ingest(T1, E1, events)
    telemetry.ingest(T1, E1, events)  # re-send same batch
    rows = db.query("""SELECT COUNT(*) n, SUM(output_tokens) o FROM usage_events
                       WHERE tenant_id=? AND source='extension'""", (T1,))
    assert rows[0]["n"] == 1, "duplicate telemetry events on re-send"
    assert rows[0]["o"] == 1500, "output tokens must be recorded"


def test_membership_rightsizing_verdicts():
    # dormant employee with a paid membership -> cancel
    dorm = db.upsert_employee(T1, "rs:dorm", "Dormant Dan", team="Sales")
    db.upsert_seat(T1, "ChatGPT Plus", dorm, 20.0, "2026-06-01T00:00:00+00:00")
    # light user -> some metered usage well under $20 -> move_to_metered
    light = db.upsert_employee(T1, "rs:light", "Light Lucy", team="Ops")
    db.upsert_seat(T1, "ChatGPT Plus", light, 20.0, "2026-07-12T00:00:00+00:00")
    telemetry.ingest(T1, light, [{"ts": "2026-07-12T09:00:00", "model": "gpt-4o-mini",
                                  "input_tokens": 2000, "output_tokens": 3000}])
    # an IDE seat must NOT appear here (extension can't measure IDE usage)
    ide = db.upsert_employee(T1, "rs:ide", "Ide Ian", team="Eng")
    db.upsert_seat(T1, "Claude Code", ide, 60.0, "2026-07-12T00:00:00+00:00")
    rows = insights.membership_rightsizing(T1)
    out = {r["employee"]: r for r in rows}
    assert out["Dormant Dan"]["verdict"] == "cancel"
    assert out["Dormant Dan"]["est_monthly_savings_usd"] == 20.0
    assert out["Light Lucy"]["verdict"] == "move_to_metered"
    assert out["Light Lucy"]["estimated_metered_usd"] < 20.0
    assert "Ide Ian" not in out, "IDE tools must be excluded from web rightsizing"


if __name__ == "__main__":
    # definition order matters (later tests build on earlier inserts)
    fns = [test_ingestion_idempotency, test_same_event_key_isolated_across_tenants,
           test_tenant_isolation_in_queries, test_member_scope_filters_to_own_employee,
           test_password_hash_and_verify, test_signup_login_and_role,
           test_estimator_ranges_bracket_base, test_estimator_empty,
           test_gateway_key_and_budget_enforcement,
           test_gateway_records_attributed_event_and_prompt,
           test_redaction, test_scim_create_and_deactivate,
           test_directory_csv_and_mapping, test_billing_trial_state,
           test_stripe_webhook_signature, test_connector_demo_sync_idempotent,
           test_telemetry_token_and_ingest_idempotent,
           test_membership_rightsizing_verdicts]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
