import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, isAbortError, NetworkError } from '../api/client'

export type AsyncState<T> =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; data: T }
  | { status: 'error'; error: string; statusCode?: number }

/** Turns any thrown value into a message suitable for display. */
export function describeError(err: unknown): { message: string; statusCode?: number } {
  if (err instanceof ApiError) {
    if (err.status === 401) return { message: 'Your session has expired. Sign in again.', statusCode: 401 }
    if (err.status === 403) {
      return { message: 'You do not have permission to view this.', statusCode: 403 }
    }
    if (err.status === 404) return { message: 'Not found.', statusCode: 404 }
    if (err.status === 502) {
      return { message: 'The upstream FHIR server is unavailable.', statusCode: 502 }
    }
    return { message: err.detail || `Request failed (${err.status})`, statusCode: err.status }
  }
  if (err instanceof NetworkError) return { message: 'Service unreachable. Is it running?' }
  if (err instanceof Error) return { message: err.message }
  return { message: 'Unexpected error' }
}

function isAbort(err: unknown): boolean {
  // Delegates to the client's cross-runtime detection.
  return isAbortError(err)
}

/**
 * Runs an async task on demand, cancelling any in-flight request when a new one
 * starts or the component unmounts. Out-of-order responses are discarded so a
 * slow earlier request can never overwrite a newer result.
 */
export function useAsyncAction<Args extends unknown[], T>(
  task: (signal: AbortSignal, ...args: Args) => Promise<T>,
): {
  state: AsyncState<T>
  run: (...args: Args) => Promise<T | undefined>
  reset: () => void
} {
  const [state, setState] = useState<AsyncState<T>>({ status: 'idle' })
  const controllerRef = useRef<AbortController | null>(null)
  const runIdRef = useRef(0)
  const mountedRef = useRef(true)
  const taskRef = useRef(task)
  taskRef.current = task

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      controllerRef.current?.abort()
    }
  }, [])

  const run = useCallback(async (...args: Args): Promise<T | undefined> => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const runId = ++runIdRef.current

    setState({ status: 'loading' })
    try {
      const data = await taskRef.current(controller.signal, ...args)
      // Ignore results from superseded runs.
      if (!mountedRef.current || runId !== runIdRef.current) return undefined
      setState({ status: 'success', data })
      return data
    } catch (err) {
      if (isAbort(err) || !mountedRef.current || runId !== runIdRef.current) return undefined
      const { message, statusCode } = describeError(err)
      setState({ status: 'error', error: message, statusCode })
      return undefined
    }
  }, [])

  const reset = useCallback(() => {
    controllerRef.current?.abort()
    runIdRef.current++
    setState({ status: 'idle' })
  }, [])

  return { state, run, reset }
}

/** Runs a task immediately (and whenever `deps` change). */
export function useAsyncData<T>(
  task: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
): { state: AsyncState<T>; reload: () => void } {
  const [state, setState] = useState<AsyncState<T>>({ status: 'loading' })
  const [nonce, setNonce] = useState(0)
  const taskRef = useRef(task)
  taskRef.current = task

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setState({ status: 'loading' })

    taskRef
      .current(controller.signal)
      .then((data) => {
        if (!active || controller.signal.aborted) return
        setState({ status: 'success', data })
      })
      .catch((err: unknown) => {
        if (!active || controller.signal.aborted || isAbort(err)) return
        const { message, statusCode } = describeError(err)
        setState({ status: 'error', error: message, statusCode })
      })

    return () => {
      active = false
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  return { state, reload: useCallback(() => setNonce((n) => n + 1), []) }
}
