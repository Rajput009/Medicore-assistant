/**
 * Playwright fixtures.
 *
 * `stubbedPage` intercepts the backend at the network layer so the UI can be
 * exercised deterministically without Postgres/Mongo/an FHIR server. Set
 * E2E_LIVE=1 to skip the stubs and hit real services instead.
 */

import { test as base, type Page, type Route } from '@playwright/test'

export const LIVE = process.env.E2E_LIVE === '1'

/** Builds an unsigned JWT — the SPA only decodes, never verifies. */
export function makeToken(
  opts: { sub?: string; roles?: string[]; expiresInSeconds?: number } = {},
): string {
  const { sub = 'e2e.user', roles = ['clinician'], expiresInSeconds = 3600 } = opts
  const b64 = (v: string) =>
    Buffer.from(v).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return [
    b64(JSON.stringify({ alg: 'HS256', typ: 'JWT' })),
    b64(JSON.stringify({ sub, roles, exp: Math.floor(Date.now() / 1000) + expiresInSeconds })),
    'signature',
  ].join('.')
}

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

/** Mirrors the real services, which reject anonymous callers. */
function unauthorized(route: Route): boolean {
  const header = route.request().headers()['authorization']
  if (!header?.toLowerCase().startsWith('bearer ')) {
    void json(route, { detail: 'Missing bearer token' }, 401)
    return true
  }
  return false
}

/** Registers the default happy-path backend stubs. */
export async function stubBackend(page: Page): Promise<void> {
  await page.route('**/api/health', (r) =>
    json(r, { status: 'ok', service: 'gateway', env: 'e2e' }),
  )
  await page.route('**/auth/health', (r) => json(r, { status: 'ok', service: 'auth', env: 'e2e' }))
  await page.route('**/flow/health', (r) =>
    json(r, { status: 'ok', service: 'patient-flow', env: 'e2e' }),
  )
  await page.route('**/cds/health', (r) => json(r, { status: 'ok', service: 'cds', env: 'e2e' }))

  await page.route('**/auth/login', async (route) => {
    const body = route.request().postDataJSON() as { username?: string; password?: string }
    if (body?.password !== 'correct-horse') {
      return json(route, { detail: 'Invalid credentials' }, 401)
    }
    return json(route, {
      access_token: makeToken({ sub: body.username, roles: ['clinician', 'admin'] }),
      token_type: 'bearer',
    })
  })

  await page.route('**/api/fhir/patient/search*', (r) =>
    json(r, {
      resourceType: 'Bundle',
      entry: [
        { resource: { resourceType: 'Patient', id: 'p1', name: [{ text: 'Ada Lovelace' }] } },
        { resource: { resourceType: 'Patient', id: 'p2', name: [{ text: 'Alan Turing' }] } },
      ],
    }),
  )

  await page.route(/\/api\/fhir\/patient\/(?!search)[^/?]+$/, (r) =>
    json(r, { resourceType: 'Patient', id: 'p1', active: true }),
  )

  await page.route('**/api/cache/**', (r) =>
    json(r, { status: 'ok', resource: 'Patient', patient: null, deleted: 3 }),
  )

  await page.route('**/flow/beds', (r) => {
    if (unauthorized(r)) return
    return json(r, [
      { id: 'bed-11111111', ward: 'A', occupied: false },
      { id: 'bed-22222222', ward: 'B', occupied: true },
    ])
  })
  await page.route('**/flow/beds/*', (r) => {
    if (unauthorized(r)) return
    return json(r, { id: 'bed-11111111', ward: 'A', occupied: true })
  })

  await page.route('**/flow/queue*', async (route) => {
    if (unauthorized(route)) return
    if (route.request().method() === 'POST') {
      return json(route, { ok: true, id: 'q1' }, 201)
    }
    return json(route, {
      items: [
        { patient_id: 'pat-1', acuity: 1, dept: 'ED' },
        { patient_id: 'pat-2', acuity: 4, dept: 'ED' },
      ],
      count: 2,
    })
  })

  await page.route('**/cds/risk', async (route) => {
    if (unauthorized(route)) return
    const b = route.request().postDataJSON() as { hr: number; sbp: number; spo2: number }
    const score = Math.min(
      Math.max(b.hr - 100, 0) / 100 + Math.max(90 - b.sbp, 0) / 90 + Math.max(95 - b.spo2, 0) / 95,
      1,
    )
    return json(route, {
      score: Number(score.toFixed(3)),
      class_label: score > 0.8 ? 'high' : score > 0.4 ? 'medium' : 'low',
    })
  })
}

/** Seeds a session so a spec can start already signed in. */
export async function signIn(
  page: Page,
  roles: string[] = ['clinician', 'admin'],
): Promise<void> {
  const token = makeToken({ roles })
  await page.addInitScript(
    ([key, value]) => window.localStorage.setItem(key as string, value as string),
    ['medicore.token', token],
  )
}

export const test = base.extend<{ stubbedPage: Page }>({
  stubbedPage: async ({ page }, use) => {
    if (!LIVE) await stubBackend(page)
    await use(page)
  },
})

export { expect } from '@playwright/test'
