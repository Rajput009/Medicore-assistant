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
| Backend unit/regression | `backend/tests/test_*.py` | 37 | pytest |
| Backend end-to-end | `backend/tests/test_e2e_api.py` | 43 | pytest |
| Frontend unit + integration | `frontend/web/src/**/*.test.tsx` | 156 | vitest |
| Browser end-to-end | `frontend/web/e2e/*.spec.ts` | 35 x 3 browsers | playwright |

Browser e2e requires `npx playwright install` once to download browsers.

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

- **gateway**: Edge API, request fan-out to internal services, OpenAPI docs at `/docs`.
- **auth**: Login, token mint/refresh, RBAC, OIDC plumbing.
- **patient-flow**: Real-time bed/queue endpoints (placeholder logic + pub/sub topics).
- **cds**: Decision support stub with example risk score endpoint and ML serving hook.

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

## MongoDB (Documents & Logs)
- `MONGO_URI`, `MONGO_DB` envs.
- Example endpoints in **patient_flow**: `POST /queue`, `GET /queue` use Mongo.

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
