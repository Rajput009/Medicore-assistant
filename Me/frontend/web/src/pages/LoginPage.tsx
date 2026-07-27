import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Alert, Card, Field, Spinner } from '../ui/components'

export const LoginPage: React.FC = () => {
  const { login, loginError, isLoggingIn, isAuthenticated } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [touched, setTouched] = useState(false)

  React.useEffect(() => {
    if (isAuthenticated) navigate('/', { replace: true })
  }, [isAuthenticated, navigate])

  const usernameError = touched && !username.trim() ? 'Username is required.' : null
  const passwordError = touched && !password ? 'Password is required.' : null

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setTouched(true)
    // Client-side guard: don't spend a round-trip on an obviously empty form.
    if (!username.trim() || !password) return
    const ok = await login(username.trim(), password)
    if (ok) navigate('/', { replace: true })
  }

  return (
    <main className="login-page">
      <Card className="login-card">
        <div className="brand" style={{ marginBottom: 6 }}>
          <span className="brand-mark" aria-hidden="true">
            +
          </span>
          <span>MediCore</span>
        </div>
        <h1 style={{ fontSize: '1.25rem', marginBottom: 4 }}>Sign in</h1>
        <p className="muted" style={{ marginTop: 0 }}>
          Clinician and administrator console.
        </p>

        <form onSubmit={onSubmit} noValidate className="stack">
          <Field id="username" label="Username" error={usernameError}>
            {(props) => (
              <input
                {...props}
                name="username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            )}
          </Field>

          <Field id="password" label="Password" error={passwordError}>
            {(props) => (
              <input
                {...props}
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
          </Field>

          {loginError && <Alert kind="error">{loginError}</Alert>}

          <button type="submit" className="primary" disabled={isLoggingIn}>
            {isLoggingIn ? (
              <>
                <Spinner label="Signing in" /> Signing in…
              </>
            ) : (
              'Sign in'
            )}
          </button>
        </form>

        <div className="divider">or</div>

        {/* Full page navigation: the OIDC flow redirects to the IdP. */}
        <a href={api.ssoUrl()}>
          <button type="button" style={{ width: '100%' }}>
            Sign in with SSO
          </button>
        </a>

        <p className="muted" style={{ fontSize: '0.8rem', marginBottom: 0, marginTop: 14 }}>
          Local development uses the demo credentials configured via{' '}
          <code>DEMO_PASSWORD</code>.
        </p>
      </Card>
    </main>
  )
}
