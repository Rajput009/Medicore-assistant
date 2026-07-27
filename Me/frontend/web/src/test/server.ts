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
    return HttpResponse.json({ resourceType: 'Patient', id: params.id, active: true })
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

  http.get('/flow/queue', ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({
      items: [
        { patient_id: 'pat-1', acuity: 1, dept: 'ED' },
        { patient_id: 'pat-2', acuity: 4, dept: 'ED' },
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
