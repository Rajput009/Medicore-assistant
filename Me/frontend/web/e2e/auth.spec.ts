/** E2E: authentication, session persistence and route protection. */

import { expect, makeToken, signIn, test } from './fixtures'

test.describe('sign-in', () => {
  test('an anonymous visitor is redirected to the login page', async ({ stubbedPage: page }) => {
    await page.goto('/')
    await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible()
  })

  test('empty submission shows validation errors', async ({ stubbedPage: page }) => {
    await page.goto('/login')
    await page.getByRole('button', { name: /^sign in$/i }).click()
    await expect(page.getByText('Username is required.')).toBeVisible()
    await expect(page.getByText('Password is required.')).toBeVisible()
  })

  test('wrong credentials show an error and keep the user signed out', async ({
    stubbedPage: page,
  }) => {
    await page.goto('/login')
    await page.getByLabel(/username/i).fill('dr.smith')
    await page.getByLabel(/password/i).fill('wrong-password')
    await page.getByRole('button', { name: /^sign in$/i }).click()

    await expect(page.getByRole('alert')).toContainText(/incorrect username or password/i)
    await expect(page).toHaveURL(/\/login/)
  })

  test('valid credentials land on the dashboard', async ({ stubbedPage: page }) => {
    await page.goto('/login')
    await page.getByLabel(/username/i).fill('dr.smith')
    await page.getByLabel(/password/i).fill('correct-horse')
    await page.getByRole('button', { name: /^sign in$/i }).click()

    await expect(page.getByRole('heading', { name: /system overview/i })).toBeVisible()
    await expect(page.getByText('dr.smith')).toBeVisible()
  })

  test('the session survives a page reload', async ({ stubbedPage: page }) => {
    await page.goto('/login')
    await page.getByLabel(/username/i).fill('dr.smith')
    await page.getByLabel(/password/i).fill('correct-horse')
    await page.getByRole('button', { name: /^sign in$/i }).click()
    await expect(page.getByRole('heading', { name: /system overview/i })).toBeVisible()

    await page.reload()
    await expect(page.getByRole('heading', { name: /system overview/i })).toBeVisible()
  })

  test('signing out clears the session', async ({ stubbedPage: page }) => {
    await signIn(page)
    await page.goto('/')
    await page.getByRole('button', { name: /sign out/i }).click()

    await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible()
    const stored = await page.evaluate(() => window.localStorage.getItem('medicore.token'))
    expect(stored).toBeNull()
  })

  test('an expired stored token does not grant access', async ({ stubbedPage: page }) => {
    const expired = makeToken({ expiresInSeconds: -60 })
    await page.addInitScript(
      ([k, v]) => window.localStorage.setItem(k as string, v as string),
      ['medicore.token', expired],
    )
    await page.goto('/')
    await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible()
  })

  test('a tampered token is rejected', async ({ stubbedPage: page }) => {
    await page.addInitScript(
      ([k, v]) => window.localStorage.setItem(k as string, v as string),
      ['medicore.token', 'not-a-real-jwt'],
    )
    await page.goto('/')
    await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible()
  })
})

test.describe('OIDC callback', () => {
  test('a token in the URL fragment establishes a session', async ({ stubbedPage: page }) => {
    await page.goto(`/oidc/callback#access_token=${makeToken({ sub: 'sso.user' })}`)
    await expect(page.getByRole('heading', { name: /system overview/i })).toBeVisible()
    // The token must not linger in the address bar.
    await expect(page).toHaveURL((url) => !url.hash.includes('access_token'))
  })

  test('a missing token reports an error', async ({ stubbedPage: page }) => {
    await page.goto('/oidc/callback')
    await expect(page.getByRole('alert')).toContainText(/no access token/i)
  })
})

test.describe('role-based access', () => {
  test('a viewer cannot open the FHIR explorer', async ({ stubbedPage: page }) => {
    await signIn(page, ['viewer'])
    await page.goto('/fhir')
    await expect(page.getByRole('heading', { name: /access denied/i })).toBeVisible()
  })

  test('a viewer sees no privileged navigation links', async ({ stubbedPage: page }) => {
    await signIn(page, ['viewer'])
    await page.goto('/')
    for (const label of [/fhir explorer/i, /patient flow/i, /decision support/i, /cache admin/i]) {
      await expect(page.getByRole('link', { name: label })).toHaveCount(0)
    }
    // The overview remains available to any authenticated user.
    await expect(page.getByRole('link', { name: /overview/i })).toBeVisible()
  })

  test('a viewer cannot open patient flow or decision support', async ({
    stubbedPage: page,
  }) => {
    await signIn(page, ['viewer'])
    for (const route of ['/flow', '/cds']) {
      await page.goto(route)
      await expect(page.getByRole('heading', { name: /access denied/i })).toBeVisible()
    }
  })

  test('a clinician cannot open cache administration', async ({ stubbedPage: page }) => {
    await signIn(page, ['clinician'])
    await page.goto('/admin')
    await expect(page.getByRole('heading', { name: /access denied/i })).toBeVisible()
  })

  test('an admin can reach every page', async ({ stubbedPage: page }) => {
    await signIn(page, ['admin'])
    await page.goto('/admin')
    await expect(page.getByRole('heading', { name: /cache administration/i })).toBeVisible()
  })
})
