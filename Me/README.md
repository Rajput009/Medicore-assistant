# MediCore AI Assistant — Starter Monorepo

Production-ready scaffold for the Hospital AI Assistant described in your PRD.  
This repo includes a microservices backend (FastAPI), a React (Vite + TS) web app, infra-as-code stubs,
Docker/Kubernetes deploys, CI, and local dev tooling.

> Target: 300‑bed hospital, HIPAA-aware architecture, FHIR-ready APIs, Kafka event bus, Postgres + Redis.

## Quick Start (Local)

```bash
# 1) copy the env template
cp backend/.env.example backend/.env

# 2) start everything (APIs, Postgres, Mongo, Redis, Kafka, Jaeger, Web)
docker compose -f deploy/docker/docker-compose.yml up --build

# 3) visit the console
open http://localhost:5173

# 4) health checks
curl http://localhost:8080/health            # gateway
curl http://localhost:8081/health            # auth
curl http://localhost:8082/health            # patient-flow
curl http://localhost:8083/health            # cds
```

### Get a token and call a protected endpoint

```bash
# The demo login only works when ENV=local/test (DEMO_PASSWORD, default "medicore-dev").
TOKEN=$(curl -s -X POST http://localhost:8081/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"dr.smith","password":"medicore-dev"}' | jq -r .access_token)

curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/fhir/patient/123
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8080/fhir/observation/search?patient=123"
```

### Run tests locally

```bash
make test      # backend unit + e2e (pytest)
make test-web  # frontend unit + integration (vitest)
make test-e2e  # browser end-to-end (playwright)
make lint      # ruff + tsc --noEmit
```

| Suite | Location | Count | Runner |
| ----- | -------- | ----- | ------ |
| Backend unit/regression + e2e | `backend/tests/test_*.py` | 413 | pytest |
| Frontend unit + integration | `frontend/web/src/**/*.test.tsx` | 156 | vitest |
| Browser end-to-end | `frontend/web/e2e/*.spec.ts` | 35 x 3 browsers | playwright |

Backend DB / residual coverage notes:

- **PostgreSQL (real):** `tests/test_cache_postgres.py` boots a genuine PostgreSQL
  server via the `pgserver` wheel (no Docker) and exercises the FHIR cache DDL,
  jsonb codec, `ON CONFLICT` upserts, SQL-side TTL, invalidation and janitor.
- **MongoDB (mock):** `tests/test_repository.py` + residual suite use
  `mongomock-motor`. **Proven on the mock:** unique partial index on
  `(patient_id) where status=waiting`, including re-queue after complete.
  **Not proven:** replica-set behaviour, retryable writes, multi-document
  transactions (real `mongod` download is TLS-blocked in this environment).
- **Redis multi-replica:** `tests/test_multi_replica_redis.py` uses one
  `fakeredis.FakeServer` with two independent clients (two pods, one broker).
  Proves a shared rate-limit budget and cross-pod revocation visibility, and
  that the in-process fallback is **not** shared (control).
- **NetworkPolicy / Ingress / mesh / FQDN:** residual suite asserts default-deny
  ingress+egress, TLS ingress, path routing, **STRICT** Istio
  `PeerAuthentication` + ServiceAccount identities (`istio-mtls.yaml`), and
  Cilium `toFQDNs` allow-lists with no bare `*` (`cilium-egress.yaml`).
- **Mongo client shape:** ConfigMap URI requires `replicaSet` + `retryWrites`;
  driver enables `retryWrites=True`. Multi-doc transactions remain unproven on
  the mock (explicitly asserted).


- **Live multi-process stack:** `tests/test_live_stack_e2e.py` boots real
  uvicorn workers for auth, gateway, CDS and patient-flow on loopback against
  genuine Postgres (`pgserver`), an HTTP FHIR stub, and mongomock for flow
  state. Covers login → token → CDS/flow clinical workflow → gateway FHIR
  cache round-trip → logout revocation. No Docker required.

