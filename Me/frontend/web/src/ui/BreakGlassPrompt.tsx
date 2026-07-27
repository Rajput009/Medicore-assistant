/**
 * Break-glass prompt.
 *
 * Shown only when the server has refused on ward/department scope *and*
 * signalled that a justified override would be accepted. Deliberately not a
 * one-click button: the clinician types why, that reason is recorded in the
 * audit trail, and the wording says so plainly before they commit.
 */

import React, { useState } from 'react'

import { Alert } from './components'

/** Matches the server's minimum; a token reason is rejected there anyway. */
export const MIN_REASON_LENGTH = 10

export function isReasonAcceptable(reason: string): boolean {
  return reason.trim().length >= MIN_REASON_LENGTH
}

export const BreakGlassPrompt: React.FC<{
  /** What the caller was refused, e.g. "ward ICU". */
  scope: string
  onConfirm: (reason: string) => void
  onCancel: () => void
  busy?: boolean
}> = ({ scope, onConfirm, onCancel, busy = false }) => {
  const [reason, setReason] = useState('')
  const ready = isReasonAcceptable(reason)

  return (
    <div
      className="alert warn"
      role="alertdialog"
      aria-label="Break-glass emergency access"
      style={{ marginTop: 12 }}
    >
      <div className="stack" style={{ gap: 8 }}>
        <strong>Emergency access to {scope}?</strong>
        <span>
          You are not assigned to {scope}. Break-glass access is recorded in the audit
          trail with your name and reason, and is reviewed afterwards. Use it only for
          genuine clinical need.
        </span>

        <label htmlFor="break-glass-reason" className="field-label">
          Reason for access
        </label>
        <textarea
          id="break-glass-reason"
          rows={3}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="e.g. Cardiac arrest call, responding as on-call registrar"
          aria-describedby="break-glass-hint"
        />
        <span id="break-glass-hint" className="muted" style={{ fontSize: '0.8rem' }}>
          At least {MIN_REASON_LENGTH} characters. Be specific — &quot;emergency&quot; alone
          is not a reason anyone can review.
        </span>

        {reason.trim().length > 0 && !ready && (
          <Alert kind="warn">Please give a more specific reason.</Alert>
        )}

        <div className="row">
          <button
            type="button"
            className="danger"
            disabled={!ready || busy}
            onClick={() => onConfirm(reason.trim())}
          >
            {busy ? 'Opening…' : 'Break glass and continue'}
          </button>
          <button type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
