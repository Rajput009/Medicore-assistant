/**
 * Vitals capture: score → save to chart → escalate.
 *
 * The CDS page used to be a calculator whose inputs vanished on submit. These
 * cover the persistence half: that a save reaches the gateway with the right
 * payload, that a retry cannot double-file readings, and that the encounter
 * link ("this visit") is carried through when supplied.
 */

import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { CdsPage, acuityFromNews2, shouldOfferEscalation } from './CdsPage'
import type { News2Response } from '../api/types'
import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'

const LOW: News2Response = {
  score: 0,
  band: 'low',
  red_flag: false,
  recommended_response: 'Continue routine monitoring.',
  monitoring_frequency: '12 hourly',
  parameters: [],
  disclaimer: 'aid',
}

function stubScore(result: Partial<News2Response> = {}) {
  server.use(http.post('/cds/news2', () => HttpResponse.json({ ...LOW, ...result })))
}

describe('shouldOfferEscalation', () => {
  it('does not offer escalation for a clean low score', () => {
    expect(shouldOfferEscalation(LOW)).toBe(false)
  })

  it('offers escalation on a red flag even when the total is low', () => {
    expect(shouldOfferEscalation({ ...LOW, red_flag: true })).toBe(true)
  })

  it('offers escalation from a total of 3', () => {
    expect(shouldOfferEscalation({ ...LOW, score: 3 })).toBe(true)
  })

  it('offers escalation for any non-low band', () => {
    expect(shouldOfferEscalation({ ...LOW, band: 'medium' })).toBe(true)
  })
})

describe('acuityFromNews2', () => {
  it('ranks a high score as ESI 1', () => {
    expect(acuityFromNews2({ ...LOW, score: 8, band: 'high' })).toBe(1)
  })

  it('ranks a medium score as ESI 2', () => {
    expect(acuityFromNews2({ ...LOW, score: 5, band: 'medium' })).toBe(2)
  })

  it('ranks a clean score as ESI 4', () => {
    expect(acuityFromNews2(LOW)).toBe(4)
  })
})

describe('saving vitals to the chart', () => {
  it('posts every recorded parameter plus the NEWS2 total', async () => {
    let body: Record<string, unknown> | null = null
    stubScore({ score: 2 })
    server.use(
      http.post('/api/fhir/observation', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ ok: true, count: 7, created: [] }, { status: 201 })
      }),
    )

    const { user } = renderWithProviders(<CdsPage />, {
      route: '/cds?patient=MRN-42',
      token: makeToken(),
    })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    await screen.findByText(/recommended response/i)
    await user.click(screen.getByRole('button', { name: /save vitals to chart/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body).toMatchObject({
      patient_id: 'MRN-42',
      respiratory_rate: 16,
      spo2: 98,
      temperature: 37,
      systolic_bp: 120,
      pulse: 72,
      consciousness: 'A',
      news2_score: 2,
    })
  })

  it('confirms how many observations were filed', async () => {
    stubScore()
    server.use(
      http.post('/api/fhir/observation', () =>
        HttpResponse.json({ ok: true, count: 7, created: [] }, { status: 201 }),
      ),
    )

    const { user } = renderWithProviders(<CdsPage />, {
      route: '/cds?patient=MRN-42',
      token: makeToken(),
    })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    await screen.findByText(/recommended response/i)
    await user.click(screen.getByRole('button', { name: /save vitals to chart/i }))

    expect(await screen.findByText(/Saved 7 observations/i)).toBeInTheDocument()
  })

  it('links the reading to an encounter when one is given', async () => {
    let body: Record<string, unknown> | null = null
    stubScore()
    server.use(
      http.post('/api/fhir/observation', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ ok: true, count: 6, created: [] }, { status: 201 })
      }),
    )

    const { user } = renderWithProviders(<CdsPage />, {
      route: '/cds?patient=MRN-42',
      token: makeToken(),
    })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    await screen.findByText(/recommended response/i)
    await user.type(screen.getByLabelText(/encounter id/i), 'ENC-7')
    await user.click(screen.getByRole('button', { name: /save vitals to chart/i }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body).toMatchObject({ encounter_id: 'ENC-7' })
  })

  it('sends one idempotency key so a retry cannot double-file', async () => {
    const keys: string[] = []
    let attempts = 0
    stubScore()
    server.use(
      http.post('/api/fhir/observation', ({ request }) => {
        keys.push(request.headers.get('idempotency-key') ?? '')
        attempts++
        // First attempt fails the way a flaky upstream would.
        if (attempts === 1) return HttpResponse.json({ detail: 'upstream' }, { status: 503 })
        return HttpResponse.json({ ok: true, count: 6, created: [] }, { status: 201 })
      }),
    )

    const { user } = renderWithProviders(<CdsPage />, {
      route: '/cds?patient=MRN-42',
      token: makeToken(),
    })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    await screen.findByText(/recommended response/i)
    await user.click(screen.getByRole('button', { name: /save vitals to chart/i }))

    expect(await screen.findByText(/Saved 6 observations/i)).toBeInTheDocument()
    expect(keys).toHaveLength(2)
    expect(new Set(keys).size).toBe(1)
  })

  it('refuses to save without a patient id', async () => {
    stubScore()
    let called = false
    server.use(
      http.post('/api/fhir/observation', () => {
        called = true
        return HttpResponse.json({ ok: true, count: 0, created: [] }, { status: 201 })
      }),
    )

    const { user } = renderWithProviders(<CdsPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    await screen.findByText(/recommended response/i)
    await user.click(screen.getByRole('button', { name: /save vitals to chart/i }))

    expect(await screen.findByText(/Enter a patient id/i)).toBeInTheDocument()
    expect(called).toBe(false)
  })

  it('surfaces a save failure instead of claiming success', async () => {
    stubScore()
    server.use(
      http.post('/api/fhir/observation', () =>
        HttpResponse.json({ detail: 'Upstream rejected the write' }, { status: 502 }),
      ),
    )

    const { user } = renderWithProviders(<CdsPage />, {
      route: '/cds?patient=MRN-42',
      token: makeToken(),
    })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    await screen.findByText(/recommended response/i)
    await user.click(screen.getByRole('button', { name: /save vitals to chart/i }))

    expect(await screen.findByText(/Upstream rejected the write/i)).toBeInTheDocument()
    expect(screen.queryByText(/Saved/i)).not.toBeInTheDocument()
  })
})
