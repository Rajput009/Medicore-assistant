/** E2E: the main clinician workflows. */

import { expect, signIn, test } from './fixtures'

test.beforeEach(async ({ stubbedPage: page }) => {
  await signIn(page, ['clinician', 'admin'])
})

test.describe('dashboard', () => {
  test('shows health for every service', async ({ stubbedPage: page }) => {
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Gateway' })).toBeVisible()
    await expect(page.getByText('healthy')).toHaveCount(4)
  })

  test('flags an unreachable service without breaking the page', async ({
    stubbedPage: page,
  }) => {
    await page.route('**/cds/health', (r) => r.abort())
    await page.goto('/')
    await expect(page.getByText('unreachable')).toBeVisible()
    await expect(page.getByText('healthy')).toHaveCount(3)
  })
})

test.describe('FHIR explorer', () => {
  test('searches and displays patients', async ({ stubbedPage: page }) => {
    await page.goto('/fhir')
    await page.getByRole('button', { name: /^search$/i }).click()

    await expect(page.getByText('Ada Lovelace')).toBeVisible()
    await expect(page.getByText('Alan Turing')).toBeVisible()
  })

  test('sends the patient filter to the gateway', async ({ stubbedPage: page }) => {
    await page.goto('/fhir')
    const request = page.waitForRequest((r) => r.url().includes('/fhir/patient/search'))
    await page.getByLabel(/patient id/i).fill('123')
    await page.getByRole('button', { name: /^search$/i }).click()
    expect((await request).url()).toContain('patient=123')
  })

  test('shows an empty state when nothing matches', async ({ stubbedPage: page }) => {
    await page.route('**/api/fhir/patient/search*', (r) =>
      r.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ resourceType: 'Bundle', entry: [] }),
      }),
    )
    await page.goto('/fhir')
    await page.getByRole('button', { name: /^search$/i }).click()
    await expect(page.getByText(/no matching resources/i)).toBeVisible()
  })

  test('surfaces an upstream failure as a clear message', async ({ stubbedPage: page }) => {
    await page.route('**/api/fhir/patient/search*', (r) =>
      r.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'upstream down' }),
      }),
    )
    await page.goto('/fhir')
    await page.getByRole('button', { name: /^search$/i }).click()
    await expect(page.getByRole('alert')).toContainText(/upstream FHIR server/i)
  })

  test('reads a single resource by id', async ({ stubbedPage: page }) => {
    await page.goto('/fhir')
    await page.getByLabel(/mode/i).selectOption('read')
    await page.getByLabel(/resource id/i).fill('p1')
    await page.getByRole('button', { name: /fetch/i }).click()
    await expect(page.getByLabel('FHIR resource')).toContainText('"id": "p1"')
  })
})

test.describe('patient flow', () => {
  test('lists beds and the triage queue', async ({ stubbedPage: page }) => {
    await page.goto('/flow')
    await expect(page.getByText(/1 of 2 available/i)).toBeVisible()
    await expect(page.getByText('pat-1')).toBeVisible()
    await expect(page.getByText('ESI 1')).toBeVisible()
  })

  test('toggles a bed', async ({ stubbedPage: page }) => {
    await page.goto('/flow')
    const request = page.waitForRequest(
      (r) => r.url().includes('/flow/beds/') && r.method() === 'PATCH',
    )
    await page.getByRole('button', { name: /mark occupied/i }).first().click()
    expect((await request).url()).toContain('occupied=true')
  })

  test('validates the enqueue form', async ({ stubbedPage: page }) => {
    await page.goto('/flow')
    await page.getByRole('button', { name: /add to queue/i }).click()
    await expect(page.getByText('Patient id is required.')).toBeVisible()
  })

  test('adds a patient to the queue', async ({ stubbedPage: page }) => {
    await page.goto('/flow')
    await page.locator('#enqueue-patient').fill('pat-99')
    await page.locator('#enqueue-acuity').selectOption('2')
    await page.locator('#enqueue-dept').fill('ICU')
    await page.getByRole('button', { name: /add to queue/i }).click()
    await expect(page.getByText(/patient added to queue/i)).toBeVisible()
  })
})

