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
import { decodeToken, isExpired, millisUntilExpiry, tokenStorage } from './token'

type AuthContextValue = {
  token: string | null
  user: AuthUser | null
  isAuthenticated: boolean
  loginError: string | null
  isLoggingIn: boolean
  login: (username: string, password: string) => Promise<boolean>
  logout: () => void
  /** Adopt a token minted elsewhere (e.g. the OIDC callback). */
  adoptToken: (token: string) => boolean
  hasRole: (...roles: Role[]) => boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

/** Reads a persisted token, discarding it when malformed or already expired. */
function loadInitialToken(): string | null {
  const stored = tokenStorage.read()
  if (!stored) return null
  const decoded = decodeToken(stored)
  if (!decoded || isExpired(decoded)) {
    tokenStorage.clear()
    return null
  }
  return stored
}

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [token, setToken] = useState<string | null>(loadInitialToken)
  const [loginError, setLoginError] = useState<string | null>(null)
  const [isLoggingIn, setIsLoggingIn] = useState(false)

  const user = useMemo(() => decodeToken(token), [token])

  const logout = useCallback(() => {
    tokenStorage.clear()
    setToken(null)
    setLoginError(null)
  }, [])

  // Expire the session in-place so the UI can't keep showing privileged nav
  // for a token the gateway will reject.
  useEffect(() => {
    if (!user) return
    const ms = millisUntilExpiry(user)
    if (ms === null) return
    if (ms <= 0) {
      logout()
      return
    }
    // setTimeout saturates above ~24.8 days; clamp to stay well inside range.
    const delay = Math.min(ms, 2_147_483_000)
    const timer = window.setTimeout(logout, delay)
    return () => window.clearTimeout(timer)
  }, [user, logout])

  // Keep multiple tabs consistent: signing out in one signs out the rest.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== null && e.key !== 'medicore.token') return
      const next = loadInitialToken()
      setToken((prev) => (prev === next ? prev : next))
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const adoptToken = useCallback((raw: string): boolean => {
    const decoded = decodeToken(raw)
    if (!decoded || isExpired(decoded)) return false
    tokenStorage.write(raw)
    setToken(raw)
    setLoginError(null)
    return true
  }, [])

  const login = useCallback(
    async (username: string, password: string): Promise<boolean> => {
      setIsLoggingIn(true)
      setLoginError(null)
      try {
        const res = await api.login(username, password)
        if (!res?.access_token || !adoptToken(res.access_token)) {
          setLoginError('Server returned an unusable token.')
          return false
        }
        return true
      } catch (err) {
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
    [adoptToken],
  )

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
      token,
      user,
      isAuthenticated: Boolean(user),
      loginError,
      isLoggingIn,
      login,
      logout,
      adoptToken,
      hasRole,
    }),
    [token, user, loginError, isLoggingIn, login, logout, adoptToken, hasRole],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
