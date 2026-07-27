/**
 * JWT helpers (decode-only) and legacy storage cleanup.
 *
 * The SPA is **cookie-only**: the access token lives in an httpOnly cookie set
 * by the auth service. JavaScript never persists the raw JWT — not in
 * localStorage, not in sessionStorage, not in memory across navigations.
 *
 * `decodeToken` remains for OIDC fragment handoff (one-shot, then discarded)
 * and for tests. UI claims come from `GET /auth/session`.
 */

import type { AuthUser, Role } from '../api/types'

const VALID_ROLES: readonly string[] = ['admin', 'clinician', 'viewer']

/** Legacy keys — wiped on boot so older builds cannot leave a durable JWT. */
export const STORAGE_KEY = 'medicore.token'
export const SESSION_STORAGE_KEY = 'medicore.session.token'

/** Base64url -> UTF-8 string, tolerating missing padding. */
function decodeSegment(segment: string): string {
  const padded = segment.replace(/-/g, '+').replace(/_/g, '/')
  const withPadding = padded + '='.repeat((4 - (padded.length % 4)) % 4)
  const binary = atob(withPadding)
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0))
  return new TextDecoder('utf-8').decode(bytes)
}

function normaliseRoles(raw: unknown): Role[] {
  let list: unknown[] = []
  if (Array.isArray(raw)) list = raw
  else if (typeof raw === 'string') list = raw.split(/[,\s]+/)

  const seen = new Set<Role>()
  for (const entry of list) {
    const value = String(entry).trim().toLowerCase()
    if (VALID_ROLES.includes(value)) seen.add(value as Role)
  }
  return [...seen]
}

/**
 * Normalise a ward/department scope claim. Accepts a list or a delimited
 * string (IdPs emit both) and drops blanks. An empty result means
 * "unrestricted", which is how the server reads an absent claim.
 */
export function normaliseScope(raw: unknown): string[] {
  let list: unknown[] = []
  if (Array.isArray(raw)) list = raw
  else if (typeof raw === 'string') list = raw.split(/[,\s]+/)

  const seen: string[] = []
  for (const entry of list) {
    const value = String(entry).trim()
    if (value && !seen.includes(value)) seen.push(value)
  }
  return seen
}

/**
 * Decode a JWT payload without verifying its signature.
 * Returns null for anything malformed rather than throwing.
 */
export function decodeToken(token: string | null | undefined): AuthUser | null {
  if (!token || typeof token !== 'string') return null
  const parts = token.split('.')
  if (parts.length !== 3) return null

  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(decodeSegment(parts[1]))
  } catch {
    return null
  }
  if (!payload || typeof payload !== 'object') return null

  const sub = payload.sub
  if (typeof sub !== 'string' || sub === '') return null

  const exp =
    typeof payload.exp === 'number' && Number.isFinite(payload.exp) ? payload.exp : undefined

  return {
    sub,
    roles: normaliseRoles(payload.roles),
    exp,
    wards: normaliseScope(payload.wards),
    departments: normaliseScope(payload.departments),
  }
}

/** True when the token carries an `exp` that has already passed. */
export function isExpired(
  user: Partial<AuthUser> | null,
  nowMs: number = Date.now(),
): boolean {
  if (!user?.exp) return false
  return user.exp * 1000 <= nowMs
}

/** Milliseconds until expiry; null when the token never expires. */
export function millisUntilExpiry(
  user: Partial<AuthUser> | null,
  nowMs: number = Date.now(),
): number | null {
  if (!user?.exp) return null
  return Math.max(0, user.exp * 1000 - nowMs)
}

export function hasAnyRole(
  user: Partial<AuthUser> | null,
  required: readonly Role[],
): boolean {
  if (!user) return false
  if (required.length === 0) return true
  const roles = user.roles ?? []
  return required.some((r) => roles.includes(r))
}

export function sessionUserFromClaims(s: {
  sub: string
  roles?: string[]
  exp?: number
  wards?: string[]
  departments?: string[]
}): AuthUser | null {
  if (!s?.sub) return null
  const roles = normaliseRoles(s.roles ?? [])
  const exp = typeof s.exp === 'number' && Number.isFinite(s.exp) ? s.exp : undefined
  return {
    sub: s.sub,
    roles,
    exp,
    wards: normaliseScope(s.wards),
    departments: normaliseScope(s.departments),
  }
}

/**
 * Wipe any JWT left by older builds. Does **not** store tokens.
 * Safe to call on every app boot.
 */
export function purgeLegacyTokenStorage(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
  try {
    window.sessionStorage.removeItem(SESSION_STORAGE_KEY)
  } catch {
    /* ignore */
  }
  // Older keys if any
  try {
    window.localStorage.removeItem('medicore.session.token')
  } catch {
    /* ignore */
  }
}

/**
 * @deprecated Cookie-only mode: no-op write / always-null read.
 * Kept so tests that still import `tokenStorage` fail closed (never persist).
 */
export const tokenStorage = {
  read(): string | null {
    purgeLegacyTokenStorage()
    return null
  },
  write(_token: string): void {
    purgeLegacyTokenStorage()
  },
  clear(): void {
    purgeLegacyTokenStorage()
  },
}
