/**
 * Disposition prompt shown when a clinician completes a triage entry.
 *
 * Completing used to be a single click. It now asks what happened, because
 * "removed from the list" could not distinguish an admission from a patient
 * who walked out unseen — and the second is what a department is judged on.
 *
 * Design rules, each one a deliberate trade against convenience:
 *
 *  - **No pre-selected disposition.** A default would be recorded by anyone
 *    clicking through quickly, and a plausible-looking wrong answer is worse
 *    for the dataset than a moment's friction.
 *  - **The note requirement is enforced in the UI as well as the API.** The
 *    server rejects a missing note anyway; catching it here means the
 *    clinician is told before losing their place, not after.
 *  - **Cancel leaves the patient in the queue.** Closing the dialog must
 *    never be a silent completion.
 */

import React, { useState } from 'react'

import type { Disposition } from '../api/types'
import {
  DISPOSITIONS,
  DISPOSITIONS_REQUIRING_NOTE,
  DISPOSITION_LABELS,
} from '../api/types'
import { Alert } from '../ui/components'

export function noteRequired(disposition: Disposition | ''): boolean {
  return Boolean(disposition) && DISPOSITIONS_REQUIRING_NOTE.includes(disposition as Disposition)
}

/** The reason a submission is blocked, or null when it may proceed. */
export function validateDisposition(
  disposition: Disposition | '',
  note: string,
): string | null {
  if (!disposition) return 'Select what happened to this patient.'
  if (noteRequired(disposition) && !note.trim()) {
    return `A note is required for "${DISPOSITION_LABELS[disposition]}".`
  }
  return null
}

export const DispositionDialog: React.FC<{
  patientId: string
  busy?: boolean
  onCancel: () => void
  onConfirm: (disposition: Disposition, note: string | null) => void
}> = ({ patientId, busy, onCancel, onConfirm }) => {
  const [disposition, setDisposition] = useState<Disposition | ''>('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const problem = validateDisposition(disposition, note)
    if (problem) {
      setError(problem)
      return
    }
    onConfirm(disposition as Disposition, note.trim() || null)
  }

  return (
    <div
      className="alert info"
      role="alertdialog"
      aria-label={`Complete ${patientId}`}
      style={{ marginTop: 8 }}
    >
      <form className="stack" style={{ gap: 8, width: '100%' }} onSubmit={submit}>
        <strong>What happened to {patientId}?</strong>

        <div className="field">
          <label htmlFor={`disposition-${patientId}`}>Disposition</label>
          <select
            id={`disposition-${patientId}`}
            value={disposition}
            onChange={(e) => {
              setDisposition(e.target.value as Disposition | '')
              setError(null)
            }}
          >
            {/* Deliberately unselected: no default outcome. */}
            <option value="">Select…</option>
            {DISPOSITIONS.map((d) => (
              <option key={d} value={d}>
                {DISPOSITION_LABELS[d]}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor={`disposition-note-${patientId}`}>
            Note{noteRequired(disposition) ? ' (required)' : ' (optional)'}
          </label>
          <input
            id={`disposition-note-${patientId}`}
            value={note}
            maxLength={500}
            onChange={(e) => {
              setNote(e.target.value)
              setError(null)
            }}
            placeholder={
              noteRequired(disposition)
                ? 'Briefly, what happened?'
                : 'Anything the next clinician should know'
            }
          />
        </div>

        {error && <Alert kind="error">{error}</Alert>}

        <div className="row" style={{ gap: 8 }}>
          <button type="submit" className="primary" disabled={busy}>
            {busy ? 'Saving…' : 'Complete'}
          </button>
          <button type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        </div>
      </form>
    </div>
  )
}
