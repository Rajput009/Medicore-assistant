/** Integration tests: real components + MSW-backed HTTP, user-driven. */

import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'
import { AdminPage } from './AdminPage'
import { CdsPage, riskTone, validateVitals } from './CdsPage'
import { DashboardPage } from './DashboardPage'
import { bundleResources, FhirPage, summariseResource } from './FhirPage'
import { LoginPage } from './LoginPage'
import { acuityTone, PatientFlowPage } from './PatientFlowPage'

// ---------------------------------------------------------------- Login

describe('LoginPage', () => {
  it('validates empty fields without calling the server', async () => {
    server.use(
      http.post('/auth/login', () => {
        throw new Error('should not be called')
      }),
    )
    const { user } = renderWithProviders(<LoginPage />)
    await user.click(screen.getByRole('button', { name: /^sign in$/i }))
    expect(await screen.findByText('Username is required.')).toBeInTheDocument()
    expect(screen.getByText('Password is required.')).toBeInTheDocument()
  })

  it('shows an error for bad credentials', async () => {
    const { user } = renderWithProviders(<LoginPage />)
    await user.type(screen.getByLabelText(/username/i), 'dr.smith')
    await user.type(screen.getByLabelText(/password/i), 'nope')
    await user.click(screen.getByRole('button', { name: /^sign in$/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/incorrect username or password/i)
  })

  it('marks invalid fields with aria-invalid for assistive tech', async () => {
    const { user } = renderWithProviders(<LoginPage />)
    await user.click(screen.getByRole('button', { name: /^sign in$/i }))
    await waitFor(() =>
      expect(screen.getByLabelText(/username/i)).toHaveAttribute('aria-invalid', 'true'),
    )
  })

  it('offers an SSO link', () => {
    renderWithProviders(<LoginPage />)
    expect(screen.getByRole('link', { name: /sign in with sso/i })).toHaveAttribute(
      'href',
      '/auth/oidc/login',
    )
  })

  it('trims whitespace from the username before submitting', async () => {
    let sent: { username?: string } = {}
    server.use(
      http.post('/auth/login', async ({ request }) => {
        sent = (await request.json()) as { username?: string }
        return HttpResponse.json({ access_token: makeToken(), token_type: 'bearer' })
      }),
    )
    const { user } = renderWithProviders(<LoginPage />)
    await user.type(screen.getByLabelText(/username/i), '  dr.smith  ')
    await user.type(screen.getByLabelText(/password/i), 'pw')
    await user.click(screen.getByRole('button', { name: /^sign in$/i }))
    await waitFor(() => expect(sent.username).toBe('dr.smith'))
  })
})

// ------------------------------------------------------------ Dashboard

describe('DashboardPage', () => {
  it('renders a card per service and marks healthy ones', async () => {
    renderWithProviders(<DashboardPage />, { token: makeToken() })
    expect(await screen.findByRole('heading', { name: 'Gateway' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('healthy')).toHaveLength(4))
  })

  it('shows the environment from the gateway in the page header', async () => {
    renderWithProviders(<DashboardPage />, { token: makeToken() })
    const header = await screen.findByRole('heading', { name: /system overview/i })
    // Scope to the header: "test" also appears in each service card.
    const banner = header.closest('header')!
    await waitFor(() => expect(within(banner).getByText('test')).toBeInTheDocument())
  })

  it('marks an unreachable service without breaking the others', async () => {
    server.use(http.get('/cds/health', () => HttpResponse.error()))
    renderWithProviders(<DashboardPage />, { token: makeToken() })
    expect(await screen.findByText('unreachable')).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('healthy')).toHaveLength(3))
  })

  it('recovers when a failing service comes back after refresh', async () => {
    let fail = true
    server.use(
      http.get('/cds/health', () =>
        fail
          ? HttpResponse.error()
          : HttpResponse.json({ status: 'ok', service: 'cds', env: 'test' }),
      ),
    )
    const { user } = renderWithProviders(<DashboardPage />, { token: makeToken() })
    expect(await screen.findByText('unreachable')).toBeInTheDocument()

    fail = false
    const cdsCard = screen.getByRole('heading', { name: 'CDS' }).closest('section')!
    await user.click(within(cdsCard).getByRole('button', { name: /refresh/i }))
    await waitFor(() => expect(screen.queryByText('unreachable')).not.toBeInTheDocument())
  })
})

// ----------------------------------------------------------------- FHIR

describe('FhirPage', () => {
  it('searches and renders a result table', async () => {
    const { user } = renderWithProviders(<FhirPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /^search$/i }))

    expect(await screen.findByText('Ada Lovelace')).toBeInTheDocument()
    // Falls back to given+family when `text` is absent.
    expect(screen.getByText('Alan Turing')).toBeInTheDocument()
    expect(screen.getByText(/results \(2\)/i)).toBeInTheDocument()
  })

  it('shows an empty state when the bundle has no entries', async () => {
    server.use(
      http.get('/api/fhir/patient/search', () => HttpResponse.json({ resourceType: 'Bundle' })),
    )
    const { user } = renderWithProviders(<FhirPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /^search$/i }))
    expect(await screen.findByText(/no matching resources/i)).toBeInTheDocument()
  })

  it('surfaces a 403 as a permission message', async () => {
    server.use(
      http.get('/api/fhir/patient/search', () =>
        HttpResponse.json({ detail: 'insufficient role' }, { status: 403 }),
      ),
    )
    const { user } = renderWithProviders(<FhirPage />, { token: makeToken({ roles: ['viewer'] }) })
    await user.click(screen.getByRole('button', { name: /^search$/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/do not have permission/i)
  })

  it('surfaces an upstream 502 distinctly', async () => {
    server.use(
      http.get('/api/fhir/patient/search', () =>
        HttpResponse.json({ detail: 'upstream down' }, { status: 502 }),
      ),
    )
    const { user } = renderWithProviders(<FhirPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /^search$/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/upstream FHIR server/i)
  })

  it('requires an id in read mode', async () => {
    const { user } = renderWithProviders(<FhirPage />, { token: makeToken() })
    await user.selectOptions(screen.getByLabelText(/mode/i), 'read')
    await user.click(screen.getByRole('button', { name: /fetch/i }))
    expect(await screen.findByText('Enter a resource id.')).toBeInTheDocument()
  })

  it('reads a single resource by id', async () => {
    const { user } = renderWithProviders(<FhirPage />, { token: makeToken() })
    await user.selectOptions(screen.getByLabelText(/mode/i), 'read')
    await user.type(screen.getByLabelText(/resource id/i), 'p-42')
    await user.click(screen.getByRole('button', { name: /fetch/i }))
    expect(await screen.findByLabelText('FHIR resource')).toHaveTextContent('p-42')
  })

  it('sends the patient and extra search parameters', async () => {
    let search = ''
    server.use(
      http.get('/api/fhir/observation/search', ({ request }) => {
        search = new URL(request.url).search
        return HttpResponse.json({ resourceType: 'Bundle', entry: [] })
      }),
    )
    const { user } = renderWithProviders(<FhirPage />, { token: makeToken() })
    await user.selectOptions(screen.getByLabelText(/resource type/i), 'Observation')
    await user.type(screen.getByLabelText(/patient id/i), '123')
    await user.type(screen.getByLabelText(/extra parameter/i), 'code')
    await user.type(screen.getByLabelText(/^value$/i), '789-8')
    await user.click(screen.getByRole('button', { name: /^search$/i }))
    await waitFor(() => expect(search).toBe('?patient=123&code=789-8'))
  })

  it('ignores an extra parameter with a key but no value', async () => {
    let search = 'unset'
    server.use(
      http.get('/api/fhir/patient/search', ({ request }) => {
        search = new URL(request.url).search
        return HttpResponse.json({ resourceType: 'Bundle', entry: [] })
      }),
    )
    const { user } = renderWithProviders(<FhirPage />, { token: makeToken() })
    await user.type(screen.getByLabelText(/extra parameter/i), 'code')
    await user.click(screen.getByRole('button', { name: /^search$/i }))
    await waitFor(() => expect(search).toBe(''))
  })

  it('clears results', async () => {
    const { user } = renderWithProviders(<FhirPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /^search$/i }))
    expect(await screen.findByText('Ada Lovelace')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /clear/i }))
    expect(screen.queryByText('Ada Lovelace')).not.toBeInTheDocument()
  })
})

