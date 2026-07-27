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
- **Audit trail** is written twice: the log stream is the system of record,
  and a Postgres `audit_events` index makes it queryable. The index stores the
  same pseudonymised refs, so it holds no raw MRNs under default settings.

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
| Accounting of disclosures | queryable audit index + admin search | real-Postgres + endpoint tests |
| Emergency access | break-glass: scope-only override, reason required, indexed | unit + live stack |
| Handoff notes | append-only, author from token, audited as PHI | unit + integration tests |
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
| R8 | Supply-chain / CVE drift | Med | **Blocking** CI audits (`make audit`); exceptions documented + guarded by tests |
| R9 | No formal pen-test | High (org) | External assessment before PHI |
| R10 | Audit index is lossy under overload (bounded queue) | Med | Alert on `/audit/stats` dropped/failed; backfill from log stream |
| R12 | Unfiltered searches are not attributed per result | Med | Attribute each entry in the returned bundle |
| ~~R11~~ | ~~Non-Patient reads are not attributed to a patient~~ | — | **Closed**: the read path resolves `subject`/`patient` and records the patient ref |

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
- Handoff notes: working notes, not the medical record, so they are **not**
  retained for the six years the audit trail is. `purge_handoffs(days)` sweeps
  them; retaining PHI past its usefulness is its own risk.
- Audit index (`audit_events`): `AUDIT_RETENTION_DAYS`, default 2555 (seven
  years, comfortably over the six required by 164.316(b)(2)(i)). `0` disables
  purging. Probe traffic (`/health`, `/ready`, `/metrics`) is never indexed.

## Audit search (accounting of disclosures)

`GET /audit/search` and `GET /audit/patient/{id}/accessors` answer "who viewed
this record?" for **admins only** — knowing who looked at whom is itself
sensitive, and both endpoints are captured by the same audit middleware, so
investigating the investigators works. Callers pass a raw MRN; the gateway
hashes it with the deployment's audit salt before matching, so searching never
writes an identifier anywhere.

Design constraint worth preserving: **indexing must never delay or fail a
clinical request.** Records go through a bounded in-memory queue drained by a
background batch writer. When the queue overflows or a write fails, records
are dropped and counted rather than blocking the caller — losing audit rows is
bad, blocking a clinician mid-resuscitation is worse. `GET /audit/stats`
exposes `dropped` / `failed`; both should be alerted on, since the searchable
trail is incomplete until they are backfilled from the log stream.

**Patient attribution.** A read of a non-Patient resource (e.g.
`GET /fhir/observation/obs-1`) is an access to a patient's record, but only
the resource body knows whose. The gateway resolves `subject` / `patient` from
the response and records the patient ref alongside the resource ref, so the
access is findable in that patient's trail. A search matches a patient's own
reference, anything explicitly filtered by that patient, and any resource
resolved to them.

Deliberately *not* attributed: subjects that are a Group/Device/Location (not
a person), and contained (`#x`) or `urn:uuid:` references, which identify a
resource inside a bundle rather than a patient id the rest of the system
would recognise. Attributing those would put unrelated accesses into
someone's disclosure accounting. Resolution is best-effort and cannot fail a
clinical read.

Remaining gap: **searches** are attributed only when filtered by an
identifying parameter. A search by name or date range that returns ten
patients records the query shape, not the ten patients it disclosed.
Closing that means attributing each entry in the result bundle.

## Break-glass (emergency scope override)

A clinician scoped to ward A who finds a ward-B patient arresting needs the
chart now. Ward scoping is correct almost always and catastrophically wrong in
that moment, so `X-Break-Glass-Reason: <why>` lets them through and makes the
override loud, attributable and reviewable. Set `BREAK_GLASS_ENABLED=false` to
disable, in which case the header is **rejected**, not ignored — a caller who
believes they have emergency access and does not must be told.

The limits are the control:

| Rule | Why |
|------|-----|
| Widens **scope only**, never role | Otherwise it is a privilege-escalation primitive wearing a safety label. A `viewer` stays a viewer. |
| Reason required, ≥10 chars | An override nobody can review is not a control. "x" in an audit column looks like compliance while providing nothing. |
| Per-request, not a session mode | Access cannot silently stay elevated; there is no session to forget to close. |
| **Not** honoured on list endpoints | Emergency access is for reaching one patient now. On a list it would become cross-ward browsing — the exact abuse scope exists to prevent. |
| Bad reason → 400, not 403 | A clinician who typo'd the header needs to know that, not receive a denial they cannot account for. |

Reviewing: `GET /audit/search?break_glass=true`, or the **Emergency overrides
only** filter in the console. The accessor summary counts overrides per
clinician, so "did anyone reach this chart by override?" is answerable per
patient. Overrides are logged at WARNING with the reason and the scope that
was crossed, *and* flagged on the request's own audit record, so the review
query does not depend on correlating separate log lines.

Note that patient-flow indexes audit records into the same Postgres the
gateway searches. It has to: the browser reaches `/flow/*` directly, so
without its own sink every bed assignment, triage claim and break-glass
override would be missing from audit search.

## Dependency audit policy

`make audit` runs both gates and **fails the build**; CI runs the same two
scripts. Previously the npm gate ended in `|| true`, which meant it reported
findings nobody was obliged to act on.

**What blocks.** Python: every advisory against an installed package. Node:
*runtime* dependencies only (`--omit=dev`). Vite, Vitest and esbuild never
reach a browser, so a dev-server advisory is a real bug but not a release
blocker — and treating it as one trains people to wave the gate through. Dev
advisories are still printed on every run.

**Exceptions.** Listed in `scripts/audit_python.sh` / `scripts/audit_node.sh`
with a status, a reason the vulnerable path is unreachable, and the condition
that withdraws the exception. Each one is backed by a test that fails if the
reasoning stops holding, because a suppressed advisory is exactly the kind of
risk acceptance that outlives the person who accepted it.

| Advisory | Package | Why suppressed | Guard |
|---|---|---|---|
| PYSEC-2026-1325 | `ecdsa` (Minerva timing attack) | No upstream fix exists. `python-jose[cryptography]` resolves EC keys to the OpenSSL backend, so the pure-Python code never runs. | `test_dependency_audit.py` runs a full ES256 round trip with the `ecdsa` module rigged to raise on any attribute access. |
| GHSA-qwww-vcr4-c8h2 | `react-router` (RSC-mode CSRF) | Fix requires >=8.3.0, unreleased; we run the newest published 7.x. The flaw is in the data-router action pipeline, and this SPA is declarative-only. | `routerSurface.test.tsx` fails if `createBrowserRouter`/`RouterProvider`/route `action` appear in app sources. |

Both guards were verified to fail when deliberately broken — a tripwire that
cannot trip is worse than none, because it manufactures confidence.
