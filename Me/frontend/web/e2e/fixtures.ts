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

  // Full NEWS2 endpoint, scoring the parameters the specs exercise.
  await page.route('**/cds/news2', async (route) => {
    if (unauthorized(route)) return
    const b = route.request().postDataJSON() as {
      respiratory_rate: number
      spo2: number
      temperature: number
      systolic_bp: number
      pulse: number
      consciousness?: string
      on_supplemental_oxygen?: boolean
    }
    const scoreRr =
      b.respiratory_rate <= 8 ? 3 : b.respiratory_rate <= 11 ? 1
        : b.respiratory_rate <= 20 ? 0 : b.respiratory_rate <= 24 ? 2 : 3
    const scoreSpo2 = b.spo2 <= 91 ? 3 : b.spo2 <= 93 ? 2 : b.spo2 <= 95 ? 1 : 0
    const scoreTemp =
      b.temperature <= 35 ? 3 : b.temperature <= 36 ? 1
        : b.temperature <= 38 ? 0 : b.temperature <= 39 ? 1 : 2
    const scoreSbp =
      b.systolic_bp <= 90 ? 3 : b.systolic_bp <= 100 ? 2 : b.systolic_bp <= 110 ? 1 : 0
    const scorePulse =
      b.pulse <= 40 ? 3 : b.pulse <= 50 ? 1 : b.pulse <= 90 ? 0
        : b.pulse <= 110 ? 1 : b.pulse <= 130 ? 2 : 3
    const scoreAcvpu = (b.consciousness ?? 'A') === 'A' ? 0 : 3
    const scoreO2 = b.on_supplemental_oxygen ? 2 : 0

    const parameters = [
      { name: 'respiratory_rate', value: b.respiratory_rate, score: scoreRr, rationale: 'RR' },
      { name: 'spo2', value: b.spo2, score: scoreSpo2, rationale: 'SpO2' },
      { name: 'temperature', value: b.temperature, score: scoreTemp, rationale: 'Temp' },
      { name: 'systolic_bp', value: b.systolic_bp, score: scoreSbp, rationale: 'SBP' },
      { name: 'pulse', value: b.pulse, score: scorePulse, rationale: 'Pulse' },
      { name: 'consciousness', value: b.consciousness ?? 'A', score: scoreAcvpu, rationale: 'ACVPU' },
    ]
    const total = parameters.reduce((sum, p) => sum + p.score, 0) + scoreO2
    const redFlag = parameters.some((p) => p.score >= 3)
    const band =
      total >= 7 ? 'high' : total >= 5 || redFlag ? 'medium' : total >= 1 ? 'low-medium' : 'low'

    return json(route, {
      score: total,
      band,
      red_flag: redFlag,
      recommended_response:
        band === 'high' ? 'Emergency critical care assessment.' : 'Routine monitoring.',
      monitoring_frequency: band === 'high' ? 'Continuous' : '12 hourly',
      parameters,
      disclaimer: 'NEWS2 is a track-and-trigger aid, not a diagnosis.',
    })
  })

  await page.route('**/api/fhir/observation', async (route) => {
    if (unauthorized(route)) return
    return json(route, { ok: true, count: 6, created: [] }, 201)
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
