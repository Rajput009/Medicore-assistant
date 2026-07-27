/**
 * Selector contract for the Playwright specs.
 *
 * The browser suite cannot run in every environment (the Playwright browser
 * CDN is unreachable from some sandboxes), so a spec can sit there for weeks
 * typechecking cleanly while silently pointing at an element that no longer
 * exists. Typechecking proves syntax, not that `getByLabel(/sbar handoff
 * note/i)` still matches anything.
 *
 * These tests assert the *same* accessible names in jsdom, where they do run
 * on every push. They are a canary, not a duplicate: if a refactor renames a
 * label or drops a role, this fails immediately instead of waiting for the
 * next environment that happens to have a browser.
 *
 * Keep in sync with e2e/clinical.spec.ts.
 */

import { screen, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { PatientChartDrawer } from './patient/PatientChartDrawer'
import { visibleNavItems } from './ui/AppShell'
import { makeToken, renderWithProviders } from './test/helpers'

const asClinician = { token: makeToken({ roles: ['clinician', 'admin'] }) }

function openChart() {
  return renderWithProviders(<PatientChartDrawer />, {
    ...asClinician,
    route: '/?patient=p1',
  })
}

describe('selectors used by e2e/clinical.spec.ts', () => {
  it('the chart drawer exposes the dialog role the spec waits on', async () => {
    openChart()
    // Caught two real bugs in the spec: it originally waited for the
    // `complementary` role, and then for the name "Patient chart" — but
    // aria-labelledby wins over aria-label, so the accessible name is the
    // patient title, not the static string.
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('the handoff textarea keeps its accessible name', async () => {
    openChart()
    expect(await screen.findByLabelText(/sbar handoff note/i)).toBeInTheDocument()
  })

  it('the handoff save button keeps its name', async () => {
    openChart()
    expect(await screen.findByRole('button', { name: /save handoff/i })).toBeInTheDocument()
  })

  it('the assistant question input keeps its accessible name', async () => {
    openChart()
    expect(
      await screen.findByLabelText(/question about this patient/i),
    ).toBeInTheDocument()
  })

  it('the assistant ask button keeps its name', async () => {
    openChart()
    expect(await screen.findByRole('button', { name: /^ask$/i })).toBeInTheDocument()
  })

  it('the example-question button text matches the spec', async () => {
    openChart()
    expect(
      await screen.findByRole('button', { name: /what allergies are recorded\?/i }),
    ).toBeInTheDocument()
  })

  it('the allergy-failure wording the spec asserts still exists', async () => {
    // This string is a safety control, not copy: it is what stops a failed
    // lookup reading as "no known allergies".
    const { server } = await import('./test/server')
    const { http, HttpResponse } = await import('msw')
    server.use(
      http.get('/api/fhir/allergyintolerance/search', () => HttpResponse.error()),
    )
    openChart()
    await waitFor(
      async () =>
        expect(await screen.findByText(/allergy list unavailable/i)).toBeInTheDocument(),
      { timeout: 3000 },
    )
  })

  it('the navigation labels the spec clicks still exist', () => {
    // Asserted against the nav model rather than a render: AppShell reads the
    // signed-in user from context, and a bare render has no session, so the
    // privileged links are correctly absent. The spec clicks these as an
    // authenticated clinician.
    const labels = visibleNavItems(['clinician', 'admin']).map((i) => i.label)
    expect(labels).toContain('My patients')
    expect(labels).toContain('Ward board')
  })
})
