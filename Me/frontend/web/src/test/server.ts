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
    const label = score > 0.8 ? 'high' : score > 0.4 ? 'medium' : 'low'
    return HttpResponse.json({
      score: Number(score.toFixed(3)),
      class_label: label,
      news2_score: Math.round(score * 20),
      red_flag: label === 'high',
      recommended_response: 'Continue routine monitoring.',
      disclaimer: 'NEWS2 is a track-and-trigger aid, not a diagnosis.',
    })
  }),
]

export const server = setupServer(...handlers)
