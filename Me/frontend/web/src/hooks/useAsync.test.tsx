/** Tests for the async hooks: races, cancellation, unmount safety. */

import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ApiError, NetworkError } from '../api/client'
import { describeError, useAsyncAction, useAsyncData } from './useAsync'

describe('describeError', () => {
  it.each([
    [401, 'Your session has expired. Sign in again.'],
    [403, 'You do not have permission to view this.'],
    [404, 'Not found.'],
    [502, 'The upstream FHIR server is unavailable.'],
  ])('maps HTTP %i to a friendly message', (status, expected) => {
    expect(describeError(new ApiError(status, 'raw')).message).toBe(expected)
  })

  it.each([
    [409, /changed this first|conflict/i],
    [413, /too large/i],
    [429, /too many requests/i],
    [503, /temporarily unavailable/i],
  ])('maps HTTP %i to actionable guidance', (status, pattern) => {
    // These arrive from the hardening middleware; a raw server string would
    // be meaningless to a clinician mid-shift.
    expect(describeError(new ApiError(status, '')).message).toMatch(pattern)
  })

  it('uses the server detail for other statuses', () => {
    expect(describeError(new ApiError(409, 'conflict')).message).toBe('conflict')
  })

  it('falls back to a generic message when detail is empty', () => {
    expect(describeError(new ApiError(500, '')).message).toBe('Request failed (500)')
  })

  it('describes network failures', () => {
    expect(describeError(new NetworkError()).message).toBe('Service unreachable. Is it running?')
  })

  it('handles a plain Error and a non-Error throw', () => {
    expect(describeError(new Error('boom')).message).toBe('boom')
    expect(describeError('a string').message).toBe('Unexpected error')
  })
})

describe('useAsyncAction', () => {
  it('starts idle and transitions to success', async () => {
    const { result } = renderHook(() => useAsyncAction(async () => 'value'))
    expect(result.current.state.status).toBe('idle')

    await act(async () => {
      await result.current.run()
    })
    expect(result.current.state).toEqual({ status: 'success', data: 'value' })
  })

  it('captures errors', async () => {
    const { result } = renderHook(() =>
      useAsyncAction(async () => {
        throw new ApiError(403, 'denied')
      }),
    )
    await act(async () => {
      await result.current.run()
    })
    expect(result.current.state).toMatchObject({ status: 'error', statusCode: 403 })
  })

  it('passes arguments through to the task', async () => {
    const spy = vi.fn(async (_s: AbortSignal, a: number, b: number) => a + b)
    const { result } = renderHook(() => useAsyncAction(spy))
    await act(async () => {
      await result.current.run(2, 3)
    })
    expect(result.current.state).toEqual({ status: 'success', data: 5 })
  })

  it('discards a slow earlier response when a newer run finishes first', async () => {
    // Guards against out-of-order responses clobbering fresh results.
    const delays = [60, 0]
    let call = 0
    const { result } = renderHook(() =>
      useAsyncAction(async (_signal: AbortSignal) => {
        const i = call++
        await new Promise((r) => setTimeout(r, delays[i]))
        return `run-${i}`
      }),
    )

    await act(async () => {
      const slow = result.current.run()
      const fast = result.current.run()
      await Promise.all([slow, fast])
      await new Promise((r) => setTimeout(r, 100))
    })

    expect(result.current.state).toEqual({ status: 'success', data: 'run-1' })
  })

  it('aborts the previous request when a new one starts', async () => {
    const signals: AbortSignal[] = []
    const { result } = renderHook(() =>
      useAsyncAction(async (signal: AbortSignal) => {
        signals.push(signal)
        await new Promise((r) => setTimeout(r, 30))
        return 'x'
      }),
    )
    await act(async () => {
      void result.current.run()
      await result.current.run()
    })
    expect(signals[0].aborted).toBe(true)
    expect(signals[1].aborted).toBe(false)
  })

  it('aborts in-flight work on unmount', async () => {
    let captured: AbortSignal | undefined
    const { result, unmount } = renderHook(() =>
      useAsyncAction(async (signal: AbortSignal) => {
        captured = signal
        await new Promise((r) => setTimeout(r, 50))
        return 'x'
      }),
    )
    act(() => {
      void result.current.run()
    })
    unmount()
    expect(captured?.aborted).toBe(true)
  })

  it('reset returns to idle', async () => {
    const { result } = renderHook(() => useAsyncAction(async () => 'v'))
    await act(async () => {
      await result.current.run()
    })
    act(() => result.current.reset())
    expect(result.current.state.status).toBe('idle')
  })

  it('ignores AbortError instead of surfacing it as a failure', async () => {
    const { result } = renderHook(() =>
      useAsyncAction(async () => {
        throw new DOMException('aborted', 'AbortError')
      }),
    )
    await act(async () => {
      await result.current.run()
    })
    // Stays in loading; a cancelled request is not an error state.
    expect(result.current.state.status).toBe('loading')
  })
})

describe('useAsyncData', () => {
  it('loads immediately', async () => {
    const { result } = renderHook(() => useAsyncData(async () => 'data', []))
    expect(result.current.state.status).toBe('loading')
    await waitFor(() => expect(result.current.state).toEqual({ status: 'success', data: 'data' }))
  })

  it('records errors', async () => {
    const { result } = renderHook(() =>
      useAsyncData(async () => {
        throw new NetworkError()
      }, []),
    )
    await waitFor(() => expect(result.current.state.status).toBe('error'))
  })

  it('re-runs when reload is called', async () => {
    let n = 0
    const { result } = renderHook(() => useAsyncData(async () => ++n, []))
    await waitFor(() => expect(result.current.state).toEqual({ status: 'success', data: 1 }))
    act(() => result.current.reload())
    await waitFor(() => expect(result.current.state).toEqual({ status: 'success', data: 2 }))
  })

  it('re-runs when a dependency changes', async () => {
    const spy = vi.fn(async () => 'v')
    const { rerender } = renderHook(({ dep }) => useAsyncData(spy, [dep]), {
      initialProps: { dep: 1 },
    })
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    rerender({ dep: 2 })
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
  })

  it('does not set state after unmount', async () => {
    const errors: unknown[] = []
    const spy = vi.spyOn(console, 'error').mockImplementation((...a) => errors.push(a))
    const { unmount } = renderHook(() =>
      useAsyncData(async () => {
        await new Promise((r) => setTimeout(r, 30))
        return 'late'
      }, []),
    )
    unmount()
    await new Promise((r) => setTimeout(r, 60))
    expect(errors.filter((e) => String(e).includes('unmounted'))).toHaveLength(0)
    spy.mockRestore()
  })
})
