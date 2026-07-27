/**
 * Read-only patient chart drawer.
 *
 * Loads FHIR Patient + recent Observations/Encounters, plus bed and queue
 * status from patient-flow, into one side panel so clinicians don't bounce
 * between FHIR / flow / CDS pages.
 */

import React, { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import type { Bed, FhirResource, QueueItem, QueueListResponse } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { describeError } from '../hooks/useAsync'
import { summariseResource } from '../pages/FhirPage'
import { Alert, Badge, Spinner } from '../ui/components'
import { usePatientChart } from './PatientChartContext'
import {
  clearHandoff,
  loadHandoff,
  saveHandoff as saveLocalDraft,
  sbarTemplate,
} from './handoffNotes'
import {
  barWidthPercent,
  extractObservationPoints,
} from './observationTrends'
import {
  partitionByActivity,
  summariseAllergy,
  summariseCondition,
  summariseMedication,
  type SafetyEntry,
} from './safetySummary'
import { rememberPatient } from './recentPatients'

type ChartData = {
  patient: FhirResource | null
  observations: FhirResource[]
  encounters: FhirResource[]
  allergies: SafetyEntry[]
  problems: SafetyEntry[]
  medications: SafetyEntry[]
  /** True when the allergy request itself failed, as opposed to returning none. */
  allergiesUnavailable: boolean
  beds: Bed[]
  queue: QueueItem[]
}

function patientLabel(p: FhirResource | null, fallbackId: string): string {
  if (!p) return fallbackId
  return summariseResource(p)
}

/** One safety list (allergies / problems / meds) with an active-first split. */
const SafetyList: React.FC<{
  entries: SafetyEntry[]
  emptyMessage: string
  inactiveLabel: string
}> = ({ entries, emptyMessage, inactiveLabel }) => {
  const { active, inactive } = partitionByActivity(entries)

  if (entries.length === 0) {
    return (
      <p className="muted" style={{ margin: 0 }}>
        {emptyMessage}
      </p>
    )
  }

  return (
    <>
      <ul className="chart-list">
        {active.map((entry) => (
          <li key={entry.id}>
            {entry.critical && (
              <>
                <Badge tone="err" withDot>
                  high risk
                </Badge>{' '}
              </>
            )}
            <strong>{entry.label}</strong>
            {entry.detail && <div className="muted">{entry.detail}</div>}
          </li>
        ))}
      </ul>
      {/* Resolved entries are history, not noise - collapsed, never dropped. */}
      {inactive.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary className="muted">
            {inactive.length} {inactiveLabel}
          </summary>
          <ul className="chart-list">
            {inactive.map((entry) => (
              <li key={entry.id}>
                <span className="muted">
                  {entry.label} ({entry.status})
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  )
}

export const PatientChartDrawer: React.FC = () => {
  const { patientId, closePatient } = usePatientChart()
  const { user } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<ChartData | null>(null)
  const [handoffText, setHandoffText] = useState('')
  const [handoffSaved, setHandoffSaved] = useState<string | null>(null)
  const [handoffError, setHandoffError] = useState<string | null>(null)
  const [handoffSaving, setHandoffSaving] = useState(false)
  const [handoffAuthor, setHandoffAuthor] = useState<string | null>(null)
  const [handoffAt, setHandoffAt] = useState<string | null>(null)

  useEffect(() => {
    if (!patientId) {
      setData(null)
      setError(null)
      setHandoffText('')
      setHandoffSaved(null)
      setHandoffError(null)
      setHandoffAuthor(null)
      setHandoffAt(null)
      return
    }
    // Show any unsent local draft immediately, then reconcile with the
    // server: the incoming shift must see what the outgoing one saved, and a
    // draft this tab never managed to send must not be silently discarded.
    const draft = loadHandoff(patientId)
    setHandoffText(draft?.text ?? sbarTemplate(patientId))
    setHandoffSaved(null)
    setHandoffError(null)
    setHandoffAuthor(null)
    setHandoffAt(null)

    const handoffAc = new AbortController()
    void (async () => {
      try {
        const { note } = await api.getHandoff(patientId, null, handoffAc.signal)
        if (handoffAc.signal.aborted || !note) return
        setHandoffAuthor(note.author)
        setHandoffAt(note.created_at)
        // An unsent local draft is newer work than the stored note, so it
        // wins the textarea; the saved version stays visible in the byline.
        if (!draft) setHandoffText(note.text)
      } catch {
        /* No stored note, or flow is unreachable — the draft still works. */
      }
    })()

    const ac = new AbortController()
    setLoading(true)
    setError(null)

    void (async () => {
      try {
        const emptyBundle = { resourceType: 'Bundle' as const, entry: [] }
        // Allergies are tracked separately: "the request failed" and "this
        // patient has no allergies" must never render the same way.
        let allergiesFailed = false
        const [
          patientResult,
          obsBundle,
          encBundle,
          allergyBundle,
          conditionBundle,
          medBundle,
          beds,
          queue,
        ] = await Promise.all([
          api.fhirRead('Patient', patientId, null, ac.signal).catch(() => null),
          api
            .fhirSearch('Observation', { patient: patientId }, null, ac.signal)
            .catch(() => emptyBundle),
          api
            .fhirSearch('Encounter', { patient: patientId }, null, ac.signal)
            .catch(() => emptyBundle),
          api
            .fhirSearch('AllergyIntolerance', { patient: patientId }, null, ac.signal)
            .catch(() => {
              allergiesFailed = true
              return emptyBundle
            }),
          api
            .fhirSearch('Condition', { patient: patientId }, null, ac.signal)
            .catch(() => emptyBundle),
          api
            .fhirSearch('MedicationRequest', { patient: patientId }, null, ac.signal)
            .catch(() => emptyBundle),
          api.listBeds(null, null, ac.signal).catch(() => [] as Bed[]),
          api.listQueue(50, null, null, ac.signal).catch(
            () => ({ items: [], count: 0, total: 0 }) as QueueListResponse,
          ),
        ])

        if (ac.signal.aborted) return

        const observations = (obsBundle.entry ?? [])
          .map((e) => e.resource)
          .filter((r): r is FhirResource => Boolean(r))
        const encounters = (encBundle.entry ?? [])
          .map((e) => e.resource)
          .filter((r): r is FhirResource => Boolean(r))

        const resourcesOf = (bundle: { entry?: { resource?: FhirResource }[] }) =>
          (bundle.entry ?? [])
            .map((e) => e.resource)
            .filter((r): r is FhirResource => Boolean(r))

        setData({
          patient: patientResult,
          observations: observations.slice(0, 12),
          encounters: encounters.slice(0, 5),
          allergies: resourcesOf(allergyBundle).map(summariseAllergy),
          problems: resourcesOf(conditionBundle).map(summariseCondition),
          medications: resourcesOf(medBundle).map(summariseMedication),
          allergiesUnavailable: allergiesFailed,
          beds: beds.filter((b) => b.patient_id === patientId),
          queue: queue.items.filter((q) => q.patient_id === patientId),
        })
        rememberPatient(patientId)
      } catch (err) {
        if (ac.signal.aborted) return
        setError(err instanceof Error ? err.message : 'Failed to load chart')
        setData(null)
      } finally {
        if (!ac.signal.aborted) setLoading(false)
      }
    })()

    return () => {
      ac.abort()
      handoffAc.abort()
    }
  }, [patientId])

  // Escape closes the drawer.
  useEffect(() => {
    if (!patientId) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closePatient()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [patientId, closePatient])

  // Hooks must run unconditionally (before any early return).
  const trendPoints = useMemo(
    () => extractObservationPoints(data?.observations ?? [], 8),
    [data?.observations],
  )
  const trendValues = trendPoints.map((p) => p.value)
  const title = patientLabel(data?.patient ?? null, patientId ?? '')

  if (!patientId) return null

  return (
    <div className="chart-root" role="presentation">
      <button
        type="button"
        className="chart-backdrop"
        aria-label="Close patient chart"
        onClick={closePatient}
      />
      <aside
        className="chart-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Patient chart"
        aria-labelledby="chart-title"
      >
        <header className="chart-header">
          <div>
            <div className="muted" style={{ fontSize: '0.75rem' }}>
              Patient chart
            </div>
            <h2 id="chart-title" style={{ margin: '2px 0 0', fontSize: '1.15rem' }}>
              {title}
            </h2>
            <div className="mono muted" style={{ fontSize: '0.85rem' }}>
              {patientId}
            </div>
          </div>
          <button type="button" className="ghost" onClick={closePatient}>
            Close
          </button>
        </header>

        <div className="chart-body">
          {loading && (
            <p className="muted">
              <Spinner label="Loading chart" /> Loading chart…
            </p>
          )}
          {error && <Alert kind="error">{error}</Alert>}

          {!loading && data && (
            <>
              <section className="chart-section">
                <h3>Location & triage</h3>
                {data.beds.length === 0 && data.queue.length === 0 && (
                  <p className="muted" style={{ margin: 0 }}>
                    Not currently assigned to a bed or waiting in triage.
                  </p>
                )}
                {data.beds.map((b) => (
                  <div key={b.bed_id} className="chart-row">
                    <Badge tone="err" withDot>
                      bed
                    </Badge>
                    <span className="mono">
                      {b.bed_id}
                    </span>
                    <span className="muted">ward {b.ward}</span>
                  </div>
                ))}
                {data.queue.map((q) => (
                  <div key={`${q.patient_id}-${q.dept}-${q.acuity}`} className="chart-row">
                    <Badge tone={q.acuity <= 2 ? 'err' : 'warn'} withDot>
                      queue
                    </Badge>
                    <span>
                      ESI {q.acuity} · {q.dept}
                    </span>
                    <span className="muted">{q.status ?? 'waiting'}</span>
                  </div>
                ))}
              </section>

              <section className="chart-section">
                <h3>
                  Allergies{' '}
                  {data.allergies.some((a) => a.active && a.critical) && (
                    <Badge tone="err">high risk</Badge>
                  )}
                </h3>
                {data.allergiesUnavailable ? (
                  // Never render a failed lookup as "no known allergies".
                  <Alert kind="error">
                    Allergy list unavailable — do not treat this as "no known allergies".
                  </Alert>
                ) : (
                  <SafetyList
                    entries={data.allergies}
                    emptyMessage="No allergies recorded for this patient."
                    inactiveLabel="resolved / inactive"
                  />
                )}
              </section>

              <section className="chart-section">
                <h3>Problem list</h3>
                <SafetyList
                  entries={data.problems}
                  emptyMessage="No conditions recorded."
                  inactiveLabel="resolved / inactive"
                />
              </section>

              <section className="chart-section">
                <h3>Medications</h3>
                <SafetyList
                  entries={data.medications}
                  emptyMessage="No medication requests found."
                  inactiveLabel="stopped / completed"
                />
              </section>

              <section className="chart-section">
                <h3>Demographics</h3>
                {data.patient ? (
                  <dl className="chart-dl">
                    <div>
                      <dt>Resource</dt>
                      <dd className="mono">
                        {data.patient.resourceType}/{data.patient.id}
                      </dd>
                    </div>
                    {typeof data.patient.gender === 'string' && (
                      <div>
                        <dt>Gender</dt>
                        <dd>{data.patient.gender}</dd>
                      </div>
                    )}
                    {typeof data.patient.birthDate === 'string' && (
                      <div>
                        <dt>Birth date</dt>
                        <dd className="mono">{data.patient.birthDate}</dd>
                      </div>
                    )}
                    <div>
                      <dt>Active</dt>
                      <dd>{data.patient.active === false ? 'no' : 'yes'}</dd>
                    </div>
                  </dl>
                ) : (
                  <p className="muted" style={{ margin: 0 }}>
                    No FHIR Patient resource found for this id (flow-only MRN).
                  </p>
                )}
              </section>

              <section className="chart-section">
                <h3>Recent encounters</h3>
                {data.encounters.length === 0 ? (
                  <p className="muted" style={{ margin: 0 }}>
                    None returned.
                  </p>
                ) : (
                  <ul className="chart-list">
                    {data.encounters.map((e, i) => (
                      <li key={e.id ?? i}>
                        <span className="mono">{e.id ?? '—'}</span>
                        <span className="muted"> {summariseResource(e)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="chart-section">
                <h3>Observation trends</h3>
                {trendPoints.length === 0 ? (
                  <p className="muted" style={{ margin: 0 }}>
                    No numeric observations to chart.
                  </p>
                ) : (
                  <ul className="trend-list">
                    {trendPoints.map((p) => (
                      <li key={p.id} className="trend-row">
                        <div className="trend-meta">
                          <span>{p.label}</span>
                          <span className="mono">
                            {p.value}
                            {p.unit ? ` ${p.unit}` : ''}
                          </span>
                        </div>
                        <div
                          className="trend-bar-track"
                          role="img"
                          aria-label={`${p.label} ${p.value}${p.unit ? ` ${p.unit}` : ''}`}
                        >
                          <div
                            className="trend-bar-fill"
                            style={{ width: `${Math.max(8, barWidthPercent(p.value, trendValues))}%` }}
                          />
                        </div>
                        {p.effective && (
                          <div className="muted" style={{ fontSize: '0.75rem' }}>
                            {p.effective}
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="chart-section">
                <h3>Handoff note (SBAR)</h3>
                <p className="muted" style={{ marginTop: 0, fontSize: '0.8rem' }}>
                  Shared with the next shift. Working note — not part of the EHR.
                  Saving keeps every earlier version.
                </p>
                {handoffAuthor && (
                  <p className="muted" style={{ marginTop: 0, fontSize: '0.8rem' }}>
                    Last saved by <strong className="mono">{handoffAuthor}</strong>
                    {handoffAt ? ` · ${new Date(handoffAt).toLocaleString()}` : ''}
                  </p>
                )}
                <textarea
                  className="handoff-textarea"
                  aria-label="SBAR handoff note"
                  rows={8}
                  value={handoffText}
                  onChange={(e) => {
                    setHandoffText(e.target.value)
                    setHandoffSaved(null)
                  }}
                />
                <div className="row" style={{ gap: 8, marginTop: 8 }}>
                  <button
                    type="button"
                    className="primary"
                    disabled={handoffSaving}
                    onClick={() => {
                      if (!patientId) return
                      setHandoffSaved(null)
                      setHandoffError(null)
                      setHandoffSaving(true)
                      // Keep a local copy first: if the request fails, the
                      // clinician's typing must not be the thing that is lost.
                      saveLocalDraft(patientId, handoffText, user?.sub)
                      void (async () => {
                        try {
                          const { note } = await api.saveHandoff(
                            patientId,
                            handoffText,
                          )
                          setHandoffAuthor(note.author)
                          setHandoffAt(note.created_at)
                          // Sent successfully, so the local draft is no longer
                          // unsent work that needs to win on reopen.
                          clearHandoff(patientId)
                          setHandoffSaved('Saved for the next shift.')
                        } catch (err) {
                          setHandoffError(
                            `${describeError(err).message} Your draft is kept in this tab.`,
                          )
                        } finally {
                          setHandoffSaving(false)
                        }
                      })()
                    }}
                  >
                    {handoffSaving ? 'Saving…' : 'Save handoff'}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      if (!patientId) return
                      setHandoffText(sbarTemplate(patientId))
                      setHandoffSaved(null)
                    }}
                  >
                    Reset template
                  </button>
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => {
                      if (!patientId) return
                      clearHandoff(patientId)
                      setHandoffText(sbarTemplate(patientId))
                      setHandoffError(null)
                      setHandoffSaved('Local draft cleared. Saved versions are kept.')
                    }}
                  >
                    Clear
                  </button>
                </div>
                {handoffSaved && (
                  <div style={{ marginTop: 8 }}>
                    <Alert kind="success">{handoffSaved}</Alert>
                  </div>
                )}
                {handoffError && (
                  <div style={{ marginTop: 8 }}>
                    <Alert kind="error">{handoffError}</Alert>
                  </div>
                )}
              </section>

              <section className="chart-section chart-actions">
                <h3>Actions</h3>
                <div className="row" style={{ gap: 8 }}>
                  <Link
                    className="button-link primary"
                    to={`/cds?patient=${encodeURIComponent(patientId)}`}
                    onClick={closePatient}
                  >
                    Score NEWS2
                  </Link>
                  <Link
                    className="button-link"
                    to={`/fhir?patient=${encodeURIComponent(patientId)}`}
                    onClick={closePatient}
                  >
                    Open in FHIR explorer
                  </Link>
                  <Link
                    className="button-link"
                    to={`/flow?patient=${encodeURIComponent(patientId)}`}
                    onClick={closePatient}
                  >
                    Patient flow
                  </Link>
                </div>
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

/** Clickable patient id used across tables. */
export const PatientLink: React.FC<{
  id: string
  children?: React.ReactNode
  className?: string
}> = ({ id, children, className }) => {
  const { openPatient } = usePatientChart()
  return (
    <button
      type="button"
      className={`patient-link mono ${className ?? ''}`.trim()}
      onClick={() => openPatient(id)}
    >
      {children ?? id}
    </button>
  )
}
