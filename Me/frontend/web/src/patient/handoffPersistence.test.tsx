/**
 * Handoff notes: server persistence.
 *
 * These lived in sessionStorage, so a clinician who closed the tab lost the
 * handoff they had just written and the incoming shift could not read it at
 * all. The behaviour worth defending now:
 *
 *   * the stored note loads for whoever opens the chart next
 *   * a failed save never silently discards what was typed
 *   * an unsent local draft is not clobbered by the stored version
 */

import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import { PatientChartDrawer } from './PatientChartDrawer'
import { saveHandoff as saveLocalDraft } from './handoffNotes'
import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'

/** Renders the drawer with a patient already open. */
function openChart(patientId = 'MRN-1') {
  return renderWithProviders(<PatientChartDrawer />, {
    token: makeToken({ roles: ['clinician'] }),
    route: `/?patient=${patientId}`,
  })
}

beforeEach(() => {
  sessionStorage.clear()
})

describe('handoff persistence', () => {
  it('loads the note saved by the previous shift', async () => {
    openChart()
    const box = await screen.findByLabelText(/sbar handoff note/i)
    await waitFor(() =>
      expect((box as HTMLTextAreaElement).value).toContain(
        'stored handoff from the previous shift',
      ),
    )
  })

  it('shows who saved it and when', async () => {
    openChart()
    expect(await screen.findByText(/last saved by/i)).toBeInTheDocument()
    expect(await screen.findByText('dr.night')).toBeInTheDocument()
  })

  it('sends the note to the server on save', async () => {
    let sent: { text?: string } | null = null
    server.use(
      http.post('/flow/handoff/:id', async ({ request }) => {
        sent = (await request.json()) as { text?: string }
        return HttpResponse.json(
          {
            ok: true,
            note: {
              patient_id: 'MRN-1',
              text: sent.text,
              author: 'test.user',
              created_at: new Date().toISOString(),
            },
          },
          { status: 201 },
        )
      }),
    )
    const { user } = openChart()
    const box = await screen.findByLabelText(/sbar handoff note/i)
    await user.clear(box)
    await user.type(box, 'Patient stable overnight')
    await user.click(screen.getByRole('button', { name: /save handoff/i }))

    await waitFor(() => expect(sent?.text).toBe('Patient stable overnight'))
    expect(await screen.findByText(/saved for the next shift/i)).toBeInTheDocument()
  })

  it('never sends an author from the client', async () => {
    /** A note that could claim to be from another clinician is worse than none. */
    let body: Record<string, unknown> = {}
    server.use(
      http.post('/flow/handoff/:id', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          {
            ok: true,
            note: {
              patient_id: 'MRN-1',
              text: 'x',
              author: 'test.user',
              created_at: new Date().toISOString(),
            },
          },
          { status: 201 },
        )
      }),
    )
    const { user } = openChart()
    await screen.findByLabelText(/sbar handoff note/i)
    await user.click(screen.getByRole('button', { name: /save handoff/i }))
    await waitFor(() => expect(Object.keys(body).length).toBeGreaterThan(0))
    expect(body).not.toHaveProperty('author')
  })

  it('keeps the draft when the save fails', async () => {
    /** The clinician's typing must not be what gets lost. */
    server.use(
      http.post('/flow/handoff/:id', () =>
        HttpResponse.json({ detail: 'Patient flow storage is temporarily unavailable' }, { status: 503 }),
      ),
    )
    const { user } = openChart()
    const box = await screen.findByLabelText(/sbar handoff note/i)
    await user.clear(box)
    await user.type(box, 'Unsent but important')
    await user.click(screen.getByRole('button', { name: /save handoff/i }))

    expect(await screen.findByText(/draft is kept in this tab/i)).toBeInTheDocument()
    // Still on screen, and still recoverable from local storage.
    expect(box).toHaveValue('Unsent but important')
    expect(sessionStorage.getItem('medicore.handoff.notes')).toContain(
      'Unsent but important',
    )
  })

  it('prefers an unsent local draft over the stored note', async () => {
    /** Reopening the tab must not silently discard work that never sent. */
    saveLocalDraft('MRN-1', 'Draft that never reached the server', 'test.user')
    openChart()
    const box = await screen.findByLabelText(/sbar handoff note/i)
    await waitFor(() => expect(box).toHaveValue('Draft that never reached the server'))
  })

  it('still shows the saved byline when a local draft wins', async () => {
    saveLocalDraft('MRN-1', 'Local draft', 'test.user')
    openChart()
    expect(await screen.findByText(/last saved by/i)).toBeInTheDocument()
  })

  it('falls back to the template when there is no stored note', async () => {
    server.use(
      http.get('/flow/handoff/:id', ({ params }) =>
        HttpResponse.json({ patient_id: params.id, note: null }),
      ),
    )
    openChart()
    const box = await screen.findByLabelText(/sbar handoff note/i)
    await waitFor(() =>
      expect((box as HTMLTextAreaElement).value).toContain('S — Situation'),
    )
  })

  it('remains usable when patient-flow is unreachable', async () => {
    server.use(
      http.get('/flow/handoff/:id', () => HttpResponse.error()),
    )
    openChart()
    const box = await screen.findByLabelText(/sbar handoff note/i)
    // The template is still offered, so a handoff can be drafted offline.
    await waitFor(() =>
      expect((box as HTMLTextAreaElement).value).toContain('S — Situation'),
    )
  })

  it('clearing drops the local draft but says saved versions are kept', async () => {
    const { user } = openChart()
    await screen.findByLabelText(/sbar handoff note/i)
    await user.click(screen.getByRole('button', { name: /^clear$/i }))
    expect(await screen.findByText(/saved versions are kept/i)).toBeInTheDocument()
  })

  it('disables the button while saving', async () => {
    server.use(
      http.post('/flow/handoff/:id', async () => {
        await new Promise((r) => setTimeout(r, 80))
        return HttpResponse.json(
          {
            ok: true,
            note: {
              patient_id: 'MRN-1',
              text: 'x',
              author: 'test.user',
              created_at: new Date().toISOString(),
            },
          },
          { status: 201 },
        )
      }),
    )
    const { user } = openChart()
    await screen.findByLabelText(/sbar handoff note/i)
    const button = screen.getByRole('button', { name: /save handoff/i })
    await user.click(button)
    await waitFor(() => expect(screen.getByRole('button', { name: /saving/i })).toBeDisabled())
  })
})
