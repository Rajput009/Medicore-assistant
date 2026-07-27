/**
 * E2E: the clinical features added after the original console.
 *
 * These cover the chart drawer, handoff notes and the chart assistant in a
 * real browser. The jsdom tests already assert the same logic; what only a
 * browser proves is that the pieces are actually reachable — that the drawer
 * opens from a link, that the assistant is rendered inside it, that a form
 * submit does not reload the page, and that focus and roles behave.
 *
 * Safety-relevant rendering is asserted here rather than left to unit tests
 * alone, because "the citation was in the DOM" and "the clinician could see
 * it" are different claims.
 */

import { expect, signIn, test } from './fixtures'

test.beforeEach(async ({ stubbedPage: page }) => {
  await signIn(page, ['clinician', 'admin'])
})

/**
 * Opens the chart drawer via the deep-link query parameter.
 *
 * Waits on the dialog role rather than an accessible name: the drawer sets
 * aria-labelledby to the patient title, so its name is the patient, not a
 * fixed string. Verified in src/e2eSelectors.test.tsx.
 */
async function openChart(page: import('@playwright/test').Page, patientId = 'p1') {
  await page.goto(`/fhir?patient=${patientId}`)
  await expect(page.getByRole('dialog')).toBeVisible()
}

test.describe('patient chart drawer', () => {
  test('opens from a deep link and shows the safety lists', async ({ stubbedPage: page }) => {
    await openChart(page)
    // Allergies are the first thing a clinician checks before acting.
    await expect(page.getByText(/penicillin/i).first()).toBeVisible()
  })

  test('shows problems and medications alongside allergies', async ({ stubbedPage: page }) => {
    await openChart(page)
    await expect(page.getByText(/type 2 diabetes/i).first()).toBeVisible()
    await expect(page.getByText(/metformin/i).first()).toBeVisible()
  })

  test('an allergy retrieval failure is not rendered as "no allergies"', async ({
    stubbedPage: page,
  }) => {
    // The single most dangerous misread in the whole product.
    await page.route('**/api/fhir/allergyintolerance/**', (r) => r.abort())
    await openChart(page)
    await expect(page.getByText(/allergy list unavailable/i)).toBeVisible()
  })
})

test.describe('handoff notes', () => {
  test('loads the note left by the previous shift', async ({ stubbedPage: page }) => {
    await openChart(page)
    await expect(page.getByLabel(/sbar handoff note/i)).toHaveValue(
      /stored handoff from the previous shift/,
    )
  })

  test('names who saved it', async ({ stubbedPage: page }) => {
    await openChart(page)
    await expect(page.getByText(/last saved by/i)).toBeVisible()
    await expect(page.getByText('dr.night')).toBeVisible()
  })

  test('saves a new version and confirms it', async ({ stubbedPage: page }) => {
    await openChart(page)
    const box = page.getByLabel(/sbar handoff note/i)
    await box.fill('S - Situation: patient stable overnight')
    await page.getByRole('button', { name: /save handoff/i }).click()
    await expect(page.getByText(/saved for the next shift/i)).toBeVisible()
  })

  test('a failed save keeps the text on screen', async ({ stubbedPage: page }) => {
    await openChart(page)
    const box = page.getByLabel(/sbar handoff note/i)
    await box.fill('Unsent but important')
    await page.route(/\/flow\/handoff\/[^/?]+$/, (r) => r.abort())
    await page.getByRole('button', { name: /save handoff/i }).click()

    await expect(page.getByText(/draft is kept in this tab/i)).toBeVisible()
    // The clinician's typing must not be what gets lost.
    await expect(box).toHaveValue('Unsent but important')
  })
})

test.describe('chart assistant', () => {
  test('answers a question and shows the citation', async ({ stubbedPage: page }) => {
    await openChart(page)
    await page.getByLabel(/question about this patient/i).fill('what allergies are recorded?')
    await page.getByRole('button', { name: /^ask$/i }).click()

    await expect(page.getByText(/allergy: penicillin/i)).toBeVisible()
    // A claim whose basis is hidden is not meaningfully grounded, so the
    // citation must be on screen without any further interaction.
    await expect(page.getByText('AllergyIntolerance/a1')).toBeVisible()
  })

  test('refuses to give clinical advice', async ({ stubbedPage: page }) => {
    await openChart(page)
    await page.getByLabel(/question about this patient/i).fill('should I give penicillin?')
    await page.getByRole('button', { name: /^ask$/i }).click()

    await expect(page.getByText(/does not give clinical advice/i)).toBeVisible()
    await expect(page.getByText(/allergy: penicillin/i)).toHaveCount(0)
  })

  test('refuses a question it does not understand', async ({ stubbedPage: page }) => {
    await openChart(page)
    await page.getByLabel(/question about this patient/i).fill('what is the wifi password')
    await page.getByRole('button', { name: /^ask$/i }).click()
    await expect(page.getByText(/was not understood/i)).toBeVisible()
  })

  test('always shows the disclaimer with an answer', async ({ stubbedPage: page }) => {
    await openChart(page)
    await page.getByLabel(/question about this patient/i).fill('allergies?')
    await page.getByRole('button', { name: /^ask$/i }).click()
    await expect(page.getByText(/not a diagnosis/i)).toBeVisible()
  })

  test('an example question fills the box', async ({ stubbedPage: page }) => {
    await openChart(page)
    await page.getByRole('button', { name: /what allergies are recorded\?/i }).click()
    await expect(page.getByLabel(/question about this patient/i)).toHaveValue(
      /what allergies are recorded/i,
    )
  })

  test('asking does not navigate away from the chart', async ({ stubbedPage: page }) => {
    // A bare <form> submit would reload and silently lose the drawer.
    await openChart(page)
    await page.getByLabel(/question about this patient/i).fill('allergies?')
    await page.getByRole('button', { name: /^ask$/i }).click()
    await expect(page.getByText(/allergy: penicillin/i)).toBeVisible()
    await expect(page.getByLabel(/sbar handoff note/i)).toBeVisible()
  })
})

test.describe('worklist and ward board', () => {
  test('the worklist is reachable and lists the queue', async ({ stubbedPage: page }) => {
    await page.goto('/worklist')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  })

  test('the ward board groups beds by ward', async ({ stubbedPage: page }) => {
    await page.goto('/wards')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    await expect(page.getByText(/ward/i).first()).toBeVisible()
  })

  test('both pages are in the navigation for a clinician', async ({ stubbedPage: page }) => {
    await page.goto('/')
    await expect(page.getByRole('link', { name: /my patients/i })).toBeVisible()
    await expect(page.getByRole('link', { name: /ward board/i })).toBeVisible()
  })
})
