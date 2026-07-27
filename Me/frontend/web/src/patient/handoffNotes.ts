/**
 * Lightweight SBAR handoff notes (tab-scoped).
 *
 * Stores free-text drafts keyed by patient id in sessionStorage only.
 * Not a clinical record of truth — clinicians must copy into the EHR.
 */

const KEY = 'medicore.handoff.notes'
const MAX_NOTES = 40
const MAX_LEN = 4000

export type HandoffNote = {
  patientId: string
  text: string
  updatedAt: number
  author?: string
}

type Store = Record<string, HandoffNote>

function readStore(): Store {
  try {
    const raw = sessionStorage.getItem(KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Store
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeStore(store: Store): void {
  try {
    const entries = Object.entries(store)
      .sort((a, b) => b[1].updatedAt - a[1].updatedAt)
      .slice(0, MAX_NOTES)
    sessionStorage.setItem(KEY, JSON.stringify(Object.fromEntries(entries)))
  } catch {
    /* private mode */
  }
}

export function loadHandoff(patientId: string): HandoffNote | null {
  const id = patientId.trim()
  if (!id) return null
  return readStore()[id] ?? null
}

export function saveHandoff(
  patientId: string,
  text: string,
  author?: string,
  now = Date.now(),
): HandoffNote {
  const id = patientId.trim()
  const cleaned = text.slice(0, MAX_LEN)
  const note: HandoffNote = {
    patientId: id,
    text: cleaned,
    updatedAt: now,
    author,
  }
  const store = readStore()
  store[id] = note
  writeStore(store)
  return note
}

export function clearHandoff(patientId: string): void {
  const store = readStore()
  delete store[patientId.trim()]
  writeStore(store)
}

/** Default SBAR scaffold for a new note. */
export function sbarTemplate(patientId: string): string {
  return [
    `S — Situation: Patient ${patientId}`,
    'B — Background:',
    'A — Assessment:',
    'R — Recommendation:',
  ].join('\n')
}
