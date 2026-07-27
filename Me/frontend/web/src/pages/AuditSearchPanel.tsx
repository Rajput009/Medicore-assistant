/**
 * Audit trail search — "who viewed MRN-X?".
 *
 * The compliance-facing half of HIPAA 164.312(b). Two presentation rules are
 * deliberate:
 *
 *  - **An empty result is stated, never implied.** "No matching access
 *    records" and a blank table look identical, and only one of them is an
 *    answer a compliance officer can act on.
 *  - **Denied attempts are highlighted.** A refused access is usually the
 *    reason someone is searching in the first place.
 */

import React, { useState } from 'react'

import { api } from '../api/client'
import type { AuditEvent, AuditSearchResponse } from '../api/types'
import { useAsyncAction } from '../hooks/useAsync'
import { Alert, Badge, Card, EmptyState, Field, Spinner } from '../ui/components'

const OUTCOMES = ['', 'success', 'failure', 'denied', 'error'] as const

/** Colour by outcome; denied is what an investigation looks for. */
export function outcomeTone(outcome: string | null | undefined): 'ok' | 'warn' | 'err' {
  if (outcome === 'denied' || outcome === 'error') return 'err'
  if (outcome === 'failure') return 'warn'
  return 'ok'
}

/** Relative-days shortcut → ISO timestamp for the `since` filter. */
export function sinceFromDays(days: number, now: number = Date.now()): string {
  return new Date(now - days * 24 * 60 * 60 * 1000).toISOString()
}

export function formatWhen(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

export const AuditSearchPanel: React.FC = () => {
  const [patient, setPatient] = useState('')
  const [actor, setActor] = useState('')
  const [outcome, setOutcome] = useState('')
  const [days, setDays] = useState('30')

  const search = useAsyncAction<[], AuditSearchResponse>((signal) =>
    api.auditSearch(
      {
        ...(patient.trim() ? { patient: patient.trim() } : {}),
        ...(actor.trim() ? { actor: actor.trim() } : {}),
        ...(outcome ? { outcome } : {}),
        ...(days ? { since: sinceFromDays(Number(days)) } : {}),
        limit: 100,
      },
      null,
      signal,
    ),
  )

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    void search.run()
  }

  const result = search.state.status === 'success' ? search.state.data : null

  return (
    <Card title="Audit trail search">
      <p className="muted" style={{ marginTop: 0 }}>
        Who accessed a patient&apos;s record, and when. Patient identifiers are matched
        against a salted hash, so the audit index never stores a raw MRN. This search is
        itself recorded in the audit trail.
      </p>

      <form onSubmit={onSubmit} className="stack">
        <div className="row">
          <Field id="audit-patient" label="Patient id / MRN" hint="Hashed before lookup.">
            {(props) => (
              <input
                {...props}
                value={patient}
                onChange={(e) => setPatient(e.target.value)}
                placeholder="Any patient"
              />
            )}
          </Field>
          <Field id="audit-actor" label="User">
            {(props) => (
              <input
                {...props}
                value={actor}
                onChange={(e) => setActor(e.target.value)}
                placeholder="Any user"
              />
            )}
          </Field>
          <Field id="audit-outcome" label="Outcome">
            {(props) => (
              <select {...props} value={outcome} onChange={(e) => setOutcome(e.target.value)}>
                {OUTCOMES.map((value) => (
                  <option key={value || 'any'} value={value}>
                    {value || 'Any'}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field id="audit-days" label="Time window">
            {(props) => (
              <select {...props} value={days} onChange={(e) => setDays(e.target.value)}>
                <option value="1">Last 24 hours</option>
                <option value="7">Last 7 days</option>
                <option value="30">Last 30 days</option>
                <option value="365">Last year</option>
              </select>
            )}
          </Field>
        </div>

        <div>
          <button type="submit" className="primary" disabled={search.state.status === 'loading'}>
            {search.state.status === 'loading' ? (
              <>
                <Spinner label="Searching" /> Searching…
              </>
            ) : (
              'Search audit trail'
            )}
          </button>
        </div>
      </form>

      {search.state.status === 'error' && (
        <div style={{ marginTop: 12 }}>
          <Alert kind="error">{search.state.error}</Alert>
        </div>
      )}

      {result && (
        <div style={{ marginTop: 16 }}>
          {result.items.length === 0 ? (
            // Say it outright: a blank table is not an answer.
            <EmptyState
              message="No matching access records"
              hint="Nobody accessed this record in the selected window."
            />
          ) : (
            <>
              <p className="muted">
                Showing {result.count} of {result.total} matching events.
              </p>
              <div className="table-wrap">
                <table className="table">
                  <caption className="visually-hidden">Audit trail search results</caption>
                  <thead>
                    <tr>
                      <th scope="col">When</th>
                      <th scope="col">User</th>
                      <th scope="col">Action</th>
                      <th scope="col">Resource</th>
                      <th scope="col">Outcome</th>
                      <th scope="col">Source IP</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.items.map((event: AuditEvent, index: number) => (
                      <tr key={`${event.request_id ?? index}-${index}`}>
                        <td className="mono">{formatWhen(event.occurred_at)}</td>
                        <td>{event.actor_sub ?? <span className="muted">anonymous</span>}</td>
                        <td className="mono">
                          {event.method} {event.path}
                        </td>
                        <td>{event.resource_type ?? '—'}</td>
                        <td>
                          <Badge tone={outcomeTone(event.outcome)}>
                            {event.outcome ?? 'unknown'} {event.status ? `(${event.status})` : ''}
                          </Badge>
                        </td>
                        <td className="mono">{event.client_ip ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </Card>
  )
}
