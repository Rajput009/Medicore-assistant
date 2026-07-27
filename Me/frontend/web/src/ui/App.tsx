import React, { useEffect, useState } from 'react'

type Health = { status: string; service: string; env: string }

// Service URLs are build-time configurable instead of hard-coded to localhost,
// which broke every non-local deployment.
const AUTH_BASE = import.meta.env.VITE_AUTH_BASE_URL ?? 'http://localhost:8081'

const SERVICES = [
  { title: 'Gateway', endpoint: '/api/health' },
  { title: 'Auth', endpoint: `${AUTH_BASE}/health` },
  {
    title: 'Patient Flow',
    endpoint: `${import.meta.env.VITE_PATIENT_FLOW_BASE_URL ?? 'http://localhost:8082'}/health`,
  },
  {
    title: 'CDS',
    endpoint: `${import.meta.env.VITE_CDS_BASE_URL ?? 'http://localhost:8083'}/health`,
  },
]

export const App: React.FC = () => {
  const [gateway, setGateway] = useState<Health | null>(null)

  useEffect(() => {
    // AbortController prevents a state update after unmount (React warning +
    // leak in StrictMode's double-invoked effects).
    const ac = new AbortController()
    fetch('/api/health', { signal: ac.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setGateway)
      .catch(() => {
        if (!ac.signal.aborted) setGateway(null)
      })
    return () => ac.abort()
  }, [])

  return (
    <div style={{ padding: 24, fontFamily: 'ui-sans-serif, system-ui' }}>
      <h1>MediCore Admin Console</h1>
      <p>
        Environment: <b>{gateway?.env ?? 'unknown'}</b>
      </p>

      <section
        style={{
          display: 'grid',
          gap: 12,
          gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
        }}
      >
        {SERVICES.map((s) => (
          <Card key={s.title} title={s.title} endpoint={s.endpoint} />
        ))}
      </section>

      {/* Single sign-in control (previously duplicated into every card). */}
      <div style={{ marginTop: 16 }}>
        <a href={`${AUTH_BASE}/oidc/login`}>
          <button style={{ padding: '10px 16px', borderRadius: 8, border: '1px solid #ddd' }}>
            Sign in with SSO
          </button>
        </a>
      </div>
    </div>
  )
}

type CardState =
  | { kind: 'loading' }
  | { kind: 'ok'; data: unknown }
  | { kind: 'error'; message: string }

const Card: React.FC<{ title: string; endpoint: string }> = ({ title, endpoint }) => {
  const [state, setState] = useState<CardState>({ kind: 'loading' })

  useEffect(() => {
    const ac = new AbortController()
    setState({ kind: 'loading' })
    fetch(endpoint, { signal: ac.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data) => setState({ kind: 'ok', data }))
      .catch((err: unknown) => {
        if (ac.signal.aborted) return
        setState({ kind: 'error', message: err instanceof Error ? err.message : 'unreachable' })
      })
    return () => ac.abort()
  }, [endpoint])

  return (
    <div style={{ padding: 16, borderRadius: 12, boxShadow: '0 2px 10px rgba(0,0,0,0.06)' }}>
      <h3>{title}</h3>
      {/* Distinguishes "still loading" from "failed", which the original
          could not do because both rendered as 'loading...'. */}
      {state.kind === 'loading' && <pre>loading…</pre>}
      {state.kind === 'error' && (
        <pre style={{ color: '#b00020', whiteSpace: 'pre-wrap' }}>unavailable: {state.message}</pre>
      )}
      {state.kind === 'ok' && (
        <pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(state.data, null, 2)}</pre>
      )}
    </div>
  )
}