Suites `importorskip` cleanly when optional engines are missing.

Browser e2e requires `npx playwright install` once to download browsers.

## Production notes

**State.** Beds and the triage queue live in MongoDB, never in process memory,
so the services scale horizontally. Bed ids are derived deterministically from
`BED_LAYOUT`, so every replica agrees on them and seeding is idempotent.

**Concurrency.** Bed assignment accepts an `expected_occupied` precondition and
returns `409` when another clinician got there first, instead of silently
overwriting. Claiming the next triage patient is a single atomic
`find_one_and_update`, so two clinicians can never be handed the same patient.
A partial unique index prevents a patient from occupying two waiting slots.

**Probes.** `/health` is liveness (process only) and `/ready` is readiness
(verifies dependencies). Kubernetes readiness uses `/ready` so a pod with a
dead database leaves the Service; liveness deliberately does *not* check
dependencies, or a database outage would restart every healthy pod.

**Clinical scoring.** CDS implements NEWS2 (Royal College of Physicians, 2017)
with a per-parameter breakdown and the single-parameter red-flag rule, rather
than an ad-hoc formula. See `backend/services/cds/scoring.py`.

**Logging.** Structured JSON on stdout with trace correlation. Third-party HTTP
client loggers are raised above INFO because they log full URLs, which in this
system contain patient identifiers.

**Data retention.** A background sweep purges FHIR cache rows older than
`CACHE_MAX_AGE_SECONDS`; the table would otherwise grow without bound and
retain PHI indefinitely.

**Fail-fast configuration.** Outside local/test/dev the services refuse to
start if `JWT_SECRET`, `SESSION_SECRET` or `POSTGRES_PASSWORD` is still a
placeholder or too short; if `ALLOWED_ORIGINS` is `*`; if `TRUSTED_HOSTS` is
empty; if demo login is enabled; if OIDC is not fully configured; or if the
access-token TTL is outside 1–60 minutes. Booting with the default signing key
— which is published in this repository — would let anyone mint a valid admin
token, and nothing downstream would notice.

**Tokens.** Access tokens default to a **15-minute** lifetime
(`ACCESS_TOKEN_TTL_MINUTES`), carry a unique `jti` and a `token_use=access`
claim. Logout hits `/auth/logout`, which places the `jti` on a denylist
(Redis when available, in-process otherwise) so a stolen token dies before
natural expiry. Demo username/password login is forced off in production.

**Sessions.** Login/OIDC also set an **httpOnly Secure** session cookie. The
SPA prefers that cookie (`credentials: 'include'`) and keeps any JS-visible
token in memory + tab-scoped `sessionStorage` only — never `localStorage`
(XSS-durable). Legacy localStorage values are migrated away on read.

**Rate limits.** Prefer Redis so N replicas share one global budget; fall back
to in-process when Redis is down so a cache outage cannot take the API with it.

**API surface.** Interactive OpenAPI docs (`/docs`, `/redoc`, `/openapi.json`)
are disabled outside local/test. `/ready` stays public (probes send no auth)
but returns only dependency status, never PHI. Every service is built through
a shared factory (`backend.common.app.create_service_app`) so security headers,
body-size limits, rate limits, audit logging, CORS allow-lists and
TrustedHost checks cannot drift between services.

**Containers.** Images run as uid 10001, drop all capabilities, use a
read-only root filesystem with an in-memory `/tmp`, and start a single uvicorn
worker (scale with replicas, not `--workers`). Kubernetes manifests set
`runAsNonRoot`, PodDisruptionBudgets, topology spread, and NetworkPolicies
that default-deny east-west traffic.

**Audit trail.** Every request is logged with the actor (`sub`, `roles`) and a
reference to the record touched (`resource_type`, `resource_ref`), so
"who viewed this chart?" is answerable as HIPAA 164.312(b) requires. Patient
identifiers are pseudonymised with a salted HMAC: stable enough to correlate
accesses, but the log stream carries no raw PHI. Denied attempts are recorded
with `outcome: denied`.

