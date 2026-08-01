"""FastAPI app: auth + tenant-scoped JSON API + dashboard.

Run:  pip install fastapi uvicorn
      python seed_demo.py            # demo tenant + logins (see its docstring)
      uvicorn main:app --port 8000
Open: http://localhost:8000   (admin@demo.co / demo1234)
"""
import io
import json
import os
import urllib.request

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

import auth
import billing
import coach
import connectors
import db
import estimator
import gateway
import insights
import scim
import telemetry
from pricing import PRICING

app = FastAPI(title="TokenTracker")
db.init_db()

# The browser extension reports telemetry cross-origin (from chatgpt.com etc.).
# Only the telemetry endpoint needs it; a bearer token still gates ingestion.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"], max_age=86400)

HERE = os.path.dirname(os.path.abspath(__file__))


# ---- auth plumbing ----

def current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    user = auth.resolve(authorization[7:])
    if not user:
        raise HTTPException(401, "Session expired — log in again")
    return user


def admin_only(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    return user


def _emp_filter(user):
    """Members see only their own usage; unmapped members see nothing."""
    if user["role"] == "admin":
        return None
    return user["employee_id"] or -1


class SignupRequest(BaseModel):
    org_name: str
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def api_signup(req: SignupRequest):
    try:
        return auth.signup(req.org_name, req.name, req.email, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/login")
def api_login(req: LoginRequest):
    try:
        return auth.login(req.email, req.password)
    except ValueError as e:
        raise HTTPException(401, str(e))


@app.post("/api/auth/logout")
def api_logout(authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        auth.logout(authorization[7:])
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(user=Depends(current_user)):
    return user


# ---- pages ----

@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/security", response_class=PlainTextResponse)
def security_page():
    with open(os.path.join(HERE, "SECURITY.md"), encoding="utf-8") as f:
        return f.read()


# ---- analytics (tenant-scoped; members auto-filtered to self) ----

@app.get("/api/summary")
def api_summary(days: int = 30, user=Depends(current_user)):
    return insights.summary(user["tenant_id"], days, _emp_filter(user))


@app.get("/api/daily")
def api_daily(days: int = 30, user=Depends(current_user)):
    return insights.daily_costs(user["tenant_id"], days, _emp_filter(user))


@app.get("/api/employees")
def api_employees(days: int = 30, user=Depends(current_user)):
    return insights.by_employee(user["tenant_id"], days, _emp_filter(user))


@app.get("/api/models")
def api_models(days: int = 30, user=Depends(current_user)):
    return insights.by_model(user["tenant_id"], days, _emp_filter(user))


# ---- admin-only: company-wide money views ----

@app.get("/api/suggestions")
def api_suggestions(days: int = 30, user=Depends(admin_only)):
    return insights.suggestions(user["tenant_id"], days)


@app.get("/api/chargeback")
def api_chargeback(days: int = 30, user=Depends(admin_only)):
    return insights.chargeback(user["tenant_id"], days)


@app.get("/api/chargeback/export")
def api_chargeback_export(days: int = 30, user=Depends(admin_only)):
    rows = insights.chargeback(user["tenant_id"], days)
    buf = io.StringIO()
    buf.write("project,client,contributors,tokens,cost_usd\n")
    for r in rows:
        buf.write(f"\"{r['project']}\",\"{r['client']}\",{r['contributors']},"
                  f"{r['tokens']},{r['cost_usd']}\n")
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=chargeback.csv"})


@app.get("/api/seats")
def api_seats(user=Depends(admin_only)):
    return insights.seats_report(user["tenant_id"])


@app.get("/api/rightsizing")
def api_rightsizing(days: int = 30, user=Depends(admin_only)):
    return insights.membership_rightsizing(user["tenant_id"], days)


@app.get("/api/alerts")
def api_alerts(days: int = 30, user=Depends(admin_only)):
    return insights.alerts(user["tenant_id"], days)


@app.get("/api/digest", response_class=PlainTextResponse)
def api_digest(user=Depends(admin_only)):
    return insights.digest(user["tenant_id"])


@app.post("/api/digest/send")
def api_digest_send(user=Depends(admin_only)):
    """Post the digest to Slack (set SLACK_WEBHOOK_URL)."""
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        raise HTTPException(400, "SLACK_WEBHOOK_URL not configured")
    body = json.dumps({"text": insights.digest(user["tenant_id"])}).encode()
    req = urllib.request.Request(
        hook, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return {"ok": r.status < 300}


# ---- tools ----

@app.get("/api/pricing")
def api_pricing(user=Depends(current_user)):
    return [
        {"provider": p, "model": m, "input": i, "cached_input": c, "output": o}
        for (p, m), (i, c, o) in PRICING.items()
    ]


class PromptRequest(BaseModel):
    prompt: str
    provider: str = "anthropic"
    model: str = "claude-sonnet-5"
    runs_per_day: int = 50


@app.post("/api/prompt/analyze")
def api_prompt_analyze(req: PromptRequest, user=Depends(current_user)):
    return coach.analyze(req.prompt, req.provider, req.model, req.runs_per_day)


@app.get("/api/project/presets")
def api_project_presets(user=Depends(current_user)):
    return estimator.presets()


class EstimateRequest(BaseModel):
    simple: int = 0
    medium: int = 0
    complex: int = 0
    provider: str = "anthropic"
    model: str = "claude-sonnet-5"
    dev_hourly_rate: float = 40.0


@app.post("/api/project/estimate")
def api_project_estimate(req: EstimateRequest, user=Depends(current_user)):
    return estimator.estimate(
        {"simple": req.simple, "medium": req.medium, "complex": req.complex},
        req.provider, req.model, req.dev_hourly_rate,
    )


# ---- gateway: virtual keys & budgets (admin manages, anyone with a key calls) ----

class KeyCreate(BaseModel):
    employee_id: int
    monthly_budget_usd: float = 100.0


class KeyUpdate(BaseModel):
    monthly_budget_usd: float | None = None
    active: bool | None = None


@app.get("/api/keys")
def api_keys(user=Depends(admin_only)):
    return gateway.list_keys(user["tenant_id"])


@app.post("/api/keys")
def api_key_create(req: KeyCreate, user=Depends(admin_only)):
    raw = gateway.create_key(user["tenant_id"], req.employee_id, req.monthly_budget_usd)
    return {"key": raw, "note": "Shown once — only a hash is stored."}


@app.post("/api/keys/{key_id}")
def api_key_update(key_id: int, req: KeyUpdate, user=Depends(admin_only)):
    gateway.update_key(user["tenant_id"], key_id, req.monthly_budget_usd, req.active)
    return {"ok": True}


@app.post("/gw/v1/chat/completions")
async def gw_completions(request: Request):
    authz = request.headers.get("authorization", "")
    if not authz.startswith("Bearer "):
        raise HTTPException(401, "Virtual key required: Authorization: Bearer vk_…")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    try:
        return gateway.complete(authz[7:], body, request.headers.get("x-project"))
    except gateway.GatewayError as e:
        raise HTTPException(e.status, e.detail)


# ---- prompt traffic (admin) ----

@app.get("/api/prompts")
def api_prompts(limit: int = 25, user=Depends(admin_only)):
    return gateway.recent_prompts(user["tenant_id"], min(limit, 100))


@app.get("/api/prompts/coach")
def api_prompts_coach(days: int = 7, user=Depends(admin_only)):
    return gateway.traffic_coach(user["tenant_id"], days)


class SettingUpdate(BaseModel):
    key: str
    value: str


@app.post("/api/settings")
def api_settings(req: SettingUpdate, user=Depends(admin_only)):
    if req.key not in {"store_prompts"}:
        raise HTTPException(400, "Unknown setting")
    db.set_setting(user["tenant_id"], req.key, req.value)
    return {"ok": True}


@app.get("/api/settings")
def api_settings_get(user=Depends(admin_only)):
    return {"store_prompts": db.get_setting(user["tenant_id"], "store_prompts", "0")}


# ---- connectors & directory (admin) ----

@app.get("/api/connectors")
def api_connectors(user=Depends(admin_only)):
    return connectors.status(user["tenant_id"])


@app.post("/api/connectors/{name}/sync")
def api_connector_sync(name: str, user=Depends(admin_only)):
    fn = connectors.SYNCS.get(name)
    if not fn:
        raise HTTPException(404, "No such connector (syncable: copilot, cursor)")
    try:
        return fn(user["tenant_id"])
    except Exception as e:
        raise HTTPException(502, f"Sync failed: {e}")


@app.get("/api/directory/employees")
def api_directory_employees(user=Depends(admin_only)):
    return db.query(
        """SELECT id, name, email, team, active FROM employees
           WHERE tenant_id=? ORDER BY name""", (user["tenant_id"],))


@app.get("/api/directory/unmapped")
def api_directory_unmapped(user=Depends(admin_only)):
    return scim.unmapped(user["tenant_id"])


class MapRequest(BaseModel):
    unmapped_id: int
    target_employee_id: int


@app.post("/api/directory/map")
def api_directory_map(req: MapRequest, user=Depends(admin_only)):
    try:
        scim.map_to(user["tenant_id"], req.unmapped_id, req.target_employee_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/directory/csv")
async def api_directory_csv(request: Request, user=Depends(admin_only)):
    text = (await request.body()).decode("utf-8", "replace")
    return {"imported": scim.import_csv(user["tenant_id"], text)}


@app.post("/api/scim/token")
def api_scim_token(user=Depends(admin_only)):
    return {"token": scim.issue_token(user["tenant_id"]),
            "base_url": "/scim/v2",
            "note": "Shown once — configure it in Okta/Entra provisioning."}


# ---- extension telemetry (counters only) ----

class WebTokenRequest(BaseModel):
    employee_id: int


@app.post("/api/telemetry/token")
def api_telemetry_token(req: WebTokenRequest, request: Request, user=Depends(admin_only)):
    raw = telemetry.issue_token(user["tenant_id"], req.employee_id)
    return {"token": raw, "tenant_url": str(request.base_url).rstrip("/"),
            "note": "Shown once — paste into the employee's extension options."}


@app.post("/api/telemetry")
async def api_telemetry(request: Request):
    authz = request.headers.get("authorization", "")
    if not authz.startswith("Bearer "):
        raise HTTPException(401, "Reporting token required")
    resolved = telemetry.resolve_token(authz[7:])
    if not resolved:
        raise HTTPException(401, "Invalid reporting token")
    tenant_id, employee_id = resolved
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    n = telemetry.ingest(tenant_id, employee_id, body.get("events", []))
    return {"ingested": n}


# ---- SCIM 2.0 server (IdP-facing; bearer = SCIM token, not a user session) ----

def scim_tenant(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "SCIM bearer token required")
    tenant_id = scim.resolve_token(authorization[7:])
    if not tenant_id:
        raise HTTPException(401, "Invalid SCIM token")
    return tenant_id


@app.get("/scim/v2/Users")
def scim_list(filter: str = None, tenant_id=Depends(scim_tenant)):
    return scim.list_users(tenant_id, filter)


@app.post("/scim/v2/Users", status_code=201)
async def scim_create(request: Request, tenant_id=Depends(scim_tenant)):
    return scim.create_user(tenant_id, await request.json())


@app.get("/scim/v2/Users/{emp_id}")
def scim_get(emp_id: int, tenant_id=Depends(scim_tenant)):
    user = scim.get_user(tenant_id, emp_id)
    if not user:
        raise HTTPException(404, "User not found")
    return user


@app.patch("/scim/v2/Users/{emp_id}")
async def scim_patch(emp_id: int, request: Request, tenant_id=Depends(scim_tenant)):
    user = scim.patch_user(tenant_id, emp_id, await request.json())
    if not user:
        raise HTTPException(404, "User not found")
    return user


# ---- billing ----

@app.get("/api/billing")
def api_billing(user=Depends(admin_only)):
    return billing.state(user["tenant_id"])


@app.post("/api/billing/checkout")
def api_billing_checkout(request: Request, user=Depends(admin_only)):
    base = str(request.base_url).rstrip("/")
    try:
        return {"url": billing.checkout_url(user["tenant_id"], base)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/billing/webhook")
async def api_billing_webhook(request: Request):
    payload = await request.body()
    try:
        return billing.handle_webhook(payload, request.headers.get("stripe-signature", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
