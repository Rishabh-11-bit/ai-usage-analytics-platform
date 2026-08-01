"""Insights engine: tenant-scoped analytics, chargeback, seats, alerts,
optimization suggestions, and the daily digest.

Every function takes tenant_id. Pass employee_id to restrict to one
employee (member-role users see only their own usage).
"""
import statistics
from datetime import datetime, timedelta, timezone

import db
from pricing import PRICING, CHEAPER_ALTERNATIVE, cost_usd


def _scope(tenant_id, employee_id=None, alias="u"):
    sql = f"{alias}.tenant_id = ?"
    params = [tenant_id]
    if employee_id:
        sql += f" AND {alias}.employee_id = ?"
        params.append(employee_id)
    return sql, params


def summary(tenant_id, days=30, employee_id=None):
    scope, params = _scope(tenant_id, employee_id)
    return db.query(f"""
        SELECT COUNT(DISTINCT u.employee_id) AS active_employees,
               COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
               COALESCE(SUM(u.cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
               ROUND(COALESCE(SUM(u.cost_usd), 0), 2) AS cost_usd
        FROM usage_events u
        WHERE {scope} AND u.ts >= datetime('now', '-{int(days)} days')""", params)[0]


def daily_costs(tenant_id, days=30, employee_id=None):
    scope, params = _scope(tenant_id, employee_id)
    return db.query(f"""
        SELECT date(u.ts) AS day, u.provider, ROUND(SUM(u.cost_usd), 2) AS cost_usd,
               SUM(u.input_tokens + u.output_tokens) AS tokens
        FROM usage_events u
        WHERE {scope} AND u.ts >= datetime('now', '-{int(days)} days')
        GROUP BY day, u.provider ORDER BY day""", params)


def by_employee(tenant_id, days=30, employee_id=None):
    scope, params = _scope(tenant_id, employee_id)
    return db.query(f"""
        SELECT e.name, e.team,
               SUM(u.input_tokens) AS input_tokens,
               SUM(u.output_tokens) AS output_tokens,
               ROUND(SUM(u.cost_usd), 2) AS cost_usd
        FROM usage_events u JOIN employees e ON e.id = u.employee_id
        WHERE {scope} AND u.ts >= datetime('now', '-{int(days)} days')
        GROUP BY e.id ORDER BY cost_usd DESC""", params)


def by_model(tenant_id, days=30, employee_id=None):
    scope, params = _scope(tenant_id, employee_id)
    return db.query(f"""
        SELECT u.provider, u.model,
               SUM(u.input_tokens) AS input_tokens,
               SUM(u.cached_tokens) AS cached_tokens,
               SUM(u.output_tokens) AS output_tokens,
               ROUND(SUM(u.cost_usd), 2) AS cost_usd
        FROM usage_events u
        WHERE {scope} AND u.ts >= datetime('now', '-{int(days)} days')
        GROUP BY u.provider, u.model ORDER BY cost_usd DESC""", params)


def chargeback(tenant_id, days=30):
    """Per-project cost report — AI spend as billable line items per client."""
    return db.query(f"""
        SELECT p.name AS project, COALESCE(p.client, '—') AS client,
               COUNT(DISTINCT u.employee_id) AS contributors,
               SUM(u.input_tokens + u.output_tokens) AS tokens,
               ROUND(SUM(u.cost_usd), 2) AS cost_usd
        FROM usage_events u JOIN projects p ON p.id = u.project_id
        WHERE u.tenant_id = ? AND u.ts >= datetime('now', '-{int(days)} days')
        GROUP BY p.id ORDER BY cost_usd DESC""", [tenant_id])


