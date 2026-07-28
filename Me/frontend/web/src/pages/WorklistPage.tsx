/**
 * Clinician worklist — actionable "my patients" across beds and triage.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { retryWithIdempotency } from '../api/retry'
import type { Bed, Disposition, QueueItem } from '../api/types'
import { DISPOSITION_LABELS } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { useAsyncData } from '../hooks/useAsync'
import { DispositionDialog } from '../patient/DispositionDialog'
import { PatientLink } from '../patient/PatientChartDrawer'
import { loadRecentPatients, type RecentPatient } from '../patient/recentPatients'
import { acuityTone } from './PatientFlowPage'
import { Alert, Badge, Card, EmptyState, Field, SkeletonRows, Spinner } from '../ui/components'

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
  const claimed =
    me && queue.some((q) => q.claimed_by)
      ? queue.filter((q) => q.claimed_by === me)
      : queue.filter((q) => q.status === 'in_progress')

  const waiting = queue.filter((q) => !q.status || q.status === 'waiting')
  const occupiedBeds = beds.filter((b) => b.occupied && b.patient_id)
  const freeBeds = beds.filter((b) => !b.occupied).length
  return {
    claimedByMe: claimed,
    waiting,
    occupiedBeds,
    freeBeds,
  }
}

/** Distinct departments present in the waiting list (for claim control). */
export function waitingDepartments(queue: QueueItem[]): string[] {
  const set = new Set<string>()
  for (const q of queue) {
    if (!q.status || q.status === 'waiting') set.add(q.dept)
  }
  return [...set].sort()
}

