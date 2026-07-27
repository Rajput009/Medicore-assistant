/**
 * Shared "open patient chart" state.
 *
 * Any page can open the side drawer for an MRN/FHIR id without navigating
 * away. Deep links use `?patient=<id>` on any console route.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'

type PatientChartContextValue = {
  patientId: string | null
  openPatient: (id: string) => void
  closePatient: () => void
}

const PatientChartContext = createContext<PatientChartContextValue | null>(null)

export const PatientChartProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const location = useLocation()
  const fromQuery = searchParams.get('patient')?.trim() || null
  const [patientId, setPatientId] = useState<string | null>(fromQuery)

  // Keep drawer in sync with the URL (back button, shared links).
  useEffect(() => {
    setPatientId(fromQuery)
  }, [fromQuery])

  const openPatient = useCallback(
    (id: string) => {
      const cleaned = id.trim()
      if (!cleaned) return
      setPatientId(cleaned)
      const next = new URLSearchParams(searchParams)
      next.set('patient', cleaned)
      setSearchParams(next, { replace: false })
    },
    [searchParams, setSearchParams],
  )

  const closePatient = useCallback(() => {
    setPatientId(null)
    const next = new URLSearchParams(searchParams)
    next.delete('patient')
    // Prefer replace so closing the drawer doesn't stack history noise.
    setSearchParams(next, { replace: true })
    // If we landed only for the patient query, stay on the same path.
    if (!next.toString() && location.search.includes('patient=')) {
      navigate({ pathname: location.pathname, search: '' }, { replace: true })
    }
  }, [searchParams, setSearchParams, location, navigate])

  const value = useMemo(
    () => ({ patientId, openPatient, closePatient }),
    [patientId, openPatient, closePatient],
  )

  return (
    <PatientChartContext.Provider value={value}>{children}</PatientChartContext.Provider>
  )
}

export function usePatientChart(): PatientChartContextValue {
  const ctx = useContext(PatientChartContext)
  if (!ctx) throw new Error('usePatientChart must be used within PatientChartProvider')
  return ctx
}
