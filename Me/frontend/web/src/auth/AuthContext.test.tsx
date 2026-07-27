/** Session lifecycle: cookie-only login, /session hydrate, expiry, logout. */

import { act, renderHook, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { makeToken, seedToken } from '../test/helpers'
import { server } from '../test/server'
import { AuthProvider, useAuth } from './AuthContext'
import { STORAGE_KEY, SESSION_STORAGE_KEY } from './token'

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <AuthProvider>{children}</AuthProvider>
)

describe('AuthProvider (cookie-only)', () => {
  it('starts unauthenticated when /session is anonymous', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isBootstrapping).toBe(false))
    expect(result.current.isAuthenticated).toBe(false)
    expect(result.current.user).toBeNull()
    expect(result.current.token).toBeNull()
  })

  it('hydrates user from /session without storing a JWT', async () => {
    seedToken(makeToken({ sub: 'restored', roles: ['admin'] }))
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.user?.sub).toBe('restored'))
    expect(result.current.user?.roles).toContain('admin')
    expect(result.current.token).toBeNull()
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('rejects an expired session from /session', async () => {
    seedToken(makeToken({ expiresInSeconds: -10 }))
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isBootstrapping).toBe(false))
    expect(result.current.isAuthenticated).toBe(false)
  })

  it('logs in via cookie session and never writes web storage', async () => {
    // After login, /session must return the signed-in subject.
    server.use(
      http.post('/auth/login', async ({ request }) => {
        const body = (await request.json()) as { username?: string; password?: string }
        if (body.password !== 'correct-horse') {
          return HttpResponse.json({ detail: 'Invalid credentials' }, { status: 401 })
        }
        document.cookie = 'medicore_session=test-session; path=/'
        server.use(
          http.get('/auth/session', () =>
            HttpResponse.json({
              sub: body.username,
              roles: ['clinician'],
              exp: Math.floor(Date.now() / 1000) + 900,
            }),
          ),
        )
        return HttpResponse.json({
          access_token: makeToken({ sub: body.username }),
          token_type: 'bearer',
          expires_in: 900,
        })
      }),
    )

    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isBootstrapping).toBe(false))

    await act(async () => {
      const ok = await result.current.login('dr.smith', 'correct-horse')
      expect(ok).toBe(true)
    })
    expect(result.current.user?.sub).toBe('dr.smith')
    expect(result.current.token).toBeNull()
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('reports wrong credentials without authenticating', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isBootstrapping).toBe(false))
    await act(async () => {
      const ok = await result.current.login('dr.smith', 'wrong')
      expect(ok).toBe(false)
    })
    expect(result.current.isAuthenticated).toBe(false)
    expect(result.current.loginError).toBe('Incorrect username or password.')
  })

  it('explains that password login is disabled on a 404', async () => {
    server.use(http.post('/auth/login', () => HttpResponse.json({}, { status: 404 })))
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isBootstrapping).toBe(false))
    await act(async () => {
      await result.current.login('u', 'p')
    })
    expect(result.current.loginError).toMatch(/disabled/i)
  })

  it('reports an unreachable auth service', async () => {
    server.use(http.post('/auth/login', () => HttpResponse.error()))
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isBootstrapping).toBe(false))
    await act(async () => {
      await result.current.login('u', 'p')
    })
    expect(result.current.loginError).toBe('Cannot reach the auth service.')
  })

  it('logout clears the in-memory user', async () => {
    seedToken(makeToken())
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isAuthenticated).toBe(true))
    act(() => result.current.logout())
    expect(result.current.isAuthenticated).toBe(false)
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('does not authenticate when /session returns an already-expired exp', async () => {
    // Cookie-only: expiry is enforced when claims are applied from /session.
    seedToken(makeToken({ expiresInSeconds: -5 }))
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isBootstrapping).toBe(false))
    expect(result.current.isAuthenticated).toBe(false)
  })

  it('adoptToken establishes a cookie session and discards the raw JWT', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isBootstrapping).toBe(false))

    let accepted = false
    await act(async () => {
      accepted = await result.current.adoptToken(
        makeToken({ sub: 'sso.user', roles: ['clinician'] }),
      )
    })
    expect(accepted).toBe(true)
    expect(result.current.user?.sub).toBe('sso.user')
    expect(result.current.token).toBeNull()
    expect(window.sessionStorage.length).toBe(0)
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('adoptToken rejects an expired token', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isBootstrapping).toBe(false))
    let accepted = true
    await act(async () => {
      accepted = await result.current.adoptToken(makeToken({ expiresInSeconds: -1 }))
    })
    expect(accepted).toBe(false)
    expect(result.current.isAuthenticated).toBe(false)
  })

  it('hasRole reflects session roles', async () => {
    seedToken(makeToken({ roles: ['clinician'] }))
    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.isAuthenticated).toBe(true))
    expect(result.current.hasRole('clinician')).toBe(true)
    expect(result.current.hasRole('admin')).toBe(false)
    expect(result.current.hasRole('admin', 'clinician')).toBe(true)
  })

  it('throws when useAuth is used outside a provider', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => renderHook(() => useAuth())).toThrow(/AuthProvider/)
    spy.mockRestore()
  })
})