def seats_report(tenant_id, inactive_days=30):
    """Per-seat utilization for flat-rate AI tools (Copilot, Cursor, …)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=inactive_days)).isoformat()
    rows = db.query("""
        SELECT s.id, s.tool, s.monthly_cost_usd, s.last_active,
               e.name AS employee, e.team
        FROM seats s LEFT JOIN employees e ON e.id = s.employee_id
        WHERE s.tenant_id = ? ORDER BY s.tool, s.last_active""", [tenant_id])
    for r in rows:
        r["inactive"] = (r["last_active"] is None) or (r["last_active"] < cutoff)
    return rows


def membership_rightsizing(tenant_id, days=30):
    """For each flat-rate seat, compare its monthly cost against the seat
    holder's ESTIMATED metered (extension-measured) usage over the window,
    and recommend keep / move-to-metered / cancel.

    Estimates only — the recommendation is deliberately conservative:
    cancel is suggested on zero measured activity, move only when metered
    cost is well under the flat fee.

    Scope: ONLY browser-based memberships (ChatGPT Plus, Claude Pro, …),
    because those are what the extension can actually measure. IDE tools
    (Copilot, Cursor, Claude Code) are excluded — the extension can't see
    IDE usage, so judging them on web activity would produce false
    'cancel' verdicts. Those stay in the seat-utilization panel instead.
    """
    seats = db.query("""
        SELECT s.id, s.tool, s.monthly_cost_usd, s.employee_id, s.last_active,
               e.name AS employee, e.team
        FROM seats s LEFT JOIN employees e ON e.id = s.employee_id
        WHERE s.tenant_id = ?
          AND (lower(s.tool) LIKE '%plus%' OR lower(s.tool) LIKE '%pro%'
               OR lower(s.tool) LIKE '%chatgpt%' OR lower(s.tool) LIKE '%membership%'
               OR lower(s.tool) LIKE '%gemini%' OR lower(s.tool) LIKE '%perplexity%')""",
        [tenant_id])
    out = []
    for s in seats:
        # measured metered-equivalent cost for this employee, extension source
        rows = db.query(f"""
            SELECT COALESCE(SUM(cost_usd), 0) AS metered,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
            FROM usage_events
            WHERE tenant_id = ? AND employee_id = ? AND source = 'extension'
              AND ts >= datetime('now', '-{int(days)} days')""",
            [tenant_id, s["employee_id"]])
        metered = round(rows[0]["metered"], 2)
        tokens = rows[0]["tokens"]
        flat = s["monthly_cost_usd"]

        if tokens == 0:
            verdict, save, why = "cancel", flat, (
                "No measured web usage this period — likely an unused membership.")
        elif metered < flat * 0.4:
            verdict, save, why = "move_to_metered", round(flat - metered, 2), (
                f"Estimated metered cost ~${metered} vs ${flat} flat fee. "
                "Trial this user on the gateway with a small budget.")
        else:
            verdict, save, why = "keep", 0.0, (
                f"Estimated metered cost ~${metered} — the flat ${flat} membership "
                "is the right deal for this usage level.")
        out.append({
            "seat_id": s["id"], "tool": s["tool"], "employee": s["employee"],
            "team": s["team"], "monthly_cost_usd": flat,
            "estimated_metered_usd": metered, "tokens": tokens,
            "verdict": verdict, "est_monthly_savings_usd": round(save, 2),
            "detail": why, "basis": "estimated",
        })
    return sorted(out, key=lambda x: -x["est_monthly_savings_usd"])


def alerts(tenant_id, days=30):
    """Z-score anomaly detection on daily tenant spend (z >= 2)."""
    daily = db.query(f"""
        SELECT date(ts) AS day, ROUND(SUM(cost_usd), 2) AS cost_usd
        FROM usage_events
        WHERE tenant_id = ? AND ts >= datetime('now', '-{int(days)} days')
        GROUP BY day ORDER BY day""", [tenant_id])
    if len(daily) < 7:
        return []
    values = [d["cost_usd"] for d in daily]
    mean = statistics.mean(values)
    std = statistics.pstdev(values) or 1e-9
    out = []
    for d in daily:
        z = (d["cost_usd"] - mean) / std
        if z >= 2:
            top = db.query("""
                SELECT e.name, ROUND(SUM(u.cost_usd), 2) AS cost_usd
                FROM usage_events u JOIN employees e ON e.id = u.employee_id
                WHERE u.tenant_id = ? AND date(u.ts) = ?
                GROUP BY e.id ORDER BY cost_usd DESC LIMIT 1""",
                [tenant_id, d["day"]])
            out.append({
                "day": d["day"], "cost_usd": d["cost_usd"],
                "z_score": round(z, 1), "daily_mean": round(mean, 2),
                "top_contributor": top[0]["name"] if top else None,
                "top_contributor_cost": top[0]["cost_usd"] if top else None,
            })
    return sorted(out, key=lambda a: a["day"], reverse=True)


def suggestions(tenant_id, days=30):
    """The moat: concrete money-saving recommendations."""
    out = []
    for row in by_model(tenant_id, days):
        provider, model = row["provider"], row["model"]
        inp, cached, outp = row["input_tokens"], row["cached_tokens"], row["output_tokens"]

        # 1. Prompt caching audit: low cached ratio on a cacheable model
        if inp > 500_000 and cached / max(inp, 1) < 0.2:
            price = PRICING.get((provider, model))
            if price:
                savings = (inp * 0.6) * (price[0] - price[1]) / 1_000_000 * (30 / days)
                out.append({
                    "type": "enable_prompt_caching",
                    "target": f"{provider}/{model}",
                    "detail": (f"Only {cached / max(inp,1):.0%} of input tokens are cached. "
                               "Repeated system prompts / context should use prompt caching."),
                    "est_monthly_savings_usd": round(savings, 2),
                })

        # 2. Model routing: heavy spend on frontier model
        alt = CHEAPER_ALTERNATIVE.get(model)
        if alt and row["cost_usd"] > 50:
            alt_cost = cost_usd(provider, alt, inp, cached, outp)
            savings = (row["cost_usd"] - alt_cost) * 0.5 * (30 / days)
            if savings > 10:
                out.append({
                    "type": "model_routing",
                    "target": f"{provider}/{model}",
                    "detail": (f"${row['cost_usd']} spent on {model} in {days}d. Route simple "
                               f"tasks (summaries, extraction, classification) to {alt}."),
                    "est_monthly_savings_usd": round(savings, 2),
                })

        # 3. Verbose outputs: output/input ratio unusually high
        if outp > 2 * max(inp, 1) and row["cost_usd"] > 20:
            out.append({
                "type": "constrain_outputs",
                "target": f"{provider}/{model}",
                "detail": ("Output tokens are >2x input. Add max_tokens limits and "
                           "'be concise' / structured-output instructions to prompts."),
                "est_monthly_savings_usd": round(row["cost_usd"] * 0.25 * (30 / days), 2),
            })

    # 4. Unused seats on flat-rate tools
    idle = [s for s in seats_report(tenant_id) if s["inactive"]]
    if idle:
        waste = sum(s["monthly_cost_usd"] for s in idle)
        by_tool = {}
        for s in idle:
            by_tool[s["tool"]] = by_tool.get(s["tool"], 0) + 1
        detail = ", ".join(f"{n}x {t}" for t, n in sorted(by_tool.items()))
        out.append({
            "type": "unused_seats",
            "target": "seat licenses",
            "detail": (f"{len(idle)} seats inactive for 30+ days ({detail}). "
                       "Reassign or cancel at next renewal."),
            "est_monthly_savings_usd": round(waste, 2),
        })

    return sorted(out, key=lambda s: -s["est_monthly_savings_usd"])


def digest(tenant_id):
    """Yesterday-focused daily digest (Slack/email text)."""
    y = db.query("""
        SELECT ROUND(SUM(cost_usd), 2) AS cost
        FROM usage_events
        WHERE tenant_id = ? AND date(ts) = date('now', '-1 day')""", [tenant_id])
    yesterday = (y[0]["cost"] or 0) if y else 0
    top = db.query("""
        SELECT e.name, ROUND(SUM(u.cost_usd), 2) AS cost_usd
        FROM usage_events u JOIN employees e ON e.id = u.employee_id
        WHERE u.tenant_id = ? AND date(u.ts) = date('now', '-1 day')
        GROUP BY e.id ORDER BY cost_usd DESC LIMIT 3""", [tenant_id])
    anomalies = alerts(tenant_id, 7)
    suggs = suggestions(tenant_id)[:3]

    lines = [f"*TokenTracker daily digest*",
             f"Yesterday's AI spend: *${yesterday:,.2f}*"]
    if top:
        lines.append("Top spenders: " + ", ".join(
            f"{t['name']} (${t['cost_usd']})" for t in top))
    if anomalies:
        a = anomalies[0]
        lines.append(f":warning: Anomaly {a['day']}: ${a['cost_usd']} "
                     f"({a['z_score']}x above normal)")
    for s in suggs:
        lines.append(f":bulb: {s['detail']} → save ~${s['est_monthly_savings_usd']}/mo")
    return "\n".join(lines)
