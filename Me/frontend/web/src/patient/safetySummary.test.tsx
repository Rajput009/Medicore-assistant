/**
 * Allergies, problem list and medications in the chart drawer.
 *
 * The rules under test are safety rules, not formatting preferences. The
 * dangerous failure mode for all three lists is the same: an entry that
 * silently does not render reads as "this patient has none".
 */

import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import type { FhirResource } from '../api/types'
import { makeToken, renderWithProviders } from '../test/helpers'
import { server } from '../test/server'
import { PatientChartDrawer } from './PatientChartDrawer'
import {
  codeableText,
  partitionByActivity,
  sortSafetyEntries,
  summariseAllergy,
  summariseCondition,
  summariseMedication,
  type SafetyEntry,
} from './safetySummary'

const bundle = (resources: FhirResource[]) => ({
  resourceType: 'Bundle',
  entry: resources.map((resource) => ({ resource })),
})

describe('codeableText', () => {
  it('prefers text', () => {
    expect(codeableText({ text: 'Penicillin', coding: [{ display: 'Other' }] })).toBe(
      'Penicillin',
    )
  })

  it('falls back to a coding display', () => {
    expect(codeableText({ coding: [{ display: 'Penicillin G' }] })).toBe('Penicillin G')
  })

  it('falls back to a raw code rather than showing nothing', () => {
    expect(codeableText({ coding: [{ code: '7980' }] })).toBe('7980')
  })

  it('returns undefined for junk', () => {
    expect(codeableText(undefined)).toBeUndefined()
    expect(codeableText({})).toBeUndefined()
  })
})

describe('summariseAllergy', () => {
  it('reads the substance and reaction', () => {
    const entry = summariseAllergy({
      resourceType: 'AllergyIntolerance',
      id: 'a1',
      code: { text: 'Penicillin' },
      clinicalStatus: { coding: [{ code: 'active' }] },
      reaction: [{ manifestation: [{ text: 'Anaphylaxis' }] }],
    })
    expect(entry.label).toBe('Penicillin')
    expect(entry.detail).toMatch(/Anaphylaxis/)
    expect(entry.active).toBe(true)
  })

  it('flags high criticality', () => {
    const entry = summariseAllergy({
      resourceType: 'AllergyIntolerance',
      id: 'a1',
      code: { text: 'Peanut' },
      criticality: 'high',
    })
    expect(entry.critical).toBe(true)
  })

  it('treats a MISSING clinical status as active', () => {
    // An unlabelled allergy must never be quietly demoted to history.
    const entry = summariseAllergy({
      resourceType: 'AllergyIntolerance',
      id: 'a1',
      code: { text: 'Latex' },
    })
    expect(entry.active).toBe(true)
    expect(entry.status).toBe('unknown')
  })

  it('marks a resolved allergy inactive', () => {
    const entry = summariseAllergy({
      resourceType: 'AllergyIntolerance',
      id: 'a1',
      code: { text: 'Latex' },
      clinicalStatus: { coding: [{ code: 'resolved' }] },
    })
    expect(entry.active).toBe(false)
  })

  it('still produces an entry for an uncodeable allergy', () => {
    // Dropping it would read as "no allergy".
    const entry = summariseAllergy({ resourceType: 'AllergyIntolerance', id: 'a9' })
    expect(entry.label).toContain('a9')
  })
})

describe('summariseCondition', () => {
  it('reads the problem and onset', () => {
    const entry = summariseCondition({
      resourceType: 'Condition',
      id: 'c1',
      code: { text: 'Type 2 diabetes' },
      clinicalStatus: { coding: [{ code: 'active' }] },
      onsetDateTime: '2019-04-02T00:00:00Z',
    })
    expect(entry.label).toBe('Type 2 diabetes')
    expect(entry.detail).toBe('Onset 2019-04-02')
    expect(entry.active).toBe(true)
  })

  it('treats resolved and remission as inactive', () => {
    for (const code of ['resolved', 'remission', 'inactive']) {
      const entry = summariseCondition({
        resourceType: 'Condition',
        id: 'c1',
        code: { text: 'X' },
        clinicalStatus: { coding: [{ code }] },
      })
      expect(entry.active).toBe(false)
    }
  })
})

describe('summariseMedication', () => {
  it('reads the drug and dosage', () => {
    const entry = summariseMedication({
      resourceType: 'MedicationRequest',
      id: 'm1',
      status: 'active',
      medicationCodeableConcept: { text: 'Amoxicillin 500mg' },
      dosageInstruction: [{ text: 'One capsule three times a day' }],
    })
    expect(entry.label).toBe('Amoxicillin 500mg')
    expect(entry.detail).toMatch(/three times a day/)
    expect(entry.active).toBe(true)
  })

  it('reads a medication reference display', () => {
    const entry = summariseMedication({
      resourceType: 'MedicationRequest',
      id: 'm2',
      status: 'active',
      medicationReference: { display: 'Warfarin' },
    })
    expect(entry.label).toBe('Warfarin')
  })

  it('treats stopped and completed orders as inactive', () => {
    for (const status of ['stopped', 'completed', 'cancelled', 'entered-in-error']) {
      const entry = summariseMedication({
        resourceType: 'MedicationRequest',
        id: 'm3',
        status,
        medicationCodeableConcept: { text: 'X' },
      })
      expect(entry.active).toBe(false)
    }
  })

  it('keeps on-hold orders active — the drug may still be in the patient', () => {
    const entry = summariseMedication({
      resourceType: 'MedicationRequest',
      id: 'm4',
      status: 'on-hold',
      medicationCodeableConcept: { text: 'X' },
    })
    expect(entry.active).toBe(true)
  })
})

