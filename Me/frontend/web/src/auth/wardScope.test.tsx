/**
 * Ward / department scope in the console.
 *
 * The server is the enforcement point; these tests cover the client half:
 * that scope claims survive decoding and reach the screens that filter by
 * them. An **empty scope means unrestricted**, matching
 * `Principal.can_access_ward` — getting that backwards would either hide every
 * ward from unscoped staff or show every ward to scoped staff.
 */

import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { WardBoardPage, scopeBeds } from '../pages/WardBoardPage'
import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'
import { decodeToken, normaliseScope, sessionUserFromClaims } from './token'

const BEDS = [
  { bed_id: 'A-001', ward: 'A', occupied: false },
  { bed_id: 'ICU-001', ward: 'ICU', occupied: true, patient_id: 'MRN-1' },
  { bed_id: 'B-001', ward: 'B', occupied: false },
]

function stubBeds(beds = BEDS) {
  server.use(http.get('/flow/beds', () => HttpResponse.json(beds)))
}

describe('normaliseScope', () => {
  it('accepts an array', () => {
    expect(normaliseScope(['ICU', 'A'])).toEqual(['ICU', 'A'])
  })

  it('accepts a delimited string, as some IdPs emit', () => {
    expect(normaliseScope('ICU, A B')).toEqual(['ICU', 'A', 'B'])
  })

  it('drops blanks and duplicates', () => {
    expect(normaliseScope([' ICU ', 'ICU', '', 'A'])).toEqual(['ICU', 'A'])
  })

  it('returns an empty list for absent or junk claims', () => {
    expect(normaliseScope(undefined)).toEqual([])
    expect(normaliseScope(null)).toEqual([])
    expect(normaliseScope(42)).toEqual([])
  })
})

describe('scope decoding', () => {
  it('reads wards and departments out of a token', () => {
    const user = decodeToken(makeToken({ wards: ['ICU'], departments: ['ED'] }))
    expect(user?.wards).toEqual(['ICU'])
    expect(user?.departments).toEqual(['ED'])
  })

  it('defaults to an empty (unrestricted) scope when the claim is absent', () => {
    const user = decodeToken(makeToken())
    expect(user?.wards).toEqual([])
    expect(user?.departments).toEqual([])
  })

  it('reads scope from session claims', () => {
    const user = sessionUserFromClaims({
      sub: 'dr.smith',
      roles: ['clinician'],
      wards: ['A'],
      departments: ['ED'],
    })
    expect(user?.wards).toEqual(['A'])
    expect(user?.departments).toEqual(['ED'])
  })

  it('tolerates a session response with no scope fields', () => {
    const user = sessionUserFromClaims({ sub: 'dr.smith', roles: ['clinician'] })
    expect(user?.wards).toEqual([])
    expect(user?.departments).toEqual([])
  })
})

describe('scopeBeds', () => {
  it('keeps only beds in the caller wards', () => {
    expect(scopeBeds(BEDS, ['ICU']).map((b) => b.bed_id)).toEqual(['ICU-001'])
  })

  it('treats an empty scope as unrestricted', () => {
    expect(scopeBeds(BEDS, [])).toHaveLength(3)
  })

  it('supports multiple wards', () => {
    expect(scopeBeds(BEDS, ['A', 'B']).map((b) => b.ward)).toEqual(['A', 'B'])
  })

  it('yields nothing when the scope matches no ward', () => {
    expect(scopeBeds(BEDS, ['NOPE'])).toEqual([])
  })
})

describe('WardBoardPage honours the caller scope', () => {
  it('hides wards outside the scope', async () => {
    stubBeds()
    renderWithProviders(<WardBoardPage />, {
      token: makeToken({ wards: ['ICU'] }),
    })

    await waitFor(() => expect(screen.getByText(/ICU-001/)).toBeInTheDocument())
    expect(screen.queryByText(/A-001/)).not.toBeInTheDocument()
    expect(screen.queryByText(/B-001/)).not.toBeInTheDocument()
  })

  it('tells the clinician which wards they are limited to', async () => {
    stubBeds()
    renderWithProviders(<WardBoardPage />, {
      token: makeToken({ wards: ['ICU'] }),
    })

    await waitFor(() => expect(screen.getByText(/Scoped to your wards/)).toBeInTheDocument())
  })

  it('shows every ward when the caller is unscoped', async () => {
    stubBeds()
    renderWithProviders(<WardBoardPage />, { token: makeToken() })

    await waitFor(() => expect(screen.getByText(/A-001/)).toBeInTheDocument())
    expect(screen.getByText(/ICU-001/)).toBeInTheDocument()
    expect(screen.getByText(/B-001/)).toBeInTheDocument()
    expect(screen.queryByText(/Scoped to your wards/)).not.toBeInTheDocument()
  })
})