**Audit search.** Records are additionally indexed in Postgres so that
question can actually be *asked*, not just answered in principle by grepping a
log archive. Admins get `GET /audit/search` and
`GET /audit/patient/{id}/accessors`. Indexing is deliberately best-effort: a
bounded queue drained by a background batch writer, because a clinical request
must never wait on — or fail because of — an audit write. Records lost to a
full queue or a database outage are counted and surfaced on `/audit/stats`
rather than silently discarded.

**Edge hardening.** Security headers on every response (including
`Cache-Control: no-store`, so PHI is never written to a shared cache),
per-caller rate limiting, and a request body cap. These duplicate what a
reverse proxy should do, deliberately: if the proxy is bypassed - a
port-forward, a mesh sidecar in permissive mode - the application still
defends itself.

**Input validation.** FHIR search parameters are allow-listed, values length
capped, and `_count` clamped, so a caller cannot reach undocumented upstream
behaviour, pull an unbounded page of PHI, or flood the response cache with
junk keys. Resource ids are validated against the FHIR id grammar.

**Network.** NetworkPolicies default-deny **ingress and egress**. The ingress
controller is the only external entry; path prefixes (`/api`, `/auth`,
`/flow`, `/cds`) match the Vite dev proxy so the SPA never needs internal
service DNS. Egress is opened only for DNS, data stores, Redis, and HTTPS to
upstream FHIR/IdP/OTLP. No Secret is committed — create `medicore-secrets`
out-of-band.

**CSRF.** Cookie-authenticated unsafe methods require a matching
`Origin`/`Referer` from `ALLOWED_ORIGINS`, or a double-submit
`X-CSRF-Token` header. Bearer-authenticated calls are unaffected.

## Web console

The clinician/admin console is a React + TypeScript SPA. It exposes the FHIR
explorer, bed and triage management, risk scoring and cache administration,
with role-aware navigation and route guards.

See **[frontend/web/README.md](frontend/web/README.md)** for architecture,
configuration, the testing strategy and troubleshooting.

```bash
cd frontend/web && npm install && npm run dev   # http://localhost:5173
```

## Structure

```
backend/                 # FastAPI microservices
  services/
    gateway/             # API gateway/edge (FastAPI + OpenAPI aggregation)
    auth/                # RBAC, JWT, SSO hooks
    patient_flow/        # bed mgmt, queues, routing
    cds/                 # clinical decision support stubs
  common/                # shared code: auth, fhir utils, schemas
frontend/
  web/                   # React + Vite + TS Admin/Clinician UI (see frontend/web/README.md)
deploy/
  docker/                # docker-compose for local dev
  k8s/                   # production k8s manifests (base overlays)
infra/
  terraform/             # AWS/Azure IaC stubs (VPC, EKS/AKS, RDS, MSK)
.github/                 # CI/CD template (ci.yml.example -> workflows/ci.yml)
Makefile                 # DX helpers
```

## Tech

- **Backend**: Python 3.11, FastAPI, Uvicorn, Pydantic, SQLAlchemy, Alembic, Postgres, Redis
- **Events**: Kafka (for streaming telemetry + audit)
- **Auth**: JWT (HS/RS), RBAC scaffolding, SSO hooks (OIDC/SAML placeholders)
- **Interop**: HL7 **FHIR R4** utilities (skeleton), DICOM/PACS client stubs
- **Frontend**: React 18 + TypeScript + Vite
- **Deploy**: Docker, Kubernetes (Kustomize), GitHub Actions, Terraform stubs

## Services (initial)

- **gateway**: Edge API, JWT + RBAC, FHIR proxy with Postgres cache. Docs off in prod.
- **auth**: OIDC SSO (required in prod), short-lived internal JWT minting, demo login local-only.
- **patient-flow**: Bed management and ED triage queue, persisted in MongoDB
  with optimistic-concurrency updates and atomic patient claiming.
