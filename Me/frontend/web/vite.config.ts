/// <reference types="vitest" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Each backend service gets a dev-server proxy prefix so the browser only ever
 * makes same-origin requests — no CORS preflights, and the Authorization
 * header survives.
 */
const proxyTargets: Record<string, string> = {
  '/api': process.env.GATEWAY_URL ?? 'http://localhost:8080',
  '/auth': process.env.AUTH_URL ?? 'http://localhost:8081',
  '/flow': process.env.PATIENT_FLOW_URL ?? 'http://localhost:8082',
  '/cds': process.env.CDS_URL ?? 'http://localhost:8083',
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      Object.entries(proxyTargets).map(([prefix, target]) => [
        prefix,
        {
          target,
          changeOrigin: true,
          rewrite: (path: string) => path.replace(new RegExp(`^${prefix}`), ''),
        },
      ]),
    ),
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
    // Playwright specs live in e2e/ and must not be picked up by Vitest.
    exclude: ['e2e/**', 'node_modules/**'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/main.tsx',
        'src/test/**',
        'src/**/*.test.{ts,tsx}',
        'src/vite-env.d.ts',
        'src/api/types.ts',
      ],
      thresholds: {
        statements: 80,
        branches: 75,
        functions: 80,
        lines: 80,
      },
    },
  },
})
