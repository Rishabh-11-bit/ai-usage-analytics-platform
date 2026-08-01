"""Pre-project cost estimator: AI-assisted vs. developer-only delivery.

Task-based model. A "task" is one unit of dev work (a feature / ticket).
Per-complexity assumptions (tokens per iteration, iterations to done,
human hours) are deliberately conservative and surfaced in the response
so the buyer can sanity-check them. All numbers are estimates.
"""
from pricing import cost_usd

COMPLEXITY = {
    # tokens per iteration          iterations   pure-dev hours per task
    "simple":  {"in": 15_000,  "out": 4_000,  "iters": 3,  "dev_hours": 4},
    "medium":  {"in": 40_000,  "out": 10_000, "iters": 6,  "dev_hours": 16},
    "complex": {"in": 100_000, "out": 25_000, "iters": 12, "dev_hours": 40},
}
CACHED_FRACTION = 0.5    # repeated context (files, system prompt) hits the cache
REVIEW_FRACTION = 0.30   # human review/integration time retained with AI

# Uncertainty band on the AI-assisted estimate. Token spend and review time
# vary with codebase familiarity and AI fluency; until an org calibrates
# these against its own completed projects, quote a range, not a number.
RANGE_LOW = 0.7
RANGE_HIGH = 1.6

# Project-type presets: task mix at size M. S = half, L = double (rounded).
# These are starting points the user can fine-tune, not verdicts.
PRESETS = {
    "crud_web":       {"label": "CRUD web app",                "mix": (12, 8, 2)},
    "mobile_app":     {"label": "Mobile app (iOS + Android)",  "mix": (10, 10, 4)},
    "api_integration": {"label": "API / 3rd-party integration", "mix": (6, 4, 1)},
    "data_migration": {"label": "Data migration / ETL",        "mix": (8, 6, 2)},
    "ecommerce":      {"label": "E-commerce site",             "mix": (14, 10, 4)},
    "dashboard":      {"label": "Internal dashboard / reports", "mix": (8, 5, 1)},
    "chatbot":        {"label": "Chatbot / AI feature",        "mix": (6, 6, 3)},
    "website":        {"label": "Marketing website / redesign", "mix": (10, 4, 0)},
}
SIZE_FACTOR = {"S": 0.5, "M": 1.0, "L": 2.0}


def presets():
    """All presets with the task mix precomputed per size."""
    out = []
    for key, p in PRESETS.items():
        sizes = {}
        for size, f in SIZE_FACTOR.items():
            s, m, c = p["mix"]
            sizes[size] = {
                "simple": round(s * f), "medium": round(m * f),
                "complex": round(c * f),
            }
        out.append({"key": key, "label": p["label"], "sizes": sizes})
    return out


def estimate(tasks, provider="anthropic", model="claude-sonnet-5",
             dev_hourly_rate=40.0):
    """tasks: {"simple": n, "medium": n, "complex": n}"""
    rows, ai_cost, dev_hours = [], 0.0, 0.0
    total_in = total_out = 0

    for level, spec in COMPLEXITY.items():
        n = int(tasks.get(level, 0) or 0)
        if n <= 0:
            continue
        tin = spec["in"] * spec["iters"] * n
        tout = spec["out"] * spec["iters"] * n
        c = cost_usd(provider, model, tin, int(tin * CACHED_FRACTION), tout)
        h = spec["dev_hours"] * n
        rows.append({
            "complexity": level, "tasks": n,
            "input_tokens": tin, "output_tokens": tout,
            "ai_cost_usd": round(c, 2), "dev_hours": h,
        })
        ai_cost += c
        dev_hours += h
        total_in += tin
        total_out += tout

    dev_only = dev_hours * dev_hourly_rate
    review_hours = dev_hours * REVIEW_FRACTION
    ai_assisted = ai_cost + review_hours * dev_hourly_rate
    ai_assisted_low = ai_assisted * RANGE_LOW
    ai_assisted_high = ai_assisted * RANGE_HIGH
    savings = dev_only - ai_assisted
    savings_low = dev_only - ai_assisted_high   # worst case
    savings_high = dev_only - ai_assisted_low   # best case
    pct = (savings / dev_only * 100) if dev_only else 0

    if not rows:
        rec = "Add at least one task to estimate."
    elif pct >= 40:
        rec = (f"AI-assisted delivery is strongly favored — ~{pct:.0f}% cheaper. "
               f"Token spend (${ai_cost:,.0f}) is small next to the "
               f"{dev_hours - review_hours:,.0f} developer hours it replaces.")
    elif pct >= 10:
        rec = (f"AI-assisted delivery is moderately favored (~{pct:.0f}% cheaper). "
               "Worth it if the team is already fluent with AI tooling.")
    else:
        rec = ("Marginal or negative savings — at this rate/mix, experienced "
               "developers without AI spend may be the simpler choice.")

    return {
        "model": f"{provider}/{model}",
        "dev_hourly_rate": dev_hourly_rate,
        "breakdown": rows,
        "totals": {
            "input_tokens": total_in, "output_tokens": total_out,
            "ai_token_cost_usd": round(ai_cost, 2),
            "dev_only_cost_usd": round(dev_only, 2),
            "dev_only_hours": dev_hours,
            "ai_assisted_cost_usd": round(ai_assisted, 2),
            "ai_assisted_low_usd": round(ai_assisted_low, 2),
            "ai_assisted_high_usd": round(ai_assisted_high, 2),
            "ai_assisted_review_hours": round(review_hours, 1),
            "savings_usd": round(savings, 2),
            "savings_low_usd": round(savings_low, 2),
            "savings_high_usd": round(savings_high, 2),
            "savings_pct": round(pct, 1),
        },
        "recommendation": rec,
        "assumptions": {
            "cached_input_fraction": CACHED_FRACTION,
            "human_review_fraction": REVIEW_FRACTION,
            "uncertainty_range": [RANGE_LOW, RANGE_HIGH],
            "per_task": COMPLEXITY,
            "note": ("Estimates are uncalibrated defaults. Tune per_task assumptions "
                     "against your own completed projects before quoting clients."),
        },
    }
