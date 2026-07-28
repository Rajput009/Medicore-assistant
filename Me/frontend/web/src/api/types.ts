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

/** What happened to a patient. Closed set — see the backend DISPOSITIONS. */
export const DISPOSITIONS = [
  'admitted',
  'discharged',
  'transferred',
  'left_without_being_seen',
  'deceased',
  'other',
] as const
export type Disposition = (typeof DISPOSITIONS)[number]

/** Dispositions that say nothing on their own and require a note. */
export const DISPOSITIONS_REQUIRING_NOTE: readonly Disposition[] = [
  'left_without_being_seen',
  'other',
]

/** Human labels; the wire values stay machine-readable. */
export const DISPOSITION_LABELS: Record<Disposition, string> = {
  admitted: 'Admitted',
  discharged: 'Discharged',
  transferred: 'Transferred',
  left_without_being_seen: 'Left without being seen',
  deceased: 'Deceased',
  other: 'Other',
}

/** A reason is mandatory at this acuity or more urgent. Mirrors the API. */
export const REASON_REQUIRED_AT_ACUITY = 2
export const MIN_REASON_LENGTH = 10

export type VitalsSnapshot = {
  respiratory_rate?: number
  spo2?: number
  temperature?: number
  systolic_bp?: number
  pulse?: number
  consciousness?: string
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

  /** Why this patient was escalated, and the evidence behind it. */
  reason?: string
  news2_score?: number
  news2_band?: string
  red_flag?: boolean
  vitals_snapshot?: VitalsSnapshot

  /** What happened, recorded at completion. */
  disposition?: Disposition
  disposition_note?: string
  completed_by?: string
  completed_at?: string
  time_to_completion_seconds?: number
}

export type QueueStats = {
  dept: string | null
  since: string
  window_hours: number
  completed: number
  waiting: number
  by_disposition: Record<string, number>
  left_without_being_seen_rate: number
  /** null when nothing completed in the window — not the same as zero. */
  median_seconds: number | null
  p90_seconds: number | null
}

export type QueueHistory = {
  patient_id: string
  entries: QueueItem[]
  count: number
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

/** Outcomes recorded in the audit trail. */
export const AUDIT_OUTCOMES = ['success', 'failure', 'denied', 'error'] as const
export type AuditOutcome = (typeof AUDIT_OUTCOMES)[number]

/**
 * One recorded access. Patient identifiers are pseudonymised server-side, so
 * `resource_ref` / `patient_ref` are salted hashes, never raw MRNs.
 */
export type AuditEvent = {
  ts: string
  request_id?: string | null
  service?: string | null
  actor_sub?: string | null
  actor_roles?: string[] | null
  method: string
  path: string
  status?: number | null
  outcome?: AuditOutcome | string | null
  resource_type?: string | null
  resource_ref?: string | null
  patient_ref?: string | null
  bed_id?: string | null
  client_ip?: string | null
  user_agent?: string | null
  duration_ms?: number | null
  query_keys?: string[] | null
  /** True when scope was overridden under break-glass. */
  break_glass?: boolean | null
  break_glass_reason?: string | null
  /** Patients disclosed by a search result (pseudonymised, truncated). */
  subject_refs?: string[] | null
  /** True number disclosed, even when subject_refs was truncated. */
  subject_count?: number | null
  /** True when subject_refs omits some of the patients disclosed. */
  subjects_truncated?: boolean | null
}

export type AuditSearchResponse = {
  items: AuditEvent[]
  count: number
  total: number
  limit: number
  offset: number
  since: string
  until: string
  /** The hash the server matched on; useful for cross-referencing raw logs. */
  subject_ref: string | null
}

/** Filters accepted by the audit search endpoint. All optional. */
export type AuditSearchParams = {
  patient?: string
  actor?: string
  outcome?: AuditOutcome | ''
  resource_type?: string
  service?: string
  since?: string
  until?: string
  /** True to review only emergency overrides. */
  break_glass?: boolean
  limit?: number
  offset?: number
}

/** One clinician's access history for a patient. */
export type AuditAccessor = {
  actor_sub: string
  accesses: number
  denied: number
  /** How many of those accesses used an emergency override. */
  break_glass: number
  first_access: string
  last_access: string
}

export type AuditAccessorsResponse = {
  patient_ref: string
  accessors: AuditAccessor[]
  count: number
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

/** A persisted shift-handoff (SBAR) note. Append-only server-side. */
export type HandoffNote = {
  patient_id: string
  text: string
  author: string
  encounter_id?: string | null
  created_at: string
}

export type HandoffResponse = {
  patient_id: string
  note: HandoffNote | null
}

export type HandoffHistoryResponse = {
  patient_id: string
  versions: HandoffNote[]
  count: number
}

/** Grounded chart Q&A (Tier 4). */

export type AssistCitation = {
  resource_type: string
  resource_id: string
  label: string
  recorded?: string | null
}

export type AssistFinding = {
  text: string
  critical: boolean
  citations: AssistCitation[]
}

export type AssistAnswer = {
  patient_id: string
  intents: string[]
  findings: AssistFinding[]
  /** What the answer does NOT establish — as clinically important as findings. */
  caveats: string[]
  answered: boolean
  disclaimer: string
  retrieved: {
    allergies?: number
    medications?: number
    conditions?: number
    observations?: number
    encounters?: number
    /** Resource types whose retrieval failed; NOT the same as "none found". */
    failed?: string[]
  }
}
