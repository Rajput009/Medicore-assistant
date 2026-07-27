/**
 * Allergies, problems and medications for the chart drawer.
 *
 * These three lists are what a clinician checks *before* acting. Reading them
 * from FHIR is easy; presenting them safely is not, so the rules here are
 * deliberate:
 *
 *  - **Never silently drop an allergy.** A resource we cannot fully parse is
 *    still shown, labelled "unknown", rather than filtered out. A missing row
 *    reads as "no allergy", which is the dangerous failure mode.
 *  - **Inactive entries are separated, not hidden.** A resolved problem is
 *    still clinical history; a *cleared* allergy is not the same as one that
 *    never existed.
 *  - **Criticality drives order.** High-criticality allergies sort first so
 *    they survive truncation in a small panel.
 */

import type { FhirResource } from '../api/types'

export type SafetyEntry = {
  id: string
  label: string
  detail?: string
  /** Display-ready status, e.g. "active" / "resolved" / "unknown". */
  status: string
  /** True when this entry currently applies to the patient. */
  active: boolean
  /** Allergies only: high criticality sorts first and is badged. */
  critical?: boolean
}

/** Best-effort human label from the shapes FHIR allows for a coded concept. */
export function codeableText(value: unknown): string | undefined {
  if (!value || typeof value !== 'object') return undefined
  const concept = value as {
    text?: unknown
    coding?: { display?: unknown; code?: unknown }[]
  }
  if (typeof concept.text === 'string' && concept.text.trim()) return concept.text.trim()
  const coding = Array.isArray(concept.coding) ? concept.coding : []
  for (const entry of coding) {
    if (typeof entry?.display === 'string' && entry.display.trim()) return entry.display.trim()
  }
  for (const entry of coding) {
    if (typeof entry?.code === 'string' && entry.code.trim()) return entry.code.trim()
  }
  return undefined
}

/** Read a FHIR `*-status` CodeableConcept down to its code string. */
function statusCode(value: unknown): string | undefined {
  const text = codeableText(value)
  return text ? text.toLowerCase() : undefined
}

/**
 * AllergyIntolerance → display entry.
 *
 * `clinicalStatus` of `active` means the allergy still applies. `resolved` or
 * `inactive` means it does not — but it is still shown, in its own section,
 * because "this was cleared" is different from "we never knew".
 */
export function summariseAllergy(resource: FhirResource): SafetyEntry {
  const label =
    codeableText(resource.code) ??
    (typeof resource.id === 'string' ? `Allergy ${resource.id}` : 'Unknown allergy')

  const clinical = statusCode(resource.clinicalStatus)
  // An absent clinicalStatus is common and does NOT mean resolved. Treat it
  // as active so an unlabelled allergy is never quietly demoted.
  const active = clinical !== 'resolved' && clinical !== 'inactive'

  const criticality =
    typeof resource.criticality === 'string' ? resource.criticality.toLowerCase() : undefined

  const reactions = Array.isArray(resource.reaction) ? resource.reaction : []
  const manifestations: string[] = []
  for (const reaction of reactions) {
    const list = (reaction as { manifestation?: unknown[] })?.manifestation
    if (!Array.isArray(list)) continue
    for (const item of list) {
      const text = codeableText(item)
      if (text && !manifestations.includes(text)) manifestations.push(text)
    }
  }

  return {
    id: String(resource.id ?? label),
    label,
    detail: manifestations.length ? `Reaction: ${manifestations.join(', ')}` : undefined,
    status: clinical ?? 'unknown',
    active,
    critical: criticality === 'high',
  }
}

/** Condition → problem-list entry. */
export function summariseCondition(resource: FhirResource): SafetyEntry {
  const label =
    codeableText(resource.code) ??
    (typeof resource.id === 'string' ? `Condition ${resource.id}` : 'Unknown problem')
  const clinical = statusCode(resource.clinicalStatus)
  const active = clinical !== 'resolved' && clinical !== 'inactive' && clinical !== 'remission'
  const onset =
    typeof resource.onsetDateTime === 'string' ? resource.onsetDateTime.slice(0, 10) : undefined

  return {
    id: String(resource.id ?? label),
    label,
    detail: onset ? `Onset ${onset}` : undefined,
    status: clinical ?? 'unknown',
    active,
  }
}

/** MedicationRequest → medication-list entry. */
export function summariseMedication(resource: FhirResource): SafetyEntry {
  const label =
    codeableText(resource.medicationCodeableConcept) ??
    (resource.medicationReference as { display?: string } | undefined)?.display ??
    (typeof resource.id === 'string' ? `Medication ${resource.id}` : 'Unknown medication')

  const status = typeof resource.status === 'string' ? resource.status.toLowerCase() : 'unknown'
  // Per FHIR, only these mean the order is still in force.
  const active = status === 'active' || status === 'on-hold' || status === 'unknown'

  const instructions = Array.isArray(resource.dosageInstruction)
    ? resource.dosageInstruction
    : []
  const dosage = instructions
    .map((d) => (d as { text?: unknown })?.text)
    .find((t): t is string => typeof t === 'string' && t.trim().length > 0)

  return {
    id: String(resource.id ?? label),
    label,
    detail: dosage,
    status,
    active,
  }
}

/** Active entries first, then high-criticality, then alphabetical. */
export function sortSafetyEntries(entries: SafetyEntry[]): SafetyEntry[] {
  return [...entries].sort((a, b) => {
    if (a.active !== b.active) return a.active ? -1 : 1
    if (Boolean(a.critical) !== Boolean(b.critical)) return a.critical ? -1 : 1
    return a.label.localeCompare(b.label)
  })
}

/** Split into what applies now vs. what is historical. */
export function partitionByActivity(entries: SafetyEntry[]): {
  active: SafetyEntry[]
  inactive: SafetyEntry[]
} {
  const sorted = sortSafetyEntries(entries)
  return {
    active: sorted.filter((e) => e.active),
    inactive: sorted.filter((e) => !e.active),
  }
}
