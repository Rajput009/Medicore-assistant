/**
 * Offline-tolerant retries for unsafe writes.
 *
 * The backend has honoured `Idempotency-Key` since day one (see
 * `backend/common/idempotency.py`), but the UI defeated it: every call minted
 * a *fresh* UUID, so a retry looked like a brand-new intent and could enqueue
 * the same patient twice or re-apply a bed change.
 *
 * The fix is not "retry harder" — it is to mint **one key per user intent**
 * and reuse it for every attempt at that intent. Clicking "Claim next" once
 * is one key, no matter how many times the network drops.
 *
 * What we retry matters as much as how:
 *  - Network failures and 5xx are retried: the request may never have landed,
 *    and if it did, the key makes the replay a no-op that returns the first
 *    response.
 *  - 429 is retried (the server is asking us to slow down), with backoff.
 *  - Other 4xx are **not** retried. A 409 means someone else got there first
 *    and a 403 means the caller lacks scope; repeating either just burns time
 *    and hides a real answer from the clinician.
 */

import { ApiError, NetworkError, newIdempotencyKey } from './client'

/** Transient failures where the same intent may safely be re-sent. */
export function isRetryable(err: unknown): boolean {
  if (err instanceof NetworkError) return true
  if (err instanceof ApiError) {
    if (err.status === 429) return true
    // 5xx: the write may or may not have committed. The idempotency key is
    // what makes re-sending safe rather than reckless.
    return err.status >= 500 && err.status <= 599
  }
  return false
}

export type RetryOptions = {
  /** Extra attempts after the first. 2 = up to 3 total. */
  retries?: number
  /** First backoff step; doubles each attempt. */
  baseDelayMs?: number
  /** Cap so a long outage cannot leave a clinician staring at a spinner. */
  maxDelayMs?: number
  signal?: AbortSignal
  /** Injectable for tests; defaults to real timers. */
  sleep?: (ms: number) => Promise<void>
  /** Called before each retry — lets the UI say "Retrying (2/3)…". */
  onRetry?: (info: { attempt: number; delayMs: number; error: unknown }) => void
}

const defaultSleep = (ms: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, ms))

/** Exponential backoff with jitter, so N retrying tabs don't sync up. */
export function backoffDelay(attempt: number, base: number, max: number): number {
  const exponential = Math.min(max, base * 2 ** attempt)
  // Full jitter: spreads a thundering herd after a service comes back.
  return Math.round(Math.random() * exponential)
}

/**
 * Run one idempotent write, retrying transient failures.
 *
 * `task` receives a key that is **stable across attempts**. Pass it straight
 * through to the API call; do not generate one inside the task.
 */
export async function retryWithIdempotency<T>(
  task: (idempotencyKey: string, attempt: number) => Promise<T>,
  options: RetryOptions = {},
): Promise<T> {
  const {
    retries = 2,
    baseDelayMs = 300,
    maxDelayMs = 4000,
    signal,
    sleep = defaultSleep,
    onRetry,
  } = options

  // Minted once, deliberately outside the loop. This single line is the
  // difference between a safe retry and a duplicate write.
  const idempotencyKey = newIdempotencyKey()

  let lastError: unknown
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError')
    try {
      return await task(idempotencyKey, attempt)
    } catch (err) {
      lastError = err
      const canRetry = attempt < retries && isRetryable(err) && !signal?.aborted
      if (!canRetry) throw err
      const delayMs = backoffDelay(attempt, baseDelayMs, maxDelayMs)
      onRetry?.({ attempt: attempt + 1, delayMs, error: err })
      await sleep(delayMs)
    }
  }
  throw lastError
}
