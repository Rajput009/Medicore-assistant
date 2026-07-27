/**
 * Offline-tolerant retries.
 *
 * The headline property is that one user intent produces one Idempotency-Key,
 * reused by every attempt. Before this, each call minted a fresh UUID, so the
 * backend's idempotency store could never match a retry to its original and
 * a dropped response could claim a second patient.
 */

import { describe, expect, it, vi } from 'vitest'

import { ApiError, NetworkError } from './client'
import { backoffDelay, isRetryable, retryWithIdempotency } from './retry'

/** Deterministic, instant sleep so tests never wait on real timers. */
const noSleep = () => Promise.resolve()

describe('isRetryable', () => {
  it('retries network failures — the request may never have landed', () => {
    expect(isRetryable(new NetworkError())).toBe(true)
  })

  it('retries 5xx', () => {
    expect(isRetryable(new ApiError(500, 'boom'))).toBe(true)
    expect(isRetryable(new ApiError(503, 'unavailable'))).toBe(true)
  })

  it('retries 429, which is the server asking us to slow down', () => {
    expect(isRetryable(new ApiError(429, 'slow down'))).toBe(true)
  })

  it('does NOT retry 409 — someone else got there first', () => {
    expect(isRetryable(new ApiError(409, 'conflict'))).toBe(false)
  })

  it('does NOT retry 403/404/422 — repeating cannot change the answer', () => {
    expect(isRetryable(new ApiError(403, 'forbidden'))).toBe(false)
    expect(isRetryable(new ApiError(404, 'missing'))).toBe(false)
    expect(isRetryable(new ApiError(422, 'invalid'))).toBe(false)
  })

  it('does not retry arbitrary programming errors', () => {
    expect(isRetryable(new TypeError('undefined is not a function'))).toBe(false)
  })
})

describe('backoffDelay', () => {
  it('never exceeds the cap', () => {
    for (let attempt = 0; attempt < 12; attempt++) {
      expect(backoffDelay(attempt, 300, 4000)).toBeLessThanOrEqual(4000)
    }
  })

  it('is never negative', () => {
    for (let attempt = 0; attempt < 12; attempt++) {
      expect(backoffDelay(attempt, 300, 4000)).toBeGreaterThanOrEqual(0)
    }
  })

  it('grows with the attempt number', () => {
    // Jitter makes single draws noisy; compare upper bounds via a fixed RNG.
    const spy = vi.spyOn(Math, 'random').mockReturnValue(1)
    try {
      expect(backoffDelay(1, 300, 100_000)).toBeGreaterThan(backoffDelay(0, 300, 100_000))
      expect(backoffDelay(2, 300, 100_000)).toBeGreaterThan(backoffDelay(1, 300, 100_000))
    } finally {
      spy.mockRestore()
    }
  })
})

describe('retryWithIdempotency', () => {
  it('reuses ONE key across every attempt', async () => {
    const keys: string[] = []
    let calls = 0
    await retryWithIdempotency(
      (key) => {
        keys.push(key)
        calls++
        if (calls < 3) return Promise.reject(new NetworkError())
        return Promise.resolve('ok')
      },
      { sleep: noSleep },
    )

    expect(keys).toHaveLength(3)
    expect(new Set(keys).size).toBe(1)
  })

  it('mints a DIFFERENT key for a separate intent', async () => {
    const keys: string[] = []
    const capture = (key: string) => {
      keys.push(key)
      return Promise.resolve('ok')
    }
    await retryWithIdempotency(capture, { sleep: noSleep })
    await retryWithIdempotency(capture, { sleep: noSleep })

    expect(new Set(keys).size).toBe(2)
  })

  it('returns the value from the first success without retrying', async () => {
    const task = vi.fn().mockResolvedValue('done')
    await expect(retryWithIdempotency(task, { sleep: noSleep })).resolves.toBe('done')
    expect(task).toHaveBeenCalledTimes(1)
  })

  it('gives up after the configured number of retries', async () => {
    const task = vi.fn().mockRejectedValue(new NetworkError())
    await expect(
      retryWithIdempotency(task, { retries: 2, sleep: noSleep }),
    ).rejects.toBeInstanceOf(NetworkError)
    // 1 initial attempt + 2 retries.
    expect(task).toHaveBeenCalledTimes(3)
  })

  it('does not retry a 409 — it surfaces immediately', async () => {
    const task = vi.fn().mockRejectedValue(new ApiError(409, 'Bed was modified'))
    await expect(retryWithIdempotency(task, { sleep: noSleep })).rejects.toBeInstanceOf(
      ApiError,
    )
    expect(task).toHaveBeenCalledTimes(1)
  })

  it('reports each retry so the UI can explain the delay', async () => {
    const onRetry = vi.fn()
    let calls = 0
    await retryWithIdempotency(
      () => {
        calls++
        return calls < 3 ? Promise.reject(new ApiError(503, 'down')) : Promise.resolve('ok')
      },
      { sleep: noSleep, onRetry },
    )
    expect(onRetry).toHaveBeenCalledTimes(2)
    expect(onRetry.mock.calls[0][0].attempt).toBe(1)
    expect(onRetry.mock.calls[1][0].attempt).toBe(2)
  })

  it('passes the attempt number to the task', async () => {
    const attempts: number[] = []
    let calls = 0
    await retryWithIdempotency(
      (_key, attempt) => {
        attempts.push(attempt)
        calls++
        return calls < 3 ? Promise.reject(new NetworkError()) : Promise.resolve('ok')
      },
      { sleep: noSleep },
    )
    expect(attempts).toEqual([0, 1, 2])
  })

  it('stops immediately when the caller aborts', async () => {
    const controller = new AbortController()
    controller.abort()
    const task = vi.fn().mockResolvedValue('never')
    await expect(
      retryWithIdempotency(task, { signal: controller.signal, sleep: noSleep }),
    ).rejects.toMatchObject({ name: 'AbortError' })
    expect(task).not.toHaveBeenCalled()
  })

  it('does not retry once aborted mid-flight', async () => {
    const controller = new AbortController()
    const task = vi.fn().mockImplementation(() => {
      controller.abort()
      return Promise.reject(new NetworkError())
    })
    await expect(
      retryWithIdempotency(task, { signal: controller.signal, sleep: noSleep }),
    ).rejects.toBeInstanceOf(NetworkError)
    expect(task).toHaveBeenCalledTimes(1)
  })

  it('waits between attempts using the injected sleeper', async () => {
    const delays: number[] = []
    let calls = 0
    await retryWithIdempotency(
      () => {
        calls++
        return calls < 3 ? Promise.reject(new NetworkError()) : Promise.resolve('ok')
      },
      {
        sleep: (ms) => {
          delays.push(ms)
          return Promise.resolve()
        },
      },
    )
    expect(delays).toHaveLength(2)
    delays.forEach((d) => expect(d).toBeGreaterThanOrEqual(0))
  })
})
