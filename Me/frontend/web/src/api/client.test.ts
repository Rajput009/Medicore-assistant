/** Unit tests for the HTTP client: query building, error mapping, edge cases. */

import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { server } from '../test/server'
import { ApiError, api, buildQuery, NetworkError, request } from './client'

describe('buildQuery', () => {
  it('returns an empty string for no params', () => {
    expect(buildQuery({})).toBe('')
  })

  it('omits undefined, null and blank values', () => {
    expect(buildQuery({ a: '1', b: undefined, c: null, d: '', e: '   ' })).toBe('?a=1')
  })

  it('keeps falsy-but-meaningful values', () => {
    expect(buildQuery({ occupied: false, count: 0 })).toBe('?occupied=false&count=0')
  })

  it('percent-encodes keys and values', () => {
    expect(buildQuery({ 'a b': 'c&d=e' })).toBe('?a+b=c%26d%3De')
  })

  it('encodes non-ASCII', () => {
    expect(buildQuery({ name: 'zoë' })).toBe('?name=zo%C3%AB')
  })
})

describe('request', () => {
  it('parses a JSON body', async () => {
    server.use(http.get('/t', () => HttpResponse.json({ hello: 'world' })))
    await expect(request<{ hello: string }>('/t')).resolves.toEqual({ hello: 'world' })
  })

  it('attaches a bearer token when provided', async () => {
    let seen: string | null = null
    server.use(
      http.get('/t', ({ request: req }) => {
        seen = req.headers.get('authorization')
        return HttpResponse.json({})
      }),
    )
    await request('/t', { token: 'abc123' })
    expect(seen).toBe('Bearer abc123')
  })

  it('omits the Authorization header when no token is given', async () => {
    let seen: string | null = 'unset'
    server.use(
      http.get('/t', ({ request: req }) => {
        seen = req.headers.get('authorization')
        return HttpResponse.json({})
      }),
    )
    await request('/t', { token: null })
    expect(seen).toBeNull()
  })

  it('serialises a JSON request body', async () => {
    let body: unknown
    server.use(
      http.post('/t', async ({ request: req }) => {
        body = await req.json()
        return HttpResponse.json({ ok: true })
      }),
    )
    await request('/t', { method: 'POST', body: { a: 1 } })
    expect(body).toEqual({ a: 1 })
  })

  it('maps an error status to ApiError with the FastAPI detail', async () => {
    server.use(http.get('/t', () => HttpResponse.json({ detail: 'nope' }, { status: 403 })))
    await expect(request('/t')).rejects.toMatchObject({
      name: 'ApiError',
      status: 403,
      detail: 'nope',
    })
  })

  it('flattens pydantic validation error arrays', async () => {
    server.use(
      http.post('/t', () =>
        HttpResponse.json(
          { detail: [{ loc: ['body', 'spo2'], msg: 'less than or equal to 100' }] },
          { status: 422 },
        ),
      ),
    )
    await expect(request('/t', { method: 'POST', body: {} })).rejects.toMatchObject({
      detail: 'spo2: less than or equal to 100',
    })
  })

  it('falls back to response text when the error body is not JSON', async () => {
    server.use(
      http.get('/t', () => new HttpResponse('upstream exploded', { status: 502 })),
    )
    await expect(request('/t')).rejects.toMatchObject({ status: 502, detail: 'upstream exploded' })
  })

  it('throws NetworkError when the request cannot be made', async () => {
    server.use(http.get('/t', () => HttpResponse.error()))
    await expect(request('/t')).rejects.toBeInstanceOf(NetworkError)
  })

  it('returns undefined for 204 No Content', async () => {
    server.use(http.delete('/t', () => new HttpResponse(null, { status: 204 })))
    await expect(request('/t', { method: 'DELETE' })).resolves.toBeUndefined()
  })

  it('returns undefined for an empty 200 body', async () => {
    server.use(http.get('/t', () => new HttpResponse('', { status: 200 })))
    await expect(request('/t')).resolves.toBeUndefined()
  })

  it('raises ApiError on malformed JSON in a success response', async () => {
    server.use(http.get('/t', () => new HttpResponse('{oops', { status: 200 })))
    await expect(request('/t')).rejects.toMatchObject({ detail: 'Malformed JSON in response' })
  })

  it('propagates AbortError when the caller cancels', async () => {
    const ac = new AbortController()
    server.use(
      http.get('/t', async () => {
        await new Promise((r) => setTimeout(r, 200))
        return HttpResponse.json({})
      }),
    )
    const promise = request('/t', { signal: ac.signal })
    ac.abort()
    await expect(promise).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('exposes isAuthError / isForbidden helpers', () => {
    expect(new ApiError(401, '').isAuthError).toBe(true)
    expect(new ApiError(403, '').isForbidden).toBe(true)
    expect(new ApiError(500, '').isAuthError).toBe(false)
  })
})

describe('api endpoints', () => {
  it('URL-encodes resource ids containing slashes', async () => {
    let url = ''
    server.use(
      http.get('/api/fhir/patient/:id', ({ request: req }) => {
        url = new URL(req.url).pathname
        return HttpResponse.json({ resourceType: 'Patient' })
      }),
    )
    await api.fhirRead('Patient', 'a/b', null)
    expect(url).toBe('/api/fhir/patient/a%2Fb')
  })

  it('routes MedicationRequest to its lowercase path segment', async () => {
    let path = ''
    server.use(
      http.get('/api/fhir/medicationrequest/search', ({ request: req }) => {
        path = new URL(req.url).pathname
        return HttpResponse.json({ resourceType: 'Bundle' })
      }),
    )
    await api.fhirSearch('MedicationRequest', {}, null)
    expect(path).toBe('/api/fhir/medicationrequest/search')
  })

  it('sends the patient filter only when provided', async () => {
    const seen: string[] = []
    server.use(
      http.delete('/api/cache/:resource', ({ request: req }) => {
        seen.push(new URL(req.url).search)
        return HttpResponse.json({ status: 'ok', resource: 'Patient', patient: null, deleted: 0 })
      }),
    )
    await api.invalidateCache('Patient', null, 't')
    await api.invalidateCache('Patient', '42', 't')
    expect(seen).toEqual(['', '?patient=42'])
  })

  it('passes bed occupancy as a query parameter', async () => {
    let search = ''
    server.use(
      http.patch('/flow/beds/:id', ({ request: req }) => {
        search = new URL(req.url).search
        return HttpResponse.json({ id: 'b', ward: 'A', occupied: true })
      }),
    )
    await api.setBedOccupancy('b', true)
    expect(search).toBe('?occupied=true')
  })
})
