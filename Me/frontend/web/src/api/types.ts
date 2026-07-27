/** Shared API types, mirroring the FastAPI response models. */

export type Health = {
  status: string
  service: string
  env: string
}

export type TokenResponse = {
  access_token: string
  token_type: string
  expires_in?: number
}

export type Bed = {
  id: string
  ward: string
  occupied: boolean
}

export type QueueItem = {
  patient_id: string
  acuity: number
  dept: string
  created_at?: string
}

export type QueueListResponse = {
  items: QueueItem[]
  count: number
}

export type RiskRequest = {
  hr: number
  sbp: number
  spo2: number
}

export type RiskResponse = {
  score: number
  class_label: 'low' | 'medium' | 'high'
}

export type CacheInvalidationResponse = {
  status: string
  resource: string
  patient: string | null
  deleted: number
}

/** Minimal FHIR shapes — the server returns full resources/bundles. */
export type FhirResource = {
  resourceType: string
  id?: string
  [key: string]: unknown
}

export type FhirBundleEntry = {
  resource?: FhirResource
  fullUrl?: string
}

export type FhirBundle = {
  resourceType: 'Bundle'
  total?: number
  entry?: FhirBundleEntry[]
  [key: string]: unknown
}

export const FHIR_RESOURCES = [
  'Patient',
  'Encounter',
  'Observation',
  'MedicationRequest',
] as const

export type FhirResourceType = (typeof FHIR_RESOURCES)[number]

/** Maps a FHIR resource type to its gateway route segment. */
export const FHIR_ROUTE: Record<FhirResourceType, string> = {
  Patient: 'patient',
  Encounter: 'encounter',
  Observation: 'observation',
  MedicationRequest: 'medicationrequest',
}

export type Role = 'admin' | 'clinician' | 'viewer'

export type AuthUser = {
  sub: string
  roles: Role[]
  /** Expiry as epoch seconds, when present in the token. */
  exp?: number
}
