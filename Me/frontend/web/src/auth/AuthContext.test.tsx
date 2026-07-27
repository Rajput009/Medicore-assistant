/** Session lifecycle: login, persistence, expiry, cross-tab sync. */

import { act, renderHook, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { server } from '../test/server'
import { makeToken, seedToken } from '../test/helpers'
import { AuthProvider, useAuth } from './AuthContext'
import { STORAGE_KEY } from './token'

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <AuthProvider>{children}</AuthProvider>
)

describe('AuthProvider', () => {
  it('starts unauthenticated with no stored token', () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    expect(result.current.isAuthenticated).toBe(false)
    expect(result.current.user).toBeNull()
  })

  it('restores a valid stored session', () => {
    seedToken(makeToken({ sub: 'restored', roles: ['admin'] }))
    const { result } = renderHook(() => useAuth(), { wrapper })
    expect(result.current.user).toMatchObject({ sub: 'restored', roles: ['admin'] })
  })

  it('discards a stored token that has already expired', () => {
    seedToken(makeToken({ expiresInSeconds: -10 }))
    const { result } = renderHook(() => useAuth(), { wrapper })
    expect(result.current.isAuthenticated).toBe(false)
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('discards a malformed stored token', () => {
    seedToken('not-a-jwt')
    const { result } = renderHook(() => useAuth(), { wrapper })
    expect(result.current.isAuthenticated).toBe(false)
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('logs in and persists the token', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    await act(async () => {
      const ok = await result.current.login('dr.smith', 'correct-horse')
      expect(ok).toBe(true)
    })
    expect(result.current.user?.sub).toBe('dr.smith')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeTruthy()
  })

  it('reports wrong credentials without authenticating', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
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
    await act(async () => {
      await result.current.login('u', 'p')
    })
    expect(result.current.loginError).toMatch(/disabled/i)
  })

  it('reports an unreachable auth service', async () => {
    server.use(http.post('/auth/login', () => HttpResponse.error()))
    const { result } = renderHook(() => useAuth(), { wrapper })
    await act(async () => {
      await result.current.login('u', 'p')
    })
    expect(result.current.loginError).toBe('Cannot reach the auth service.')
  })

  it('rejects a token the server returns that cannot be decoded', async () => {
    server.use(
      http.post('/auth/login', () =>
        HttpResponse.json({ access_token: 'garbage', token_type: 'bearer' }),
      ),
    )
    const { result } = renderHook(() => useAuth(), { wrapper })
    await act(async () => {
      const ok = await result.current.login('u', 'p')
      expect(ok).toBe(false)
    })
    expect(result.current.loginError).toMatch(/unusable token/i)
  })

  it('logout clears the session and storage', async () => {
    seedToken(makeToken())
    const { result } = renderHook(() => useAuth(), { wrapper })
    expect(result.current.isAuthenticated).toBe(true)
    act(() => result.current.logout())
    expect(result.current.isAuthenticated).toBe(false)
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('expires the session automatically when the token lapses', async () => {
    vi.useFakeTimers()
    try {
      seedToken(makeToken({ expiresInSeconds: 2 }))
      const { result } = renderHook(() => useAuth(), { wrapper })
      expect(result.current.isAuthenticated).toBe(true)

      await act(async () => {
        vi.advanceTimersByTime(2500)
      })
      expect(result.current.isAuthenticated).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps a session without an exp claim alive', async () => {
    vi.useFakeTimers()
    try {
      seedToken(makeToken({ expiresInSeconds: null }))
      const { result } = renderHook(() => useAuth(), { wrapper })
      await act(async () => {
        vi.advanceTimersByTime(10_000_000)
      })
      expect(result.current.isAuthenticated).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('signs out when another tab clears the token', async () => {
    seedToken(makeToken())
    const { result } = renderHook(() => useAuth(), { wrapper })
    expect(result.current.isAuthenticated).toBe(true)

    act(() => {
      window.localStorage.removeItem(STORAGE_KEY)
      window.dispatchEvent(new StorageEvent('storage', { key: STORAGE_KEY }))
    })
    await waitFor(() => expect(result.current.isAuthenticated).toBe(false))
  })

  it('ignores storage events for unrelated keys', async () => {
    seedToken(makeToken())
    const { result } = renderHook(() => useAuth(), { wrapper })
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'some.other.key' }))
    })
    expect(result.current.isAuthenticated).toBe(true)
  })

  it('adoptToken rejects an expired token', () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    let accepted = true
    act(() => {
      accepted = result.current.adoptToken(makeToken({ expiresInSeconds: -1 }))
    })
    expect(accepted).toBe(false)
    expect(result.current.isAuthenticated).toBe(false)
  })

  it('hasRole reflects the token roles', () => {
    seedToken(makeToken({ roles: ['clinician'] }))
    const { result } = renderHook(() => useAuth(), { wrapper })
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
