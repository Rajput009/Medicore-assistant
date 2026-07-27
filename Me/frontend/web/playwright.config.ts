import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end configuration.
 *
 * By default the suite runs against the Vite dev server with the backend
 * stubbed at the network layer (see e2e/fixtures.ts), so it needs no database
 * or running services. Set E2E_LIVE=1 together with E2E_BASE_URL to run the
 * same specs against a real deployment.
 */
const PORT = Number(process.env.E2E_PORT ?? 5173)
const baseURL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${PORT}`
const live = process.env.E2E_LIVE === '1'

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 7_000 },
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list']],

  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },

  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'mobile-safari', use: { ...devices['iPhone 13'] } },
  ],

  // When testing a live deployment the caller is responsible for the server.
  webServer: live
    ? undefined
    : {
        command: `npm run dev -- --port ${PORT} --strictPort`,
        url: baseURL,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
})
