/**
 * Ward whiteboard — beds grouped by ward for charge-nurse style overview.
 */

import React, { useMemo, useState } from 'react'

import { api } from '../api/client'
import type { Bed } from '../api/types'
import { useAsyncData } from '../hooks/useAsync'
import { PatientLink } from '../patient/PatientChartDrawer'
import { Alert, Badge, Card, EmptyState, Field, SkeletonRows } from '../ui/components'

export function groupBedsByWard(beds: Bed[]): Record<string, Bed[]> {
  const groups: Record<string, Bed[]> = {}
  for (const bed of beds) {
    const ward = bed.ward || '—'
    if (!groups[ward]) groups[ward] = []
    groups[ward].push(bed)
  }
  for (const ward of Object.keys(groups)) {
    groups[ward].sort((a, b) => a.bed_id.localeCompare(b.bed_id))
  }
  return groups
}

export function wardSummary(beds: Bed[]): { free: number; total: number } {
  return { free: beds.filter((b) => !b.occupied).length, total: beds.length }
}

export const WardBoardPage: React.FC = () => {
  const [filter, setFilter] = useState('')
  const { state, reload } = useAsyncData((signal) => api.listBeds(null, null, signal), [])

  const groups = useMemo(() => {
    const beds = state.status === 'success' ? state.data : []
    const all = groupBedsByWard(beds)
    const f = filter.trim().toLowerCase()
    if (!f) return all
    return Object.fromEntries(Object.entries(all).filter(([ward]) => ward.toLowerCase().includes(f)))
  }, [state, filter])

  const wards = Object.keys(groups).sort()

  return (
    <>
      <header className="page-header">
        <h1>Ward board</h1>
        <p>Live bed census by ward. Click a patient to open the chart.</p>
      </header>

      <div className="row" style={{ marginBottom: 14, alignItems: 'flex-end' }}>
        <Field id="ward-filter" label="Filter wards">
          {(props) => (
            <input
              {...props}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="e.g. ICU"
            />
          )}
        </Field>
        <button type="button" className="ghost" onClick={reload}>
          Refresh
        </button>
      </div>

      {state.status === 'loading' && <SkeletonRows rows={6} />}
      {state.status === 'error' && <Alert kind="error">{state.error}</Alert>}

      {state.status === 'success' && wards.length === 0 && (
        <EmptyState message="No wards match" hint="Clear the filter or seed beds." />
      )}

      {state.status === 'success' && (
        <div className="ward-board">
          {wards.map((ward) => {
            const beds = groups[ward]
            const { free, total } = wardSummary(beds)
            return (
              <Card
                key={ward}
                title={
                  <>
                    Ward {ward}{' '}
                    <Badge tone={free === 0 ? 'err' : free < total / 2 ? 'warn' : 'ok'}>
                      {free}/{total} free
                    </Badge>
                  </>
                }
              >
                {beds.map((bed) => (
                  <div
                    key={bed.bed_id}
                    className={`bed-tile ${bed.occupied ? 'busy' : 'free'}`}
                  >
                    <div className="bed-id">{bed.bed_id}</div>
                    <div style={{ marginTop: 4 }}>
                      {bed.occupied ? (
                        <>
                          <Badge tone="err" withDot>
                            occupied
                          </Badge>{' '}
                          {bed.patient_id ? (
                            <PatientLink id={bed.patient_id} />
                          ) : (
                            <span className="muted">unknown patient</span>
                          )}
                        </>
                      ) : (
                        <Badge tone="ok" withDot>
                          available
                        </Badge>
                      )}
                    </div>
                  </div>
                ))}
              </Card>
            )
          })}
        </div>
      )}
    </>
  )
}
