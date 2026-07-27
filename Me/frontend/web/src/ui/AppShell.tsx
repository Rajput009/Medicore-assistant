import React from 'react'
import { NavLink } from 'react-router-dom'

import type { Role } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { Badge } from './components'

type NavItem = { to: string; label: string; roles?: Role[] }

// Roles mirror what each backend service enforces, so the console never shows
// a link that would immediately 403.
export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Overview' },
  { to: '/worklist', label: 'My patients', roles: ['clinician', 'admin'] },
  { to: '/wards', label: 'Ward board', roles: ['clinician', 'admin'] },
  { to: '/fhir', label: 'FHIR explorer', roles: ['clinician', 'admin'] },
  { to: '/flow', label: 'Patient flow', roles: ['clinician', 'admin'] },
  { to: '/cds', label: 'Decision support', roles: ['clinician', 'admin'] },
  { to: '/admin', label: 'Cache admin', roles: ['admin'] },
]

/** Nav items the given roles may see. */
export function visibleNavItems(roles: Role[]): NavItem[] {
  return NAV_ITEMS.filter((item) => !item.roles || item.roles.some((r) => roles.includes(r)))
}

export const AppShell: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user, logout } = useAuth()
  const items = visibleNavItems(user?.roles ?? [])

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>

      <nav className="sidebar" aria-label="Primary">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            +
          </span>
          <span>MediCore</span>
        </div>

        <div className="nav">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className="nav-link"
              // NavLink sets aria-current="page" automatically when active.
            >
              {item.label}
            </NavLink>
          ))}
        </div>

        <div className="sidebar-footer">
          <div>
            Signed in as <strong className="mono">{user?.sub}</strong>
          </div>
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 6 }}>
            {user?.roles.length ? (
              user.roles.map((r) => (
                <Badge key={r} tone="neutral">
                  {r}
                </Badge>
              ))
            ) : (
              <Badge tone="warn">no roles</Badge>
            )}
          </div>
        </div>
      </nav>

      <div className="main">
        <header className="topbar">
          <strong>Clinician console</strong>
          <button type="button" onClick={logout}>
            Sign out
          </button>
        </header>
        <main id="main-content" className="content" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  )
}
