/** Feature tests: chart drawer, worklist partition, ward board, CDS escalate. */

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import React from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { AuthProvider } from '../auth/AuthContext'
import { CdsPage, acuityFromRisk } from '../pages/CdsPage'
import { DashboardPage } from '../pages/DashboardPage'
import { groupBedsByWard, wardSummary, WardBoardPage } from '../pages/WardBoardPage'
import { partitionWorklist, waitingDepartments, WorklistPage } from '../pages/WorklistPage'
import { makeToken, renderWithProviders, seedToken } from '../test/helpers'
import { server } from '../test/server'
import { PatientChartDrawer } from './PatientChartDrawer'
import { PatientChartProvider } from './PatientChartContext'
import type { Bed, QueueItem, RiskResponse } from '../api/types'

function shell(ui: React.ReactElement, route = '/') {
  seedToken(makeToken({ sub: 'dr.smith', roles: ['clinician'] }))
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>
        <PatientChartProvider>
          {ui}
          <PatientChartDrawer />
        </PatientChartProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('partitionWorklist', () => {
  const beds: Bed[] = [
    { bed_id: 'A-001', ward: 'A', occupied: true, patient_id: 'P1' },
    { bed_id: 'A-002', ward: 'A', occupied: false, patient_id: null },
  ]
  const queue: QueueItem[] = [
    { patient_id: 'P2', acuity: 1, dept: 'ED', status: 'waiting' },
    {
      patient_id: 'P3',
      acuity: 2,
      dept: 'ED',
      status: 'in_progress',
      // claimed_by matches me
      ...( { claimed_by: 'dr.smith' } as object),
    } as QueueItem,
  ]

  it('splits waiting, claimed, and occupied beds', () => {
    const p = partitionWorklist(beds, queue, 'dr.smith')
    expect(p.waiting.map((q) => q.patient_id)).toEqual(['P2'])
    expect(p.claimedByMe.map((q) => q.patient_id)).toEqual(['P3'])
    expect(p.occupiedBeds).toHaveLength(1)
    expect(p.freeBeds).toBe(1)
  })
})

describe('groupBedsByWard', () => {
  it('groups and sorts bed ids', () => {
    const groups = groupBedsByWard([
      { bed_id: 'B-002', ward: 'B', occupied: false },
      { bed_id: 'A-002', ward: 'A', occupied: true, patient_id: 'x' },
      { bed_id: 'A-001', ward: 'A', occupied: false },
    ])
    expect(Object.keys(groups).sort()).toEqual(['A', 'B'])
    expect(groups.A.map((b) => b.bed_id)).toEqual(['A-001', 'A-002'])
    expect(wardSummary(groups.A)).toEqual({ free: 1, total: 2 })
  })
})

describe('acuityFromRisk', () => {
  const base: RiskResponse = {
    score: 0.2,
    class_label: 'low',
    news2_score: 1,
    red_flag: false,
    recommended_response: 'ok',
    disclaimer: 'd',
  }
  it('maps bands to ESI-like acuity', () => {
    expect(acuityFromRisk(base)).toBe(4)
    expect(acuityFromRisk({ ...base, class_label: 'medium', news2_score: 5 })).toBe(2)
    expect(acuityFromRisk({ ...base, class_label: 'high', news2_score: 9 })).toBe(1)
    expect(acuityFromRisk({ ...base, red_flag: true, news2_score: 0 })).toBe(1)
  })
})

describe('PatientChartDrawer', () => {
  it('opens from ?patient= and shows demographics + location', async () => {
    server.use(
      http.get('/flow/beds', () =>
        HttpResponse.json([
          { bed_id: 'A-001', ward: 'A', occupied: true, patient_id: 'p1' },
        ]),
      ),
      http.get('/flow/queue', () =>
        HttpResponse.json({
          items: [{ patient_id: 'p1', acuity: 2, dept: 'ED', status: 'waiting' }],
          count: 1,
          total: 1,
        }),
      ),
    )

    shell(
      <Routes>
        <Route path="/" element={<div>home</div>} />
      </Routes>,
      '/?patient=p1',
    )

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()
    expect(await screen.findByText(/Ada Lovelace/i)).toBeInTheDocument()
    expect(screen.getByText(/A-001/)).toBeInTheDocument()
    expect(dialog.textContent).toMatch(/ESI\s*2/)
    expect(screen.getByText(/Heart rate/i)).toBeInTheDocument()
  })

  it('closes on Escape and removes the patient query', async () => {
    const user = userEvent.setup()
    shell(
      <Routes>
        <Route path="/" element={<div>home</div>} />
      </Routes>,
      '/?patient=p1',
    )
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})

describe('WorklistPage', () => {
  it('renders waiting and occupied sections', async () => {
    server.use(
      http.get('/flow/beds', () =>
        HttpResponse.json([
          { bed_id: 'A-001', ward: 'A', occupied: true, patient_id: 'MRN-8' },
          { bed_id: 'A-002', ward: 'A', occupied: false, patient_id: null },
        ]),
      ),
      http.get(/\/flow\/queue\/?(\?.*)?$/, ({ request }) => {
        if (new URL(request.url).pathname.includes('/claim')) {
          return new HttpResponse(null, { status: 405 })
        }
        return HttpResponse.json({
          items: [
            { patient_id: 'pat-1', acuity: 1, dept: 'ED', status: 'waiting' },
            {
              patient_id: 'pat-me',
              acuity: 2,
              dept: 'ED',
              status: 'in_progress',
              claimed_by: 'test.user',
            },
          ],
          count: 2,
          total: 2,
        })
      }),
    )

    renderWithProviders(<WorklistPage />, {
      token: makeToken({ sub: 'test.user', roles: ['clinician'] }),
    })
    expect(await screen.findByRole('heading', { name: /my patients/i })).toBeInTheDocument()
    expect(await screen.findByText('pat-1')).toBeInTheDocument()
    expect(screen.getByText('MRN-8')).toBeInTheDocument()
    expect(screen.getByText('pat-me')).toBeInTheDocument()
  })
})

describe('WardBoardPage', () => {
  it('groups beds into ward columns', async () => {
    server.use(
      http.get('/flow/beds', () =>
        HttpResponse.json([
          { bed_id: 'A-001', ward: 'A', occupied: false, patient_id: null },
          { bed_id: 'ICU-001', ward: 'ICU', occupied: true, patient_id: 'P9' },
        ]),
      ),
    )
    renderWithProviders(<WardBoardPage />)
    expect(await screen.findByRole('heading', { name: /ward board/i })).toBeInTheDocument()
    expect(screen.getByText(/Ward A/i)).toBeInTheDocument()
    expect(screen.getByText(/Ward ICU/i)).toBeInTheDocument()
    expect(screen.getByText('P9')).toBeInTheDocument()
  })
})

describe('observationTrends', () => {
  it('extracts numeric values and bar widths', async () => {
    const { extractObservationPoints, barWidthPercent, observationNumericValue } =
      await import('./observationTrends')
    const obs = [
      {
        resourceType: 'Observation',
        id: '1',
        code: { text: 'HR' },
        valueQuantity: { value: 60, unit: '/min' },
      },
      {
        resourceType: 'Observation',
        id: '2',
        code: { text: 'HR' },
        valueQuantity: { value: 100, unit: '/min' },
      },
      { resourceType: 'Observation', id: '3', code: { text: 'note' }, valueString: 'alert' },
    ]
    const points = extractObservationPoints(obs)
    expect(points).toHaveLength(2)
    expect(points[0].value).toBe(60)
    expect(observationNumericValue(obs[2])).toBeNull()
    expect(barWidthPercent(60, [60, 100])).toBe(0)
    expect(barWidthPercent(100, [60, 100])).toBe(100)
    expect(barWidthPercent(80, [80, 80])).toBe(50)
  })
})

describe('recentPatients', () => {
  it('remembers ids newest-first and caps length', async () => {
    const { rememberPatient, loadRecentPatients, clearRecentPatients } = await import(
      './recentPatients'
    )
    clearRecentPatients()
    rememberPatient('A', 1)
    rememberPatient('B', 2)
    rememberPatient('A', 3)
    const list = loadRecentPatients()
    expect(list.map((p) => p.id)).toEqual(['A', 'B'])
    expect(list[0].viewedAt).toBe(3)
  })
})

describe('Worklist actions (API wiring)', () => {
  it('claimNext posts dept and returns the claimed item', async () => {
    let claimedDept: string | null = null
    server.use(
      http.post(/\/flow\/queue\/claim/, ({ request }) => {
        claimedDept = new URL(request.url).searchParams.get('dept')
        return HttpResponse.json({
          ok: true,
          item: {
            patient_id: 'claimed-1',
            acuity: 1,
            dept: claimedDept,
            status: 'in_progress',
            claimed_by: 'dr.smith',
          },
        })
      }),
    )
    document.cookie = 'medicore_session=test-session; path=/'
    const { api } = await import('../api/client')
    const res = await api.claimNext('ED')
    expect(claimedDept).toBe('ED')
    expect(res.item.patient_id).toBe('claimed-1')
    expect(res.item.status).toBe('in_progress')
  })

  it('completeQueue posts the patient id path', async () => {
    let completedId: string | null = null
    server.use(
      http.post(/\/flow\/queue\/[^/]+\/complete/, ({ request }) => {
        const parts = new URL(request.url).pathname.split('/').filter(Boolean)
        completedId = parts[parts.length - 2] ?? null
        return HttpResponse.json({
          ok: true,
          item: { patient_id: completedId, acuity: 2, dept: 'ED', status: 'completed' },
        })
      }),
    )
    document.cookie = 'medicore_session=test-session; path=/'
    const { api } = await import('../api/client')
    const res = await api.completeQueue('pat-me')
    expect(completedId).toBe('pat-me')
    expect(res.item.status).toBe('completed')
  })

  it('waitingDepartments lists unique depts', () => {
    expect(
      waitingDepartments([
        { patient_id: 'a', acuity: 1, dept: 'ED', status: 'waiting' },
        { patient_id: 'b', acuity: 2, dept: 'ICU', status: 'waiting' },
        { patient_id: 'c', acuity: 3, dept: 'ED', status: 'in_progress' },
      ]),
    ).toEqual(['ED', 'ICU'])
  })
})

describe('handoffNotes', () => {
  it('saves and loads SBAR drafts per patient', async () => {
    const { saveHandoff, loadHandoff, clearHandoff, sbarTemplate } = await import(
      './handoffNotes'
    )
    clearHandoff('P1')
    const tpl = sbarTemplate('P1')
    expect(tpl).toMatch(/Situation/)
    saveHandoff('P1', 'S — chest pain\nR — admit', 'dr.a', 100)
    const loaded = loadHandoff('P1')
    expect(loaded?.text).toContain('chest pain')
    expect(loaded?.author).toBe('dr.a')
    clearHandoff('P1')
    expect(loadHandoff('P1')).toBeNull()
  })
})

describe('Idempotency-Key on writes', () => {
  it('sends Idempotency-Key on enqueue and claim', async () => {
    const seen: Record<string, string | null> = {}
    server.use(
      http.post('/flow/queue', ({ request }) => {
        if (new URL(request.url).pathname.includes('/claim')) {
          return new HttpResponse(null, { status: 405 })
        }
        seen.enqueue = request.headers.get('idempotency-key')
        return HttpResponse.json({ ok: true, id: 'p' }, { status: 201 })
      }),
      http.post(/\/flow\/queue\/claim/, ({ request }) => {
        seen.claim = request.headers.get('idempotency-key')
        return HttpResponse.json({
          ok: true,
          item: { patient_id: 'c1', acuity: 1, dept: 'ED', status: 'in_progress' },
        })
      }),
      http.patch(/\/flow\/beds\//, ({ request }) => {
        seen.bed = request.headers.get('idempotency-key')
        return HttpResponse.json({
          bed_id: 'A-001',
          ward: 'A',
          occupied: true,
          patient_id: 'p',
        })
      }),
    )
    document.cookie = 'medicore_session=test-session; path=/'
    const { api } = await import('../api/client')
    await api.enqueue({ patient_id: 'p', acuity: 3, dept: 'ED' }, null, undefined, 'key-enq-1')
    await api.claimNext('ED', null, undefined, 'key-claim-1')
    await api.setBedOccupancy(
      'A-001',
      { occupied: true, patient_id: 'p', expected_occupied: false },
      null,
      undefined,
      'key-bed-1',
    )
    expect(seen.enqueue).toBe('key-enq-1')
    expect(seen.claim).toBe('key-claim-1')
    expect(seen.bed).toBe('key-bed-1')
  })
})

describe('computeCensus', () => {
  it('counts free beds and queue statuses', async () => {
    const { computeCensus } = await import('../pages/DashboardPage')
    const stats = computeCensus(
      [
        { bed_id: 'A-001', ward: 'A', occupied: true, patient_id: 'p' },
        { bed_id: 'A-002', ward: 'A', occupied: false, patient_id: null },
        { bed_id: 'B-001', ward: 'B', occupied: false, patient_id: null },
      ],
      {
        items: [
          { patient_id: 'w1', acuity: 1, dept: 'ED', status: 'waiting' },
          { patient_id: 'w2', acuity: 2, dept: 'ED', status: 'waiting' },
          {
            patient_id: 'c1',
            acuity: 1,
            dept: 'ED',
            status: 'in_progress',
            claimed_by: 'dr.a',
          },
        ],
        count: 3,
        total: 3,
      },
    )
    expect(stats).toEqual({
      freeBeds: 2,
      totalBeds: 3,
      waiting: 2,
      inProgress: 1,
    })
  })
})


describe('PatientSearch visibility', () => {
  it('renders the search field for clinicians after bootstrap', async () => {
    const user = userEvent.setup()
    const { PatientSearch } = await import('../ui/PatientSearch')
    seedToken(makeToken({ roles: ['clinician'] }))
    render(
      <MemoryRouter>
        <AuthProvider>
          <PatientChartProvider>
            <PatientSearch />
            <PatientChartDrawer />
          </PatientChartProvider>
        </AuthProvider>
      </MemoryRouter>,
    )
    const input = await screen.findByPlaceholderText(/patient id \/ mrn/i)
    await user.type(input, 'MRN-42')
    await user.click(screen.getByRole('button', { name: /open chart/i }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('MRN-42')).toBeInTheDocument()
  })
})

describe('Ward board assign', () => {
  it('assigns a free bed via prompt', async () => {
    const user = userEvent.setup()
    let assigned: unknown = null
    server.use(
      http.get('/flow/beds', () =>
        HttpResponse.json([
          { bed_id: 'A-001', ward: 'A', occupied: false, patient_id: null },
        ]),
      ),
      http.patch(/\/flow\/beds\//, async ({ request }) => {
        assigned = await request.json()
        return HttpResponse.json({
          bed_id: 'A-001',
          ward: 'A',
          occupied: true,
          patient_id: 'P-NEW',
        })
      }),
    )
    vi.spyOn(window, 'prompt').mockReturnValue('P-NEW')
    renderWithProviders(<WardBoardPage />)
    expect(await screen.findByText('A-001')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /^assign$/i }))
    await waitFor(() =>
      expect(assigned).toMatchObject({
        occupied: true,
        patient_id: 'P-NEW',
        expected_occupied: false,
      }),
    )
    expect(await screen.findByText(/Assigned P-NEW/i)).toBeInTheDocument()
  })
})

describe('CdsPage escalate', () => {
  it('enqueues the patient after a high-risk score', async () => {
    const user = userEvent.setup()
    let enqueued: unknown = null
    server.use(
      http.post('/cds/risk', () =>
        HttpResponse.json({
          score: 0.9,
          class_label: 'high',
          news2_score: 9,
          red_flag: true,
          recommended_response: 'Emergency assessment',
          disclaimer: 'NEWS2',
        }),
      ),
      http.post('/flow/queue', async ({ request }) => {
        enqueued = await request.json()
        return HttpResponse.json({ ok: true, id: 'MRN-99' }, { status: 201 })
      }),
    )

    renderWithProviders(<CdsPage />, { route: '/cds?patient=MRN-99' })
    await user.click(screen.getByRole('button', { name: /calculate risk/i }))
    expect(await screen.findByText('high')).toBeInTheDocument()

    const escalateBtn = await screen.findByRole('button', { name: /escalate to triage/i })
    await user.click(escalateBtn)

    await waitFor(() =>
      expect(enqueued).toEqual({ patient_id: 'MRN-99', acuity: 1, dept: 'ED' }),
    )
    expect(await screen.findByText(/Added MRN-99/i)).toBeInTheDocument()
  })
})
