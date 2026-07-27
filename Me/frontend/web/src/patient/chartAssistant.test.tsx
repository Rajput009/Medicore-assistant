/**
 * Chart assistant panel.
 *
 * The presentation is a safety surface, not decoration. These assert that a
 * clinician cannot read a partial or failed answer as a complete one:
 * citations are always visible, failed lookups are styled as errors, and a
 * caveat is shown even when findings exist alongside it.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { ChartAssistant, EXAMPLE_QUESTIONS, isFailureCaveat } from './ChartAssistant'
import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'

const asClinician = { token: makeToken({ roles: ['clinician'] }) }

function answerPayload(overrides: Record<string, unknown> = {}) {
  return {
    patient_id: 'MRN-1',
    intents: ['allergies'],
    findings: [],
    caveats: [],
    answered: false,
    disclaimer: 'Not a diagnosis.',
    retrieved: {},
    ...overrides,
  }
}

describe('isFailureCaveat', () => {
  it('treats a failed lookup as a failure', () => {
    expect(isFailureCaveat('Allergy list could not be retrieved — this is NOT a statement…')).toBe(
      true,
    )
  })

  it('treats an ordinary absence as informational', () => {
    expect(isFailureCaveat('No medications recorded for this patient.')).toBe(false)
  })
})

describe('ChartAssistant', () => {
  it('shows nothing until a question is asked', () => {
    renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    expect(screen.queryByText(/penicillin/i)).not.toBeInTheDocument()
  })

  it('answers a question and shows the citation for the claim', async () => {
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.type(screen.getByLabelText(/question about this patient/i), 'allergies?')
    await user.click(screen.getByRole('button', { name: /^ask$/i }))

    expect(await screen.findByText(/penicillin/i)).toBeInTheDocument()
    // The citation must be visible without interaction — a claim whose basis
    // is hidden behind a click is not meaningfully grounded.
    expect(screen.getByText('AllergyIntolerance/a1')).toBeInTheDocument()
  })

  it('badges a critical finding', async () => {
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.type(screen.getByLabelText(/question about this patient/i), 'allergies?')
    await user.click(screen.getByRole('button', { name: /^ask$/i }))
    // Exact match on the badge; the finding text also mentions "criticality".
    expect(await screen.findByText('critical')).toHaveClass('badge', 'err')
  })

  it('renders a failed lookup as an error, not a neutral note', async () => {
    /** The dangerous misread is "nothing found"; it must not look calm. */
    server.use(
      http.post('/api/assist/ask', () =>
        HttpResponse.json(
          answerPayload({
            caveats: [
              'Allergy list could not be retrieved — this is NOT a statement that the patient has no allergies.',
            ],
            retrieved: { failed: ['allergies'] },
          }),
        ),
      ),
    )
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.type(screen.getByLabelText(/question about this patient/i), 'allergies?')
    await user.click(screen.getByRole('button', { name: /^ask$/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/could not be retrieved/i)
    expect(alert).toHaveTextContent(/NOT a statement/i)
  })

  it('shows caveats even when there are findings', async () => {
    /** A partial answer is exactly when an unnoticed caveat causes harm. */
    server.use(
      http.post('/api/assist/ask', () =>
        HttpResponse.json(
          answerPayload({
            answered: true,
            findings: [
              {
                text: 'Medication: Amoxicillin [active]',
                critical: false,
                citations: [
                  { resource_type: 'MedicationRequest', resource_id: 'm1', label: 'Amoxicillin' },
                ],
              },
            ],
            caveats: ['Allergy list could not be retrieved.'],
          }),
        ),
      ),
    )
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.type(screen.getByLabelText(/question about this patient/i), 'meds and allergies')
    await user.click(screen.getByRole('button', { name: /^ask$/i }))

    expect(await screen.findByText(/amoxicillin/i)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/could not be retrieved/i)
  })

  it('surfaces a refusal for an advice question', async () => {
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.type(
      screen.getByLabelText(/question about this patient/i),
      'should I give penicillin?',
    )
    await user.click(screen.getByRole('button', { name: /^ask$/i }))
    expect(await screen.findByText(/does not give clinical advice/i)).toBeInTheDocument()
  })

  it('always shows the disclaimer with an answer', async () => {
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.type(screen.getByLabelText(/question about this patient/i), 'allergies?')
    await user.click(screen.getByRole('button', { name: /^ask$/i }))
    expect(await screen.findByText(/not a diagnosis/i)).toBeInTheDocument()
  })

  it('offers example questions that fill the box', async () => {
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.click(screen.getByRole('button', { name: EXAMPLE_QUESTIONS[0] }))
    expect(screen.getByLabelText(/question about this patient/i)).toHaveValue(
      EXAMPLE_QUESTIONS[0],
    )
  })

  it('does not call the server for an empty question', async () => {
    let called = false
    server.use(
      http.post('/api/assist/ask', () => {
        called = true
        return HttpResponse.json(answerPayload())
      }),
    )
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.click(screen.getByRole('button', { name: /^ask$/i }))
    expect(called).toBe(false)
  })

  it('reports a server error without pretending to answer', async () => {
    server.use(
      http.post('/api/assist/ask', () =>
        HttpResponse.json({ detail: 'Upstream clinical data service unavailable' }, { status: 502 }),
      ),
    )
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.type(screen.getByLabelText(/question about this patient/i), 'allergies?')
    await user.click(screen.getByRole('button', { name: /^ask$/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/upstream|unavailable/i)
  })

  it('renders every citation when a finding has several', async () => {
    server.use(
      http.post('/api/assist/ask', () =>
        HttpResponse.json(
          answerPayload({
            answered: true,
            findings: [
              {
                text: 'Latest Potassium: 5.4 mmol/L; previous: 4.8, 4.1',
                critical: false,
                citations: [
                  { resource_type: 'Observation', resource_id: 'o3', label: 'Potassium' },
                  { resource_type: 'Observation', resource_id: 'o2', label: 'Potassium' },
                  { resource_type: 'Observation', resource_id: 'o1', label: 'Potassium' },
                ],
              },
            ],
          }),
        ),
      ),
    )
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.type(screen.getByLabelText(/question about this patient/i), 'potassium?')
    await user.click(screen.getByRole('button', { name: /^ask$/i }))

    const finding = (await screen.findByText(/latest potassium/i)).closest('div')!
    // Each cited observation is listed, so a trend claim can be checked.
    for (const id of ['o1', 'o2', 'o3']) {
      expect(within(finding.parentElement!).getByText(`Observation/${id}`)).toBeInTheDocument()
    }
  })

  it('bounds the question length in the input itself', () => {
    renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    expect(screen.getByLabelText(/question about this patient/i)).toHaveAttribute(
      'maxLength',
      '300',
    )
  })

  it('disables the button while a question is in flight', async () => {
    server.use(
      http.post('/api/assist/ask', async () => {
        await new Promise((r) => setTimeout(r, 60))
        return HttpResponse.json(answerPayload())
      }),
    )
    const { user } = renderWithProviders(<ChartAssistant patientId="MRN-1" />, asClinician)
    await user.type(screen.getByLabelText(/question about this patient/i), 'allergies?')
    await user.click(screen.getByRole('button', { name: /^ask$/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /asking/i })).toBeDisabled())
  })
})
