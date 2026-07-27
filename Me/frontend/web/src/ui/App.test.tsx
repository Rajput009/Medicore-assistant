/** Routing, route guards, role-based navigation and the OIDC callback. */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { AuthProvider } from '../auth/AuthContext'
import { SESSION_STORAGE_KEY, STORAGE_KEY } from '../auth/token'
import { makeToken, seedToken } from '../test/helpers'
import { App } from './App'
import { visibleNavItems } from './AppShell'
import { ErrorBoundary } from './ErrorBoundary'

function renderApp(route: string, token?: string) {
  if (token) seedToken(token)
  const user = userEvent.setup()
  const utils = render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  )
  return { ...utils, user }
}

describe('route protection', () => {
  it('redirects an anonymous visitor to the login page', async () => {
    renderApp('/')
    expect(await screen.findByRole('heading', { name: /sign in/i })).toBeInTheDocument()
  })

  it.each(['/fhir', '/flow', '/cds', '/admin'])('protects %s', async (route) => {
    renderApp(route)
    expect(await screen.findByRole('heading', { name: /sign in/i })).toBeInTheDocument()
  })

  it('renders the dashboard for an authenticated user', async () => {
    renderApp('/', makeToken())
    expect(await screen.findByRole('heading', { name: /system overview/i })).toBeInTheDocument()
  })

  it('shows a not-found page for an unknown route', async () => {
    renderApp('/nope', makeToken())
    expect(await screen.findByRole('heading', { name: /page not found/i })).toBeInTheDocument()
  })
})

describe('role-based access', () => {
  it('denies the FHIR explorer to a viewer', async () => {
    renderApp('/fhir', makeToken({ roles: ['viewer'] }))
    expect(await screen.findByRole('heading', { name: /access denied/i })).toBeInTheDocument()
  })

  it('allows the FHIR explorer for a clinician', async () => {
    renderApp('/fhir', makeToken({ roles: ['clinician'] }))
    expect(await screen.findByRole('heading', { name: /fhir explorer/i })).toBeInTheDocument()
  })

  it('denies cache admin to a clinician', async () => {
    renderApp('/admin', makeToken({ roles: ['clinician'] }))
    expect(await screen.findByRole('heading', { name: /access denied/i })).toBeInTheDocument()
  })

  it('allows cache admin for an admin', async () => {
    renderApp('/admin', makeToken({ roles: ['admin'] }))
    expect(await screen.findByRole('heading', { name: /cache administration/i })).toBeInTheDocument()
  })

  it('denies decision support to a viewer', async () => {
    // Vitals are clinical data; the CDS service enforces this server-side too.
    renderApp('/cds', makeToken({ roles: ['viewer'] }))
    expect(await screen.findByRole('heading', { name: /access denied/i })).toBeInTheDocument()
  })

  it('denies patient flow to a viewer', async () => {
    renderApp('/flow', makeToken({ roles: ['viewer'] }))
    expect(await screen.findByRole('heading', { name: /access denied/i })).toBeInTheDocument()
  })

  it('allows patient flow and decision support for a clinician', async () => {
    renderApp('/flow', makeToken({ roles: ['clinician'] }))
    expect(await screen.findByRole('heading', { name: /^patient flow$/i })).toBeInTheDocument()
  })

  it('always allows the overview page', async () => {
    renderApp('/', makeToken({ roles: ['viewer'] }))
    expect(await screen.findByRole('heading', { name: /system overview/i })).toBeInTheDocument()
  })
})

describe('navigation', () => {
  it('hides every clinical link from a viewer', () => {
    expect(visibleNavItems(['viewer']).map((i) => i.to)).toEqual(['/'])
  })

  it('shows FHIR but not admin to a clinician', () => {
    expect(visibleNavItems(['clinician']).map((i) => i.to)).toEqual([
      '/',
      '/worklist',
      '/wards',
      '/fhir',
      '/flow',
      '/cds',
    ])
  })

  it('shows everything to an admin', () => {
    expect(visibleNavItems(['admin'])).toHaveLength(7)
  })

  it('shows only the overview when the user has no roles', () => {
    expect(visibleNavItems([]).map((i) => i.to)).toEqual(['/'])
  })

  it('navigates between pages via the sidebar', async () => {
    const { user } = renderApp('/', makeToken({ roles: ['admin'] }))
    await user.click(await screen.findByRole('link', { name: /patient flow/i }))
    expect(await screen.findByRole('heading', { name: /^patient flow$/i })).toBeInTheDocument()
  })

  it('marks the active link with aria-current', async () => {
    renderApp('/cds', makeToken())
    await waitFor(() =>
      expect(screen.getByRole('link', { name: /decision support/i })).toHaveAttribute(
        'aria-current',
        'page',
      ),
    )
  })

  it('signs out and returns to the login page', async () => {
    const { user } = renderApp('/', makeToken())
    await user.click(await screen.findByRole('button', { name: /sign out/i }))
    expect(await screen.findByRole('heading', { name: /sign in/i })).toBeInTheDocument()
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('exposes a skip link for keyboard users', async () => {
    renderApp('/', makeToken())
    expect(await screen.findByRole('link', { name: /skip to main content/i })).toHaveAttribute(
      'href',
      '#main-content',
    )
  })
})

describe('OIDC callback', () => {
  it('accepts a token from the URL fragment and lands on the dashboard', async () => {
    const token = makeToken({ sub: 'sso.user' })
    renderApp(`/oidc/callback#access_token=${token}`)
    expect(await screen.findByRole('heading', { name: /system overview/i })).toBeInTheDocument()
    // Cookie-only: raw JWT must never land in web storage.
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('accepts a token from the query string', async () => {
    const token = makeToken({ sub: 'sso.user' })
    renderApp(`/oidc/callback?access_token=${token}`)
    expect(await screen.findByRole('heading', { name: /system overview/i })).toBeInTheDocument()
  })

  it('reports a missing token', async () => {
    renderApp('/oidc/callback')
    expect(await screen.findByRole('alert')).toHaveTextContent(/no access token/i)
  })

  it('rejects an expired token from the IdP', async () => {
    renderApp(`/oidc/callback#access_token=${makeToken({ expiresInSeconds: -5 })}`)
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid or expired/i)
  })

  it('rejects a malformed token from the IdP', async () => {
    renderApp('/oidc/callback#access_token=not-a-jwt')
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid or expired/i)
  })
})

describe('ErrorBoundary', () => {
  const Boom: React.FC = () => {
    throw new Error('kaboom')
  }

  it('renders a recovery card instead of a blank page', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('heading', { name: /something went wrong/i })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('kaboom')
    spy.mockRestore()
  })

  it('renders children when nothing throws', () => {
    render(
      <ErrorBoundary>
        <p>all good</p>
      </ErrorBoundary>,
    )
    expect(screen.getByText('all good')).toBeInTheDocument()
  })
})
