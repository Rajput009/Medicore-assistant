/**
 * Clinical decision support — full NEWS2 assessment.
 *
 * Previously this page posted three vitals to `/risk`, which assumes normal
 * respiratory rate and temperature when they are missing. Those two carry
 * real NEWS2 weight, so the resulting score was a floor, not an assessment —
 * a septic patient with RR 26 could read "low risk". The form now collects
 * all six parameters and calls `/news2`, which returns the per-parameter
 * breakdown the RCP standard requires for an explainable score.
 */

import React, { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import { retryWithIdempotency } from '../api/retry'
import type { Acvpu, News2Response, VitalsWrite } from '../api/types'
import { ACVPU } from '../api/types'
import { useAsyncAction } from '../hooks/useAsync'
import { usePatientChart } from '../patient/PatientChartContext'
import { Alert, Badge, Card, Field, Spinner } from '../ui/components'

type VitalKey =
  | 'respiratory_rate'
  | 'spo2'
  | 'temperature'
  | 'systolic_bp'
  | 'pulse'

/** Bounds mirror the server-side pydantic constraints so we fail fast. */
export const LIMITS: Record<
  VitalKey,
  { min: number; max: number; label: string; unit: string; step?: string }
> = {
  respiratory_rate: { min: 1, max: 80, label: 'Respiratory rate', unit: '/min' },
  spo2: { min: 1, max: 100, label: 'Oxygen saturation', unit: '%' },
  temperature: { min: 25, max: 45, label: 'Temperature', unit: '°C', step: '0.1' },
  systolic_bp: { min: 1, max: 300, label: 'Systolic BP', unit: 'mmHg' },
  pulse: { min: 1, max: 300, label: 'Pulse', unit: 'bpm' },
}

const VITAL_ORDER: VitalKey[] = [
  'respiratory_rate',
  'spo2',
  'temperature',
  'systolic_bp',
  'pulse',
]

const ACVPU_LABELS: Record<Acvpu, string> = {
  A: 'Alert',
  C: 'Confusion',
  V: 'Voice',
  P: 'Pain',
  U: 'Unresponsive',
}

export function validateVitals(values: Record<VitalKey, string>): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const key of Object.keys(LIMITS) as VitalKey[]) {
    const raw = values[key].trim()
    const { min, max, label } = LIMITS[key]
    if (raw === '') {
      errors[key] = `${label} is required.`
      continue
    }
    const num = Number(raw)
    if (!Number.isFinite(num)) {
      errors[key] = `${label} must be a number.`
    } else if (num < min || num > max) {
      errors[key] = `${label} must be between ${min} and ${max}.`
    }
  }
  return errors
}

export function riskTone(band: News2Response['band']): 'ok' | 'warn' | 'err' {
  if (band === 'high') return 'err'
  if (band === 'medium') return 'warn'
  if (band === 'low-medium') return 'warn'
  return 'ok'
}

/**
 * Map a NEWS2 band onto an ESI-style acuity for the triage queue.
 *
 * The red flag matters as much as the total: a single parameter scoring 3
 * mandates urgent review even when the aggregate looks reassuring.
 */
export function acuityFromNews2(result: News2Response): number {
  if (result.red_flag || result.band === 'high' || result.score >= 7) return 1
  if (result.band === 'medium' || result.score >= 5) return 2
  if (result.score >= 3) return 3
  return 4
}

/** Only escalate when the score actually warrants it. */
export function shouldOfferEscalation(result: News2Response): boolean {
  return result.red_flag || result.score >= 3 || result.band !== 'low'
}

