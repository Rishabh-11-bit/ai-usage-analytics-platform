# TokenTracker — AI usage analytics & chargeback for IT services firms

Tracks each employee's AI token usage across providers, turns AI spend into
**billable per-client line items**, finds idle seat licenses, and gives
concrete money-saving recommendations. Multi-tenant with role-based access.

## Quick start (demo mode)

```bash
pip install fastapi uvicorn
python seed_demo.py          # demo tenant: 30 days of data, projects, seats
uvicorn main:app --port 8000
# open http://localhost:8000
```

Demo logins (password `demo1234`):
- `admin@demo.co` — org admin: full dashboard, chargeback, seats, alerts, suggestions
- `neha@demo.co` — employee: sees only her own usage

Or create your own organization from the login screen.

## What the admin sees

- **KPI tiles** — spend, tokens, cache hit rate, identified monthly savings
- **Spend anomaly alerts** — z-score detection on daily spend, top contributor named
- **Daily cost by provider / cost by model** — hand-rolled SVG, light+dark
- **Client chargeback** — AI spend grouped by project/client, CSV export for invoicing
- **Seat utilization** — Copilot/Cursor/Claude Code seats, idle 30d+ flagged with recoverable $
- **Optimization suggestions** — caching, model routing, output constraints, unused seats
- **Daily digest** — Slack-ready summary (`SLACK_WEBHOOK_URL` to auto-post)
- **Prompt coach** — paste a prompt, get token-reduction findings + monthly cost of the bloat
- **Project estimator** — preset project types (S/M/L), AI-assisted vs. developer-only
  cost with an explicit uncertainty range and auditable assumptions

## Real data mode

Set org admin keys and run the pollers on a cron (e.g. hourly):

```bash
export OPENAI_ADMIN_KEY=sk-admin-...          # OpenAI org Usage/Costs API
export ANTHROPIC_ADMIN_KEY=sk-ant-admin-...   # Anthropic Usage & Cost API
export TOKEN_TRACKER_TENANT_ID=1              # tenant to ingest into
python pollers.py
```

Pollers are idempotent — re-fetching overlapping windows never duplicates
events (unique `(tenant_id, event_key)`). Admin keys live in env vars only;
they are never written to the database or logs (see [SECURITY.md](SECURITY.md)).

DB path: set `TOKEN_TRACKER_DB` (default `./token_tracker.db`).

## Files

| File | Purpose |
|---|---|
| `db.py` | SQLite multi-tenant schema + idempotent writes |
| `auth.py` | Signup/login, PBKDF2 hashing, bearer sessions, roles |
| `pricing.py` | Per-model pricing table + cost calculator |
| `pollers.py` | OpenAI / Anthropic admin-API pollers (tenant-scoped) |
| `insights.py` | Analytics, chargeback, seats, alerts, suggestions, digest |
| `coach.py` | Prompt coach: heuristic token-reduction analysis |
| `estimator.py` | Project cost estimator with presets + uncertainty ranges |
| `gateway.py` | Virtual keys, budgets, completion proxy, redaction, traffic coach |
| `connectors.py` | GitHub Copilot + Cursor seat sync (real APIs, demo fallback) |
| `scim.py` | SCIM 2.0 server, CSV import, unmapped-ID merging |
| `billing.py` | Trial, per-employee pricing, Stripe checkout + webhook |
| `schema_postgres.sql` / `migrate_to_postgres.py` | Postgres migration path |
| `seed_demo.py` | Demo tenant generator (incl. gateway traffic + keys) |
| `main.py` | FastAPI app (auth-protected JSON API + dashboard) |
| `index.html` | Single-file dashboard (zero deps, light/dark, login gate) |
| `SECURITY.md` | What we store, what we never store |
| `tests.py` | 16 tests: idempotency, isolation, auth, gateway, SCIM, billing |

## API (all endpoints require `Authorization: Bearer <token>`)

Auth: `POST /api/auth/signup` · `POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/me`

Analytics (members auto-filtered to their own usage):
`GET /api/summary` · `/api/daily` · `/api/employees` · `/api/models`

Admin only:
`GET /api/suggestions` · `/api/chargeback` · `/api/chargeback/export` (CSV) ·
`/api/seats` · `/api/alerts` · `/api/digest` · `POST /api/digest/send`

Tools: `GET /api/pricing` · `/api/project/presets` ·
`POST /api/prompt/analyze` · `POST /api/project/estimate`

Gateway: `POST /gw/v1/chat/completions` (Authorization: Bearer vk_…, optional
X-Project header for chargeback attribution). Admin: `GET/POST /api/keys`,
`POST /api/keys/{id}` (budget/revoke), `GET /api/prompts`, `GET /api/prompts/coach`.

Directory: `GET /api/directory/employees` · `/api/directory/unmapped` ·
`POST /api/directory/map` · `POST /api/directory/csv` · `POST /api/scim/token`;
SCIM 2.0 server at `/scim/v2/Users` for Okta/Entra provisioning.

Ops: `GET /api/connectors` · `POST /api/connectors/{copilot|cursor}/sync`
(real with `GITHUB_TOKEN`+`GITHUB_ORG` / `CURSOR_API_KEY`, demo otherwise) ·
`GET /api/billing` · `POST /api/billing/checkout` · `POST /api/billing/webhook`
(Stripe, set `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET`).

## Tests

```bash
python -m pytest tests.py -q     # or: python tests.py
```

Covers ingestion idempotency, cross-tenant isolation, member-role filtering,
password hashing, and estimator ranges.

## Production path

SQLite → Postgres + ClickHouse, cron → Celery/Redis, LiteLLM gateway for
per-employee virtual keys + prompt-level coaching (opt-in, redacted,
self-hostable), SCIM identity sync (Okta/Entra), Copilot/Cursor seat-usage
API connectors, Stripe billing.
