/** MSW mock backend shared by the unit/integration tests. */

import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'

import { makeToken } from './helpers'

/** Mirrors the real services: patient-flow and CDS reject anonymous callers. */
function requireAuth(request: Request): Response | null {
  const header = request.headers.get('authorization')
  if (!header?.toLowerCase().startsWith('bearer ')) {
    return HttpResponse.json({ detail: 'Missing bearer token' }, { status: 401 })
  }
  return null
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
    return HttpResponse.json({
      access_token: makeToken({ sub: body.username ?? 'user', roles: ['clinician'] }),
      token_type: 'bearer',
    })
  }),

  http.get('/api/fhir/patient/search', () =>
    HttpResponse.json({
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
    }),
  ),

  http.get('/api/fhir/patient/:id', ({ params }) =>
    HttpResponse.json({ resourceType: 'Patient', id: params.id, active: true }),
  ),

  http.delete('/api/cache/:resource', ({ params }) =>
    HttpResponse.json({ status: 'ok', resource: params.resource, patient: null, deleted: 4 }),
  ),

  http.get('/flow/beds', ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json([
      { id: 'bed-aaaaaaaa-1', ward: 'A', occupied: false },
      { id: 'bed-bbbbbbbb-2', ward: 'A', occupied: true },
    ])
  }),

  http.patch('/flow/beds/:id', ({ params, request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const occupied = new URL(request.url).searchParams.get('occupied') === 'true'
    return HttpResponse.json({ id: params.id, ward: 'A', occupied })
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
    })
  }),

  http.post('/flow/queue', ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    return HttpResponse.json({ ok: true, id: 'q1' }, { status: 201 })
  }),

  http.post('/cds/risk', async ({ request }) => {
    const denied = requireAuth(request)
    if (denied) return denied
    const body = (await request.json()) as { hr: number; sbp: number; spo2: number }
    const score = Math.min(
      Math.max(body.hr - 100, 0) / 100 +
        Math.max(90 - body.sbp, 0) / 90 +
        Math.max(95 - body.spo2, 0) / 95,
      1,
    )
    return HttpResponse.json({
      score: Number(score.toFixed(3)),
      class_label: score > 0.8 ? 'high' : score > 0.4 ? 'medium' : 'low',
    })
  }),
]

export const server = setupServer(...handlers)
