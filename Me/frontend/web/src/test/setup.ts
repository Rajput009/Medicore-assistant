import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, vi } from 'vitest'

import { tokenStorage } from '../auth/token'
import { server } from './server'

// Fail tests on unhandled requests so a missing mock is loud, not silent.
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))

afterEach(() => {
  cleanup()
  server.resetHandlers()
  // Clear durable + in-memory session state so tests cannot leak auth.
  tokenStorage.clear()
  window.localStorage.clear()
  window.sessionStorage.clear()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

afterAll(() => server.close())

// jsdom does not implement these; several components rely on them.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}
