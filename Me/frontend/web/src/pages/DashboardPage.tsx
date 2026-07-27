import React, { useMemo } from 'react'
import { Link } from 'react-router-dom'

import { api, BASE } from '../api/client'
import type { Bed, Health, QueueListResponse } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { useAsyncData } from '../hooks/useAsync'
import { Alert, Badge, Card, JsonBlock, SkeletonRows } from '../ui/components'

const SERVICES: { key: keyof typeof BASE; label: string }[] = [
  { key: 'gateway', label: 'Gateway' },
  { key: 'auth', label: 'Auth' },
  { key: 'patientFlow', label: 'Patient Flow' },
  { key: 'cds', label: 'CDS' },
]

export type CensusStats = {
  freeBeds: number
  totalBeds: number
  waiting: number
  inProgress: number
}

/** Pure census math — unit-tested without network. */
export function computeCensus(beds: Bed[], queue: QueueListResponse | null): CensusStats {
  const freeBeds = beds.filter((b) => !b.occupied).length
  const totalBeds = beds.length
  const items = queue?.items ?? []
  const waiting = items.filter((q) => !q.status || q.status === 'waiting').length
  const inProgress = items.filter(
    (q) => q.status === 'in_progress' || Boolean(q.claimed_by),
  ).length
  return { freeBeds, totalBeds, waiting, inProgress }
}

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

const CensusStrip: React.FC = () => {
  const { hasRole, isBootstrapping, isAuthenticated } = useAuth()
  const beds = useAsyncData((signal) => api.listBeds(null, null, signal), [])
  const queue = useAsyncData((signal) => api.listQueue(50, null, null, signal), [])

  const stats = useMemo(() => {
    const b = beds.state.status === 'success' ? beds.state.data : []
    const q = queue.state.status === 'success' ? queue.state.data : null
    return computeCensus(b, q)
  }, [beds.state, queue.state])

  // Wait for session hydrate so role checks are meaningful.
  if (isBootstrapping || !isAuthenticated) return null
  if (!hasRole('clinician', 'admin')) return null

  const loading = beds.state.status === 'loading' || queue.state.status === 'loading'

  return (
    <Card
      title="Live census"
      actions={
        <button
          type="button"
          className="ghost"
          onClick={() => {
            beds.reload()
            queue.reload()
          }}
        >
          Refresh
        </button>
      }
    >
      {loading && <SkeletonRows rows={1} />}
      {!loading && (
        <>
          <div className="census-grid">
            <div className="census-tile">
              <div className="census-value">
                {stats.freeBeds}
                <span className="muted" style={{ fontSize: '1rem', fontWeight: 500 }}>
                  /{stats.totalBeds}
                </span>
              </div>
              <div className="census-label">Free beds</div>
            </div>
            <div className="census-tile">
              <div className="census-value">{stats.waiting}</div>
              <div className="census-label">Waiting triage</div>
            </div>
            <div className="census-tile">
              <div className="census-value">{stats.inProgress}</div>
              <div className="census-label">In progress</div>
            </div>
            <div className="census-tile">
              <div className="census-value">{stats.totalBeds - stats.freeBeds}</div>
              <div className="census-label">Occupied beds</div>
            </div>
          </div>
          <div className="row" style={{ gap: 12 }}>
            <Link to="/worklist">My patients →</Link>
            <Link to="/wards">Ward board →</Link>
            <Link to="/flow">Triage →</Link>
          </div>
        </>
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

      <CensusStrip />

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
