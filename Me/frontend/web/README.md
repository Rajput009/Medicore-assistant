# MediCore Console (Web)

React 18 + TypeScript + Vite admin/clinician console for the MediCore platform.

---

## Contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Features](#features)
- [Authentication & RBAC](#authentication--rbac)
- [Testing](#testing)
- [Accessibility](#accessibility)
- [Troubleshooting](#troubleshooting)

---

## Quick start

```bash
cd Me/frontend/web
npm install
npm run dev          # http://localhost:5173
```

The dev server proxies each backend service, so **start the backend first**
(`docker compose -f deploy/docker/docker-compose.yml up`) or point the proxy at
running instances:

```bash
GATEWAY_URL=http://localhost:8080 \
AUTH_URL=http://localhost:8081 \
PATIENT_FLOW_URL=http://localhost:8082 \
CDS_URL=http://localhost:8083 \
npm run dev
```

Sign in with the demo credentials (any username, password from `DEMO_PASSWORD`,
default `medicore-dev`), or use **Sign in with SSO** when OIDC is configured.

### Scripts

| Command                 | Purpose                                        |
| ----------------------- | ---------------------------------------------- |
| `npm run dev`           | Dev server with hot reload + backend proxies    |
| `npm run build`         | Typecheck and produce a production bundle       |
| `npm run preview`       | Serve the production build locally              |
| `npm run typecheck`     | `tsc --noEmit` over `src`                       |
| `npm test`              | Unit + integration tests (Vitest)               |
| `npm run test:watch`    | Vitest in watch mode                            |
| `npm run test:coverage` | Coverage report with enforced thresholds        |
| `npm run test:e2e`      | Browser end-to-end tests (Playwright)           |

---

## Architecture

```
src/
  api/
    client.ts        Typed fetch wrapper: errors, query building, cancellation
    types.ts         Response types mirroring the FastAPI models
  auth/
    token.ts         JWT decode, expiry, role normalisation, safe storage
    AuthContext.tsx  Session state, login/logout, auto-expiry, cross-tab sync
  hooks/
    useAsync.ts      Async state with cancellation and race protection
  pages/             One module per screen
  ui/
    App.tsx          Routes + route guards
    AppShell.tsx     Sidebar, top bar, role-filtered navigation
    ErrorBoundary.tsx
    components.tsx   Card, Alert, Badge, Field, EmptyState, ...
  styles.css         Design tokens, layout, component styles
```

**Data flow.** Pages call `api.*`, which returns typed promises. Those are
driven by `useAsyncData` (load on mount) or `useAsyncAction` (run on demand).
Both return a discriminated union — `idle | loading | success | error` — so
every screen renders all four states explicitly rather than conflating "loading"
with "failed".

### Request handling

The client normalises the messy parts of `fetch`:

- Non-2xx responses become `ApiError` carrying `status` and the FastAPI
  `detail` (including flattened Pydantic validation arrays).
- Connection failures become `NetworkError`, distinct from HTTP errors.
- Cancellations propagate as `AbortError` and are **ignored** by the hooks —
  a cancelled request never renders as an error. Detection works across
  browser and Node/undici, whose abort error types differ.
- Every request is cancellable; hooks abort in-flight work on unmount and when
  superseded, and discard out-of-order responses so a slow earlier request
  cannot overwrite a newer result.

---

## Configuration

Base URLs are read at build time from `import.meta.env`. Defaults are the dev
proxy prefixes, which keep all requests same-origin and avoid CORS entirely.

| Variable                     | Default | Purpose                |
| ---------------------------- | ------- | ---------------------- |
| `VITE_GATEWAY_BASE_URL`      | `/api`  | Gateway (FHIR, cache)  |
| `VITE_AUTH_BASE_URL`         | `/auth` | Login, OIDC            |
| `VITE_PATIENT_FLOW_BASE_URL` | `/flow` | Beds, triage queue     |
| `VITE_CDS_BASE_URL`          | `/cds`  | Risk scoring           |

Dev-proxy targets are set with `GATEWAY_URL`, `AUTH_URL`, `PATIENT_FLOW_URL`
and `CDS_URL` (see `vite.config.ts`).

For a production build served from a different origin than the APIs:

```bash
VITE_GATEWAY_BASE_URL=https://api.medicore.example \
VITE_AUTH_BASE_URL=https://auth.medicore.example \
npm run build
```

> Cross-origin deployments require the services to allow the console's origin
> via `ALLOWED_ORIGINS`.

---

## Features

| Page                  | Route    | Capabilities                                             | Required role      |
| --------------------- | -------- | -------------------------------------------------------- | ------------------ |
| **Overview**          | `/`      | Live health of all four services, refresh, resolved config | any                |
| **My patients**       | `/worklist` | Claimed queue, waiting triage, occupied beds, recent charts | clinician or admin |
| **Ward board**        | `/wards` | Bed census by ward, filtered to your `wards` claim         | clinician or admin |
| **FHIR explorer**     | `/fhir`  | Search or read Patient / Encounter / Observation / MedicationRequest / AllergyIntolerance / Condition; result table + raw bundle | clinician or admin |
| **Patient flow**      | `/flow`  | Bed occupancy with toggle, triage queue with filters, enqueue form | clinician or admin |
| **Decision support**  | `/cds`   | Full NEWS2 (6 parameters + O2/Scale 2) with per-parameter breakdown, save vitals as FHIR Observations, escalate to triage | clinician or admin |
| **Administration**    | `/admin` | Audit trail search (who accessed a record, denied attempts, time windows) and FHIR cache invalidation | admin              |

Every destructive action (cache invalidation) requires explicit confirmation.

---

## Authentication & RBAC

1. `POST /auth/login` (or the OIDC flow) returns an internal JWT.
2. The token is stored in `localStorage` under `medicore.token`.
3. `AuthProvider` decodes it for `sub`, `roles` and `exp`.
4. `api.*` sends it as `Authorization: Bearer <token>`.

Session handling covers the cases that usually break in practice:

- A stored token that is malformed or already expired is discarded on load.
- A timer signs the user out the moment the token expires, so the UI never
  shows privileged navigation for a token the gateway would reject.
- A `storage` event signs the user out in **every** tab when one tab logs out.
- `localStorage` failures (Safari private mode, disabled storage) degrade to a
  tab-scoped session instead of crashing.

### The client-side role gate is an affordance, not a control

`RequireRole` and the filtered navigation only reduce confusion. **Every service
independently enforces RBAC on every request.** A user who edits their token or
navigates directly to `/admin` gets a 403 from the server, which the UI renders
as a permission message. Treat the browser as untrusted.

Enforcement is per-service, not only at the gateway: `patient-flow` and `cds`
are directly reachable inside a cluster, so they validate the bearer token
themselves via `backend/common/deps.py`. Only `/health` is public, because
liveness and readiness probes need it. Consequently **every** call the console
makes to those services carries the `Authorization` header.

### OIDC callback

`/oidc/callback` accepts `access_token` from the URL **fragment** (preferred —
fragments are never sent to a server) or the query string, validates it, then
replaces the history entry so the token does not linger in the address bar.

---

## Testing

Three layers, each catching a different class of defect.

```bash
npm test                 # 156 unit + integration tests
npm run test:coverage    # with thresholds (80% statements/lines/functions)
npm run test:e2e         # 35 browser specs across 3 browsers
```

### Unit & integration (Vitest + Testing Library + MSW)

HTTP is intercepted by [MSW](https://mswjs.io) at the network layer, so
components run against realistic request/response cycles without mocking
`fetch`. Unhandled requests **fail the test**, so a missing mock is loud.

| File                     | Focus                                                      |
| ------------------------ | ---------------------------------------------------------- |
| `auth/token.test.ts`     | JWT decoding, expiry maths, role coercion, storage failures |
| `api/client.test.ts`     | Query building, error mapping, cancellation, encoding       |
| `hooks/useAsync.test.tsx`| Races, out-of-order responses, unmount safety               |
| `auth/AuthContext.test.tsx` | Login, persistence, auto-expiry, cross-tab sync          |
| `pages/pages.test.tsx`   | Each screen driven through real user interactions           |
| `ui/App.test.tsx`        | Routing, route guards, RBAC, OIDC callback, error boundary  |

Tests query by **role and label** (`getByRole`, `getByLabelText`) rather than
CSS classes or test ids, so they assert the accessible UI a user perceives and
survive styling changes.

#### Edge cases covered

Deliberately included because these are where UIs break:

- Tokens: malformed, wrong segment count, non-JSON payload, missing/blank/
  non-string `sub`, absent `exp`, exact-expiry instant, unknown roles, roles as
  a delimited string, non-ASCII subjects, unpadded base64url.
- Network: 401 / 403 / 404 / 422 / 502 / 503, connection refused, empty body,
  204, malformed JSON, non-JSON error body.
- Concurrency: overlapping requests, out-of-order responses, unmount mid-flight.
- Forms: empty submission, whitespace-only input, out-of-range vitals,
  partially filled optional parameters.
- Environment: `localStorage` throwing, a single service down while others are
  healthy, recovery after refresh.

### End-to-end (Playwright)

`e2e/` runs the real app in Chromium, Firefox and mobile Safari. By default the
backend is stubbed at the network layer, so the suite needs no database:

```bash
npx playwright install    # once, downloads browsers
npm run test:e2e
npm run test:e2e -- --project=chromium --headed
```

Run the same specs against a live deployment:

```bash
E2E_LIVE=1 E2E_BASE_URL=https://console.medicore.example npm run test:e2e
```

Specs cover the sign-in journey, session persistence across reload, expired and
tampered tokens, the OIDC callback, RBAC for each role, all four feature
workflows, keyboard navigation, heading structure and a narrow viewport.

> **Note:** browsers must be downloaded via `npx playwright install`. In
> sandboxes without network egress this step fails; the specs are still
> typechecked (`tsc -p tsconfig.e2e.json`) and collected (`playwright test
> --list`) in CI.

### Backend end-to-end

`Me/backend/tests/test_e2e_api.py` drives the real FastAPI apps over HTTP,
covering the full middleware stack and the auth→gateway contract (a token
minted by the auth service is accepted by the gateway). Run with
`pytest backend/tests -q`.

---

## Accessibility

- Semantic landmarks (`nav`, `main`, `header`) and a skip link.
- Every input has a real `<label>`; errors use `aria-invalid` +
  `aria-describedby` and are announced via `role="alert"`.
- Async results announce politely via `role="status"`.
- Visible focus rings on all interactive elements; the app is fully keyboard
  operable.
- Tables use `<caption>` (visually hidden) and `<th scope>`.
- Colour is never the sole signal — status badges pair colour with text.
- Dark mode via `prefers-color-scheme`; animations respect
  `prefers-reduced-motion`.

---

## Troubleshooting

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| Cards show "unreachable" | Backend not running | Start the stack; check `GATEWAY_URL` etc. |
| Sign-in says "Cannot reach the auth service" | Auth service down or proxy misconfigured | Verify `http://localhost:8081/health` |
| "Password sign-in is disabled" | `ENV` is not `local`/`test` | Use SSO, or set `ENABLE_DEMO_LOGIN=1` |
| Immediately signed out | Token expired or clock skew | Check container clock; sign in again |
| 403 on FHIR pages | Token lacks `clinician`/`admin` | Check the `roles` claim your IdP issues |
| CORS errors in console | Calling services cross-origin | Use the dev proxy, or set `ALLOWED_ORIGINS` |
| `npm run test:e2e` fails to start | Browsers not installed | `npx playwright install` |
