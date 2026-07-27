import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'

import { api, ApiError } from '../api/client'
import type { AuthUser, Role } from '../api/types'
import {
  decodeToken,
  isExpired,
  millisUntilExpiry,
  purgeLegacyTokenStorage,
  sessionUserFromClaims,
} from './token'

type AuthContextValue = {
  /** Always null in cookie-only mode — kept for type compatibility. */
  token: string | null
  user: AuthUser | null
  isAuthenticated: boolean
  /** True until the first /session probe finishes (avoids login flash). */
  isBootstrapping: boolean
  loginError: string | null
  isLoggingIn: boolean
  login: (username: string, password: string) => Promise<boolean>
  logout: () => void
  /**
   * One-shot OIDC handoff: POSTs the IdP token to /auth/session/establish so
   * the httpOnly cookie is set server-side. The raw JWT is never stored in JS.
   */
  adoptToken: (token: string) => Promise<boolean>
  hasRole: (...roles: Role[]) => boolean
  /** Re-fetch claims from the cookie session (e.g. after tab focus). */
  refreshSession: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loginError, setLoginError] = useState<string | null>(null)
  const [isLoggingIn, setIsLoggingIn] = useState(false)
  const [isBootstrapping, setIsBootstrapping] = useState(true)

  // Drop any JWT an older build left in web storage.
  useEffect(() => {
    purgeLegacyTokenStorage()
  }, [])

  const applySession = useCallback((s: { sub: string; roles?: string[]; exp?: number }) => {
    const next = sessionUserFromClaims(s)
    if (!next || isExpired(next)) {
      setUser(null)
      return false
    }
    setUser(next)
    return true
  }, [])

  const refreshSession = useCallback(async () => {
    try {
      const s = await api.session()
      if (!applySession(s)) setUser(null)
    } catch {
      setUser(null)
    }
  }, [applySession])

  // Boot: hydrate from httpOnly cookie via /session.
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const s = await api.session()
        if (!cancelled) applySession(s)
      } catch {
        if (!cancelled) setUser(null)
      } finally {
        if (!cancelled) setIsBootstrapping(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [applySession])

  const logout = useCallback(() => {
    void api.logout().catch(() => undefined)
    purgeLegacyTokenStorage()
    setUser(null)
    setLoginError(null)
  }, [])

  // Expire the UI session when the cookie JWT would expire.
  useEffect(() => {
    if (!user) return
    const ms = millisUntilExpiry(user)
    if (ms === null) return
    if (ms <= 0) {
      logout()
      return
    }
    const delay = Math.min(ms, 2_147_483_000)
    const timer = window.setTimeout(logout, delay)
    return () => window.clearTimeout(timer)
  }, [user, logout])

  // Multi-tab: storage events only clear legacy keys; BroadcastChannel for logout.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === 'medicore.token' || e.key === 'medicore.session.token') {
        purgeLegacyTokenStorage()
      }
    }
    window.addEventListener('storage', onStorage)

    let bc: BroadcastChannel | null = null
    try {
      bc = new BroadcastChannel('medicore-auth')
      bc.onmessage = (ev) => {
        if (ev.data === 'logout') {
          setUser(null)
          purgeLegacyTokenStorage()
        }
        if (ev.data === 'login') {
          void refreshSession()
        }
      }
    } catch {
      /* BroadcastChannel unavailable */
    }

    return () => {
      window.removeEventListener('storage', onStorage)
      bc?.close()
    }
  }, [refreshSession])

  const broadcast = useCallback((msg: string) => {
    try {
      const bc = new BroadcastChannel('medicore-auth')
      bc.postMessage(msg)
      bc.close()
    } catch {
      /* ignore */
    }
  }, [])

  const adoptToken = useCallback(
    async (raw: string): Promise<boolean> => {
      const decoded = decodeToken(raw)
      if (!decoded || isExpired(decoded)) return false
      try {
        const s = await api.establishSession(raw)
        if (!applySession(s)) return false
        purgeLegacyTokenStorage()
        setLoginError(null)
        broadcast('login')
        return true
      } catch {
        return false
      }
    },
    [applySession, broadcast],
  )

  const login = useCallback(
    async (username: string, password: string): Promise<boolean> => {
      setIsLoggingIn(true)
      setLoginError(null)
      try {
        // Server sets httpOnly cookie; body token is ignored and never stored.
        await api.login(username, password)
        const s = await api.session()
        if (!applySession(s)) {
          setLoginError('Server did not establish a session cookie.')
          return false
        }
        purgeLegacyTokenStorage()
        broadcast('login')
        return true
      } catch (err) {
        setUser(null)
        if (err instanceof ApiError) {
          setLoginError(
            err.status === 401
              ? 'Incorrect username or password.'
              : err.status === 404
                ? 'Password sign-in is disabled on this environment. Use SSO.'
                : err.detail || 'Sign-in failed.',
          )
        } else {
          setLoginError('Cannot reach the auth service.')
        }
        return false
      } finally {
        setIsLoggingIn(false)
      }
    },
    [applySession, broadcast],
  )

  const logoutAndBroadcast = useCallback(() => {
    logout()
    broadcast('logout')
  }, [logout, broadcast])

  const hasRole = useCallback(
    (...roles: Role[]) => {
      if (!user) return false
      if (roles.length === 0) return true
      return roles.some((r) => user.roles.includes(r))
    },
    [user],
  )

  const value = useMemo<AuthContextValue>(
    () => ({
      token: null,
      user,
      isAuthenticated: Boolean(user),
      isBootstrapping,
      loginError,
      isLoggingIn,
      login,
      logout: logoutAndBroadcast,
      adoptToken,
      hasRole,
      refreshSession,
    }),
    [
      user,
      isBootstrapping,
      loginError,
      isLoggingIn,
      login,
      logoutAndBroadcast,
      adoptToken,
      hasRole,
      refreshSession,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
