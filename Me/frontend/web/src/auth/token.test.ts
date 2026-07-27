/** Unit tests for JWT decoding and cookie-only storage hygiene. */

import { describe, expect, it, vi } from 'vitest'

import { makeToken } from '../test/helpers'
import {
  decodeToken,
  hasAnyRole,
  isExpired,
  millisUntilExpiry,
  purgeLegacyTokenStorage,
  sessionUserFromClaims,
  tokenStorage,
} from './token'

function b64url(value: string): string {
  return btoa(value).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function tokenWithPayload(payload: unknown): string {
  return [b64url(JSON.stringify({ alg: 'HS256' })), b64url(JSON.stringify(payload)), 'sig'].join(
    '.',
  )
}

describe('decodeToken', () => {
  it('extracts sub, roles and exp', () => {
    const user = decodeToken(makeToken({ sub: 'dr.who', roles: ['admin', 'clinician'] }))
    expect(user).toMatchObject({ sub: 'dr.who', roles: ['admin', 'clinician'] })
    expect(typeof user?.exp).toBe('number')
  })

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['empty string', ''],
    ['not a jwt', 'abc'],
    ['two segments', 'a.b'],
    ['four segments', 'a.b.c.d'],
    ['non-base64 payload', 'a.!!!!.c'],
  ])('returns null for %s', (_label, input) => {
    expect(decodeToken(input as string | null | undefined)).toBeNull()
  })

  it('returns null when the payload is not JSON', () => {
    expect(decodeToken(`${b64url('{}')}.${b64url('not json')}.sig`)).toBeNull()
  })

  it('returns null when sub is missing or empty', () => {
    expect(decodeToken(tokenWithPayload({ roles: ['admin'] }))).toBeNull()
    expect(decodeToken(tokenWithPayload({ sub: '' }))).toBeNull()
  })

  it('defaults roles to an empty array when absent', () => {
    expect(decodeToken(tokenWithPayload({ sub: 'x' }))?.roles).toEqual([])
  })

  it('accepts roles as a space or comma separated string', () => {
    expect(decodeToken(tokenWithPayload({ sub: 'x', roles: 'admin clinician' }))?.roles).toEqual([
      'admin',
      'clinician',
    ])
  })

  it('ignores unknown roles and de-duplicates', () => {
    const user = decodeToken(
      tokenWithPayload({ sub: 'x', roles: ['admin', 'ADMIN', 'superuser', 'viewer'] }),
    )
    expect(user?.roles).toEqual(['admin', 'viewer'])
  })
})

describe('isExpired / millisUntilExpiry / hasAnyRole', () => {
  it('is false for a future expiry', () => {
    expect(isExpired(decodeToken(makeToken({ expiresInSeconds: 60 })))).toBe(false)
  })

  it('is true for a past expiry', () => {
    expect(isExpired(decodeToken(makeToken({ expiresInSeconds: -60 })))).toBe(true)
  })

  it('is false when the token has no exp claim', () => {
    expect(isExpired(decodeToken(makeToken({ expiresInSeconds: null })))).toBe(false)
  })

  it('millisUntilExpiry never returns a negative value', () => {
    expect(millisUntilExpiry({ sub: 'x', roles: [], exp: 1 }, 9_999_999_999)).toBe(0)
  })

  it('hasAnyRole matches required roles', () => {
    const user = { sub: 'x', roles: ['clinician' as const] }
    expect(hasAnyRole(user, ['clinician', 'admin'])).toBe(true)
    expect(hasAnyRole(user, ['admin'])).toBe(false)
    expect(hasAnyRole(null, [])).toBe(false)
  })
})

describe('sessionUserFromClaims', () => {
  it('normalises roles from the session endpoint', () => {
    expect(sessionUserFromClaims({ sub: 'a', roles: ['ADMIN', 'nope'] })).toEqual({
      sub: 'a',
      roles: ['admin'],
      exp: undefined,
    })
  })
})

describe('cookie-only storage hygiene', () => {
  it('tokenStorage never persists a JWT', () => {
    tokenStorage.write('abc.def.ghi')
    expect(tokenStorage.read()).toBeNull()
    expect(window.sessionStorage.getItem('medicore.session.token')).toBeNull()
    expect(window.localStorage.getItem('medicore.token')).toBeNull()
  })

  it('purgeLegacyTokenStorage wipes old localStorage and sessionStorage keys', () => {
    window.localStorage.setItem('medicore.token', 'legacy-jwt')
    window.sessionStorage.setItem('medicore.session.token', 'legacy-jwt')
    purgeLegacyTokenStorage()
    expect(window.localStorage.getItem('medicore.token')).toBeNull()
    expect(window.sessionStorage.getItem('medicore.session.token')).toBeNull()
  })

  it('degrades gracefully when storage throws', () => {
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(() => purgeLegacyTokenStorage()).not.toThrow()
    expect(() => tokenStorage.write('x')).not.toThrow()
    expect(tokenStorage.read()).toBeNull()
  })
})
