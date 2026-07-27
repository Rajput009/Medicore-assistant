/**
 * Audit search panel.
 *
 * The behaviour that matters: an admin can ask "who viewed MRN-X?", denied
 * attempts are visually distinct from successful ones, and the panel never
 * invents a raw identifier the server did not return.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { AdminPage } from './AdminPage'
import { AuditSearchPanel, formatTimestamp, outcomeTone, shortRef } from './AuditSearchPanel'
import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'

const asAdmin = { token: makeToken({ roles: ['admin'] }) }

describe('audit display helpers', () => {
  it('renders a timestamp in local time', () => {
    expect(formatTimestamp('2026-07-27T10:00:00Z')).not.toBe('—')
  })

  it('falls back to the raw value for an unparseable timestamp', () => {
    expect(formatTimestamp('not-a-date')).toBe('not-a-date')
  })

  it('renders an empty timestamp as a dash', () => {
    expect(formatTimestamp(null)).toBe('—')
    expect(formatTimestamp(undefined)).toBe('—')
  })

  it('marks denied attempts as errors and successes as ok', () => {
    // Denied access is the row a privacy investigation is looking for, so it
    // must not be visually indistinguishable from a normal read.
    expect(outcomeTone('denied')).toBe('err')
    expect(outcomeTone('success')).toBe('ok')
    expect(outcomeTone('failure')).toBe('warn')
    expect(outcomeTone('error')).toBe('warn')
    expect(outcomeTone(null)).toBe('neutral')
  })

  it('shortens a hash without inventing content', () => {
    expect(shortRef('sha256:0123456789abcdef0123')).toBe('0123456789ab…')
    expect(shortRef('short')).toBe('short')
    expect(shortRef(null)).toBe('—')
  })
})

describe('AuditSearchPanel', () => {
  it('answers "who viewed MRN-X?" with a per-clinician summary', async () => {
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)

    await user.type(screen.getByLabelText(/patient id/i), 'MRN-000123')
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    const summary = await screen.findByRole('table', {
      name: /clinicians who accessed this record/i,
    })
    expect(within(summary).getByText('dr.smith')).toBeInTheDocument()
    expect(within(summary).getByText('dr.snoop')).toBeInTheDocument()
    // Access counts are what an investigator reads first.
    expect(within(summary).getByText('4')).toBeInTheDocument()
  })

  it('sends the raw identifier for the server to hash', async () => {
    let sent: string | null = null
    server.use(
      http.get('/api/audit/search', ({ request }) => {
        sent = new URL(request.url).searchParams.get('patient')
        return HttpResponse.json({
          items: [],
          count: 0,
          total: 0,
          limit: 25,
          offset: 0,
          since: new Date().toISOString(),
          until: new Date().toISOString(),
          subject_ref: 'sha256:abc',
        })
      }),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.type(screen.getByLabelText(/patient id/i), 'MRN-42')
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    await waitFor(() => expect(sent).toBe('MRN-42'))
  })

  it('lists the individual access events behind the counts', async () => {
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    const events = await screen.findByRole('table', { name: /audit events/i })
    expect(within(events).getByText('denied')).toBeInTheDocument()
    expect(within(events).getByText('success')).toBeInTheDocument()
    expect(within(events).getByText('198.51.100.4')).toBeInTheDocument()
  })

  it('omits the accessor summary when no patient is given', async () => {
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    await screen.findByRole('table', { name: /audit events/i })
    expect(
      screen.queryByRole('table', { name: /clinicians who accessed this record/i }),
    ).not.toBeInTheDocument()
  })

  it('passes the actor and outcome filters through', async () => {
    let query = ''
    server.use(
      http.get('/api/audit/search', ({ request }) => {
        query = new URL(request.url).search
        return HttpResponse.json({
          items: [],
          count: 0,
          total: 0,
          limit: 25,
          offset: 0,
          since: new Date().toISOString(),
          until: new Date().toISOString(),
          subject_ref: null,
        })
      }),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.type(screen.getByLabelText(/clinician/i), 'dr.snoop')
    await user.selectOptions(screen.getByLabelText(/outcome/i), 'denied')
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    await waitFor(() => {
      expect(query).toContain('actor=dr.snoop')
      expect(query).toContain('outcome=denied')
    })
  })

  it('reports an empty trail rather than an empty table', async () => {
    server.use(
      http.get('/api/audit/search', () =>
        HttpResponse.json({
          items: [],
          count: 0,
          total: 0,
          limit: 25,
          offset: 0,
          since: new Date().toISOString(),
          until: new Date().toISOString(),
          subject_ref: null,
        }),
      ),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    expect(await screen.findByText(/no matching audit events/i)).toBeInTheDocument()
  })

  it('distinguishes "no recorded access" from a failed query', async () => {
    server.use(
      http.get('/api/audit/patient/:id/accessors', () =>
        HttpResponse.json({ patient_ref: 'sha256:abc', accessors: [], count: 0 }),
      ),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.type(screen.getByLabelText(/patient id/i), 'MRN-999')
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    expect(await screen.findByText(/no recorded access/i)).toBeInTheDocument()
  })

  it('surfaces a 503 when the index is unavailable', async () => {
    server.use(
      http.get('/api/audit/search', () =>
        HttpResponse.json({ detail: 'Audit index temporarily unavailable' }, { status: 503 }),
      ),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/temporarily unavailable/i)
  })

  it('reports a 403 for a non-admin', async () => {
    server.use(
      http.get('/api/audit/search', () =>
        HttpResponse.json({ detail: 'Insufficient role' }, { status: 403 }),
      ),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/do not have permission/i)
  })

  it('pages forward and back without losing the filter', async () => {
    let lastOffset = -1
    server.use(
      http.get('/api/audit/search', ({ request }) => {
        const url = new URL(request.url)
        lastOffset = Number(url.searchParams.get('offset') ?? 0)
        return HttpResponse.json({
          items: [
            {
              ts: new Date().toISOString(),
              actor_sub: `dr.page${lastOffset}`,
              method: 'GET',
              path: '/fhir/patient/{id}',
              status: 200,
              outcome: 'success',
              resource_ref: 'sha256:abc',
            },
          ],
          count: 1,
          total: 60,
          limit: 25,
          offset: lastOffset,
          since: new Date().toISOString(),
          until: new Date().toISOString(),
          subject_ref: null,
        })
      }),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    await screen.findByText('dr.page0')

    await user.click(screen.getByRole('button', { name: /^next$/i }))
    await waitFor(() => expect(lastOffset).toBe(25))
    await user.click(screen.getByRole('button', { name: /^previous$/i }))
    await waitFor(() => expect(lastOffset).toBe(0))
  })

  it('disables Previous on the first page', async () => {
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    await screen.findByRole('table', { name: /audit events/i })
    expect(screen.getByRole('button', { name: /^previous$/i })).toBeDisabled()
  })

  it('shows nothing until a search is run', () => {
    renderWithProviders(<AuditSearchPanel />, asAdmin)
    expect(screen.queryByRole('table', { name: /audit events/i })).not.toBeInTheDocument()
  })
})

describe('break-glass review', () => {
  it('shows the override and its reason on the event', async () => {
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    const events = await screen.findByRole('table', { name: /audit events/i })
    expect(within(events).getByText(/break-glass/i)).toBeInTheDocument()
    // The reason is why a reviewer is here; it must not be hidden.
    expect(
      within(events).getByText(/cardiac arrest in bay 4/i),
    ).toBeInTheDocument()
  })

  it('counts overrides per clinician in the summary', async () => {
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.type(screen.getByLabelText(/patient id/i), 'MRN-000123')
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))

    const summary = await screen.findByRole('table', {
      name: /clinicians who accessed this record/i,
    })
    const snoop = within(summary).getByText('dr.snoop').closest('tr')!
    expect(within(snoop).getByText('2')).toBeInTheDocument()
  })

  it('filters to overrides only when asked', async () => {
    let query = ''
    server.use(
      http.get('/api/audit/search', ({ request }) => {
        query = new URL(request.url).search
        return HttpResponse.json({
          items: [],
          count: 0,
          total: 0,
          limit: 25,
          offset: 0,
          since: new Date().toISOString(),
          until: new Date().toISOString(),
          subject_ref: null,
        })
      }),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByLabelText(/emergency overrides only/i))
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    await waitFor(() => expect(query).toContain('break_glass=true'))
  })

  it('does not send the filter when unchecked', async () => {
    let query = ''
    server.use(
      http.get('/api/audit/search', ({ request }) => {
        query = new URL(request.url).search
        return HttpResponse.json({
          items: [],
          count: 0,
          total: 0,
          limit: 25,
          offset: 0,
          since: new Date().toISOString(),
          until: new Date().toISOString(),
          subject_ref: null,
        })
      }),
    )
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    // Unset must mean "either", not "non-override".
    await waitFor(() => expect(query).not.toContain('break_glass'))
  })

  it('does not label ordinary access as an override', async () => {
    const { user } = renderWithProviders(<AuditSearchPanel />, asAdmin)
    await user.click(screen.getByRole('button', { name: /search audit trail/i }))
    const events = await screen.findByRole('table', { name: /audit events/i })
    const smith = within(events).getByText('dr.smith').closest('tr')!
    expect(within(smith).queryByText(/break-glass/i)).not.toBeInTheDocument()
  })
})

describe('AdminPage integration', () => {
  it('offers audit search alongside cache invalidation', async () => {
    renderWithProviders(<AdminPage />, asAdmin)
    expect(await screen.findByRole('heading', { name: /audit search/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /invalidate cache/i })).toBeInTheDocument()
  })

  it('keeps a single h1 on the page', async () => {
    renderWithProviders(<AdminPage />, asAdmin)
    expect(await screen.findAllByRole('heading', { level: 1 })).toHaveLength(1)
  })
})
