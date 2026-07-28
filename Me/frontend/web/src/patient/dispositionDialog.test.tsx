/**
 * Disposition prompt.
 *
 * Completing a patient used to be one click. It now asks what happened, and
 * the tests below are mostly about the friction being *deliberate*: no
 * default outcome, no completing without answering, and cancel never being a
 * silent completion.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { DispositionDialog, noteRequired, validateDisposition } from './DispositionDialog'
import { PatientFlowPage } from '../pages/PatientFlowPage'
import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'

const asClinician = { token: makeToken({ roles: ['clinician'] }) }

describe('disposition rules', () => {
  it('requires a note only for outcomes that say nothing on their own', () => {
    expect(noteRequired('left_without_being_seen')).toBe(true)
    expect(noteRequired('other')).toBe(true)
    expect(noteRequired('admitted')).toBe(false)
    expect(noteRequired('')).toBe(false)
  })

  it('blocks submission until an outcome is chosen', () => {
    expect(validateDisposition('', '')).toMatch(/select what happened/i)
  })

  it('blocks an ambiguous outcome with no note', () => {
    expect(validateDisposition('other', '   ')).toMatch(/note is required/i)
  })

  it('allows a plain outcome with no note', () => {
    expect(validateDisposition('admitted', '')).toBeNull()
  })
})

describe('DispositionDialog', () => {
  const noop = () => {}

  it('offers no pre-selected outcome', () => {
    /** A default would be recorded by anyone clicking through quickly, and a
        plausible wrong answer is worse than a moment's friction. */
    renderWithProviders(
      <DispositionDialog patientId="MRN-1" onCancel={noop} onConfirm={noop} />,
      asClinician,
    )
    expect(screen.getByLabelText(/disposition/i)).toHaveValue('')
  })

  it('does not confirm until an outcome is selected', async () => {
    const onConfirm = vi.fn()
    const { user } = renderWithProviders(
      <DispositionDialog patientId="MRN-1" onCancel={noop} onConfirm={onConfirm} />,
      asClinician,
    )
    await user.click(screen.getByRole('button', { name: /^complete$/i }))
    expect(onConfirm).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent(/select what happened/i)
  })

  it('demands a note for "left without being seen"', async () => {
    const onConfirm = vi.fn()
    const { user } = renderWithProviders(
      <DispositionDialog patientId="MRN-1" onCancel={noop} onConfirm={onConfirm} />,
      asClinician,
    )
    await user.selectOptions(screen.getByLabelText(/disposition/i), 'left_without_being_seen')
    await user.click(screen.getByRole('button', { name: /^complete$/i }))
    expect(onConfirm).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent(/note is required/i)
  })

  it('confirms with the chosen outcome and note', async () => {
    const onConfirm = vi.fn()
    const { user } = renderWithProviders(
      <DispositionDialog patientId="MRN-1" onCancel={noop} onConfirm={onConfirm} />,
      asClinician,
    )
    await user.selectOptions(screen.getByLabelText(/disposition/i), 'other')
    await user.type(screen.getByLabelText(/note/i), 'Moved to observation ward')
    await user.click(screen.getByRole('button', { name: /^complete$/i }))
    expect(onConfirm).toHaveBeenCalledWith('other', 'Moved to observation ward')
  })

  it('passes null rather than an empty string when no note is given', async () => {
    const onConfirm = vi.fn()
    const { user } = renderWithProviders(
      <DispositionDialog patientId="MRN-1" onCancel={noop} onConfirm={onConfirm} />,
      asClinician,
    )
    await user.selectOptions(screen.getByLabelText(/disposition/i), 'admitted')
    await user.click(screen.getByRole('button', { name: /^complete$/i }))
    expect(onConfirm).toHaveBeenCalledWith('admitted', null)
  })

  it('cancelling never completes the patient', async () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    const { user } = renderWithProviders(
      <DispositionDialog patientId="MRN-1" onCancel={onCancel} onConfirm={onConfirm} />,
      asClinician,
    )
    await user.selectOptions(screen.getByLabelText(/disposition/i), 'admitted')
    await user.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onCancel).toHaveBeenCalled()
    expect(onConfirm).not.toHaveBeenCalled()
  })
})

describe('completing from the triage queue', () => {
  it('asks what happened instead of completing immediately', async () => {
    let called = false
    server.use(
      http.post(/\/flow\/queue\/[^/]+\/complete/, () => {
        called = true
        return HttpResponse.json({ ok: true, item: { patient_id: 'x', acuity: 2, dept: 'ED' } })
      }),
      http.get(/\/flow\/queue\/?(\?.*)?$/, () =>
        HttpResponse.json({
          items: [
            { patient_id: 'pat-1', acuity: 2, dept: 'ED', status: 'in_progress', claimed_by: 'me' },
          ],
          count: 1,
          total: 1,
        }),
      ),
    )
    const { user } = renderWithProviders(<PatientFlowPage />, asClinician)
    await user.click(await screen.findByRole('button', { name: /^complete$/i }))

    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
    // Nothing recorded until the clinician answers.
    expect(called).toBe(false)
  })

  it('sends the chosen disposition to the server', async () => {
    let sent: Record<string, unknown> = {}
    server.use(
      http.post(/\/flow\/queue\/[^/]+\/complete/, async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({
          ok: true,
          item: { patient_id: 'pat-1', acuity: 2, dept: 'ED', status: 'completed' },
        })
      }),
      http.get(/\/flow\/queue\/?(\?.*)?$/, () =>
        HttpResponse.json({
          items: [
            { patient_id: 'pat-1', acuity: 2, dept: 'ED', status: 'in_progress', claimed_by: 'me' },
          ],
          count: 1,
          total: 1,
        }),
      ),
    )
    const { user } = renderWithProviders(<PatientFlowPage />, asClinician)
    await user.click(await screen.findByRole('button', { name: /^complete$/i }))

    // Two "Complete" buttons exist once the prompt is open (the row button
    // and the dialog's submit); scope to the dialog.
    const dialog = await screen.findByRole('alertdialog')
    await user.selectOptions(within(dialog).getByLabelText(/disposition/i), 'admitted')
    await user.click(within(dialog).getByRole('button', { name: /^complete$/i }))

    await waitFor(() => expect(sent.disposition).toBe('admitted'))
  })
})
