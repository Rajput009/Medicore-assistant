import React, { useState } from 'react'

import { api } from '../api/client'
import type { Bed, QueueItem, QueueListResponse } from '../api/types'
import { useAsyncAction, useAsyncData } from '../hooks/useAsync'
import { PatientLink } from '../patient/PatientChartDrawer'
import { Alert, Badge, Card, EmptyState, Field, SkeletonRows, Spinner } from '../ui/components'

/** ESI acuity 1 (most urgent) .. 5 (least). */
export function acuityTone(acuity: number): 'err' | 'warn' | 'neutral' {
  if (acuity <= 2) return 'err'
  if (acuity === 3) return 'warn'
  return 'neutral'
}

const BedsCard: React.FC = () => {
  const { state, reload } = useAsyncData<Bed[]>((signal) => api.listBeds(null, null, signal), [
    null,
  ])
  const toggle = useAsyncAction<[Bed, string | null], Bed>((signal, bed, patientId) =>
    api.setBedOccupancy(
      bed.bed_id,
      {
        occupied: !bed.occupied,
        patient_id: bed.occupied ? null : patientId,
        expected_occupied: bed.occupied,
      },
      null,
      signal,
    ),
  )

  const onToggle = async (bed: Bed) => {
    let patientId: string | null = null
    if (!bed.occupied) {
      patientId = window.prompt(`Patient ID to assign to bed ${bed.bed_id}:`)?.trim() || null
      if (!patientId) return
    }
    const updated = await toggle.run(bed, patientId)
    if (updated) reload()
  }

  const beds = state.status === 'success' ? state.data : []
  const free = beds.filter((b) => !b.occupied).length

  return (
    <Card
      title="Beds"
      actions={
        <button type="button" className="ghost" onClick={reload}>
          Refresh
        </button>
      }
    >
      {state.status === 'loading' && <SkeletonRows rows={4} />}
      {state.status === 'error' && <Alert kind="error">{state.error}</Alert>}
      {toggle.state.status === 'error' && <Alert kind="error">{toggle.state.error}</Alert>}

      {state.status === 'success' &&
        (beds.length === 0 ? (
          <EmptyState message="No beds configured" />
        ) : (
          <>
            <p className="muted" style={{ marginTop: 0 }}>
              {free} of {beds.length} available
            </p>
            <div className="table-wrap">
              <table>
                <caption className="visually-hidden">Bed occupancy</caption>
                <thead>
                  <tr>
                    <th scope="col">Bed</th>
                    <th scope="col">Ward</th>
                    <th scope="col">Status</th>
                    <th scope="col">Patient</th>
                    <th scope="col">
                      <span className="visually-hidden">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {beds.map((bed) => (
                    <tr key={bed.bed_id}>
                      <td className="mono">{bed.bed_id}</td>
                      <td>{bed.ward}</td>
                      <td>
                        <Badge tone={bed.occupied ? 'err' : 'ok'} withDot>
                          {bed.occupied ? 'occupied' : 'available'}
                        </Badge>
                      </td>
                      <td className="mono">
                        {bed.patient_id ? <PatientLink id={bed.patient_id} /> : '—'}
                      </td>
                      <td>
                        <button
                          type="button"
                          onClick={() => void onToggle(bed)}
                          disabled={toggle.state.status === 'loading'}
                        >
                          {bed.occupied ? 'Discharge' : 'Assign'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ))}
    </Card>
  )
}

const QueueCard: React.FC = () => {
  const [dept, setDept] = useState('ED')
  const [limit, setLimit] = useState(25)
  const [actionMsg, setActionMsg] = useState<string | null>(null)
  const list = useAsyncAction<[number, string | null], QueueListResponse>((signal, l, d) =>
    api.listQueue(l, d, null, signal),
  )
  const claim = useAsyncAction<[string], { ok: boolean; item: QueueItem }>((signal, d) =>
    api.claimNext(d, null, signal),
  )
  const complete = useAsyncAction<[string], { ok: boolean; item: QueueItem }>((signal, pid) =>
    api.completeQueue(pid, null, signal),
  )

  React.useEffect(() => {
    void list.run(limit, dept.trim() || null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const refresh = () => void list.run(limit, dept.trim() || null)

  const onClaim = async () => {
    setActionMsg(null)
    const d = dept.trim() || 'ED'
    const res = await claim.run(d)
    if (res) {
      setActionMsg(`Claimed ${res.item.patient_id} in ${d}.`)
      refresh()
    }
  }

  const onComplete = async (patientId: string) => {
    setActionMsg(null)
    const res = await complete.run(patientId)
    if (res) {
      setActionMsg(`Completed ${patientId}.`)
      refresh()
    }
  }

  const items = list.state.status === 'success' ? list.state.data.items : []
  const total = list.state.status === 'success' ? list.state.data.total : 0
  const busy =
    list.state.status === 'loading' ||
    claim.state.status === 'loading' ||
    complete.state.status === 'loading'

  return (
    <Card
      title="Triage queue"
      actions={
        <button type="button" className="ghost" onClick={refresh} disabled={busy}>
          Refresh
        </button>
      }
    >
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault()
          refresh()
        }}
      >
        <Field id="queue-dept" label="Department">
          {(props) => (
            <input
              {...props}
              value={dept}
              onChange={(e) => setDept(e.target.value)}
              placeholder="ED"
            />
          )}
        </Field>
        <Field id="queue-limit" label="Limit">
          {(props) => (
            <input
              {...props}
              type="number"
              min={1}
              max={200}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            />
          )}
        </Field>
        <button type="submit" disabled={busy}>
          Apply
        </button>
        <button
          type="button"
          className="primary"
          disabled={busy || !dept.trim()}
          onClick={() => void onClaim()}
        >
          {claim.state.status === 'loading' ? (
            <>
              <Spinner label="Claiming" /> Claiming…
            </>
          ) : (
            'Claim next'
          )}
        </button>
      </form>

      {(claim.state.status === 'error' || complete.state.status === 'error') && (
        <div style={{ marginTop: 10 }}>
          {/* Read each error inside its own narrowed branch: TypeScript cannot
              infer `complete.state` is the error variant from the || above. */}
          <Alert kind="error">
            {claim.state.status === 'error'
              ? claim.state.error
              : complete.state.status === 'error'
                ? complete.state.error
                : null}
          </Alert>
        </div>
      )}
      {actionMsg && (
        <div style={{ marginTop: 10 }}>
          <Alert kind="success">{actionMsg}</Alert>
        </div>
      )}

      <div style={{ marginTop: 12 }}>
        {list.state.status === 'loading' && <SkeletonRows rows={3} />}
        {list.state.status === 'error' && <Alert kind="error">{list.state.error}</Alert>}
        {list.state.status === 'success' &&
          (items.length === 0 ? (
            <EmptyState message="Queue is empty" hint="Add a patient using the form below." />
          ) : (
            <div className="table-wrap">
              <p className="muted" style={{ marginTop: 0 }}>
                Showing {items.length} of {total}
              </p>
              <table>
                <caption className="visually-hidden">Triage queue, most urgent first</caption>
                <thead>
                  <tr>
                    <th scope="col">Patient</th>
                    <th scope="col">Acuity</th>
                    <th scope="col">Department</th>
                    <th scope="col">Status</th>
                    <th scope="col">
                      <span className="visually-hidden">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, i) => (
                    <tr key={`${item.patient_id}-${i}`}>
                      <td className="mono">
                        <PatientLink id={item.patient_id} />
                      </td>
                      <td>
                        <Badge tone={acuityTone(item.acuity)}>ESI {item.acuity}</Badge>
                      </td>
                      <td>{item.dept}</td>
                      <td className="muted">{item.status ?? 'waiting'}</td>
                      <td>
                        {item.status === 'in_progress' || item.claimed_by ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => void onComplete(item.patient_id)}
                          >
                            Complete
                          </button>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
      </div>
    </Card>
  )
}

const EnqueueCard: React.FC = () => {
  const [patientId, setPatientId] = useState('')
  const [acuity, setAcuity] = useState(3)
  const [dept, setDept] = useState('ED')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const enqueue = useAsyncAction<[string, number, string], { ok: boolean; id: string }>(
    (signal, pid, ac, d) => api.enqueue({ patient_id: pid, acuity: ac, dept: d }, null, signal),
  )

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const next: Record<string, string> = {}
    if (!patientId.trim()) next.patientId = 'Patient id is required.'
    if (!dept.trim()) next.dept = 'Department is required.'
    if (!Number.isInteger(acuity) || acuity < 1 || acuity > 5) {
      next.acuity = 'Acuity must be a whole number from 1 to 5.'
    }
    setErrors(next)
    if (Object.keys(next).length > 0) return

    const res = await enqueue.run(patientId.trim(), acuity, dept.trim())
    if (res) {
      setPatientId('')
      setDept('ED')
      setAcuity(3)
    }
  }

  return (
    <Card title="Add to triage queue">
      <form onSubmit={onSubmit} noValidate className="stack">
        <div className="row">
          <Field id="enqueue-patient" label="Patient id" error={errors.patientId}>
            {(props) => (
              <input
                {...props}
                value={patientId}
                onChange={(e) => setPatientId(e.target.value)}
              />
            )}
          </Field>
          <Field
            id="enqueue-acuity"
            label="Acuity"
            error={errors.acuity}
            hint="1 = most urgent, 5 = least"
          >
            {(props) => (
              <select
                {...props}
                value={acuity}
                onChange={(e) => setAcuity(Number(e.target.value))}
              >
                {[1, 2, 3, 4, 5].map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field id="enqueue-dept" label="Department" error={errors.dept}>
            {(props) => (
              <input {...props} value={dept} onChange={(e) => setDept(e.target.value)} />
            )}
          </Field>
        </div>

        {enqueue.state.status === 'error' && <Alert kind="error">{enqueue.state.error}</Alert>}
        {enqueue.state.status === 'success' && <Alert kind="success">Patient added to queue.</Alert>}

        <div>
          <button type="submit" className="primary" disabled={enqueue.state.status === 'loading'}>
            {enqueue.state.status === 'loading' ? (
              <>
                <Spinner label="Adding" /> Adding…
              </>
            ) : (
              'Add to queue'
            )}
          </button>
        </div>
      </form>
    </Card>
  )
}

export const PatientFlowPage: React.FC = () => (
  <>
    <header className="page-header">
      <h1>Patient flow</h1>
      <p>Bed occupancy and emergency department triage — claim, complete, assign.</p>
    </header>
    <BedsCard />
    <QueueCard />
    <EnqueueCard />
  </>
)
