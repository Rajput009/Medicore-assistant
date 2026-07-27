/**
 * Audit search — "who viewed MRN-X?"
 *
 * Two views over the same trail:
 *   * **Accessors**: one row per clinician, with counts and first/last access.
 *     This is where a privacy investigation starts.
 *   * **Events**: the individual requests behind those counts.
 *
 * Patient identifiers are hashed server-side, so nothing here can display a
 * raw MRN it was not already given by the person typing it in.
 */

import React, { useState } from 'react'

import { api } from '../api/client'
import type {
  AuditAccessorsResponse,
  AuditEvent,
  AuditOutcome,
  AuditSearchResponse,
} from '../api/types'
import { AUDIT_OUTCOMES } from '../api/types'
import { useAsyncAction } from '../hooks/useAsync'
import { Alert, Badge, Card, EmptyState, Field, SkeletonRows, Spinner } from '../ui/components'

const PAGE_SIZE = 25

/** Local time, or the raw value when the server sent something unparseable. */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

/** Outcome → badge tone. Denied is the row an investigator is looking for. */
export function outcomeTone(outcome: string | null | undefined): 'ok' | 'err' | 'warn' | 'neutral' {
  if (outcome === 'success') return 'ok'
  if (outcome === 'denied') return 'err'
  if (outcome === 'failure' || outcome === 'error') return 'warn'
  return 'neutral'
}

/** Shortens a hash for display; the full value stays in the title attribute. */
export function shortRef(ref: string | null | undefined): string {
  if (!ref) return '—'
  const body = ref.startsWith('sha256:') ? ref.slice(7) : ref
  return body.length > 12 ? `${body.slice(0, 12)}…` : body
}

const EventRow: React.FC<{ event: AuditEvent }> = ({ event }) => (
  <tr>
    <td className="mono">{formatTimestamp(event.ts)}</td>
    <td className="mono">{event.actor_sub ?? <span className="muted">anonymous</span>}</td>
    <td>
      <span className="mono">{event.method}</span> <span className="mono">{event.path}</span>
      {/* An override is the reviewer's whole reason for being here, so the
          reason is shown inline rather than hidden behind a tooltip. */}
      {event.break_glass && (
        <div style={{ marginTop: 4 }}>
          <Badge tone="warn">break-glass</Badge>{' '}
          {event.break_glass_reason && (
            <span className="muted">{event.break_glass_reason}</span>
          )}
        </div>
      )}
    </td>
    <td>
      <Badge tone={outcomeTone(event.outcome)}>{event.outcome ?? '—'}</Badge>
      {event.status ? <span className="muted"> {event.status}</span> : null}
    </td>
    <td className="mono" title={event.resource_ref ?? event.patient_ref ?? undefined}>
      {shortRef(event.resource_ref ?? event.patient_ref)}
      {/* A search discloses a page of patients; the count is the honest
          measure of how many, since the stored list is truncated. */}
      {typeof event.subject_count === 'number' && event.subject_count > 0 && (
        <div className="muted" style={{ fontSize: '0.78rem' }}>
          {event.subject_count} {event.subject_count === 1 ? 'patient' : 'patients'} returned
        </div>
      )}
    </td>
    <td className="mono">{event.client_ip ?? '—'}</td>
  </tr>
)

