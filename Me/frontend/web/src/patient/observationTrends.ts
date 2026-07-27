/**
 * Extract simple numeric trends from FHIR Observation resources for the chart.
 */

import type { FhirResource } from '../api/types'
import { summariseResource } from '../pages/FhirPage'

export type ObservationPoint = {
  id: string
  label: string
  value: number
  unit?: string
  effective?: string
}

export function observationNumericValue(obs: FhirResource): { value: number; unit?: string } | null {
  const vq = obs.valueQuantity as { value?: unknown; unit?: string } | undefined
  if (vq && typeof vq.value === 'number' && Number.isFinite(vq.value)) {
    return { value: vq.value, unit: typeof vq.unit === 'string' ? vq.unit : undefined }
  }
  if (typeof obs.valueInteger === 'number' && Number.isFinite(obs.valueInteger)) {
    return { value: obs.valueInteger }
  }
  if (typeof obs.valueDecimal === 'number' && Number.isFinite(obs.valueDecimal)) {
    return { value: obs.valueDecimal }
  }
  // valueString like "98 %" — best-effort
  if (typeof obs.valueString === 'string') {
    const m = obs.valueString.trim().match(/^(-?\d+(?:\.\d+)?)/)
    if (m) return { value: Number(m[1]) }
  }
  return null
}

export function observationEffective(obs: FhirResource): string | undefined {
  if (typeof obs.effectiveDateTime === 'string') return obs.effectiveDateTime
  const period = obs.effectivePeriod as { end?: string; start?: string } | undefined
  return period?.end || period?.start
}

/** Newest-first numeric observations, capped. */
export function extractObservationPoints(observations: FhirResource[], limit = 8): ObservationPoint[] {
  const points: ObservationPoint[] = []
  for (const obs of observations) {
    const num = observationNumericValue(obs)
    if (!num) continue
    points.push({
      id: String(obs.id ?? points.length),
      label: summariseResource(obs),
      value: num.value,
      unit: num.unit,
      effective: observationEffective(obs),
    })
  }
  return points.slice(0, limit)
}

/** 0..100 width for a simple bar given min/max of the series (or label defaults). */
export function barWidthPercent(value: number, series: number[]): number {
  if (!series.length) return 0
  const min = Math.min(...series)
  const max = Math.max(...series)
  if (max === min) return 50
  return Math.round(((value - min) / (max - min)) * 100)
}
