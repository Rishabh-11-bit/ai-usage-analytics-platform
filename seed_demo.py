"""Seed a full demo tenant: 30 days of usage for 12 employees, 5 client
projects (chargeback), seat licenses (some idle), and login accounts.

Demo logins (password: demo1234):
  admin@demo.co  — org admin, sees everything
  neha@demo.co   — member, sees only her own usage
"""
import random
from datetime import datetime, timezone, timedelta

import auth
import coach
import db
import gateway
from pricing import cost_usd

random.seed(42)

EMPLOYEES = [
    ("Rishabh Gaikwad", "Engineering"), ("Priya Sharma", "Engineering"),
    ("Arjun Mehta", "Engineering"), ("Sneha Patil", "Engineering"),
    ("Karan Desai", "Data"), ("Ananya Iyer", "Data"),
    ("Rohit Kulkarni", "Support"), ("Divya Nair", "Support"),
    ("Amit Joshi", "Product"), ("Neha Reddy", "Product"),
    ("Vikram Singh", "Marketing"), ("Pooja Verma", "Marketing"),
]

MODELS = [
    ("openai", "gpt-4o", 0.30), ("openai", "gpt-4o-mini", 0.25),
    ("anthropic", "claude-sonnet-5", 0.25), ("anthropic", "claude-opus-4-8", 0.10),
    ("anthropic", "claude-haiku-4-5", 0.10),
]

PROJECTS = [
    ("Acme portal rebuild", "Acme Corp"),
    ("Globex data pipeline", "Globex"),
    ("Initech mobile app", "Initech"),
    ("Umbrella support bot", "Umbrella Ltd"),
    ("Internal R&D", None),
]

SEAT_TOOLS = [("GitHub Copilot", 19.0), ("Cursor", 20.0), ("Claude Code", 60.0)]


