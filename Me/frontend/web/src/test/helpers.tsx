import { render, type RenderResult } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'

import type { Role } from '../api/types'
import { AuthProvider } from '../auth/AuthContext'
import { SESSION_STORAGE_KEY, STORAGE_KEY, tokenStorage } from '../auth/token'

/** base64url encode, matching what a JWT issuer produces. */
function b64url(value: string): string {
  return btoa(value).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/**
 * Builds an unsigned JWT for tests. The frontend only ever decodes tokens —
 * signature verification is the gateway's job — so an unsigned token is a
 * faithful stand-in.
 */
export function makeToken(opts: {
  sub?: string
  roles?: Role[] | string
  /** Seconds from now. Negative values produce an already-expired token. */
  expiresInSeconds?: number | null
} = {}): string {
  const { sub = 'test.user', roles = ['clinician'], expiresInSeconds = 3600 } = opts
  const payload: Record<string, unknown> = { sub, roles }
  if (expiresInSeconds !== null) {
    payload.exp = Math.floor(Date.now() / 1000) + expiresInSeconds
  }
  return [
    b64url(JSON.stringify({ alg: 'HS256', typ: 'JWT' })),
    b64url(JSON.stringify(payload)),
    'signature',
  ].join('.')
}

export function seedToken(token: string): void {
  // Prefer the real storage helper so tests exercise the same path as prod
  // (memory + sessionStorage; never durable localStorage).
  tokenStorage.write(token)
  // Also seed sessionStorage directly for specs that construct AuthProvider
  // before any write() call.
  try {
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, token)
  } catch {
    window.localStorage.setItem(STORAGE_KEY, token)
  }
}

/** Renders a tree inside the router + auth providers. */
export function renderWithProviders(
  ui: React.ReactElement,
  { route = '/', token }: { route?: string; token?: string } = {},
): RenderResult & { user: ReturnType<typeof userEvent.setup> } {
  if (token) seedToken(token)
  const user = userEvent.setup()
  const result = render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>{ui}</AuthProvider>
    </MemoryRouter>,
  )
  return { ...result, user }
}
