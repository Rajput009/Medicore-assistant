import React, { useEffect, useState } from 'react'

type Health = { status: string; service: string; env: string }

export const App: React.FC = () => {
  const [gateway, setGateway] = useState<Health | null>(null)

  useEffect(() => {
    fetch('/api/health').then(r => r.json()).then(setGateway).catch(() => setGateway(null))
  }, [])

  return (
    <div style={{ padding: 24, fontFamily: 'ui-sans-serif, system-ui' }}>
      <h1>MediCore Admin Console</h1>
      <p>Environment: <b>{gateway?.env ?? 'unknown'}</b></p>
      <section style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))' }}>
        <Card title="Gateway" endpoint="/api/health" />
        <Card title="Auth" endpoint="http://localhost:8081/health" />
        <Card title="Patient Flow" endpoint="http://localhost:8082/health" />
        <Card title="CDS" endpoint="http://localhost:8083/health" />
      </section>
    
      <div style={{marginTop: 16}}>
        <a href="http://localhost:8081/oidc/login">
          <button style={{padding: '10px 16px', borderRadius: 8, border: '1px solid #ddd'}}>Sign in with SSO</button>
        </a>
      </div>
    </div>
  )
}

const Card: React.FC<{ title: string; endpoint: string }> = ({ title, endpoint }) => {
  const [data, setData] = useState<any>(null)
  useEffect(() => {
    fetch(endpoint).then(r => r.json()).then(setData).catch(() => setData(null))
  }, [endpoint])
  return (
    <div style={{ padding: 16, borderRadius: 12, boxShadow: '0 2px 10px rgba(0,0,0,0.06)' }}>
      <h3>{title}</h3>
      <pre style={{ whiteSpace: 'pre-wrap' }}>{data ? JSON.stringify(data, null, 2) : 'loading...'}</pre>
    
      <div style={{marginTop: 16}}>
        <a href="http://localhost:8081/oidc/login">
          <button style={{padding: '10px 16px', borderRadius: 8, border: '1px solid #ddd'}}>Sign in with SSO</button>
        </a>
      </div>
    </div>
  )
}
