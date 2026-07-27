/**
 * JWT helpers and session storage.
 *
 * Decode is used only to drive UI affordances (which nav items to show,
 * whether the session looks expired). The gateway remains the only authority:
 * every protected call is still verified server-side.
 *
 * Storage strategy (defence in depth against XSS):
 *   1. Preferred: httpOnly Secure cookie set by the auth service. JS never
 *      sees the raw JWT; fetch uses `credentials: 'include'`.
 *   2. In-memory hold of the token for Authorization headers (API clients that
 *      cannot rely on cookies, and to populate UI claims without a /session round-trip).
 *   3. sessionStorage fallback (tab-scoped) when cookies are unavailable — never
 *      localStorage. sessionStorage is cleared when the tab closes and is not
 *      shared with other tabs' long-lived state the way localStorage is.
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

/** Legacy key — migrated away from localStorage on read. */
export const STORAGE_KEY = 'medicore.token'
export const SESSION_STORAGE_KEY = 'medicore.session.token'

/** Process-lifetime hold; never written to durable storage by default. */
let memoryToken: string | null = null

/**
 * Session token access.
 *
 * - `write` keeps the token in memory and (optionally) tab-scoped sessionStorage
 *   so a refresh within the same tab can restore UI state. It never writes to
 *   localStorage.
 * - On first `read`, any legacy localStorage value is migrated to sessionStorage
 *   and removed from localStorage.
 */
export const tokenStorage = {
  read(): string | null {
    if (memoryToken) return memoryToken
    try {
      // Migrate away from localStorage (XSS-durable) if a prior build left a value.
      const legacy = window.localStorage.getItem(STORAGE_KEY)
      if (legacy) {
        try {
          window.sessionStorage.setItem(SESSION_STORAGE_KEY, legacy)
        } catch {
          /* ignore */
        }
        try {
          window.localStorage.removeItem(STORAGE_KEY)
        } catch {
          /* ignore */
        }
        memoryToken = legacy
        return legacy
      }
      const tab = window.sessionStorage.getItem(SESSION_STORAGE_KEY)
      if (tab) {
        memoryToken = tab
        return tab
      }
    } catch {
      /* private mode / disabled storage */
    }
    return null
  },
  write(token: string): void {
    memoryToken = token
    try {
      window.sessionStorage.setItem(SESSION_STORAGE_KEY, token)
    } catch {
      /* non-fatal: memory-only for this tab */
    }
    // Ensure we never leave a durable copy behind.
    try {
      window.localStorage.removeItem(STORAGE_KEY)
    } catch {
      /* ignore */
    }
  },
  clear(): void {
    memoryToken = null
    try {
      window.sessionStorage.removeItem(SESSION_STORAGE_KEY)
    } catch {
      /* ignore */
    }
    try {
      window.localStorage.removeItem(STORAGE_KEY)
    } catch {
      /* ignore */
    }
  },
}
