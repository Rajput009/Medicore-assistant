/**
 * Audit trail search UI.
 *
 * The critical behaviour is that "no results" is stated as an answer rather
 * than shown as a blank table — a compliance officer asking "did anyone open
 * this chart?" needs to distinguish "no" from "the query did not run".
 */

import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'
import { AuditSearchPanel, formatWhen, outcomeTone, sinceFromDays } from './AuditSearchPanel'

const EVENT = {
  occurred_at: '2026-07-27T10:00:00+00:00',
  request_id: 'req-1',
  service: 'gateway',
  method: 'GET',
  path: '/fhir/patient/{id}',
  status: 200,
  outcome: 'success',
  actor_sub: 'dr.smith',
  actor_roles: ['clinician'],
  resource_type: 'patient',
  resource_ref: 'sha256:abc',
  patient_ref: 'sha256:abc',
  client_ip: '10.0.0.1',
  duration_ms: 4.2,
}

function stubAudit(
  body: Partial<{ items: unknown[]; count: number; total: number; patient_ref: string | null }>,
  capture?: (url: URL) => void,
) {
  server.use(
    http.get('/api/audit/search', ({ request }) => {
      capture?.(new URL(request.url))
      return HttpResponse.json({
        items: body.items ?? [],
        count: body.count ?? (body.items?.length ?? 0),
        total: body.total ?? (body.items?.length ?? 0),
        patient_ref: body.patient_ref ?? null,
      })
    }),
  )
}

describe('outcomeTone', () => {
  it('flags denied and error as the strongest signal', () => {
    expect(outcomeTone('denied')).toBe('err')
    expect(outcomeTone('error')).toBe('err')
  })

  it('flags failure as a warning', () => {
    expect(outcomeTone('failure')).toBe('warn')
  })

  it('treats success and unknown as neutral', () => {
    expect(outcomeTone('success')).toBe('ok')
    expect(outcomeTone(null)).toBe('ok')
  })
})

describe('sinceFromDays', () => {
  it('converts a day window into an ISO timestamp in the past', () => {
    const now = Date.UTC(2026, 6, 27, 12, 0, 0)
    expect(sinceFromDays(1, now)).toBe('2026-07-26T12:00:00.000Z')
  })

  it('handles a year window', () => {
    const now = Date.UTC(2026, 6, 27, 12, 0, 0)
    expect(sinceFromDays(365, now).startsWith('2025-07-27')).toBe(true)
  })
})

describe('formatWhen', () => {
  it('renders a parseable timestamp', () => {
    expect(formatWhen('2026-07-27T10:00:00+00:00')).not.toBe('Invalid Date')
  })

  it('passes through an unparseable value rather than showing NaN', () => {
    expect(formatWhen('not-a-date')).toBe('not-a-date')
  })
})

describe('AuditSearchPanel', () => {
  it('lists who accessed a record', async () => {
    stubAudit({ items: [EVENT] })
    const { user } = renderWithProviders(<AuditSearchPanel />, {
      token: makeToken({ roles: ['admin'] }),
    })

    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    expect(await screen.findByText('dr.smith')).toBeInTheDocument()
    expect(screen.getByText(/10\.0\.0\.1/)).toBeInTheDocument()
  })

  it('states plainly when nobody accessed the record', async () => {
    stubAudit({ items: [] })
    const { user } = renderWithProviders(<AuditSearchPanel />, {
      token: makeToken({ roles: ['admin'] }),
    })

    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    // A blank table would not distinguish "no" from "did not run".
    expect(await screen.findByText(/No matching access records/i)).toBeInTheDocument()
  })

  it('sends the patient filter to the gateway', async () => {
    let seen: URL | null = null
    stubAudit({ items: [EVENT] }, (url) => {
      seen = url
    })
    const { user } = renderWithProviders(<AuditSearchPanel />, {
      token: makeToken({ roles: ['admin'] }),
    })

    await user.type(screen.getByLabelText(/patient id/i), 'MRN-000123')
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    await waitFor(() => expect(seen).not.toBeNull())
    expect(seen!.searchParams.get('patient')).toBe('MRN-000123')
  })

  it('sends the outcome filter for denied-access investigations', async () => {
    let seen: URL | null = null
    stubAudit({ items: [] }, (url) => {
      seen = url
    })
    const { user } = renderWithProviders(<AuditSearchPanel />, {
      token: makeToken({ roles: ['admin'] }),
    })

    await user.selectOptions(screen.getByLabelText(/outcome/i), 'denied')
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    await waitFor(() => expect(seen).not.toBeNull())
    expect(seen!.searchParams.get('outcome')).toBe('denied')
  })

  it('always bounds the query by a time window', async () => {
    let seen: URL | null = null
    stubAudit({ items: [] }, (url) => {
      seen = url
    })
    const { user } = renderWithProviders(<AuditSearchPanel />, {
      token: makeToken({ roles: ['admin'] }),
    })

    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    await waitFor(() => expect(seen).not.toBeNull())
    expect(seen!.searchParams.get('since')).toBeTruthy()
    expect(seen!.searchParams.get('limit')).toBe('100')
  })

  it('shows the page size against the full match count', async () => {
    stubAudit({ items: [EVENT], count: 1, total: 57 })
    const { user } = renderWithProviders(<AuditSearchPanel />, {
      token: makeToken({ roles: ['admin'] }),
    })

    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    expect(await screen.findByText(/Showing 1 of 57/i)).toBeInTheDocument()
  })

  it('surfaces a backend failure instead of an empty result', async () => {
    server.use(
      http.get('/api/audit/search', () =>
        HttpResponse.json({ detail: 'Audit index temporarily unavailable' }, { status: 503 }),
      ),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, {
      token: makeToken({ roles: ['admin'] }),
    })

    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    // Must not read as "nobody accessed this record".
    expect(await screen.findByText(/temporarily unavailable/i)).toBeInTheDocument()
    expect(screen.queryByText(/No matching access records/i)).not.toBeInTheDocument()
  })

  it('explains that identifiers are hashed and the search is itself audited', () => {
    renderWithProviders(<AuditSearchPanel />, { token: makeToken({ roles: ['admin'] }) })
    expect(screen.getByText(/never stores a raw MRN/i)).toBeInTheDocument()
    expect(screen.getByText(/itself recorded in the audit trail/i)).toBeInTheDocument()
  })
})
