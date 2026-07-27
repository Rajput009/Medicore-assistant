# Security model & residual risk register

MediCore is a **HIPAA-aware hospital AI assistant scaffold**. This document is
the living threat model and residual-risk register. It is intentionally
honest: “designed” is not the same as “proven in your cluster”.

## Trust boundaries

```
[Browser / SPA]
      |  TLS (ingress)
      v
[Ingress]  ---- path route /api /auth /flow /cds
      |
      +--> gateway (JWT or cookie, RBAC, FHIR proxy, cache)
      +--> auth (login/OIDC, session cookie, revoke)
      +--> patient-flow (JWT/cookie, ward/dept scope, Mongo)
      +--> cds (JWT/cookie, NEWS2)
      |
      +--> Postgres (FHIR response cache only)
      +--> MongoDB (beds + triage queue)
      +--> Redis (rate limit + revoke + idempotency) [optional]
      +--> Upstream FHIR + IdP (HTTPS)
```

- **Browser never holds the access token** (cookie-only SPA; httpOnly cookie).
- **Internal services enforce auth themselves** — NetworkPolicy is defence in
  depth, not the only control.
- **PHI** lives in Mongo, upstream FHIR, and (briefly) the Postgres cache.
  Audit logs carry **pseudonymised** patient refs by default.

## Controls (what we claim)

| Control | Mechanism | Verified how |
|---------|-----------|--------------|
| Fail-closed prod boot | Settings validator | unit tests |
| Short-lived access tokens | 15m default, `jti`, `token_use` | unit + live stack |
| Revocation | denylist until `exp` | unit + multi-replica fakeredis |
| Cookie session | httpOnly + CSRF middleware | unit + live stack |
| SPA XSS credential theft | no JWT in web storage | frontend tests |
| Rate limit | Redis shared / in-process fallback | residual tests |
| Cache correctness | real Postgres via pgserver | `test_cache_postgres` |
| Horizontal ward state | Mongo, not process memory | repository tests |
| Edge hardening | headers, body cap, TrustedHost | hardening tests |
| Error scrubbing | `errors.install_error_handlers` | unit tests |
| Idempotent writes | `Idempotency-Key` on bed/queue | unit tests |
| Ward/dept scope | JWT claims `wards` / `departments` | unit tests |
| L3/L4 isolation | NetworkPolicy default-deny | YAML residual tests |
| Mesh mTLS (optional) | Istio STRICT manifests | YAML present; needs mesh |
| FQDN egress (optional) | Cilium `toFQDNs` | YAML present; needs Cilium |

## Residual risks (not fully closed)

| ID | Risk | Severity | Mitigation / next step |
|----|------|----------|------------------------|
| R1 | No browser→compose live E2E in all environments | Med | Run Playwright `E2E_LIVE=1` in CI with compose |
| R2 | Real `mongod` RS/transactions unproven here | Med | Mongo service container in CI |
| R3 | Mesh mTLS inert without Istio | Med | Enable injection in staging |
| R4 | Vanilla NP still allows `0.0.0.0/0:443` without Cilium | Med | Deploy Cilium FQDN policies with real hosts |
| R5 | Patient ACL is ward/dept only (no encounter graph) | Med | Wire `can_access_patient` to encounter index |
| R6 | No refresh tokens — 15m hard session | Low | Sliding session or step-up if UX requires |
| R7 | In-process rate limit/revoke when Redis down | Med | Monitor fallback; require Redis in prod |
| R8 | Supply-chain / CVE drift | Med | CI `pip-audit` + `npm audit` |
| R9 | No formal pen-test | High (org) | External assessment before PHI |

## AuthZ model (current)

1. **Authentication**: valid access JWT (cookie or Bearer), not revoked.
2. **Roles**: `admin` > `clinician` > `viewer`. Clinical routes need
   clinician|admin.
3. **Scope** (optional claims from IdP):
   - `wards`: list of ward codes; empty = unrestricted.
   - `departments` / `depts`: triage departments; empty = unrestricted.
   - `admin` bypasses scope checks.
4. **Patient-level**: hook `Principal.can_access_patient` — defaults to allow
   for clinicians until an encounter assignment source is configured.

## Reporting vulnerabilities

Do **not** open a public issue for suspected vulnerabilities that could expose
PHI. Contact the repository maintainers privately. Include reproduction steps
and impact assessment.

## Key rotation (runbook sketch)

1. Generate new `JWT_SECRET` / `SESSION_SECRET` (`openssl rand -hex 32`).
2. Deploy dual-read if available (future); otherwise schedule a short window.
3. Roll pods; existing cookies/tokens invalidate at next verify (users re-auth).
4. Revoke store is secret-agnostic (jti-based) and survives rotation.

## Data retention

- FHIR cache rows purged by janitor (`CACHE_MAX_AGE_SECONDS`).
- Idempotency keys TTL 24h.
- Revocation entries TTL = token remaining lifetime.
- Audit logs: retain per organisational policy in a HIPAA-capable sink.