export const WorklistPage: React.FC = () => {
  const { user } = useAuth()
  const beds = useAsyncData((signal) => api.listBeds(null, null, signal), [])
  const queue = useAsyncData((signal) => api.listQueue(50, null, null, signal), [])
  const [claimDept, setClaimDept] = useState('ED')
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionOk, setActionOk] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Which patient's disposition prompt is open, if any.
  const [completing, setCompleting] = useState<string | null>(null)
  const [retryNotice, setRetryNotice] = useState<string | null>(null)
  const [recent, setRecent] = useState<RecentPatient[]>(() => loadRecentPatients())

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

  const depts = useMemo(() => {
    const q = queue.state.status === 'success' ? queue.state.data.items : []
    const d = waitingDepartments(q)
    return d.length ? d : ['ED']
  }, [queue.state])

  useEffect(() => {
    if (depts.length && !depts.includes(claimDept)) setClaimDept(depts[0])
  }, [depts, claimDept])

  // Refresh "recent" when returning to the tab / after chart views.
  useEffect(() => {
    const sync = () => setRecent(loadRecentPatients())
    window.addEventListener('focus', sync)
    const id = window.setInterval(sync, 2000)
    return () => {
      window.removeEventListener('focus', sync)
      window.clearInterval(id)
    }
  }, [])

  const reload = useCallback(() => {
    beds.reload()
    queue.reload()
    setRecent(loadRecentPatients())
  }, [beds, queue])

  const onClaim = async () => {
    setActionError(null)
    setActionOk(null)
    setRetryNotice(null)
    setBusy(true)
    try {
      // One key for the whole intent: a retry after a dropped response
      // replays the original claim instead of grabbing a second patient.
      const res = await retryWithIdempotency(
        (key) => api.claimNext(claimDept, null, undefined, key),
        {
          onRetry: ({ attempt }) =>
            setRetryNotice(`Connection problem — retrying (attempt ${attempt + 1})…`),
        },
      )
      setRetryNotice(null)
      setActionOk(`Claimed ${res.item.patient_id} in ${claimDept}.`)
      reload()
    } catch (err) {
      setRetryNotice(null)
      setActionError(err instanceof Error ? err.message : 'Claim failed')
    } finally {
      setBusy(false)
    }
  }

  const onComplete = async (
    patientId: string,
    disposition: Disposition,
    note: string | null,
  ) => {
    setActionError(null)
    setActionOk(null)
    setRetryNotice(null)
    setBusy(true)
    try {
      await retryWithIdempotency(
        (key) => api.completeQueue(patientId, disposition, note, null, undefined, key),
        {
          onRetry: ({ attempt }) =>
            setRetryNotice(`Connection problem — retrying (attempt ${attempt + 1})…`),
        },
      )
      setRetryNotice(null)
      setCompleting(null)
      setActionOk(
        `Completed ${patientId} — ${DISPOSITION_LABELS[disposition].toLowerCase()}.`,
      )
      reload()
    } catch (err) {
      setRetryNotice(null)
      setActionError(err instanceof Error ? err.message : 'Complete failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <header className="page-header">
        <h1>My patients</h1>
        <p>
          Claim the next triage patient, finish encounters, and jump into charts — without leaving
          the worklist.
        </p>
      </header>

      <Card title="Quick actions">
        <div className="row" style={{ alignItems: 'flex-end', gap: 12 }}>
          <Field id="claim-dept" label="Claim next in">
            {(props) => (
              <select
                {...props}
                value={claimDept}
                onChange={(e) => setClaimDept(e.target.value)}
              >
                {depts.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <button
            type="button"
            className="primary"
            disabled={busy || loading}
            onClick={() => void onClaim()}
          >
            {busy ? (
              <>
                <Spinner label="Claiming" /> Working…
              </>
            ) : (
              'Claim next patient'
            )}
          </button>
          <button type="button" className="ghost" onClick={reload} disabled={busy}>
            Refresh
          </button>
        </div>
        {retryNotice && (
          <div style={{ marginTop: 10 }}>
            <Alert kind="info">{retryNotice}</Alert>
          </div>
        )}
        {actionError && (
          <div style={{ marginTop: 10 }}>
            <Alert kind="error">{actionError}</Alert>
          </div>
        )}
        {actionOk && (
          <div style={{ marginTop: 10 }}>
            <Alert kind="success">{actionOk}</Alert>
          </div>
        )}
      </Card>

      {error && <Alert kind="error">{error}</Alert>}
      {loading && <SkeletonRows rows={5} />}

      {!loading && !error && (
        <div className="worklist-grid">
          <Card
            title={
              <>
                Claimed / in progress <Badge tone="warn">{parts.claimedByMe.length}</Badge>
              </>
            }
          >
            {parts.claimedByMe.length === 0 ? (
              <EmptyState
                message="No patients claimed"
                hint="Use Claim next patient above."
              />
            ) : (
              parts.claimedByMe.map((q) => (
                <div key={`c-${q.patient_id}-${q.dept}`} className="worklist-item">
                  <div>
                    <PatientLink id={q.patient_id} />
                    <div className="muted" style={{ fontSize: '0.8rem' }}>
                      {q.dept} · {q.status ?? 'in_progress'}
                      {q.claimed_by ? ` · ${q.claimed_by}` : ''}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <Badge tone={acuityTone(q.acuity)}>ESI {q.acuity}</Badge>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        setCompleting(completing === q.patient_id ? null : q.patient_id)
                      }
                    >
                      Complete
                    </button>
                  </div>
                  {completing === q.patient_id && (
                    <DispositionDialog
                      patientId={q.patient_id}
                      busy={busy}
                      onCancel={() => setCompleting(null)}
                      onConfirm={(disposition, note) =>
                        void onComplete(q.patient_id, disposition, note)
                      }
                    />
                  )}
                </div>
              ))
            )}
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
            <div style={{ marginTop: 10 }}>
              <Link to="/flow">Full triage board →</Link>
            </div>
          </Card>

          <Card
            title={
              <>
                Occupied beds <Badge tone="neutral">{parts.occupiedBeds.length}</Badge>
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

          <Card
            title={
              <>
                Recently viewed <Badge tone="neutral">{recent.length}</Badge>
              </>
            }
          >
            {recent.length === 0 ? (
              <EmptyState
                message="No recent charts"
                hint="Open a patient from beds, queue, or FHIR."
              />
            ) : (
              recent.map((r) => (
                <div key={r.id} className="worklist-item">
                  <PatientLink id={r.id} />
                  <span className="muted" style={{ fontSize: '0.75rem' }}>
                    {new Date(r.viewedAt).toLocaleTimeString()}
                  </span>
                </div>
              ))
            )}
          </Card>
        </div>
      )}
    </>
  )
}
