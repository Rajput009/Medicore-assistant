/** MSW mock backend shared by the unit/integration tests. */

import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'

import { makeToken } from './helpers'

/**
 * Cookie-only auth: accept either a Bearer header or the test session cookie.
 * Mirrors gateway/deps which accept cookie or bearer.
 */
function requireAuth(request: Request): Response | null {
  const header = request.headers.get('authorization')
  if (header?.toLowerCase().startsWith('bearer ')) return null
  const cookie = request.headers.get('cookie') || ''
  if (cookie.includes('medicore_session=')) return null
  // credentials:include may not forward cookies through MSW the same way;
  // also accept when document has the session cookie (jsdom unit tests).
  if (typeof document !== 'undefined' && document.cookie.includes('medicore_session=')) {
    return null
  }
  return HttpResponse.json({ detail: 'Missing bearer token' }, { status: 401 })
}

export const handlers = [
  http.get('/api/health', () =>
    HttpResponse.json({ status: 'ok', service: 'gateway', env: 'test' }),
  ),
  http.get('/auth/health', () => HttpResponse.json({ status: 'ok', service: 'auth', env: 'test' })),
  http.get('/flow/health', () =>
    HttpResponse.json({ status: 'ok', service: 'patient-flow', env: 'test' }),
  ),
  http.get('/cds/health', () => HttpResponse.json({ status: 'ok', service: 'cds', env: 'test' })),

  http.post('/auth/login', async ({ request }) => {
    const body = (await request.json()) as { username?: string; password?: string }
    if (body.password !== 'correct-horse') {
      return HttpResponse.json({ detail: 'Invalid credentials' }, { status: 401 })
    }
    const token = makeToken({ sub: body.username ?? 'user', roles: ['clinician'] })
    // Simulate Set-Cookie (MSW + jsdom won't store httpOnly; session stub does).
    if (typeof document !== 'undefined') {
      document.cookie = 'medicore_session=test-session; path=/'
      document.cookie = 'medicore_csrf=test-csrf; path=/'
    }
    return HttpResponse.json(
      {
        access_token: token,
        token_type: 'bearer',
        expires_in: 900,
      },
      {
        headers: {
          'Set-Cookie': 'medicore_session=test-session; Path=/; HttpOnly; SameSite=Lax',
        },
      },
    )
  }),

  http.post('/auth/logout', () => {
    if (typeof document !== 'undefined') {
      document.cookie = 'medicore_session=; path=/; max-age=0'
      document.cookie = 'medicore_csrf=; path=/; max-age=0'
    }
    return HttpResponse.json({ status: 'ok' })
  }),

  http.get('/auth/session', ({ request }) => {
    const cookie = request.headers.get('cookie') || ''
    const hasCookie =
      cookie.includes('medicore_session=') ||
      (typeof document !== 'undefined' && document.cookie.includes('medicore_session='))
    const bearer = request.headers.get('authorization')
    if (!hasCookie && !bearer?.toLowerCase().startsWith('bearer ')) {
      return HttpResponse.json({ detail: 'Not authenticated' }, { status: 401 })
    }
    return HttpResponse.json({
      sub: 'session.user',
      roles: ['clinician'],
      exp: Math.floor(Date.now() / 1000) + 900,
    })
  }),

  http.post('/auth/session/establish', async ({ request }) => {
    const body = (await request.json()) as { access_token?: string }
    const raw = body.access_token?.trim() ?? ''
    const parts = raw.split('.')
    if (parts.length !== 3) {
      return HttpResponse.json({ detail: 'Invalid or expired token' }, { status: 401 })
    }
    try {
      const padded = parts[1].replace(/-/g, '+').replace(/_/g, '/')
      const withPad = padded + '='.repeat((4 - (padded.length % 4)) % 4)
      const payload = JSON.parse(atob(withPad)) as {
        sub?: string
        roles?: string[]
        exp?: number
      }
      if (!payload.sub) {
        return HttpResponse.json({ detail: 'Invalid or expired token' }, { status: 401 })
      }
      if (typeof payload.exp === 'number' && payload.exp * 1000 <= Date.now()) {
        return HttpResponse.json({ detail: 'Invalid or expired token' }, { status: 401 })
      }
      if (typeof document !== 'undefined') {
        document.cookie = 'medicore_session=test-session; path=/'
        document.cookie = 'medicore_csrf=test-csrf; path=/'
      }
      // Subsequent /session calls should reflect the established user.
      server.use(
        http.get('/auth/session', () =>
          HttpResponse.json({
            sub: payload.sub,
            roles: payload.roles ?? ['clinician'],
            exp: payload.exp ?? Math.floor(Date.now() / 1000) + 900,
          }),
        ),
      )
      return HttpResponse.json({
        sub: payload.sub,
        roles: payload.roles ?? ['clinician'],
        exp: payload.exp,
      })
    } catch {
      return HttpResponse.json({ detail: 'Invalid or expired token' }, { status: 401 })
    }
  }),

  http.get('/api/fhir/patient/search', ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({
      resourceType: 'Bundle',
      entry: [
        { resource: { resourceType: 'Patient', id: 'p1', name: [{ text: 'Ada Lovelace' }] } },
        {
          resource: {
            resourceType: 'Patient',
            id: 'p2',
            name: [{ given: ['Alan'], family: 'Turing' }],
          },
        },
      ],
    })
  }),

  http.get('/api/fhir/patient/:id', ({ params, request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({
      resourceType: 'Patient',
      id: params.id,
      active: true,
      gender: 'female',
      birthDate: '1815-12-10',
      name: [{ text: 'Ada Lovelace' }],
    })
  }),

  http.get(/\/api\/fhir\/observation\/search/, ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({
      resourceType: 'Bundle',
      entry: [
        {
          resource: {
            resourceType: 'Observation',
            id: 'obs-1',
            code: { text: 'Heart rate' },
            valueQuantity: { value: 88, unit: '/min' },
          },
        },
      ],
    })
  }),

  http.get(/\/api\/fhir\/encounter\/search/, ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({
      resourceType: 'Bundle',
      entry: [
        {
          resource: {
            resourceType: 'Encounter',
            id: 'enc-1',
            status: 'in-progress',
            class: { code: 'EMER' },
          },
        },
      ],
    })
  }),

  /**
   * Audit search. Mirrors the gateway: the raw MRN is never echoed back —
   * only the hash the server matched on.
   */
  http.get('/api/audit/search', ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const url = new URL(request.url)
    const patient = url.searchParams.get('patient')
    const limit = Number(url.searchParams.get('limit') ?? 25)
    const offset = Number(url.searchParams.get('offset') ?? 0)
    const now = new Date().toISOString()
    return HttpResponse.json({
      items: [
        {
          ts: now,
          request_id: 'req-1',
          service: 'gateway',
          actor_sub: 'dr.smith',
          actor_roles: ['clinician'],
          method: 'GET',
          path: '/fhir/patient/{id}',
          status: 200,
          outcome: 'success',
          resource_type: 'patient',
          resource_ref: 'sha256:0123456789abcdef',
          patient_ref: null,
          client_ip: '203.0.113.7',
        },
        {
          ts: now,
          request_id: 'req-2',
          service: 'gateway',
          actor_sub: 'dr.snoop',
          actor_roles: ['clinician'],
          method: 'GET',
          path: '/fhir/patient/{id}',
          status: 403,
          outcome: 'denied',
          resource_type: 'patient',
          resource_ref: 'sha256:0123456789abcdef',
          patient_ref: null,
          client_ip: '198.51.100.4',
          break_glass: true,
          break_glass_reason: 'Cardiac arrest in bay 4, covering clinician unavailable',
        },
      ],
      count: 2,
      total: 2,
      limit,
      offset,
      since: new Date(Date.now() - 30 * 86400_000).toISOString(),
      until: now,
      subject_ref: patient ? 'sha256:0123456789abcdef' : null,
    })
  }),

  http.get('/api/audit/patient/:id/accessors', ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({
      patient_ref: 'sha256:0123456789abcdef',
      accessors: [
        {
          actor_sub: 'dr.smith',
          accesses: 4,
          denied: 0,
          break_glass: 0,
          first_access: new Date(Date.now() - 3 * 86400_000).toISOString(),
          last_access: new Date().toISOString(),
        },
        {
          actor_sub: 'dr.snoop',
          accesses: 1,
          denied: 1,
          break_glass: 2,
          first_access: new Date(Date.now() - 86400_000).toISOString(),
          last_access: new Date(Date.now() - 86400_000).toISOString(),
        },
      ],
      count: 2,
    })
  }),

  http.delete('/api/cache/:resource', ({ params, request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({
      status: 'ok',
      resource: params.resource,
      patient: null,
      deleted: 4,
    })
  }),

  /** Handoff notes: append-only server-side. */
  http.get('/flow/handoff/:id', ({ params, request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({
      patient_id: params.id,
      note: {
        patient_id: params.id,
        text: 'S - Situation: stored handoff from the previous shift',
        author: 'dr.night',
        encounter_id: null,
        created_at: new Date(Date.now() - 3600_000).toISOString(),
      },
    })
  }),

  http.get('/flow/handoff/:id/history', ({ params, request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({
      patient_id: params.id,
      versions: [
        {
          patient_id: params.id,
          text: 'newer version',
          author: 'dr.day',
          created_at: new Date().toISOString(),
        },
        {
          patient_id: params.id,
          text: 'older version',
          author: 'dr.night',
          created_at: new Date(Date.now() - 7200_000).toISOString(),
        },
      ],
      count: 2,
    })
  }),

  http.post('/flow/handoff/:id', async ({ params, request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const body = (await request.json()) as { text?: string }
    if (!body.text?.trim()) {
      return HttpResponse.json({ detail: 'Handoff note cannot be blank' }, { status: 422 })
    }
    return HttpResponse.json(
      {
        ok: true,
        note: {
          patient_id: params.id,
          text: body.text,
          // Author comes from the session server-side, never the body.
          author: 'test.user',
          encounter_id: null,
          created_at: new Date().toISOString(),
        },
      },
      { status: 201 },
    )
  }),

  http.get('/flow/beds', ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json([
      { bed_id: 'A-001', ward: 'A', occupied: false, patient_id: null },
      { bed_id: 'A-002', ward: 'A', occupied: true, patient_id: 'MRN-8' },
    ])
  }),

  http.patch('/flow/beds/:id', async ({ params, request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const body = (await request.json()) as { occupied: boolean; patient_id?: string | null }
    return HttpResponse.json({
      bed_id: params.id,
      ward: 'A',
      occupied: body.occupied,
      patient_id: body.occupied ? (body.patient_id ?? null) : null,
    })
  }),

  http.get(/\/flow\/queue\/?(\?.*)?$/, ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    // Don't match /queue/claim
    if (new URL(request.url).pathname.includes('/claim')) {
      return HttpResponse.json({ detail: 'method' }, { status: 405 })
    }
    return HttpResponse.json({
      items: [
        { patient_id: 'pat-1', acuity: 1, dept: 'ED', status: 'waiting' },
        { patient_id: 'pat-2', acuity: 4, dept: 'ED', status: 'waiting' },
      ],
      count: 2,
      total: 2,
    })
  }),

  http.post('/flow/queue', async ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const body = (await request.json()) as { patient_id: string }
    return HttpResponse.json({ ok: true, id: body.patient_id }, { status: 201 })
  }),

  http.post(/\/flow\/queue\/claim/, ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const url = new URL(request.url)
    const dept = url.searchParams.get('dept') || 'ED'
    return HttpResponse.json({
      ok: true,
      item: {
        patient_id: 'claimed-1',
        acuity: 1,
        dept,
        status: 'in_progress',
        claimed_by: 'dr.smith',
      },
    })
  }),

  http.post(/\/flow\/queue\/[^/]+\/complete\/?$/, ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const parts = new URL(request.url).pathname.split('/').filter(Boolean)
    // .../queue/:id/complete
    const patientId = parts[parts.length - 2]
    return HttpResponse.json({
      ok: true,
      item: {
        patient_id: patientId,
        acuity: 2,
        dept: 'ED',
        status: 'completed',
      },
    })
  }),

  /**
   * Full NEWS2 stub. Implements the real RCP thresholds for the parameters
   * the tests exercise, so a page test cannot pass against a score the actual
   * standard would never produce.
   */
  http.post('/cds/news2', async ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const body = (await request.json()) as Record<string, unknown>
    const rr = Number(body.respiratory_rate ?? 16)
    const spo2 = Number(body.spo2 ?? 98)
    const temp = Number(body.temperature ?? 37)
    const sbp = Number(body.systolic_bp ?? 120)
    const pulse = Number(body.pulse ?? 72)
    const acvpu = String(body.consciousness ?? 'A')
    const onOxygen = Boolean(body.on_supplemental_oxygen)

    const scoreRr = rr <= 8 ? 3 : rr <= 11 ? 1 : rr <= 20 ? 0 : rr <= 24 ? 2 : 3
    const scoreSpo2 = spo2 <= 91 ? 3 : spo2 <= 93 ? 2 : spo2 <= 95 ? 1 : 0
    const scoreTemp =
      temp <= 35 ? 3 : temp <= 36 ? 1 : temp <= 38 ? 0 : temp <= 39 ? 1 : 2
    const scoreSbp = sbp <= 90 ? 3 : sbp <= 100 ? 2 : sbp <= 110 ? 1 : sbp >= 220 ? 3 : 0
    const scorePulse =
      pulse <= 40 ? 3 : pulse <= 50 ? 1 : pulse <= 90 ? 0 : pulse <= 110 ? 1 : pulse <= 130 ? 2 : 3
    const scoreAcvpu = acvpu === 'A' ? 0 : 3
    const scoreO2 = onOxygen ? 2 : 0

    const parameters = [
      { name: 'respiratory_rate', value: rr, score: scoreRr, rationale: 'RR band' },
      { name: 'spo2', value: spo2, score: scoreSpo2, rationale: 'SpO2 band' },
      { name: 'temperature', value: temp, score: scoreTemp, rationale: 'Temp band' },
      { name: 'systolic_bp', value: sbp, score: scoreSbp, rationale: 'SBP band' },
      { name: 'pulse', value: pulse, score: scorePulse, rationale: 'Pulse band' },
      { name: 'consciousness', value: acvpu, score: scoreAcvpu, rationale: 'ACVPU' },
      { name: 'supplemental_oxygen', value: onOxygen ? 'yes' : 'no', score: scoreO2, rationale: 'O2' },
    ]
    const total = parameters.reduce((sum, p) => sum + p.score, 0)
    const redFlag = parameters.some((p) => p.score >= 3)
    const band = total >= 7 ? 'high' : total >= 5 || redFlag ? 'medium' : total >= 1 ? 'low-medium' : 'low'

    return HttpResponse.json({
      score: total,
      band,
      red_flag: redFlag,
      recommended_response:
        band === 'high' ? 'Emergency critical care assessment.' : 'Continue routine monitoring.',
      monitoring_frequency: band === 'high' ? 'Continuous' : '12 hourly',
      parameters,
      disclaimer: 'NEWS2 is a track-and-trigger aid, not a diagnosis.',
    })
  }),

  /** Observation write (vitals capture). */
  http.post('/api/fhir/observation', async ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const body = (await request.json()) as Record<string, unknown>
    if (!body.patient_id) {
      return HttpResponse.json({ detail: 'patient_id is required' }, { status: 422 })
    }
    const keys = [
      'respiratory_rate',
      'spo2',
      'temperature',
      'systolic_bp',
      'pulse',
      'consciousness',
      'news2_score',
    ]
    const created = keys
      .filter((k) => body[k] !== undefined && body[k] !== null)
      .map((k, i) => ({ id: `obs-${i + 1}`, code: k }))
    return HttpResponse.json({ ok: true, created, count: created.length }, { status: 201 })
  }),

  http.post('/cds/risk', async ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const body = (await request.json()) as { hr?: number; sbp?: number; spo2?: number }
    const hr = Number(body.hr ?? 72)
    const sbp = Number(body.sbp ?? 120)
    const spo2 = Number(body.spo2 ?? 98)
    // Crude stand-in matching the page tests' expectations.
    const critical = hr >= 130 || sbp <= 90 || spo2 <= 91
    const class_label = critical ? 'high' : 'low'
    const score = critical ? 0.85 : 0
    return HttpResponse.json({
      score,
      class_label,
      news2_score: critical ? 9 : 0,
      red_flag: critical,
      recommended_response: critical
        ? 'Emergency assessment by a critical-care team.'
        : 'Continue routine monitoring.',
      disclaimer: 'NEWS2 is a track-and-trigger aid.',
    })
  }),
]

export const server = setupServer(...handlers)
