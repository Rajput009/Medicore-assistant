import React from 'react'

import { api, BASE } from '../api/client'
import type { Health } from '../api/types'
import { useAsyncData } from '../hooks/useAsync'
import { Alert, Badge, Card, JsonBlock, SkeletonRows } from '../ui/components'

const SERVICES: { key: keyof typeof BASE; label: string }[] = [
  { key: 'gateway', label: 'Gateway' },
  { key: 'auth', label: 'Auth' },
  { key: 'patientFlow', label: 'Patient Flow' },
  { key: 'cds', label: 'CDS' },
]

const ServiceCard: React.FC<{ serviceKey: keyof typeof BASE; label: string }> = ({
  serviceKey,
  label,
}) => {
  const { state, reload } = useAsyncData<Health>(
    (signal) => api.health(serviceKey, signal),
    [serviceKey],
  )

  return (
    <Card
      title={
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <h3>{label}</h3>
          {state.status === 'success' && (
            <Badge tone="ok" withDot>
              healthy
            </Badge>
          )}
          {state.status === 'error' && (
            <Badge tone="err" withDot>
              unreachable
            </Badge>
          )}
          {state.status === 'loading' && <Badge tone="neutral">checking…</Badge>}
        </div>
      }
      actions={
        <button type="button" className="ghost" onClick={reload}>
          Refresh
        </button>
      }
    >
      {state.status === 'loading' && <SkeletonRows rows={2} />}
      {state.status === 'error' && <Alert kind="error">{state.error}</Alert>}
      {state.status === 'success' && (
        <dl style={{ margin: 0, display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 12px' }}>
          <dt className="muted">Status</dt>
          <dd style={{ margin: 0 }}>{state.data.status}</dd>
          <dt className="muted">Service</dt>
          <dd style={{ margin: 0 }} className="mono">
            {state.data.service}
          </dd>
          <dt className="muted">Env</dt>
          <dd style={{ margin: 0 }}>{state.data.env}</dd>
        </dl>
      )}
    </Card>
  )
}

export const DashboardPage: React.FC = () => {
  const gateway = useAsyncData<Health>((signal) => api.health('gateway', signal), [])

  return (
    <>
      <header className="page-header">
        <h1>System overview</h1>
        <p>
          Environment:{' '}
          <strong>
            {gateway.state.status === 'success' ? gateway.state.data.env : 'unknown'}
          </strong>
        </p>
      </header>

      <div className="grid">
        {SERVICES.map((s) => (
          <ServiceCard key={s.key} serviceKey={s.key} label={s.label} />
        ))}
      </div>

      <Card title="Endpoint configuration">
        <p className="muted" style={{ marginTop: 0 }}>
          Base URLs resolved at build time from <code>VITE_*_BASE_URL</code>.
        </p>
        <JsonBlock value={BASE} label="Configured service base URLs" />
      </Card>
    </>
  )
}
