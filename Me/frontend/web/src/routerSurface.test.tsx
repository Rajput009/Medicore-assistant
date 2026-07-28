/**
 * Guard for the react-router audit exception (GHSA-qwww-vcr4-c8h2).
 *
 * `scripts/audit_node.sh` suppresses that advisory because the CSRF bypass
 * lives in React Router's RSC / data-router action pipeline, and this SPA
 * uses declarative routing only. That reasoning stops being true the moment
 * someone adopts `createBrowserRouter` or a route `action` — and because the
 * advisory is suppressed, nobody would be told.
 *
 * These tests fail if the router surface changes, forcing the exception to be
 * revisited rather than silently outlived.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Vitest runs with the package root as cwd, so resolve from there rather than
// from import.meta.url (which points into the transformed module graph).
const SRC = resolve(process.cwd(), 'src')

function sourceFiles(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      sourceFiles(full, acc)
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      acc.push(full)
    }
  }
  return acc
}

const appSources = sourceFiles(SRC).map((path) => ({
  path,
  text: readFileSync(path, 'utf8'),
}))

describe('router surface (audit exception guard)', () => {
  it('finds application sources to inspect', () => {
    // A silently-empty scan would make every assertion below vacuous.
    expect(appSources.length).toBeGreaterThan(5)
  })

  it.each([
    'createBrowserRouter',
    'createMemoryRouter',
    'createHashRouter',
    'RouterProvider',
  ])('does not use the data router API %s', (api) => {
    const offenders = appSources.filter((f) => f.text.includes(api))
    expect(
      offenders.map((f) => f.path),
      `${api} builds a data router, which activates the action pipeline that ` +
        'GHSA-qwww-vcr4-c8h2 affects. Withdraw the exception in ' +
        'scripts/audit_node.sh before adopting it.',
    ).toEqual([])
  })

  it('declares no route actions or loaders', () => {
    // Route-level action/loader are the entry points to the vulnerable path.
    const pattern = /\b(action|loader)\s*:/
    const offenders = appSources
      .filter((f) => f.text.includes('react-router'))
      .filter((f) => pattern.test(f.text))
    expect(offenders.map((f) => f.path)).toEqual([])
  })

  it('still uses the declarative router the exception assumes', () => {
    const all = appSources.map((f) => f.text).join('\n')
    expect(all).toContain('BrowserRouter')
    expect(all).toContain('<Routes>')
  })
})