export const CdsPage: React.FC = () => {
  const [searchParams] = useSearchParams()
  const { openPatient } = usePatientChart()
  const [patientId, setPatientId] = useState(() => searchParams.get('patient')?.trim() ?? '')
  const [encounterId, setEncounterId] = useState('')
  const [dept, setDept] = useState('ED')
  const [values, setValues] = useState<Record<VitalKey, string>>({
    respiratory_rate: '16',
    spo2: '98',
    temperature: '37.0',
    systolic_bp: '120',
    pulse: '72',
  })
  const [consciousness, setConsciousness] = useState<Acvpu>('A')
  const [onOxygen, setOnOxygen] = useState(false)
  const [scale2, setScale2] = useState(false)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [escalateMsg, setEscalateMsg] = useState<string | null>(null)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const score = useAsyncAction<[], News2Response>((signal) =>
    api.news2(
      {
        respiratory_rate: Number(values.respiratory_rate),
        spo2: Number(values.spo2),
        temperature: Number(values.temperature),
        systolic_bp: Number(values.systolic_bp),
        pulse: Number(values.pulse),
        consciousness,
        on_supplemental_oxygen: onOxygen,
        use_spo2_scale2: scale2,
      },
      null,
      signal,
    ),
  )

  const escalate = useAsyncAction<[string, number, string], { ok: boolean; id?: string }>(
    (signal, pid, acuity, d) =>
      api.enqueue({ patient_id: pid, acuity, dept: d }, null, signal),
  )

  useEffect(() => {
    const p = searchParams.get('patient')?.trim()
    if (p) setPatientId(p)
  }, [searchParams])

  const set = (key: VitalKey) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setValues((v) => ({ ...v, [key]: e.target.value }))

  const result = score.state.status === 'success' ? score.state.data : null

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setEscalateMsg(null)
    setSaveMsg(null)
    setSaveError(null)
    const next = validateVitals(values)
    setErrors(next)
    if (Object.keys(next).length > 0) return
    void score.run()
  }

  const onEscalate = async () => {
    setEscalateMsg(null)
    if (!result) return
    const pid = patientId.trim()
    if (!pid) {
      setEscalateMsg('Enter a patient id before escalating to triage.')
      return
    }
    if (!dept.trim()) {
      setEscalateMsg('Enter a department (e.g. ED).')
      return
    }
    const acuity = acuityFromNews2(result)
    const res = await escalate.run(pid, acuity, dept.trim())
    if (res) {
      setEscalateMsg(`Added ${pid} to ${dept.trim()} triage at ESI ${acuity}.`)
      openPatient(pid)
    }
  }

  const onSaveVitals = async () => {
    setSaveMsg(null)
    setSaveError(null)
    const pid = patientId.trim()
    if (!pid) {
      setSaveError('Enter a patient id before saving vitals.')
      return
    }
    const payload: VitalsWrite = {
      patient_id: pid,
      respiratory_rate: Number(values.respiratory_rate),
      spo2: Number(values.spo2),
      temperature: Number(values.temperature),
      systolic_bp: Number(values.systolic_bp),
      pulse: Number(values.pulse),
      consciousness,
      ...(result ? { news2_score: result.score } : {}),
      ...(encounterId.trim() ? { encounter_id: encounterId.trim() } : {}),
    }
    setSaving(true)
    try {
      // One key for the whole save: a retry must not double-file readings.
      const res = await retryWithIdempotency((key) =>
        api.saveVitals(payload, null, undefined, key),
      )
      setSaveMsg(`Saved ${res.count} observation${res.count === 1 ? '' : 's'} to the chart.`)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Could not save vitals')
    } finally {
      setSaving(false)
    }
  }

  const acuity = useMemo(() => (result ? acuityFromNews2(result) : null), [result])

  return (
    <>
      <header className="page-header">
        <h1>Clinical decision support</h1>
        <p>NEWS2 deterioration scoring from a full set of vital signs.</p>
      </header>

      <Alert kind="info">
        Scoring follows NEWS2 (Royal College of Physicians, 2017). It is a track-and-trigger aid
        for escalation, not a diagnosis, and is not validated for children or pregnancy.
      </Alert>

      <Card title="Vital signs">
        <form onSubmit={onSubmit} noValidate className="stack">
          <div className="row">
            {VITAL_ORDER.map((key) => (
              <Field
                key={key}
                id={`vital-${key}`}
                label={`${LIMITS[key].label} (${LIMITS[key].unit})`}
                error={errors[key]}
              >
                {(props) => (
                  <input
                    {...props}
                    type="number"
                    inputMode="decimal"
                    step={LIMITS[key].step ?? '1'}
                    min={LIMITS[key].min}
                    max={LIMITS[key].max}
                    value={values[key]}
                    onChange={set(key)}
                  />
                )}
              </Field>
            ))}

            <Field id="vital-consciousness" label="Consciousness (ACVPU)">
              {(props) => (
                <select
                  {...props}
                  value={consciousness}
                  onChange={(e) => setConsciousness(e.target.value as Acvpu)}
                >
                  {ACVPU.map((level) => (
                    <option key={level} value={level}>
                      {level} — {ACVPU_LABELS[level]}
                    </option>
                  ))}
                </select>
              )}
            </Field>
          </div>

          <div className="row" style={{ gap: 18 }}>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={onOxygen}
                onChange={(e) => setOnOxygen(e.target.checked)}
              />
              On supplemental oxygen (+2)
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={scale2}
                onChange={(e) => setScale2(e.target.checked)}
              />
              SpO₂ Scale 2 (hypercapnic respiratory failure)
            </label>
          </div>

          {score.state.status === 'error' && <Alert kind="error">{score.state.error}</Alert>}

          <div>
            <button type="submit" className="primary" disabled={score.state.status === 'loading'}>
              {score.state.status === 'loading' ? (
                <>
                  <Spinner label="Scoring" /> Scoring…
                </>
              ) : (
                'Calculate NEWS2'
              )}
            </button>
          </div>
        </form>
      </Card>

      {result && (
        <Card title="Result">
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
            <div>
              <div className="muted" style={{ fontSize: '0.8rem' }}>
                NEWS2 aggregate
              </div>
              <div style={{ fontSize: '2rem', fontWeight: 700 }}>{result.score}</div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: '0.8rem' }}>
                Band
              </div>
              <Badge tone={riskTone(result.band)} withDot>
                {result.band}
              </Badge>
            </div>
            <div>
              <div className="muted" style={{ fontSize: '0.8rem' }}>
                Monitoring
              </div>
              <div>{result.monitoring_frequency}</div>
            </div>
          </div>

          {/* A single extreme parameter mandates review even at a low total. */}
          {result.red_flag && (
            <div style={{ marginTop: 12 }}>
              <Alert kind="error">
                Red flag: a single parameter is severely abnormal. Escalate regardless of the
                aggregate score.
              </Alert>
            </div>
          )}

          <p style={{ marginTop: 12, marginBottom: 0 }}>
            <strong>Recommended response:</strong> {result.recommended_response}
          </p>

          {/* Per-parameter breakdown: an unexplained score is not actionable. */}
          <table className="table" style={{ marginTop: 16 }}>
            <caption className="visually-hidden">NEWS2 score breakdown by parameter</caption>
            <thead>
              <tr>
                <th scope="col">Parameter</th>
                <th scope="col">Value</th>
                <th scope="col">Points</th>
                <th scope="col">Why</th>
              </tr>
            </thead>
            <tbody>
              {result.parameters.map((p) => (
                <tr key={p.name}>
                  <td>{p.name.replace(/_/g, ' ')}</td>
                  <td className="mono">{p.value}</td>
                  <td>
                    <Badge tone={p.score >= 3 ? 'err' : p.score > 0 ? 'warn' : 'ok'}>
                      {p.score}
                    </Badge>
                  </td>
                  <td className="muted">{p.rationale}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="escalate-bar" style={{ marginTop: 16 }}>
            <Field id="escalate-patient" label="Patient id">
              {(props) => (
                <input
                  {...props}
                  value={patientId}
                  onChange={(e) => setPatientId(e.target.value)}
                  placeholder="MRN / FHIR id"
                />
              )}
            </Field>
            <Field id="escalate-encounter" label="Encounter id (optional)">
              {(props) => (
                <input
                  {...props}
                  value={encounterId}
                  onChange={(e) => setEncounterId(e.target.value)}
                  placeholder="This visit"
                />
              )}
            </Field>
            <button
              type="button"
              onClick={() => void onSaveVitals()}
              disabled={saving}
            >
              {saving ? (
                <>
                  <Spinner label="Saving" /> Saving…
                </>
              ) : (
                'Save vitals to chart'
              )}
            </button>
          </div>

          {saveError && (
            <div style={{ marginTop: 10 }}>
              <Alert kind="error">{saveError}</Alert>
            </div>
          )}
          {saveMsg && (
            <div style={{ marginTop: 10 }}>
              <Alert kind="success">{saveMsg}</Alert>
            </div>
          )}

          {shouldOfferEscalation(result) && (
            <div className="escalate-bar" style={{ marginTop: 12 }}>
              <Field id="escalate-dept" label="Department">
                {(props) => (
                  <input
                    {...props}
                    value={dept}
                    onChange={(e) => setDept(e.target.value)}
                    placeholder="ED"
                  />
                )}
              </Field>
              <button
                type="button"
                className="primary"
                onClick={() => void onEscalate()}
                disabled={escalate.state.status === 'loading'}
              >
                {escalate.state.status === 'loading' ? (
                  <>
                    <Spinner label="Escalating" /> Adding…
                  </>
                ) : (
                  `Escalate to triage (ESI ${acuity})`
                )}
              </button>
            </div>
          )}

          {escalate.state.status === 'error' && (
            <div style={{ marginTop: 10 }}>
              <Alert kind="error">{escalate.state.error}</Alert>
            </div>
          )}
          {escalateMsg && (
            <div style={{ marginTop: 10 }}>
              <Alert kind="success">{escalateMsg}</Alert>
            </div>
          )}
        </Card>
      )}
    </>
  )
}
