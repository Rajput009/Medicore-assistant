import React from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'

import type { Role } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { PatientChartDrawer } from '../patient/PatientChartDrawer'
import { PatientChartProvider } from '../patient/PatientChartContext'
import { AdminPage } from '../pages/AdminPage'
import { CdsPage } from '../pages/CdsPage'
import { DashboardPage } from '../pages/DashboardPage'
import { FhirPage } from '../pages/FhirPage'
import { LoginPage } from '../pages/LoginPage'
import { PatientFlowPage } from '../pages/PatientFlowPage'
import { WorklistPage } from '../pages/WorklistPage'
import { WardBoardPage } from '../pages/WardBoardPage'
import { AppShell } from './AppShell'
import { Alert, Card } from './components'

/** Redirects unauthenticated users to the login page, preserving intent. */
const RequireAuth: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, isBootstrapping } = useAuth()
  const location = useLocation()
  if (isBootstrapping) {
    return (
      <main className="login-page">
        <p className="muted">Checking session…</p>
      </main>
    )
  }
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <>{children}</>
}

/**
 * Client-side role gate. Purely an affordance — the gateway independently
 * enforces RBAC, so bypassing this only yields a 403 from the server.
 */
export const RequireRole: React.FC<{ roles: Role[]; children: React.ReactNode }> = ({
  roles,
  children,
}) => {
  const { hasRole } = useAuth()
  if (!hasRole(...roles)) {
    return (
      <Card title="Access denied">
        <Alert kind="error">
          This page requires the {roles.join(' or ')} role. Contact an administrator if you believe
          this is an error.
        </Alert>
      </Card>
    )
  }
  return <>{children}</>
}

/**
 * Handles the OIDC redirect. The auth service returns JSON, but when it is
 * configured to redirect back to the SPA the token arrives in the URL fragment
 * (`#access_token=...`) — the fragment is never sent to a server, which is why
 * it is preferred over a query parameter for tokens.
 */
const OidcCallback: React.FC = () => {
  const { adoptToken } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    let cancelled = false
    const fromHash = new URLSearchParams(location.hash.replace(/^#/, ''))
    const fromQuery = new URLSearchParams(location.search)
    const token = fromHash.get('access_token') ?? fromQuery.get('access_token')

    if (!token) {
      setError('No access token was returned by the identity provider.')
      return
    }
    void (async () => {
      const ok = await adoptToken(token)
      if (cancelled) return
      if (!ok) {
        setError('The identity provider returned an invalid or expired token.')
        return
      }
      // Strip the token from the address bar — cookie is the only credential.
      navigate('/', { replace: true })
    })()
    return () => {
      cancelled = true
    }
  }, [location, adoptToken, navigate])

  return (
    <main className="login-page">
      <Card className="login-card" title="Completing sign-in">
        {error ? (
          <>
            <Alert kind="error">{error}</Alert>
            <div style={{ marginTop: 12 }}>
              <button type="button" onClick={() => navigate('/login', { replace: true })}>
                Back to sign in
              </button>
            </div>
          </>
        ) : (
          <p className="muted">Validating your session…</p>
        )}
      </Card>
    </main>
  )
}

const NotFound: React.FC = () => (
  <Card title="Page not found">
    <Alert kind="info">The page you requested does not exist.</Alert>
  </Card>
)

export const App: React.FC = () => (
  <Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route path="/oidc/callback" element={<OidcCallback />} />
    <Route
      path="*"
      element={
        <RequireAuth>
          <PatientChartProvider>
            <AppShell>
              <Routes>
                <Route path="/" element={<DashboardPage />} />
                <Route
                  path="/worklist"
                  element={
                    <RequireRole roles={['clinician', 'admin']}>
                      <WorklistPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/wards"
                  element={
                    <RequireRole roles={['clinician', 'admin']}>
                      <WardBoardPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/fhir"
                  element={
                    <RequireRole roles={['clinician', 'admin']}>
                      <FhirPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/flow"
                  element={
                    <RequireRole roles={['clinician', 'admin']}>
                      <PatientFlowPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/cds"
                  element={
                    <RequireRole roles={['clinician', 'admin']}>
                      <CdsPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/admin"
                  element={
                    <RequireRole roles={['admin']}>
                      <AdminPage />
                    </RequireRole>
                  }
                />
                <Route path="*" element={<NotFound />} />
              </Routes>
              <PatientChartDrawer />
            </AppShell>
          </PatientChartProvider>
        </RequireAuth>
      }
    />
  </Routes>
)
