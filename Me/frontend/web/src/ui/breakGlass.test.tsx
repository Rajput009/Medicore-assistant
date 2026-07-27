/**
 * Break-glass in the console.
 *
 * The UI half of the emergency override. What is tested here is mostly what
 * the UI must *refuse* to do: offer the override unprompted, accept a
 * throwaway reason, or let it quietly become a standing privilege.
 */

import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'
import { WardBoardPage } from '../pages/WardBoardPage'
import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'
import { BreakGlassPrompt, isReasonAcceptable, MIN_REASON_LENGTH } from './BreakGlassPrompt'

const REASON = 'Cardiac arrest in ICU, responding as on-call'

describe('isReasonAcceptable', () => {
  it('rejects an empty or whitespace reason', () => {
    expect(isReasonAcceptable('')).toBe(false)
    expect(isReasonAcceptable('    ')).toBe(false)
  })

  it('rejects a token reason', () => {
    expect(isReasonAcceptable('x')).toBe(false)
    expect(isReasonAcceptable('urgent')).toBe(false)
  })

  it('accepts a specific reason', () => {
    expect(isReasonAcceptable(REASON)).toBe(true)
  })

  it('matches the server minimum', () => {
    expect(isReasonAcceptable('y'.repeat(MIN_REASON_LENGTH))).toBe(true)
    expect(isReasonAcceptable('y'.repeat(MIN_REASON_LENGTH - 1))).toBe(false)
  })
})

describe('ApiError', () => {
  it('carries the server break-glass hint', () => {
    expect(new ApiError(403, 'Not authorised for this ward', true).breakGlassAvailable).toBe(
      true,
    )
  })

  it('defaults to unavailable, so the UI never invents the option', () => {
    expect(new ApiError(403, 'Insufficient role').breakGlassAvailable).toBe(false)
  })
})

describe('BreakGlassPrompt', () => {
  it('states that the access is recorded and reviewed', () => {
    renderWithProviders(
      <BreakGlassPrompt scope="ward ICU" onConfirm={() => {}} onCancel={() => {}} />,
    )
    expect(screen.getByText(/recorded in the audit trail/i)).toBeInTheDocument()
    expect(screen.getByText(/reviewed afterwards/i)).toBeInTheDocument()
  })

  it('disables confirmation until a real reason is given', async () => {
    const { user } = renderWithProviders(
      <BreakGlassPrompt scope="ward ICU" onConfirm={() => {}} onCancel={() => {}} />,
    )
    const confirm = screen.getByRole('button', { name: /break glass and continue/i })
    expect(confirm).toBeDisabled()

    await user.type(screen.getByLabelText(/reason for access/i), 'x')
    expect(confirm).toBeDisabled()

    await user.type(screen.getByLabelText(/reason for access/i), REASON)
    expect(confirm).toBeEnabled()
  })

  it('passes the trimmed reason to the caller', async () => {
    const onConfirm = vi.fn()
    const { user } = renderWithProviders(
      <BreakGlassPrompt scope="ward ICU" onConfirm={onConfirm} onCancel={() => {}} />,
    )
    await user.type(screen.getByLabelText(/reason for access/i), `  ${REASON}  `)
    await user.click(screen.getByRole('button', { name: /break glass and continue/i }))
    expect(onConfirm).toHaveBeenCalledWith(REASON)
  })

  it('can be cancelled without overriding anything', async () => {
    const onCancel = vi.fn()
    const onConfirm = vi.fn()
    const { user } = renderWithProviders(
      <BreakGlassPrompt scope="ward ICU" onConfirm={onConfirm} onCancel={onCancel} />,
    )
    await user.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onCancel).toHaveBeenCalled()
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('is announced assertively', () => {
    renderWithProviders(
      <BreakGlassPrompt scope="ward ICU" onConfirm={() => {}} onCancel={() => {}} />,
    )
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })
})

describe('ward board break-glass flow', () => {
  it('does not offer an override when access succeeds', async () => {
    server.use(
      http.get('/flow/beds', () =>
        HttpResponse.json([{ bed_id: 'A-001', ward: 'A', occupied: false }]),
      ),
    )
    renderWithProviders(<WardBoardPage />, { token: makeToken() })

    await waitFor(() => expect(screen.getByText(/A-001/)).toBeInTheDocument())
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('offers the override after a scope refusal', async () => {
    server.use(
      http.get('/flow/beds', () =>
        HttpResponse.json({ detail: 'Not authorised for this ward' }, { status: 403 }),
      ),
    )
    renderWithProviders(<WardBoardPage />, { token: makeToken() })

    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
  })

  it('retries with the reason attached and shows the data', async () => {
    let sentReason: string | null = null
    server.use(
      http.get('/flow/beds', ({ request }) => {
        const reason = request.headers.get('x-break-glass-reason')
        if (!reason) {
          return HttpResponse.json({ detail: 'Not authorised for this ward' }, { status: 403 })
        }
        sentReason = reason
        return HttpResponse.json([{ bed_id: 'ICU-001', ward: 'ICU', occupied: false }])
      }),
    )
    const { user } = renderWithProviders(<WardBoardPage />, { token: makeToken() })

    await screen.findByRole('alertdialog')
    await user.type(screen.getByLabelText(/reason for access/i), REASON)
    await user.click(screen.getByRole('button', { name: /break glass and continue/i }))

    await waitFor(() => expect(screen.getByText(/ICU-001/)).toBeInTheDocument())
    expect(sentReason).toBe(REASON)
  })

  it('keeps telling the clinician the override is active', async () => {
    server.use(
      http.get('/flow/beds', ({ request }) => {
        if (!request.headers.get('x-break-glass-reason')) {
          return HttpResponse.json({ detail: 'Not authorised' }, { status: 403 })
        }
        return HttpResponse.json([{ bed_id: 'ICU-001', ward: 'ICU', occupied: false }])
      }),
    )
    const { user } = renderWithProviders(<WardBoardPage />, { token: makeToken() })

    await screen.findByRole('alertdialog')
    await user.type(screen.getByLabelText(/reason for access/i), REASON)
    await user.click(screen.getByRole('button', { name: /break glass and continue/i }))

    // It must not silently look like ordinary access afterwards.
    expect(await screen.findByText(/Break-glass access is active/i)).toBeInTheDocument()
  })

  it('does not send the header on an ordinary request', async () => {
    let sawHeader = true
    server.use(
      http.get('/flow/beds', ({ request }) => {
        sawHeader = request.headers.has('x-break-glass-reason')
        return HttpResponse.json([{ bed_id: 'A-001', ward: 'A', occupied: false }])
      }),
    )
    renderWithProviders(<WardBoardPage />, { token: makeToken() })

    await waitFor(() => expect(screen.getByText(/A-001/)).toBeInTheDocument())
    expect(sawHeader).toBe(false)
  })

  it('can be dismissed, leaving access refused', async () => {
    server.use(
      http.get('/flow/beds', () =>
        HttpResponse.json({ detail: 'Not authorised for this ward' }, { status: 403 }),
      ),
    )
    const { user } = renderWithProviders(<WardBoardPage />, { token: makeToken() })

    await screen.findByRole('alertdialog')
    await user.click(screen.getByRole('button', { name: /cancel/i }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.queryByText(/Break-glass access is active/i)).not.toBeInTheDocument()
  })
})
