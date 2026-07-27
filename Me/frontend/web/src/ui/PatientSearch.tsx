/**
 * Global patient jump box in the top bar.
 *
 * Opens the chart drawer for an MRN / FHIR id without leaving the current page.
 */

import React, { useState } from 'react'

import { useAuth } from '../auth/AuthContext'
import { usePatientChart } from '../patient/PatientChartContext'

export const PatientSearch: React.FC = () => {
  const { hasRole, isBootstrapping, isAuthenticated } = useAuth()
  const { openPatient } = usePatientChart()
  const [value, setValue] = useState('')

  if (isBootstrapping || !isAuthenticated) return null
  if (!hasRole('clinician', 'admin')) return null

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const id = value.trim()
    if (!id) return
    openPatient(id)
    setValue('')
  }

  return (
    <form className="patient-search" onSubmit={submit} role="search">
      <label htmlFor="global-patient-search" className="visually-hidden">
        Open patient chart
      </label>
      <input
        id="global-patient-search"
        type="search"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Patient id / MRN"
        autoComplete="off"
      />
      <button type="submit" className="primary" disabled={!value.trim()}>
        Open chart
      </button>
    </form>
  )
}
