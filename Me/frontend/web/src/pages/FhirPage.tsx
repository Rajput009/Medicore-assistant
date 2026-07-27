import React, { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import type { FhirBundle, FhirResource, FhirResourceType } from '../api/types'
import { FHIR_RESOURCES } from '../api/types'
import { useAsyncAction } from '../hooks/useAsync'
import { PatientLink } from '../patient/PatientChartDrawer'
import { Alert, Card, EmptyState, Field, JsonBlock, Spinner } from '../ui/components'

type Mode = 'search' | 'read'

/** Pulls a human-readable label out of an arbitrary FHIR resource. */
export function summariseResource(resource: FhirResource): string {
  const name = resource.name
  if (Array.isArray(name) && name.length > 0) {
    const n = name[0] as { text?: string; family?: string; given?: string[] }
    if (n.text) return n.text
    const given = Array.isArray(n.given) ? n.given.join(' ') : ''
    const full = [given, n.family].filter(Boolean).join(' ')
    if (full) return full
  }
  const code = resource.code as { text?: string; coding?: { display?: string }[] } | undefined
  if (code?.text) return code.text
  if (code?.coding?.[0]?.display) return code.coding[0].display as string
  if (typeof resource.description === 'string') return resource.description
  return resource.id ? `${resource.resourceType}/${resource.id}` : resource.resourceType
}

/** Bundle entries, tolerating servers that omit `entry` entirely. */
export function bundleResources(bundle: FhirBundle | undefined): FhirResource[] {
  if (!bundle?.entry || !Array.isArray(bundle.entry)) return []
  return bundle.entry
    .map((e) => e.resource)
    .filter((r): r is FhirResource => Boolean(r && typeof r === 'object'))
}

export const FhirPage: React.FC = () => {
  const [searchParams] = useSearchParams()
  const [resource, setResource] = useState<FhirResourceType>('Patient')
  const [mode, setMode] = useState<Mode>('search')
  const [resourceId, setResourceId] = useState('')
  const [patient, setPatient] = useState(() => searchParams.get('patient')?.trim() ?? '')
  const [extraKey, setExtraKey] = useState('')
  const [extraValue, setExtraValue] = useState('')
  const [validation, setValidation] = useState<string | null>(null)

  // Prefill patient filter from deep link / chart action.
  useEffect(() => {
    const p = searchParams.get('patient')?.trim()
    if (p) setPatient(p)
  }, [searchParams])

  const search = useAsyncAction<[FhirResourceType, Record<string, string>], FhirBundle>(
    (signal, res, params) => api.fhirSearch(res, params, null, signal),
  )
  const read = useAsyncAction<[FhirResourceType, string], FhirResource>((signal, res, id) =>
    api.fhirRead(res, id, null, signal),
  )

  const active = mode === 'search' ? search : read
  const busy = active.state.status === 'loading'

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setValidation(null)

    if (mode === 'read') {
      const id = resourceId.trim()
      if (!id) {
        setValidation('Enter a resource id.')
        return
      }
      void read.run(resource, id)
      return
    }

    const params: Record<string, string> = {}
    if (patient.trim()) params.patient = patient.trim()
    if (extraKey.trim() && extraValue.trim()) params[extraKey.trim()] = extraValue.trim()
    void search.run(resource, params)
  }

  const results = search.state.status === 'success' ? bundleResources(search.state.data) : []

  return (
    <>
      <header className="page-header">
        <h1>FHIR explorer</h1>
        <p>Query the gateway&apos;s cached FHIR proxy. Requires the clinician or admin role.</p>
      </header>

      <Card title="Query">
        <form onSubmit={onSubmit} className="stack">
          <div className="row">
            <Field id="resource" label="Resource type">
              {(props) => (
                <select
                  {...props}
                  value={resource}
                  onChange={(e) => setResource(e.target.value as FhirResourceType)}
                >
                  {FHIR_RESOURCES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              )}
            </Field>

            <Field id="mode" label="Mode">
              {(props) => (
                <select {...props} value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
                  <option value="search">Search</option>
                  <option value="read">Read by id</option>
                </select>
              )}
            </Field>
          </div>

          {mode === 'read' ? (
            <Field id="resource-id" label="Resource id" error={validation}>
              {(props) => (
                <input
                  {...props}
                  value={resourceId}
                  onChange={(e) => setResourceId(e.target.value)}
                  placeholder="e.g. 123"
                />
              )}
            </Field>
          ) : (
            <div className="row">
              <Field id="patient" label="Patient id" hint="Maps to the ?patient= search parameter.">
                {(props) => (
                  <input
                    {...props}
                    value={patient}
                    onChange={(e) => setPatient(e.target.value)}
                    placeholder="e.g. 123"
                  />
                )}
              </Field>
              <Field id="extra-key" label="Extra parameter">
                {(props) => (
                  <input
                    {...props}
                    value={extraKey}
                    onChange={(e) => setExtraKey(e.target.value)}
                    placeholder="e.g. code"
                  />
                )}
              </Field>
              <Field id="extra-value" label="Value">
                {(props) => (
                  <input
                    {...props}
                    value={extraValue}
                    onChange={(e) => setExtraValue(e.target.value)}
                    placeholder="e.g. 789-8"
                  />
                )}
              </Field>
            </div>
          )}

          {mode === 'search' && validation && <Alert kind="error">{validation}</Alert>}

          <div className="row">
            <button type="submit" className="primary" disabled={busy}>
              {busy ? (
                <>
                  <Spinner label="Running query" /> Running…
                </>
              ) : mode === 'search' ? (
                'Search'
              ) : (
                'Fetch'
              )}
            </button>
            <button
              type="button"
              onClick={() => {
                search.reset()
                read.reset()
                setValidation(null)
              }}
            >
              Clear
            </button>
          </div>
        </form>
      </Card>

      {active.state.status === 'error' && <Alert kind="error">{active.state.error}</Alert>}

      {mode === 'search' && search.state.status === 'success' && (
        <Card title={`Results (${results.length})`}>
          {results.length === 0 ? (
            <EmptyState
              message="No matching resources"
              hint="Try a different patient id or search parameter."
            />
          ) : (
            <div className="table-wrap">
              <table>
                <caption className="visually-hidden">FHIR search results</caption>
                <thead>
                  <tr>
                    <th scope="col">Type</th>
                    <th scope="col">Id</th>
                    <th scope="col">Summary</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((r, i) => {
                    const chartId =
                      r.resourceType === 'Patient'
                        ? r.id
                        : typeof (r as { subject?: { reference?: string } }).subject
                              ?.reference === 'string'
                          ? String(
                              (r as { subject?: { reference?: string } }).subject?.reference,
                            )
                              .split('/')
                              .pop()
                          : patient || r.id
                    return (
                      <tr key={`${r.id ?? 'row'}-${i}`}>
                        <td>{r.resourceType}</td>
                        <td className="mono">
                          {chartId ? <PatientLink id={chartId}>{r.id ?? chartId}</PatientLink> : (r.id ?? '—')}
                        </td>
                        <td>{summariseResource(r)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
          <details style={{ marginTop: 12 }}>
            <summary>Raw bundle</summary>
            <JsonBlock value={search.state.data} label="Raw FHIR bundle" />
          </details>
        </Card>
      )}

      {mode === 'read' && read.state.status === 'success' && (
        <Card title={summariseResource(read.state.data)}>
          <JsonBlock value={read.state.data} label="FHIR resource" />
        </Card>
      )}
    </>
  )
}
