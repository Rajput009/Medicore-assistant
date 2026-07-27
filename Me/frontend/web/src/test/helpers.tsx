import { render, type RenderResult } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { http, HttpResponse } from 'msw'

import type { Role } from '../api/types'
import { AuthProvider } from '../auth/AuthContext'
import { purgeLegacyTokenStorage } from '../auth/token'
import { PatientChartProvider } from '../patient/PatientChartContext'
import { server } from './server'

/** base64url encode, matching what a JWT issuer produces. */
function b64url(value: string): string {
  return btoa(value).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/**
 * Builds an unsigned JWT for tests. The frontend only ever decodes tokens —
 * signature verification is the gateway's job — so an unsigned token is a
 * faithful stand-in.
 */
export function makeToken(
  opts: {
    sub?: string
    roles?: Role[] | string
    /** Seconds from now. Negative values produce an already-expired token. */
    expiresInSeconds?: number | null
  } = {},
): string {
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

/**
 * Seed an authenticated cookie session for tests.
 *
 * Cookie-only mode: the SPA never stores the JWT. We stub `/auth/session` so
 * AuthProvider hydrates `user` from claims, and MSW `requireAuth` accepts
 * requests that carry the test session marker cookie (or any credentials).
 */
export function seedToken(token: string): void {
  purgeLegacyTokenStorage()
  const payloadPart = token.split('.')[1]
  let claims: { sub?: string; roles?: Role[] | string; exp?: number } = {
    sub: 'test.user',
    roles: ['clinician'],
  }
  try {
    const padded = payloadPart.replace(/-/g, '+').replace(/_/g, '/')
    const withPad = padded + '='.repeat((4 - (padded.length % 4)) % 4)
    claims = JSON.parse(atob(withPad))
  } catch {
    /* use defaults */
  }
  const roles = Array.isArray(claims.roles)
    ? claims.roles
    : typeof claims.roles === 'string'
      ? claims.roles.split(/[,\s]+/)
      : ['clinician']

  server.use(
    http.get('/auth/session', () =>
      HttpResponse.json({
        sub: claims.sub ?? 'test.user',
        roles,
        exp: claims.exp ?? Math.floor(Date.now() / 1000) + 3600,
      }),
    ),
  )
  // Mark document cookie so requireAuth in MSW can treat the client as signed in.
  try {
    document.cookie = `medicore_session=test-session; path=/`
  } catch {
    /* jsdom always allows this */
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
      <AuthProvider>
        <PatientChartProvider>{ui}</PatientChartProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
  return { ...result, user }
}