describe('FHIR helpers', () => {
  it('summarises using name.text, then given+family, then code, then id', () => {
    expect(summariseResource({ resourceType: 'Patient', name: [{ text: 'A B' }] })).toBe('A B')
    expect(
      summariseResource({ resourceType: 'Patient', name: [{ given: ['A'], family: 'B' }] }),
    ).toBe('A B')
    expect(summariseResource({ resourceType: 'Observation', code: { text: 'HR' } })).toBe('HR')
    expect(
      summariseResource({ resourceType: 'Observation', code: { coding: [{ display: 'Pulse' }] } }),
    ).toBe('Pulse')
    expect(summariseResource({ resourceType: 'Encounter', id: 'e1' })).toBe('Encounter/e1')
    expect(summariseResource({ resourceType: 'Encounter' })).toBe('Encounter')
  })

  it('handles bundles with missing or malformed entries', () => {
    expect(bundleResources(undefined)).toEqual([])
    expect(bundleResources({ resourceType: 'Bundle' })).toEqual([])
    expect(
      bundleResources({
        resourceType: 'Bundle',
        entry: [{}, { resource: { resourceType: 'Patient' } }],
      }),
    ).toHaveLength(1)
  })
})

// --------------------------------------------------------- Patient flow

describe('PatientFlowPage', () => {
  it('lists beds with an availability summary', async () => {
    renderWithProviders(<PatientFlowPage />, { token: makeToken() })
    expect(await screen.findByText(/1 of 2 available/i)).toBeInTheDocument()
  })

  it('assigns a bed with a patient id and a concurrency check', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      http.patch('/flow/beds/:id', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({
          bed_id: 'A-001',
          ward: 'A',
          occupied: true,
          patient_id: 'MRN-42',
        })
      }),
    )
    vi.spyOn(window, 'prompt').mockReturnValue('MRN-42')

    const { user } = renderWithProviders(<PatientFlowPage />, { token: makeToken() })
    await user.click(await screen.findByRole('button', { name: /assign/i }))

    await waitFor(() =>
      expect(body).toEqual({
        occupied: true,
        patient_id: 'MRN-42',
        expected_occupied: false,
      }),
    )
  })

  it('does not call the server when the assign prompt is cancelled', async () => {
    let called = false
    server.use(
      http.patch('/flow/beds/:id', () => {
        called = true
        return HttpResponse.json({ bed_id: 'A-001', ward: 'A', occupied: true })
      }),
    )
    vi.spyOn(window, 'prompt').mockReturnValue(null)

    const { user } = renderWithProviders(<PatientFlowPage />, { token: makeToken() })
    await user.click(await screen.findByRole('button', { name: /assign/i }))
    expect(called).toBe(false)
  })

  it('surfaces a 409 when another clinician took the bed first', async () => {
    server.use(
      http.patch('/flow/beds/:id', () =>
        HttpResponse.json(
          { detail: 'Bed was modified by another user; reload and retry' },
          { status: 409 },
        ),
      ),
    )
    vi.spyOn(window, 'prompt').mockReturnValue('MRN-42')

    const { user } = renderWithProviders(<PatientFlowPage />, { token: makeToken() })
    await user.click(await screen.findByRole('button', { name: /assign/i }))
    expect(await screen.findByText(/modified by another user/i)).toBeInTheDocument()
  })

  it('renders the triage queue ordered as returned', async () => {
    renderWithProviders(<PatientFlowPage />, { token: makeToken() })
    expect(await screen.findByText('pat-1')).toBeInTheDocument()
    expect(screen.getByText('ESI 1')).toBeInTheDocument()
  })

  it('shows an empty queue state', async () => {
    server.use(http.get('/flow/queue', () => HttpResponse.json({ items: [], count: 0 })))
    renderWithProviders(<PatientFlowPage />, { token: makeToken() })
    expect(await screen.findByText(/queue is empty/i)).toBeInTheDocument()
  })

  it('reports a 503 when Mongo is unavailable', async () => {
    server.use(
      http.get('/flow/queue', () =>
        HttpResponse.json({ detail: 'queue unavailable' }, { status: 503 }),
      ),
    )
    renderWithProviders(<PatientFlowPage />, { token: makeToken() })
    // Clinician-facing wording, not the raw driver/server string.
    expect(await screen.findByText(/temporarily unavailable/i)).toBeInTheDocument()
  })

  it('validates the enqueue form client-side', async () => {
    const { user } = renderWithProviders(<PatientFlowPage />, { token: makeToken() })
    // Department defaults to ED; only patient id is empty.
    await user.click(await screen.findByRole('button', { name: /add to queue/i }))
    expect(await screen.findByText('Patient id is required.')).toBeInTheDocument()
  })

  it('submits a valid enqueue and clears the form', async () => {
    let body: unknown
    server.use(
      http.post('/flow/queue', async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ ok: true, id: 'q9' }, { status: 201 })
      }),
    )
    const { user } = renderWithProviders(<PatientFlowPage />, { token: makeToken() })
    await user.type(await screen.findByLabelText(/patient id/i), 'pat-99')
    await user.selectOptions(screen.getByLabelText(/acuity/i), '2')
    const dept = screen.getByLabelText(/department/i, { selector: '#enqueue-dept' })
    await user.clear(dept)
    await user.type(dept, 'ICU')
    await user.click(screen.getByRole('button', { name: /add to queue/i }))

    await waitFor(() =>
      expect(body).toEqual({ patient_id: 'pat-99', acuity: 2, dept: 'ICU' }),
    )
    expect(await screen.findByText(/patient added to queue/i)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText(/patient id/i)).toHaveValue(''))
  })

  it('colour-codes acuity by urgency', () => {
    expect(acuityTone(1)).toBe('err')
    expect(acuityTone(2)).toBe('err')
    expect(acuityTone(3)).toBe('warn')
    expect(acuityTone(5)).toBe('neutral')
  })
})

