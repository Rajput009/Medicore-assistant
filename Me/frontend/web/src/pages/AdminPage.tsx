import React, { useState } from 'react'

import { api } from '../api/client'
import type { CacheInvalidationResponse, FhirResourceType } from '../api/types'
import { FHIR_RESOURCES } from '../api/types'
import { useAsyncAction } from '../hooks/useAsync'
import { Alert, Card, Field, Spinner } from '../ui/components'
import { AuditSearchPanel } from './AuditSearchPanel'

export const AdminPage: React.FC = () => {
  const [resource, setResource] = useState<FhirResourceType>('Patient')
  const [patient, setPatient] = useState('')
  const [confirming, setConfirming] = useState(false)

  const invalidate = useAsyncAction<
    [FhirResourceType, string | null],
    CacheInvalidationResponse
  >((signal, res, pid) => api.invalidateCache(res, pid, null, signal))

  const scope = patient.trim()
    ? `${resource} entries for patient ${patient.trim()}`
    : `all ${resource} entries`

  const onConfirm = async () => {
    setConfirming(false)
    await invalidate.run(resource, patient.trim() || null)
  }

  return (
    <>
      <header className="page-header">
        <h1>Administration</h1>
        <p>Audit trail search and FHIR cache management. Requires the admin role.</p>
      </header>

      <AuditSearchPanel />

      <Card title="Invalidate cache">
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault()
            setConfirming(true)
          }}
        >
          <div className="row">
            <Field id="cache-resource" label="Resource type">
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
            <Field
              id="cache-patient"
              label="Patient id (optional)"
              hint="Leave blank to clear every entry for the resource type."
            >
              {(props) => (
                <input
                  {...props}
                  value={patient}
                  onChange={(e) => setPatient(e.target.value)}
                  placeholder="All patients"
                />
              )}
            </Field>
          </div>

          {invalidate.state.status === 'error' && (
            <Alert kind="error">{invalidate.state.error}</Alert>
          )}
          {invalidate.state.status === 'success' && (
            <Alert kind="success">
              Cleared {invalidate.state.data.deleted} cached{' '}
              {invalidate.state.data.deleted === 1 ? 'entry' : 'entries'} for{' '}
              {invalidate.state.data.resource}.
            </Alert>
          )}

          {/* Destructive + irreversible, so require explicit confirmation. */}
          {confirming ? (
            <div className="alert info" role="alertdialog" aria-label="Confirm cache invalidation">
              <div className="stack" style={{ gap: 8 }}>
                <strong>Clear {scope}?</strong>
                <span>Subsequent requests will re-fetch from the upstream FHIR server.</span>
                <div className="row">
                  <button type="button" className="danger" onClick={() => void onConfirm()}>
                    Yes, clear cache
                  </button>
                  <button type="button" onClick={() => setConfirming(false)}>
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div>
              <button
                type="submit"
                className="danger"
                disabled={invalidate.state.status === 'loading'}
              >
                {invalidate.state.status === 'loading' ? (
                  <>
                    <Spinner label="Clearing" /> Clearing…
                  </>
                ) : (
                  'Clear cache…'
                )}
              </button>
            </div>
          )}
        </form>
      </Card>
    </>
  )
}
