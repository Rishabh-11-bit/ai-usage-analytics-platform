# TokenTracker — Security posture

The first question every buyer asks is "you want my org admin API keys?"
This page answers exactly what we store, where, and what we never do.

## What we store

| Data | Stored? | Where | Notes |
|---|---|---|---|
| Usage metadata (tokens, model, cost, timestamps) | Yes | Your tenant's rows, tenant-isolated | This is the product |
| Employee names/emails/teams | Yes | Tenant-isolated | Only what you upload or sync |
| Provider admin API keys | **No** (this build) | Environment variables on the polling host only | Never written to the database, never logged, never returned by any API |
| Prompt or completion content | **No** | — | The poller path sees only aggregate usage numbers. Prompt content never leaves your infrastructure |
| Passwords | Hashed only | PBKDF2-HMAC-SHA256, 200k iterations, per-user salt | Plaintext never stored |
| Sessions | Server-side opaque tokens | 7-day expiry, revocable | No JWTs — nothing decodable client-side |

## Tenant isolation

Every table carries `tenant_id`; every query filters on it. Employee-role
users are additionally filtered to their own `employee_id` server-side —
role checks happen in the API layer, never in the frontend.

## The prompt coach and estimator

Prompts pasted into the coach are analyzed **in-memory and discarded** —
they are not persisted, logged, or used for anything else. Project
estimates run entirely on the parameters you enter.

## Deployment options

- **Self-hosted** (this repository): everything above runs on your
  infrastructure; no data leaves your network.
- **Managed SaaS** (roadmap): encrypted at rest (per-tenant envelope
  encryption for connector credentials), TLS in transit, SOC 2 Type II
  audit planned before general availability.

## The gateway and prompt storage

Gateway mode (per-employee virtual keys, prompt-level coaching) sees
prompt content in transit. Storage of that content is **opt-in per
tenant** (off by default, toggled in the dashboard) and, when enabled,
every prompt is **redacted before it is written**: emails, API keys,
bearer/JWT tokens, and long numbers are replaced with placeholders, and
content is truncated at 4,000 characters. Virtual keys themselves are
stored as SHA-256 hashes — the full key is shown exactly once at
creation. SCIM provisioning tokens are likewise stored hashed.

Self-hosting this repository keeps all of it — including gateway
traffic — inside your own network.

## Reporting a vulnerability

Email security@tokentracker.example with details. We commit to a
response within 48 hours.
