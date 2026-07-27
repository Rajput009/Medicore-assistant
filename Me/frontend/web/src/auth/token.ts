/**
 * JWT helpers.
 *
 * These decode the token purely to drive UI affordances (which nav items to
 * show, whether the session looks expired). The gateway remains the only
 * authority: every protected call is still verified server-side, so a user who
 * tampers with a token in localStorage gains nothing but a 401.
 */

import type { AuthUser, Role } from '../api/types'

const VALID_ROLES: readonly string[] = ['admin', 'clinician', 'viewer']

/** Base64url -> UTF-8 string, tolerating missing padding. */
function decodeSegment(segment: string): string {
  const padded = segment.replace(/-/g, '+').replace(/_/g, '/')
  const withPadding = padded + '='.repeat((4 - (padded.length % 4)) % 4)
  const binary = atob(withPadding)
  // Round-trip through percent-encoding so non-ASCII names survive.
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

  const exp = typeof payload.exp === 'number' && Number.isFinite(payload.exp) ? payload.exp : undefined

  return { sub, roles: normaliseRoles(payload.roles), exp }
}

/** True when the token carries an `exp` that has already passed. */
export function isExpired(user: AuthUser | null, nowMs: number = Date.now()): boolean {
  if (!user?.exp) return false
  return user.exp * 1000 <= nowMs
}

/** Milliseconds until expiry; null when the token never expires. */
export function millisUntilExpiry(user: AuthUser | null, nowMs: number = Date.now()): number | null {
  if (!user?.exp) return null
  return Math.max(0, user.exp * 1000 - nowMs)
}

export function hasAnyRole(user: AuthUser | null, required: readonly Role[]): boolean {
  if (!user) return false
  if (required.length === 0) return true
  return required.some((r) => user.roles.includes(r))
}

export const STORAGE_KEY = 'medicore.token'

/** localStorage access guarded for Safari private mode / disabled storage. */
export const tokenStorage = {
  read(): string | null {
    try {
      return window.localStorage.getItem(STORAGE_KEY)
    } catch {
      return null
    }
  },
  write(token: string): void {
    try {
      window.localStorage.setItem(STORAGE_KEY, token)
    } catch {
      /* non-fatal: session becomes tab-scoped */
    }
  },
  clear(): void {
    try {
      window.localStorage.removeItem(STORAGE_KEY)
    } catch {
      /* ignore */
    }
  },
}
