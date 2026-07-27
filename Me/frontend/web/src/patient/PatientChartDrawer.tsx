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
import { summariseResource } from '../pages/FhirPage'
import { Alert, Badge, Spinner } from '../ui/components'
import { usePatientChart } from './PatientChartContext'
import {
  barWidthPercent,
  extractObservationPoints,
} from './observationTrends'
import { rememberPatient } from './recentPatients'

type ChartData = {
  patient: FhirResource | null
  observations: FhirResource[]
  encounters: FhirResource[]
  beds: Bed[]
  queue: QueueItem[]
}

function patientLabel(p: FhirResource | null, fallbackId: string): string {
  if (!p) return fallbackId
  return summariseResource(p)
}

export const PatientChartDrawer: React.FC = () => {
  const { patientId, closePatient } = usePatientChart()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<ChartData | null>(null)

  useEffect(() => {
    if (!patientId) {
      setData(null)
      setError(null)
      return
    }
    const ac = new AbortController()
    setLoading(true)
    setError(null)

    void (async () => {
      try {
        const [patientResult, obsBundle, encBundle, beds, queue] = await Promise.all([
          api.fhirRead('Patient', patientId, null, ac.signal).catch(() => null),
          api
            .fhirSearch('Observation', { patient: patientId }, null, ac.signal)
            .catch(() => ({ resourceType: 'Bundle' as const, entry: [] })),
          api
            .fhirSearch('Encounter', { patient: patientId }, null, ac.signal)
            .catch(() => ({ resourceType: 'Bundle' as const, entry: [] })),
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

        setData({
          patient: patientResult,
          observations: observations.slice(0, 12),
          encounters: encounters.slice(0, 5),
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

    return () => ac.abort()
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
