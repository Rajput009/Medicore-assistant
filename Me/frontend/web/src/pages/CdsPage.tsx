import React, { useState } from 'react'

import { api } from '../api/client'
import type { RiskResponse } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { useAsyncAction } from '../hooks/useAsync'
import { Alert, Badge, Card, Field, Spinner } from '../ui/components'

type VitalKey = 'hr' | 'sbp' | 'spo2'

/** Bounds mirror the server-side pydantic constraints so we fail fast. */
const LIMITS: Record<VitalKey, { min: number; max: number; label: string; unit: string }> = {
  hr: { min: 1, max: 300, label: 'Heart rate', unit: 'bpm' },
  sbp: { min: 1, max: 300, label: 'Systolic BP', unit: 'mmHg' },
  spo2: { min: 1, max: 100, label: 'Oxygen saturation', unit: '%' },
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

export function riskTone(label: RiskResponse['class_label']): 'ok' | 'warn' | 'err' {
  if (label === 'high') return 'err'
  if (label === 'medium') return 'warn'
  return 'ok'
}

export const CdsPage: React.FC = () => {
  const { token } = useAuth()
  const [values, setValues] = useState<Record<VitalKey, string>>({
    hr: '72',
    sbp: '120',
    spo2: '98',
  })
  const [errors, setErrors] = useState<Record<string, string>>({})
  const risk = useAsyncAction<[number, number, number], RiskResponse>((signal, hr, sbp, spo2) =>
    api.risk({ hr, sbp, spo2 }, token, signal),
  )

  const set = (key: VitalKey) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setValues((v) => ({ ...v, [key]: e.target.value }))

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const next = validateVitals(values)
    setErrors(next)
    if (Object.keys(next).length > 0) return
    void risk.run(Number(values.hr), Number(values.sbp), Number(values.spo2))
  }

  return (
    <>
      <header className="page-header">
        <h1>Clinical decision support</h1>
        <p>Deterioration risk from vital signs.</p>
      </header>

      <Alert kind="info">
        Scoring follows NEWS2 (Royal College of Physicians, 2017). It is a track-and-trigger aid
        for escalation, not a diagnosis, and is not validated for children or pregnancy.
      </Alert>

      <Card title="Vital signs">
        <form onSubmit={onSubmit} noValidate className="stack">
          <div className="row">
            {(Object.keys(LIMITS) as VitalKey[]).map((key) => (
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
                    min={LIMITS[key].min}
                    max={LIMITS[key].max}
                    value={values[key]}
                    onChange={set(key)}
                  />
                )}
              </Field>
            ))}
          </div>

          {risk.state.status === 'error' && <Alert kind="error">{risk.state.error}</Alert>}

          <div>
            <button type="submit" className="primary" disabled={risk.state.status === 'loading'}>
              {risk.state.status === 'loading' ? (
                <>
                  <Spinner label="Scoring" /> Scoring…
                </>
              ) : (
                'Calculate risk'
              )}
            </button>
          </div>
        </form>
      </Card>

      {risk.state.status === 'success' && (
        <Card title="Result">
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
            <div>
              <div className="muted" style={{ fontSize: '0.8rem' }}>
                Score
              </div>
              <div style={{ fontSize: '2rem', fontWeight: 700 }}>
                {risk.state.data.score.toFixed(3)}
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: '0.8rem' }}>
                Classification
              </div>
              <Badge tone={riskTone(risk.state.data.class_label)} withDot>
                {risk.state.data.class_label}
              </Badge>
            </div>
            <div>
              <div className="muted" style={{ fontSize: '0.8rem' }}>
                NEWS2 aggregate
              </div>
              <div style={{ fontSize: '1.5rem', fontWeight: 700 }}>
                {risk.state.data.news2_score}
              </div>
            </div>
          </div>

          {/* A single extreme parameter mandates review even at a low total. */}
          {risk.state.data.red_flag && (
            <div style={{ marginTop: 12 }}>
              <Alert kind="error">
                Red flag: a single parameter is severely abnormal. Escalate regardless of the
                aggregate score.
              </Alert>
            </div>
          )}

          <p style={{ marginTop: 12, marginBottom: 0 }}>
            <strong>Recommended response:</strong> {risk.state.data.recommended_response}
          </p>
          {/* Visual scale; aria attributes expose the value to screen readers. */}
          <div
            role="meter"
            aria-valuenow={risk.state.data.score}
            aria-valuemin={0}
            aria-valuemax={1}
            aria-label="Risk score"
            style={{
              marginTop: 16,
              height: 8,
              borderRadius: 999,
              background: 'var(--surface-alt)',
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                width: `${Math.min(100, Math.max(0, risk.state.data.score * 100))}%`,
                height: '100%',
                background:
                  risk.state.data.class_label === 'high'
                    ? 'var(--danger)'
                    : risk.state.data.class_label === 'medium'
                      ? 'var(--warning)'
                      : 'var(--success)',
              }}
            />
          </div>
        </Card>
      )}
    </>
  )
}
