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
  bed_id: string
  ward: string
  occupied: boolean
  patient_id?: string | null
}

export type QueueItem = {
  patient_id: string
  acuity: number
  dept: string
  created_at?: string
  status?: 'waiting' | 'in_progress' | 'completed'
  created_by?: string
  claimed_by?: string
  claimed_at?: string
}

export type QueueListResponse = {
  items: QueueItem[]
  /** Items in this page. */
  count: number
  /** Total matching the filter, so the UI can show "25 of 108". */
  total: number
}

/** ACVPU level of consciousness — anything but Alert scores 3 on NEWS2. */
export const ACVPU = ['A', 'C', 'V', 'P', 'U'] as const
export type Acvpu = (typeof ACVPU)[number]

/** Full NEWS2 input set (the /news2 endpoint), as opposed to the 3-vital /risk. */
export type News2Request = {
  respiratory_rate: number
  spo2: number
  temperature: number
  systolic_bp: number
  pulse: number
  consciousness?: Acvpu
  on_supplemental_oxygen?: boolean
  use_spo2_scale2?: boolean
}

export type News2Parameter = {
  name: string
  value: number | string
  score: number
  rationale: string
}

export type News2Response = {
  score: number
  band: 'low' | 'low-medium' | 'medium' | 'high'
  red_flag: boolean
  recommended_response: string
  monitoring_frequency: string
  parameters: News2Parameter[]
  disclaimer: string
}

/** Vitals persisted as FHIR Observations. */
export type VitalsWrite = {
  patient_id: string
  respiratory_rate?: number
  spo2?: number
  temperature?: number
  systolic_bp?: number
  pulse?: number
  consciousness?: Acvpu
  news2_score?: number
  encounter_id?: string
}

export type VitalsWriteResponse = {
  ok: boolean
  count: number
  created: { id: string; code: string }[]
}

export type RiskRequest = {
  hr: number
  sbp: number
  spo2: number
}

export type RiskResponse = {
  score: number
  class_label: 'low' | 'medium' | 'high'
  news2_score: number
  red_flag: boolean
  recommended_response: string
  disclaimer: string
}

export type BedUpdate = {
  occupied: boolean
  patient_id?: string | null
  expected_occupied?: boolean
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
  'AllergyIntolerance',
  'Condition',
] as const

export type FhirResourceType = (typeof FHIR_RESOURCES)[number]

/** Maps a FHIR resource type to its gateway route segment. */
export const FHIR_ROUTE: Record<FhirResourceType, string> = {
  Patient: 'patient',
  Encounter: 'encounter',
  Observation: 'observation',
  MedicationRequest: 'medicationrequest',
  AllergyIntolerance: 'allergyintolerance',
  Condition: 'condition',
}

export type Role = 'admin' | 'clinician' | 'viewer'

export type AuthUser = {
  sub: string
  roles: Role[]
  /** Expiry as epoch seconds, when present in the token. */
  exp?: number
  /**
   * Ward / department scope from the IdP. An **empty list means
   * unrestricted**, matching the server's `Principal.can_access_ward`. The UI
   * uses these only to filter what it shows; the server enforces access.
   */
  wards: string[]
  departments: string[]
}