// ------------------------------------------------------------------ CDS

describe('CdsPage', () => {
  it('scores a healthy full vital set as low risk', async () => {
    const { user } = renderWithProviders(<CdsPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    expect(await screen.findByText('low')).toBeInTheDocument()
  })

  it('scores critical vitals as high risk', async () => {
    const { user } = renderWithProviders(<CdsPage />, { token: makeToken() })
    const rr = screen.getByLabelText(/respiratory rate/i)
    await user.clear(rr)
    await user.type(rr, '30')
    const sbp = screen.getByLabelText(/systolic/i)
    await user.clear(sbp)
    await user.type(sbp, '80')
    const spo2 = screen.getByLabelText(/oxygen saturation/i)
    await user.clear(spo2)
    await user.type(spo2, '85')
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    expect(await screen.findByText('high')).toBeInTheDocument()
  })

  it('collects respiratory rate and temperature, which carry real NEWS2 weight', () => {
    renderWithProviders(<CdsPage />, { token: makeToken() })
    // The old 3-vital form assumed these were normal, so a tachypnoeic
    // patient could score "low".
    expect(screen.getByLabelText(/respiratory rate/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/temperature/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/consciousness/i)).toBeInTheDocument()
  })

  it('sends the full parameter set to /news2', async () => {
    let received: Record<string, unknown> | null = null
    server.use(
      http.post('/cds/news2', async ({ request }) => {
        received = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({
          score: 0,
          band: 'low',
          red_flag: false,
          recommended_response: 'Routine monitoring.',
          monitoring_frequency: '12 hourly',
          parameters: [],
          disclaimer: 'aid',
        })
      }),
    )
    const { user } = renderWithProviders(<CdsPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    await screen.findByText(/recommended response/i)
    expect(received).toMatchObject({
      respiratory_rate: 16,
      spo2: 98,
      temperature: 37,
      systolic_bp: 120,
      pulse: 72,
      consciousness: 'A',
    })
  })

  it('scores supplemental oxygen, which adds 2 points', async () => {
    let received: Record<string, unknown> | null = null
    server.use(
      http.post('/cds/news2', async ({ request }) => {
        received = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({
          score: 2,
          band: 'low-medium',
          red_flag: false,
          recommended_response: 'Nurse review.',
          monitoring_frequency: '6 hourly',
          parameters: [],
          disclaimer: 'aid',
        })
      }),
    )
    const { user } = renderWithProviders(<CdsPage />, { token: makeToken() })
    await user.click(screen.getByLabelText(/supplemental oxygen/i))
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    await screen.findByText(/recommended response/i)
    expect(received).toMatchObject({ on_supplemental_oxygen: true })
  })

  it('rejects out-of-range vitals before calling the server', async () => {
    server.use(
      http.post('/cds/news2', () => {
        throw new Error('should not be called')
      }),
    )
    const { user } = renderWithProviders(<CdsPage />, { token: makeToken() })
    const spo2 = screen.getByLabelText(/oxygen saturation/i)
    await user.clear(spo2)
    await user.type(spo2, '400')
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    expect(await screen.findByText(/must be between 1 and 100/i)).toBeInTheDocument()
  })

  it('states the standard used and its clinical limits', () => {
    renderWithProviders(<CdsPage />, { token: makeToken() })
    // "NEWS2" also appears on the submit button, so match the disclaimer text.
    const banner = screen.getByText(/Royal College of Physicians/i)
    expect(banner).toBeInTheDocument()
    expect(banner).toHaveTextContent(/not a diagnosis/i)
    expect(banner).toHaveTextContent(/not validated for children or pregnancy/i)
  })

  it('shows the NEWS2 aggregate and the recommended response', async () => {
    const { user } = renderWithProviders(<CdsPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    expect(await screen.findByText(/NEWS2 aggregate/i)).toBeInTheDocument()
    expect(screen.getByText(/recommended response/i)).toBeInTheDocument()
  })

  it('shows the per-parameter breakdown that explains the score', async () => {
    const { user } = renderWithProviders(<CdsPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    // An unexplained aggregate is not clinically actionable.
    expect(await screen.findByText(/score breakdown by parameter/i)).toBeInTheDocument()
  })

  it('escalates visibly when a single parameter is critical', async () => {
    server.use(
      http.post('/cds/news2', () =>
        HttpResponse.json({
          score: 3,
          band: 'medium',
          red_flag: true,
          recommended_response: 'Urgent review by a clinician.',
          monitoring_frequency: '1 hourly',
          parameters: [],
          disclaimer: 'NEWS2 is a track-and-trigger aid.',
        }),
      ),
    )
    const { user } = renderWithProviders(<CdsPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /calculate news2/i }))
    // A low aggregate must not hide a severely abnormal single parameter.
    expect(await screen.findByText(/red flag/i)).toBeInTheDocument()
  })

  it('validateVitals covers empty, non-numeric and out-of-range input', () => {
    const base = {
      respiratory_rate: '16',
      spo2: '98',
      temperature: '37',
      systolic_bp: '120',
      pulse: '72',
    }
    expect(validateVitals({ ...base, pulse: '' }).pulse).toMatch(/required/)
    expect(validateVitals({ ...base, pulse: 'abc' }).pulse).toMatch(/must be a number/)
    expect(validateVitals({ ...base, pulse: '999' }).pulse).toMatch(/between/)
    expect(validateVitals(base)).toEqual({})
  })

  it('maps NEWS2 bands to tones', () => {
    expect(riskTone('low')).toBe('ok')
    expect(riskTone('medium')).toBe('warn')
    expect(riskTone('high')).toBe('err')
  })
})

// ---------------------------------------------------------------- Admin

describe('AdminPage', () => {
  it('requires confirmation before clearing the cache', async () => {
    let called = false
    server.use(
      http.delete('/api/cache/:resource', () => {
        called = true
        return HttpResponse.json({ status: 'ok', resource: 'Patient', patient: null, deleted: 2 })
      }),
    )
    const { user } = renderWithProviders(<AdminPage />, { token: makeToken({ roles: ['admin'] }) })

    await user.click(screen.getByRole('button', { name: /clear cache/i }))
    expect(called).toBe(false)
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /yes, clear cache/i }))
    await waitFor(() => expect(called).toBe(true))
    expect(await screen.findByText(/cleared 2 cached entries/i)).toBeInTheDocument()
  })

  it('can be cancelled without calling the server', async () => {
    server.use(
      http.delete('/api/cache/:resource', () => {
        throw new Error('should not be called')
      }),
    )
    const { user } = renderWithProviders(<AdminPage />, { token: makeToken({ roles: ['admin'] }) })
    await user.click(screen.getByRole('button', { name: /clear cache/i }))
    await user.click(screen.getByRole('button', { name: /cancel/i }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('scopes invalidation to a patient when one is given', async () => {
    let search = ''
    server.use(
      http.delete('/api/cache/:resource', ({ request }) => {
        search = new URL(request.url).search
        return HttpResponse.json({ status: 'ok', resource: 'Observation', patient: '7', deleted: 1 })
      }),
    )
    const { user } = renderWithProviders(<AdminPage />, { token: makeToken({ roles: ['admin'] }) })
    await user.selectOptions(screen.getByLabelText(/resource type/i), 'Observation')
    await user.type(screen.getByLabelText(/patient id/i), '7')
    await user.click(screen.getByRole('button', { name: /clear cache/i }))
    await user.click(screen.getByRole('button', { name: /yes, clear cache/i }))
    await waitFor(() => expect(search).toBe('?patient=7'))
    expect(await screen.findByText(/cleared 1 cached entry/i)).toBeInTheDocument()
  })

  it('reports a 403 for a non-admin', async () => {
    server.use(
      http.delete('/api/cache/:resource', () =>
        HttpResponse.json({ detail: 'Admin role required' }, { status: 403 }),
      ),
    )
    const { user } = renderWithProviders(<AdminPage />, { token: makeToken() })
    await user.click(screen.getByRole('button', { name: /clear cache/i }))
    await user.click(screen.getByRole('button', { name: /yes, clear cache/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/do not have permission/i)
  })
})
