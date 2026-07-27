/**
 * Clinician worklist — "my patients" across beds and triage.
 *
 * Pulls beds + queue once and partitions into: assigned to me (claimed),
 * waiting in triage, and occupied beds (ward census). Opens the chart drawer
 * without leaving the page.
 */

import React, { useMemo } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import type { Bed, QueueItem } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { useAsyncData } from '../hooks/useAsync'
import { PatientLink } from '../patient/PatientChartDrawer'
import { acuityTone } from './PatientFlowPage'
import { Alert, Badge, Card, EmptyState, SkeletonRows } from '../ui/components'

export function partitionWorklist(
  beds: Bed[],
  queue: QueueItem[],
  me: string | undefined,
): {
  claimedByMe: QueueItem[]
  waiting: QueueItem[]
  occupiedBeds: Bed[]
  freeBeds: number
} {
  const claimedByMe = queue.filter(
    (q) =>
      (q.status === 'in_progress' || Boolean((q as { claimed_by?: string }).claimed_by)) &&
      (me
        ? (q as { claimed_by?: string }).claimed_by === me || q.status === 'in_progress'
        : false),
  )
  // Prefer explicit claimed_by when present; otherwise treat in_progress as "active".
  const claimed =
    me && queue.some((q) => (q as { claimed_by?: string }).claimed_by)
      ? queue.filter((q) => (q as { claimed_by?: string }).claimed_by === me)
      : queue.filter((q) => q.status === 'in_progress')

  const waiting = queue.filter((q) => !q.status || q.status === 'waiting')
  const occupiedBeds = beds.filter((b) => b.occupied && b.patient_id)
  const freeBeds = beds.filter((b) => !b.occupied).length
  return {
    claimedByMe: claimed.length ? claimed : claimedByMe,
    waiting,
    occupiedBeds,
    freeBeds,
  }
}

export const WorklistPage: React.FC = () => {
  const { user } = useAuth()
  const beds = useAsyncData((signal) => api.listBeds(null, null, signal), [])
  const queue = useAsyncData((signal) => api.listQueue(50, null, null, signal), [])

  const loading = beds.state.status === 'loading' || queue.state.status === 'loading'
  const error =
    (beds.state.status === 'error' && beds.state.error) ||
    (queue.state.status === 'error' && queue.state.error) ||
    null

  const parts = useMemo(() => {
    const b = beds.state.status === 'success' ? beds.state.data : []
    const q = queue.state.status === 'success' ? queue.state.data.items : []
    return partitionWorklist(b, q, user?.sub)
  }, [beds.state, queue.state, user?.sub])

  const reload = () => {
    beds.reload()
    queue.reload()
  }

  return (
    <>
      <header className="page-header">
        <h1>My patients</h1>
        <p>Worklist across triage and beds. Click any patient id to open the chart.</p>
      </header>

      <div style={{ marginBottom: 12 }}>
        <button type="button" className="ghost" onClick={reload}>
          Refresh
        </button>
      </div>

      {error && <Alert kind="error">{error}</Alert>}
      {loading && <SkeletonRows rows={5} />}

      {!loading && !error && (
        <div className="worklist-grid">
          <Card
            title={
              <>
                Claimed / in progress{' '}
                <Badge tone="warn">{parts.claimedByMe.length}</Badge>
              </>
            }
          >
            {parts.claimedByMe.length === 0 ? (
              <EmptyState
                message="No patients claimed"
                hint="Claim the next triage patient from Patient flow."
              />
            ) : (
              parts.claimedByMe.map((q) => (
                <div key={`c-${q.patient_id}-${q.dept}`} className="worklist-item">
                  <div>
                    <PatientLink id={q.patient_id} />
                    <div className="muted" style={{ fontSize: '0.8rem' }}>
                      {q.dept} · {q.status ?? 'in_progress'}
                    </div>
                  </div>
                  <Badge tone={acuityTone(q.acuity)}>ESI {q.acuity}</Badge>
                </div>
              ))
            )}
            <div style={{ marginTop: 10 }}>
              <Link to="/flow">Go to triage →</Link>
            </div>
          </Card>

          <Card
            title={
              <>
                Waiting in triage <Badge tone="err">{parts.waiting.length}</Badge>
              </>
            }
          >
            {parts.waiting.length === 0 ? (
              <EmptyState message="Queue is clear" />
            ) : (
              parts.waiting.slice(0, 12).map((q) => (
                <div key={`w-${q.patient_id}-${q.dept}-${q.acuity}`} className="worklist-item">
                  <div>
                    <PatientLink id={q.patient_id} />
                    <div className="muted" style={{ fontSize: '0.8rem' }}>
                      {q.dept}
                    </div>
                  </div>
                  <Badge tone={acuityTone(q.acuity)}>ESI {q.acuity}</Badge>
                </div>
              ))
            )}
          </Card>

          <Card
            title={
              <>
                Occupied beds{' '}
                <Badge tone="neutral">{parts.occupiedBeds.length}</Badge>
              </>
            }
          >
            <p className="muted" style={{ marginTop: 0 }}>
              {parts.freeBeds} free bed{parts.freeBeds === 1 ? '' : 's'} hospital-wide
            </p>
            {parts.occupiedBeds.length === 0 ? (
              <EmptyState message="No occupied beds" />
            ) : (
              parts.occupiedBeds.map((b) => (
                <div key={b.bed_id} className="worklist-item">
                  <div>
                    {b.patient_id ? (
                      <PatientLink id={b.patient_id} />
                    ) : (
                      <span className="mono">—</span>
                    )}
                    <div className="muted mono" style={{ fontSize: '0.8rem' }}>
                      {b.bed_id} · ward {b.ward}
                    </div>
                  </div>
                  <Badge tone="err" withDot>
                    occupied
                  </Badge>
                </div>
              ))
            )}
            <div style={{ marginTop: 10 }}>
              <Link to="/wards">Open ward board →</Link>
            </div>
          </Card>
        </div>
      )}
    </>
  )
}