- **cds**: NEWS2 deterioration scoring with a per-parameter breakdown and
  escalation guidance.

## Security Notes

- Secrets use environment variables only. Do not commit secrets.
- Transport: terminate TLS at ingress (k8s `Ingress`/ALB). mTLS inside mesh optional.
- Logging: structured JSON (PII-safe), opt-in PHI redaction middleware.
- Basic HIPAA checklist and audit trail event topics included as stubs.

## Next Steps

- Replace stubs with your hospital-specific integrations (EHR: Epic/Cerner via FHIR; devices).
- Implement real auth flows (OIDC with hospital IdP).
- Connect CDS to validated models & add monitoring/alerting.
- Add unit/integration tests, load tests, and threat modeling.


## Epic/Cerner FHIR Integration (Starter)
- Configure `.env` / `backend/.env` with `FHIR_BASE_URL`, `FHIR_OAUTH_TOKEN_URL`, `FHIR_CLIENT_ID`, `FHIR_CLIENT_SECRET`.
- Sample proxy: `GET /fhir/patient/{id}` on the **gateway** calls the FHIR API via OAuth2.

## MongoDB (beds & triage queue)
- `MONGO_URI`, `MONGO_DB`, and pool/timeout envs (see `backend/.env.example`).
- **patient_flow** endpoints:
  - `GET /beds`, `GET /beds/{id}`, `PATCH /beds/{id}` (assign/discharge)
  - `POST /queue`, `GET /queue`, `POST /queue/claim`, `POST /queue/{id}/complete`
- Requires a replica set for retryable writes; compose runs a single-node `rs0`.

## Observability (OpenTelemetry + Jaeger)
- All services auto-instrumented; spans exported to `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://jaeger:4318`).
- Jaeger UI at `http://localhost:16686` when running docker-compose.

> For production: switch to managed tracing (e.g., AWS OTEL Collector), add metrics/log pipelines, and enable TLS.


## Single Sign-On (OIDC) — Quick Start
1. Set envs in `.env` and `backend/.env`:
   - `OIDC_ISSUER` (full metadata URL, e.g., `https://<your-idp>/.well-known/openid-configuration`)
   - `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`
   - `OIDC_REDIRECT_URI` (default `http://localhost:8081/oidc/callback`)
   - `SESSION_SECRET` (random string)
   - `ALLOWED_ORIGINS` (dev: `http://localhost:5173`)

2. Run docker compose and click **Sign in with SSO** in the web UI, or directly open `http://localhost:8081/oidc/login`.

3. On callback, the **auth** service exchanges the code, fetches user info, and returns an **internal JWT** you can use as `Authorization: Bearer <token>`.

> For production: configure HTTPS, use PKCE if doing SPA auth, store tokens server-side, and add RBAC role mapping from IdP claims/groups.


## Gateway Auth & RBAC

- `JWTAuthMiddleware` enforces a valid bearer token on **every** gateway route
  except an exact-match allow-list (`/health`, `/docs`, `/openapi.json`, `/metrics`, ...).
- Route handlers additionally enforce roles: FHIR reads/searches require
  `clinician` or `admin`; cache invalidation requires `admin`.
- **HS256 (dev):** tokens are verified with `JWT_SECRET`.
- **RS256/ES256 (prod):** set `OIDC_JWKS_URI` (preferred) or `OIDC_ISSUER`; the
  gateway fetches the JWKS and matches on the `kid` header, re-fetching once on
  an unknown `kid` to tolerate key rotation.
- `alg: none` and algorithm-downgrade attempts are rejected outright.
- Optionally set `OIDC_AUDIENCE` / `OIDC_ISSUER_CLAIM` to validate `aud`/`iss`.

