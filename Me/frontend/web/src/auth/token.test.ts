/** Unit tests for JWT decoding, expiry and role handling — including edge cases. */

import { describe, expect, it, vi } from 'vitest'

import { makeToken } from '../test/helpers'
import { decodeToken, hasAnyRole, isExpired, millisUntilExpiry, tokenStorage } from './token'

function b64url(value: string): string {
  return btoa(value).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function tokenWithPayload(payload: unknown): string {
  return [b64url(JSON.stringify({ alg: 'HS256' })), b64url(JSON.stringify(payload)), 'sig'].join('.')
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

  it('returns null when sub is not a string', () => {
    expect(decodeToken(tokenWithPayload({ sub: 12345 }))).toBeNull()
  })

  it('defaults roles to an empty array when absent', () => {
    expect(decodeToken(tokenWithPayload({ sub: 'x' }))?.roles).toEqual([])
  })

  it('accepts roles as a space or comma separated string', () => {
    expect(decodeToken(tokenWithPayload({ sub: 'x', roles: 'admin clinician' }))?.roles).toEqual([
      'admin',
      'clinician',
    ])
    expect(decodeToken(tokenWithPayload({ sub: 'x', roles: 'admin,viewer' }))?.roles).toEqual([
      'admin',
      'viewer',
    ])
  })

  it('ignores unknown roles and de-duplicates', () => {
    const user = decodeToken(
      tokenWithPayload({ sub: 'x', roles: ['admin', 'ADMIN', 'superuser', 'viewer'] }),
    )
    expect(user?.roles).toEqual(['admin', 'viewer'])
  })

  it('handles a payload with non-ASCII characters', () => {
    // btoa cannot encode raw non-ASCII, so encode UTF-8 bytes first.
    const json = JSON.stringify({ sub: 'zoë.科', roles: ['viewer'] })
    const utf8 = String.fromCharCode(...new TextEncoder().encode(json))
    const token = `${b64url('{}')}.${b64url(utf8)}.sig`
    expect(decodeToken(token)?.sub).toBe('zoë.科')
  })

  it('tolerates base64url payloads without padding', () => {
    const token = makeToken({ sub: 'abc' })
    expect(token.split('.')[1]).not.toContain('=')
    expect(decodeToken(token)?.sub).toBe('abc')
  })

  it('treats a non-numeric exp as absent', () => {
    expect(decodeToken(tokenWithPayload({ sub: 'x', exp: 'soon' }))?.exp).toBeUndefined()
  })
})

describe('isExpired', () => {
  it('is false for a future expiry', () => {
    expect(isExpired(decodeToken(makeToken({ expiresInSeconds: 60 })))).toBe(false)
  })

  it('is true for a past expiry', () => {
    expect(isExpired(decodeToken(makeToken({ expiresInSeconds: -60 })))).toBe(true)
  })

  it('is false when the token has no exp claim', () => {
    expect(isExpired(decodeToken(makeToken({ expiresInSeconds: null })))).toBe(false)
  })

  it('is false for a null user', () => {
    expect(isExpired(null)).toBe(false)
  })

  it('treats the exact expiry instant as expired', () => {
    const exp = 1_000_000
    expect(isExpired({ sub: 'x', roles: [], exp }, exp * 1000)).toBe(true)
  })
})

describe('millisUntilExpiry', () => {
  it('returns null without an exp claim', () => {
    expect(millisUntilExpiry({ sub: 'x', roles: [] })).toBeNull()
  })

  it('never returns a negative value', () => {
    expect(millisUntilExpiry({ sub: 'x', roles: [], exp: 1 }, 9_999_999_999)).toBe(0)
  })

  it('computes the remaining time', () => {
    expect(millisUntilExpiry({ sub: 'x', roles: [], exp: 200 }, 100_000)).toBe(100_000)
  })
})

describe('hasAnyRole', () => {
  const user = { sub: 'x', roles: ['clinician' as const] }

  it('matches when a required role is present', () => {
    expect(hasAnyRole(user, ['clinician', 'admin'])).toBe(true)
  })

  it('rejects when no required role is present', () => {
    expect(hasAnyRole(user, ['admin'])).toBe(false)
  })

  it('allows an empty requirement list', () => {
    expect(hasAnyRole(user, [])).toBe(true)
  })

  it('rejects a null user even with an empty requirement', () => {
    expect(hasAnyRole(null, [])).toBe(false)
  })
})

describe('tokenStorage', () => {
  it('round-trips a value via sessionStorage, not localStorage', () => {
    tokenStorage.write('abc')
    expect(tokenStorage.read()).toBe('abc')
    expect(window.sessionStorage.getItem('medicore.session.token')).toBe('abc')
    expect(window.localStorage.getItem('medicore.token')).toBeNull()
    tokenStorage.clear()
    expect(tokenStorage.read()).toBeNull()
  })

  it('migrates a legacy localStorage value and deletes it', () => {
    window.localStorage.setItem('medicore.token', 'legacy-jwt')
    expect(tokenStorage.read()).toBe('legacy-jwt')
    expect(window.localStorage.getItem('medicore.token')).toBeNull()
    expect(window.sessionStorage.getItem('medicore.session.token')).toBe('legacy-jwt')
  })

  it('degrades gracefully when storage throws', () => {
    // Safari private mode / storage disabled by policy.
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('denied')
    })

    // Memory still works after a successful write path that swallows errors.
    expect(() => tokenStorage.write('x')).not.toThrow()
    expect(tokenStorage.read()).toBe('x')
    expect(() => tokenStorage.clear()).not.toThrow()
  })
})
