/**
 * Recently viewed patients (local, non-PHI-sensitive ids only).
 *
 * Stores only patient identifiers and last-viewed timestamps in sessionStorage
 * so the list dies with the tab. Never stores clinical payloads.
 */

const KEY = 'medicore.recent.patients'
const MAX = 12

export type RecentPatient = {
  id: string
  viewedAt: number
}

export function loadRecentPatients(): RecentPatient[] {
  try {
    const raw = sessionStorage.getItem(KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as RecentPatient[]
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((p) => p && typeof p.id === 'string' && typeof p.viewedAt === 'number')
      .slice(0, MAX)
  } catch {
    return []
  }
}

export function rememberPatient(id: string, now = Date.now()): RecentPatient[] {
  const cleaned = id.trim()
  if (!cleaned) return loadRecentPatients()
  const prev = loadRecentPatients().filter((p) => p.id !== cleaned)
  const next = [{ id: cleaned, viewedAt: now }, ...prev].slice(0, MAX)
  try {
    sessionStorage.setItem(KEY, JSON.stringify(next))
  } catch {
    /* private mode */
  }
  return next
}

export function clearRecentPatients(): void {
  try {
    sessionStorage.removeItem(KEY)
  } catch {
    /* ignore */
  }
}