## Expanded FHIR Endpoints
The gateway now supports these protected endpoints (require clinician/admin role):
- `GET /fhir/patient/{id}` and `/fhir/patient/search?...`
- `GET /fhir/encounter/{id}` and `/fhir/encounter/search?...`
- `GET /fhir/observation/{id}` and `/fhir/observation/search?...`
- `GET /fhir/medicationrequest/{id}` and `/fhir/medicationrequest/search?...`

Example:
```bash
curl -H "Authorization: Bearer <token>" "http://localhost:8080/fhir/observation/search?patient=123&code=789-8"
```


## Cache Invalidation (Admin-only)
The gateway provides admin-only endpoints to clear cache entries:

- `DELETE /cache/{resource}` → clears all cached entries for a resource (e.g., Patient, Encounter)
- `DELETE /cache/{resource}?patient={id}` → clears cached entries for a resource specific to a patient

Example:
```bash
curl -X DELETE -H "Authorization: Bearer <admin_token>" "http://localhost:8080/cache/Observation?patient=123"
```

## Audit Search (Admin-only)

Answers "who viewed MRN-X?" from the queryable audit index. Pass the raw
identifier — the gateway hashes it with the deployment's audit salt before
matching, so you never need to know the pseudonymisation scheme and no raw id
is written as a result of searching.

- `GET /audit/patient/{id}/accessors` → one row per clinician: access count,
  denied count, first/last access. Where an investigation starts.
- `GET /audit/search` → the individual events. Filters: `patient`, `actor`,
  `outcome` (`success|failure|denied|error`), `resource_type`, `service`,
  `since`, `until`, `limit`, `offset`. Defaults to the last 30 days; the
  window is capped at 366 days.
- `GET /audit/stats` → index health. **Alert on `dropped` and `failed`**: they
  are non-zero only when audit records did not reach the index, meaning the
  searchable trail is incomplete (the log stream still has them).

```bash
# Who opened this chart?
curl -H "Authorization: Bearer <admin_token>" \
  "http://localhost:8080/audit/patient/MRN-000123/accessors"

# Every refused attempt against that chart in the last week
curl -H "Authorization: Bearer <admin_token>" \
  "http://localhost:8080/audit/search?patient=MRN-000123&outcome=denied&since=2026-07-20T00:00:00Z"
```

Both are also available in the console under **Administration**.

## Break-glass (emergency access)

A clinician scoped to ward A who finds a ward-B patient arresting needs the
chart *now*. Send a reason and the scope check is overridden:

```bash
curl -X PATCH -H "Authorization: Bearer <clinician_token>" \
  -H "X-Break-Glass-Reason: Cardiac arrest in ICU bay 4, covering clinician unavailable" \
  -H "Content-Type: application/json" \
  -d '{"occupied": true, "patient_id": "MRN-123"}' \
  "http://localhost:8082/beds/ICU-001"
```

It widens **scope, never role** — a `viewer` stays a viewer, and nobody
becomes `admin`. The reason is mandatory and must be substantive; a trivial
one is rejected (400) rather than accepted and logged. List endpoints refuse
it, so an override cannot become cross-ward browsing.

Every use is logged at WARNING and flagged in the audit index. Review with
`GET /audit/search?break_glass=true` or the **Emergency overrides only**
filter in the console; the per-patient accessor summary counts overrides per
clinician. See SECURITY.md for the full rationale.

## Handoff notes (SBAR)

Shift-handoff notes are persisted server-side by patient-flow:

- `GET /handoff/{patient_id}` — the current note (or `null`)
- `GET /handoff/{patient_id}/history` — every version, newest first
- `POST /handoff/{patient_id}` — append a new version

**Append-only.** A note is a contemporaneous clinical communication, so "what
was I told at 07:00?" must stay answerable after someone edits it at 09:00.
Saving supersedes; it never overwrites.