test.describe('decision support', () => {
  test('scores a healthy full vital set as low risk', async ({ stubbedPage: page }) => {
    await page.goto('/cds')
    await page.getByRole('button', { name: /calculate news2/i }).click()
    await expect(page.getByText('low', { exact: true })).toBeVisible()
  })

  test('scores critical vitals as high risk', async ({ stubbedPage: page }) => {
    await page.goto('/cds')
    await page.getByLabel(/respiratory rate/i).fill('30')
    await page.getByLabel(/systolic/i).fill('80')
    await page.getByLabel(/oxygen saturation/i).fill('85')
    await page.getByRole('button', { name: /calculate news2/i }).click()
    await expect(page.getByText('high', { exact: true })).toBeVisible()
  })

  test('shows the per-parameter breakdown behind the score', async ({ stubbedPage: page }) => {
    await page.goto('/cds')
    await page.getByRole('button', { name: /calculate news2/i }).click()
    await expect(page.getByRole('table')).toBeVisible()
  })

  test('saves vitals to the chart', async ({ stubbedPage: page }) => {
    await page.goto('/cds?patient=MRN-42')
    await page.getByRole('button', { name: /calculate news2/i }).click()
    await page.getByRole('button', { name: /save vitals to chart/i }).click()
    await expect(page.getByText(/saved 6 observations/i)).toBeVisible()
  })

  test('blocks impossible vitals before calling the server', async ({ stubbedPage: page }) => {
    await page.goto('/cds')
    await page.getByLabel(/oxygen saturation/i).fill('400')
    await page.getByRole('button', { name: /calculate news2/i }).click()
    await expect(page.getByText(/must be between 1 and 100/i)).toBeVisible()
  })

  test('displays the clinical safety disclaimer', async ({ stubbedPage: page }) => {
    await page.goto('/cds')
    await expect(page.getByText(/not a diagnosis/i)).toBeVisible()
  })
})

test.describe('audit trail search', () => {
  test('answers "who viewed this patient?"', async ({ stubbedPage: page }) => {
    await page.goto('/admin')
    await page.getByLabel(/patient id \/ MRN/i).fill('MRN-000123')
    await page.getByRole('button', { name: /search audit trail/i }).click()
    await expect(page.getByText('dr.smith')).toBeVisible()
  })

  test('never sends the raw MRN into the results table', async ({ stubbedPage: page }) => {
    await page.goto('/admin')
    await page.getByLabel(/patient id \/ MRN/i).fill('MRN-000123')
    await page.getByRole('button', { name: /search audit trail/i }).click()
    await expect(page.getByRole('table')).toBeVisible()
    await expect(page.getByRole('table')).not.toContainText('MRN-000123')
  })
})

test.describe('cache administration', () => {
  test('requires confirmation before clearing', async ({ stubbedPage: page }) => {
    await page.goto('/admin')
    await page.getByRole('button', { name: /clear cache/i }).click()
    await expect(page.getByRole('alertdialog')).toBeVisible()

    await page.getByRole('button', { name: /yes, clear cache/i }).click()
    await expect(page.getByText(/cleared 3 cached entries/i)).toBeVisible()
  })

  test('can be cancelled', async ({ stubbedPage: page }) => {
    await page.goto('/admin')
    await page.getByRole('button', { name: /clear cache/i }).click()
    await page.getByRole('button', { name: /cancel/i }).click()
    await expect(page.getByRole('alertdialog')).toHaveCount(0)
  })
})

test.describe('accessibility and responsiveness', () => {
  test('is keyboard navigable from the skip link', async ({ stubbedPage: page }) => {
    await page.goto('/')
    await page.keyboard.press('Tab')
    await expect(page.getByRole('link', { name: /skip to main content/i })).toBeFocused()
  })

  test('every page has exactly one h1', async ({ stubbedPage: page }) => {
    for (const route of ['/', '/fhir', '/flow', '/cds', '/admin']) {
      await page.goto(route)
      await expect(page.locator('h1')).toHaveCount(1)
    }
  })

  test('renders usably on a narrow viewport', async ({ stubbedPage: page }) => {
    await page.setViewportSize({ width: 375, height: 720 })
    await page.goto('/')
    await expect(page.getByRole('heading', { name: /system overview/i })).toBeVisible()
    await expect(page.getByRole('navigation', { name: /primary/i })).toBeVisible()
  })

  test('shows a 404 page for an unknown route', async ({ stubbedPage: page }) => {
    await page.goto('/does-not-exist')
    await expect(page.getByRole('heading', { name: /page not found/i })).toBeVisible()
  })
})