export const AuditSearchPanel: React.FC = () => {
  const [patient, setPatient] = useState('')
  const [actor, setActor] = useState('')
  const [outcome, setOutcome] = useState<AuditOutcome | ''>('')
  const [overridesOnly, setOverridesOnly] = useState(false)
  const [offset, setOffset] = useState(0)
  // The identifier the current results belong to, so paging cannot drift onto
  // a different patient after the field is edited but not re-submitted.
  const [submitted, setSubmitted] = useState<{ patient: string; actor: string } | null>(null)

  const events = useAsyncAction<[number], AuditSearchResponse>((signal, nextOffset) =>
    api.auditSearch(
      {
        patient: patient.trim() || undefined,
        actor: actor.trim() || undefined,
        outcome: outcome || undefined,
        break_glass: overridesOnly ? true : undefined,
        limit: PAGE_SIZE,
        offset: nextOffset,
      },
      null,
      signal,
    ),
  )

  const accessors = useAsyncAction<[string], AuditAccessorsResponse>((signal, id) =>
    api.auditAccessors(id, 50, null, signal),
  )

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const id = patient.trim()
    setOffset(0)
    setSubmitted({ patient: id, actor: actor.trim() })
    // The accessor summary only makes sense for a specific patient.
    if (id) {
      void accessors.run(id)
    } else {
      accessors.reset()
    }
    await events.run(0)
  }

  const goToPage = async (nextOffset: number) => {
    setOffset(nextOffset)
    await events.run(nextOffset)
  }

  const result = events.state.status === 'success' ? events.state.data : null
  const hasMore = result ? result.offset + result.count < result.total : false

  return (
    <>
      <Card title="Audit search">
        <form className="stack" onSubmit={onSubmit}>
          <p className="muted" style={{ margin: 0 }}>
            Answers “who viewed this record?”. Patient identifiers are hashed before matching, so
            the audit trail never stores a raw MRN.
          </p>

          <div className="row">
            <Field
              id="audit-patient"
              label="Patient id / MRN"
              hint="Leave blank to search all records."
            >
              {(props) => (
                <input
                  {...props}
                  value={patient}
                  onChange={(e) => setPatient(e.target.value)}
                  placeholder="MRN-000123"
                />
              )}
            </Field>
            <Field id="audit-actor" label="Clinician (sub)" hint="Exact match on the token subject.">
              {(props) => (
                <input
                  {...props}
                  value={actor}
                  onChange={(e) => setActor(e.target.value)}
                  placeholder="dr.smith"
                />
              )}
            </Field>
            <Field id="audit-outcome" label="Outcome">
              {(props) => (
                <select
                  {...props}
                  value={outcome}
                  onChange={(e) => setOutcome(e.target.value as AuditOutcome | '')}
                >
                  <option value="">Any</option>
                  {AUDIT_OUTCOMES.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              )}
            </Field>
          </div>

          <div className="field">
            <label htmlFor="audit-break-glass" style={{ display: 'flex', gap: 8 }}>
              <input
                id="audit-break-glass"
                type="checkbox"
                checked={overridesOnly}
                onChange={(e) => setOverridesOnly(e.target.checked)}
              />
              <span>Emergency overrides only</span>
            </label>
          </div>

          <div>
            <button type="submit" disabled={events.state.status === 'loading'}>
              {events.state.status === 'loading' ? (
                <>
                  <Spinner label="Searching" /> Searching…
                </>
              ) : (
                'Search audit trail'
              )}
            </button>
          </div>

          {events.state.status === 'error' && <Alert kind="error">{events.state.error}</Alert>}
        </form>
      </Card>

      {/* Accessor summary — only meaningful when scoped to one patient. */}
      {submitted?.patient && (
        <Card title={`Who accessed ${submitted.patient}`}>
          {accessors.state.status === 'loading' && <SkeletonRows rows={3} />}
          {accessors.state.status === 'error' && (
            <Alert kind="error">{accessors.state.error}</Alert>
          )}
          {accessors.state.status === 'success' &&
            (accessors.state.data.count === 0 ? (
              <EmptyState
                message="No recorded access"
                hint="Nobody has opened this record within the retention window."
              />
            ) : (
              <div className="table-wrap">
                <table>
                  <caption className="visually-hidden">Clinicians who accessed this record</caption>
                  <thead>
                    <tr>
                      <th scope="col">Clinician</th>
                      <th scope="col">Accesses</th>
                      <th scope="col">Denied</th>
                      <th scope="col">Overrides</th>
                      <th scope="col">First access</th>
                      <th scope="col">Last access</th>
                    </tr>
                  </thead>
                  <tbody>
                    {accessors.state.data.accessors.map((a) => (
                      <tr key={a.actor_sub}>
                        <td className="mono">{a.actor_sub}</td>
                        <td>{a.accesses}</td>
                        <td>
                          {a.denied > 0 ? (
                            <Badge tone="err">{a.denied}</Badge>
                          ) : (
                            <span className="muted">0</span>
                          )}
                        </td>
                        <td>
                          {a.break_glass > 0 ? (
                            <Badge tone="warn">{a.break_glass}</Badge>
                          ) : (
                            <span className="muted">0</span>
                          )}
                        </td>
                        <td className="mono">{formatTimestamp(a.first_access)}</td>
                        <td className="mono">{formatTimestamp(a.last_access)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
        </Card>
      )}

      {/* Individual events behind the counts. */}
      {events.state.status !== 'idle' && (
        <Card
          title="Access events"
          actions={
            result ? (
              <span className="muted">
                {result.total === 0
                  ? 'No matches'
                  : `${result.offset + 1}–${result.offset + result.count} of ${result.total}`}
              </span>
            ) : null
          }
        >
          {events.state.status === 'loading' && <SkeletonRows rows={4} />}
          {result &&
            (result.total === 0 ? (
              <EmptyState
                message="No matching audit events"
                hint="Try widening the filters, or check the retention window."
              />
            ) : (
              <>
                <div className="table-wrap">
                  <table>
                    <caption className="visually-hidden">Audit events</caption>
                    <thead>
                      <tr>
                        <th scope="col">When</th>
                        <th scope="col">Clinician</th>
                        <th scope="col">Request</th>
                        <th scope="col">Outcome</th>
                        <th scope="col">Record ref</th>
                        <th scope="col">Client IP</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.items.map((event, i) => (
                        <EventRow key={`${event.request_id ?? 'evt'}-${i}`} event={event} />
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="row" style={{ marginTop: 12, alignItems: 'center' }}>
                  <button
                    type="button"
                    disabled={offset === 0 || events.state.status === 'loading'}
                    onClick={() => void goToPage(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Previous
                  </button>
                  <button
                    type="button"
                    disabled={!hasMore || events.state.status === 'loading'}
                    onClick={() => void goToPage(offset + PAGE_SIZE)}
                  >
                    Next
                  </button>
                  <span className="muted">
                    Window {formatTimestamp(result.since)} – {formatTimestamp(result.until)}
                  </span>
                </div>
              </>
            ))}
        </Card>
      )}
    </>
  )
}