**Not the record of truth.** These are working notes that survive the shift —
the hospital EHR remains authoritative. That is why they live in MediCore's own
storage rather than being written to a FHIR `Communication`: unverified free
text should not land in the legal record, and the gateway's only FHIR write is
deliberately narrow (coded Observations) so what was written is always
auditable. Promoting a note into the EHR is a separate, explicit action.

The author is taken from the verified session, never from the request body.
Reads and writes are audited against the patient like any other PHI access,
and the note text itself is never logged.

## Chart assistant (grounded Q&A)

`POST /assist/ask` answers questions about one patient's chart with citations:

```bash
curl -X POST -H "Authorization: Bearer <clinician_token>" \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "MRN-123", "question": "what allergies are recorded?"}' \
  "http://localhost:8080/assist/ask"
```

Every finding names the resources it came from, and an uncited claim is
rejected server-side rather than displayed. `GET /assist/capabilities`
publishes what it can and cannot do.

**It calls no model, deliberately.** No LLM is configured and egress is
default-deny; routing PHI to a third-party model needs a BAA and an egress
change. The default answerer copies values out of retrieved FHIR resources, so
it cannot invent a result. `AnswerComposer` is the seam where a model-backed
implementation drops in — and `validate_answer` holds it to the same
citation requirement.

Three refusals are worth knowing about:

- **Advice** — "should I give penicillin?" is refused *before* retrieval, so
  an out-of-scope question never becomes a disclosure.
- **Unrecognised questions** — refused with the list of answerable topics,
  rather than guessed at and answered wrongly.
- **Failed lookups** — reported as failures. "Allergy list could not be
  retrieved" is never rendered as "no allergies", which is the single most
  dangerous thing this feature could say.

## Clinical workflow notes

**Ward / department scope.** Access tokens may carry `wards` and
`departments` claims. An **absent or empty claim means unrestricted**, so
legacy tokens and IdPs that publish no scope keep working unchanged. The auth
service maps prefixed IdP groups (`medicore-ward-ICU`, `medicore-dept-ED`,
configurable) onto those claims; `patient-flow` enforces them, and the console
filters the ward board to match. `DEMO_WARDS` / `DEMO_DEPARTMENTS` exercise
this locally without an IdP.

**Retries.** Unsafe writes carry an `Idempotency-Key`. The console mints **one
key per user intent** and reuses it across retries (`api/retry.ts`), so a
dropped response cannot claim a second patient or double-file a set of vitals.
Network errors, 5xx and 429 are retried with exponential backoff and jitter;
409/403/404 are not, because repeating them only hides a real answer.

**Vitals capture.** `POST /fhir/observation` on the gateway is the only write
path. It accepts validated vital signs and emits properly coded FHIR
Observations (LOINC codes, UCUM units, `vital-signs` category) rather than
proxying arbitrary FHIR bodies, so what was recorded is always auditable. The
NEWS2 aggregate is filed separately under the `survey` category — it is a
derived score, and filing it as a vital sign would corrupt flowsheets. All
readings in one submission share an effective timestamp so a flowsheet groups
them into a single column, and the patient's cached Observation searches are
invalidated immediately so the clinician can see what they just saved.

**Safety context in the chart.** The patient chart drawer loads allergies
(`AllergyIntolerance`), the problem list (`Condition`) and medications
(`MedicationRequest`) alongside vitals, and renders them *before* demographics
— they are what a clinician checks before acting. Three rules are enforced in
`safetySummary.ts` and covered by tests, because the dangerous failure mode for
all three lists is an entry that silently does not render:

- A resource that cannot be fully parsed is still displayed, labelled
  "unknown", never filtered out.
- An allergy with no `clinicalStatus` counts as **active**; absent is not
  the same as resolved.
- A failed allergy lookup renders an explicit error, never an empty list —
  "no known allergies" must not be indistinguishable from "we could not ask".

Resolved and inactive entries are collapsed rather than discarded, since a
cleared allergy is still clinical history.