def main():
    db.init_db()
    if db.query("SELECT 1 FROM tenants WHERE name='Demo IT Services'"):
        print("Demo tenant already exists — delete the DB file to reseed.")
        return

    tenant_id = db.create_tenant("Demo IT Services")

    emp_ids = []
    for i, (name, team) in enumerate(EMPLOYEES):
        email = name.lower().replace(" ", ".") + "@demo.co"
        emp_ids.append((db.upsert_employee(tenant_id, f"demo:{i}", name, email, team), i))

    proj_ids = [db.upsert_project(tenant_id, n, c) for n, c in PROJECTS]

    # Login accounts
    auth.create_user(tenant_id, "Demo Admin", "admin@demo.co", "demo1234", role="admin")
    neha_emp = next(eid for eid, i in emp_ids if EMPLOYEES[i][0] == "Neha Reddy")
    auth.create_user(tenant_id, "Neha Reddy", "neha@demo.co", "demo1234",
                     role="member", employee_id=neha_emp)

    now = datetime.now(timezone.utc)
    n = 0
    for day in range(30):
        d = now - timedelta(days=day)
        if d.weekday() >= 5 and random.random() < 0.7:
            continue  # light weekends
        for emp_id, i in emp_ids:
            heaviness = 0.4 + (i % 5) * 0.4  # some employees use far more
            # each employee mostly works on 1-2 projects
            proj = proj_ids[(i + (day // 10)) % len(proj_ids)]
            for provider, model, weight in MODELS:
                if random.random() > weight * heaviness:
                    continue
                inp = int(random.gauss(80_000, 30_000) * heaviness)
                inp = max(inp, 2_000)
                cached = int(inp * random.choice([0.02, 0.05, 0.5, 0.7]))
                out = int(inp * random.uniform(0.3, 2.6))
                # one engineered anomaly ~6 days ago
                if day == 6 and i == 4:
                    inp *= 6
                    out *= 6
                ts = d.replace(hour=random.randint(8, 19)).isoformat()
                db.insert_event(
                    tenant_id, f"demo|{i}|{provider}|{model}|{day}", emp_id,
                    provider, model, inp, cached, out,
                    cost_usd(provider, model, inp, cached, out), "poller", ts,
                    project_id=proj)
                n += 1

    # Seat licenses: everyone has Copilot; eng+data also Cursor; 4 eng Claude Code.
    # A handful are idle (>45 days) to light up the unused-seats suggestion.
    seats = 0
    for emp_id, i in emp_ids:
        team = EMPLOYEES[i][1]
        tools = [SEAT_TOOLS[0]]
        if team in ("Engineering", "Data"):
            tools.append(SEAT_TOOLS[1])
        if team == "Engineering":
            tools.append(SEAT_TOOLS[2])
        for tool, cost in tools:
            idle = random.random() < 0.22
            last = (now - timedelta(days=random.randint(46, 90))) if idle \
                else (now - timedelta(days=random.randint(0, 12)))
            db.add_seat(tenant_id, tool, emp_id, cost, last.isoformat())
            seats += 1

    # ChatGPT Plus / Claude Pro memberships (the SMB "no enterprise" case),
    # plus extension-measured web usage so membership rightsizing has data.
    MEMBERSHIPS = [("ChatGPT Plus", 20.0), ("Claude Pro", 20.0)]
    WEB_MODELS = [("openai", "gpt-4o"), ("anthropic", "claude-sonnet-5"),
                  ("openai", "gpt-4o-mini")]
    for emp_id, i in emp_ids:
        tool, cost = MEMBERSHIPS[i % len(MEMBERSHIPS)]
        db.add_seat(tenant_id, tool, emp_id, cost,
                    (now - timedelta(days=random.randint(0, 20))).isoformat())
        seats += 1
        # usage profile: some heavy, some light, some dormant (i % 3)
        profile = i % 3   # 0 heavy, 1 light, 2 dormant
        if profile == 2:
            continue       # dormant → no web events → "cancel" verdict
        days_active = 18 if profile == 0 else 6
        for d in range(days_active):
            provider, model = random.choice(WEB_MODELS)
            scale = 6.0 if profile == 0 else 0.5
            inp = int(random.gauss(1200, 400) * scale)
            out = int(inp * random.uniform(1.5, 4.0))
            inp, out = max(inp, 100), max(out, 100)
            ts = (now - timedelta(days=d, hours=random.randint(0, 10))).isoformat()
            db.insert_event(
                tenant_id, f"extdemo|{emp_id}|{d}|{random.randint(0, 10**9)}",
                emp_id, provider, model, inp, 0, out,
                cost_usd(provider, model, inp, 0, out), "extension", ts)

    # ---- gateway: virtual keys + a week of bloated prompt traffic ----
    db.set_setting(tenant_id, "store_prompts", "1")
    db.execute("INSERT INTO billing (tenant_id, plan, trial_started) VALUES (?,?,?)",
               (tenant_id, "trial",
                (now - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")))

    BLOATED_PROMPTS = [
        ("You are a highly intelligent, world-class expert senior software engineer "
         "with decades of experience in every framework. Please could you kindly "
         "review the following code diff. Please make sure to check for bugs. "
         "It is VERY IMPORTANT that you actually check EVERYTHING carefully!! "
         "In order to do this properly, please basically go through each line. "
         "Thank you so much. Diff: {}"),
        ("Please summarize the following support ticket. Please be thorough and "
         "please make sure to include all the details. I would like you to "
         "essentially capture everything due to the fact that we report to the "
         "client weekly. Please and thank you. Ticket: {}"),
        ("You are an expert data analyst. In order to help me, please analyze "
         "this CSV excerpt and please tell me basically everything interesting "
         "about it. It is CRITICAL that you are very very thorough!! Data: {}"),
        ("Extract the action items from this meeting transcript. Return them as "
         "a JSON list with owner and due date, max 200 words. Transcript: {}"),
    ]
    GW_MODELS = ["claude-sonnet-5", "claude-haiku-4-5", "gpt-4o"]
    key_emps = [e for e, i in emp_ids if EMPLOYEES[i][1] in ("Engineering", "Data")][:4]
    keys = 0
    for idx, emp in enumerate(key_emps):
        budget = 5.0 if idx == 0 else 100.0  # first key runs hot for the demo
        raw = gateway.create_key(tenant_id, emp, budget)
        krow = db.query("SELECT id FROM virtual_keys WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
                        (tenant_id,))[0]
        keys += 1
        for d in range(7):
            for _ in range(random.randint(1, 3)):
                tmpl = random.choice(BLOATED_PROMPTS)
                filler = " ".join(random.choices(
                    ["lorem", "ipsum", "data", "value", "line", "item"], k=random.randint(80, 300)))
                prompt = tmpl.format(filler)
                model = random.choice(GW_MODELS)
                provider = "anthropic" if model.startswith("claude") else "openai"
                inp = coach.estimate_tokens(prompt)
                out = random.randint(150, 900)
                c = cost_usd(provider, model, inp, 0, out)
                ts = (now - timedelta(days=d, hours=random.randint(0, 10))).isoformat()
                ekey = f"gwdemo|{emp}|{d}|{random.randint(0, 10**9)}"
                db.insert_event(tenant_id, ekey, emp, provider, model,
                                inp, 0, out, c, "gateway", ts,
                                project_id=proj_ids[emp % len(proj_ids)])
                db.execute("UPDATE usage_events SET virtual_key_id=? WHERE tenant_id=? AND event_key=?",
                           (krow["id"], tenant_id, ekey))
                db.execute(
                    """INSERT INTO prompt_logs (tenant_id, employee_id, virtual_key_id,
                       project_id, model, prompt_redacted, input_tokens, output_tokens,
                       cost_usd, ts) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (tenant_id, emp, krow["id"], proj_ids[emp % len(proj_ids)],
                     model, gateway.redact(prompt), inp, out, c, ts))

    print(f"Seeded tenant 'Demo IT Services': {n} usage events, "
          f"{len(EMPLOYEES)} employees, {len(PROJECTS)} projects, {seats} seats, "
          f"{keys} virtual keys + gateway traffic.")
    print("Logins (password demo1234): admin@demo.co (admin), neha@demo.co (member)")


if __name__ == "__main__":
    main()