describe('ordering', () => {
  const entries: SafetyEntry[] = [
    { id: '1', label: 'Zinc', status: 'active', active: true },
    { id: '2', label: 'Aspirin', status: 'resolved', active: false },
    { id: '3', label: 'Penicillin', status: 'active', active: true, critical: true },
    { id: '4', label: 'Amoxicillin', status: 'active', active: true },
  ]

  it('puts high-criticality entries first so truncation cannot hide them', () => {
    expect(sortSafetyEntries(entries)[0].label).toBe('Penicillin')
  })

  it('puts inactive entries last', () => {
    const sorted = sortSafetyEntries(entries)
    expect(sorted[sorted.length - 1].label).toBe('Aspirin')
  })

  it('splits active from inactive without losing any entry', () => {
    const { active, inactive } = partitionByActivity(entries)
    expect(active).toHaveLength(3)
    expect(inactive).toHaveLength(1)
    expect(active.length + inactive.length).toBe(entries.length)
  })
})

describe('chart drawer safety sections', () => {
  function stubChart(options: {
    allergies?: FhirResource[]
    conditions?: FhirResource[]
    medications?: FhirResource[]
    allergyStatus?: number
  }) {
    server.use(
      http.get('/api/fhir/patient/:id', () =>
        HttpResponse.json({ resourceType: 'Patient', id: 'p1' }),
      ),
      http.get('/api/fhir/observation/search', () => HttpResponse.json(bundle([]))),
      http.get('/api/fhir/encounter/search', () => HttpResponse.json(bundle([]))),
      http.get('/api/fhir/allergyintolerance/search', () =>
        options.allergyStatus
          ? HttpResponse.json({ detail: 'upstream down' }, { status: options.allergyStatus })
          : HttpResponse.json(bundle(options.allergies ?? [])),
      ),
      http.get('/api/fhir/condition/search', () =>
        HttpResponse.json(bundle(options.conditions ?? [])),
      ),
      http.get('/api/fhir/medicationrequest/search', () =>
        HttpResponse.json(bundle(options.medications ?? [])),
      ),
      http.get('/flow/beds', () => HttpResponse.json([])),
      http.get('/flow/queue', () => HttpResponse.json({ items: [], count: 0, total: 0 })),
    )
  }

  it('shows an active allergy with its reaction', async () => {
    stubChart({
      allergies: [
        {
          resourceType: 'AllergyIntolerance',
          id: 'a1',
          code: { text: 'Penicillin' },
          criticality: 'high',
          reaction: [{ manifestation: [{ text: 'Anaphylaxis' }] }],
        },
      ],
    })
    renderWithProviders(<PatientChartDrawer />, {
      route: '/?patient=p1',
      token: makeToken(),
    })

    expect(await screen.findByText('Penicillin')).toBeInTheDocument()
    expect(screen.getByText(/Anaphylaxis/)).toBeInTheDocument()
  })

  it('says explicitly when no allergies are recorded', async () => {
    stubChart({ allergies: [] })
    renderWithProviders(<PatientChartDrawer />, {
      route: '/?patient=p1',
      token: makeToken(),
    })

    expect(await screen.findByText(/No allergies recorded/i)).toBeInTheDocument()
  })

  it('does NOT render a failed allergy lookup as "no known allergies"', async () => {
    stubChart({ allergyStatus: 502 })
    renderWithProviders(<PatientChartDrawer />, {
      route: '/?patient=p1',
      token: makeToken(),
    })

    expect(await screen.findByText(/Allergy list unavailable/i)).toBeInTheDocument()
    expect(screen.queryByText(/No allergies recorded/i)).not.toBeInTheDocument()
  })

  it('shows the problem list and medications', async () => {
    stubChart({
      conditions: [
        {
          resourceType: 'Condition',
          id: 'c1',
          code: { text: 'Type 2 diabetes' },
          clinicalStatus: { coding: [{ code: 'active' }] },
        },
      ],
      medications: [
        {
          resourceType: 'MedicationRequest',
          id: 'm1',
          status: 'active',
          medicationCodeableConcept: { text: 'Metformin 500mg' },
        },
      ],
    })
    renderWithProviders(<PatientChartDrawer />, {
      route: '/?patient=p1',
      token: makeToken(),
    })

    expect(await screen.findByText('Type 2 diabetes')).toBeInTheDocument()
    expect(screen.getByText('Metformin 500mg')).toBeInTheDocument()
  })

  it('keeps resolved entries available instead of discarding them', async () => {
    stubChart({
      allergies: [
        {
          resourceType: 'AllergyIntolerance',
          id: 'a1',
          code: { text: 'Sulfa' },
          clinicalStatus: { coding: [{ code: 'resolved' }] },
        },
      ],
    })
    renderWithProviders(<PatientChartDrawer />, {
      route: '/?patient=p1',
      token: makeToken(),
    })

    await waitFor(() =>
      expect(screen.getByText(/1 resolved \/ inactive/i)).toBeInTheDocument(),
    )
    expect(screen.getByText(/Sulfa/)).toBeInTheDocument()
  })
})
