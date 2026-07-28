/**
 * Grounded chart Q&A panel.
 *
 * The presentation rules exist because an assistant that *looks* confident is
 * more dangerous than one that looks uncertain:
 *
 *  - **Citations are always visible**, never behind a hover or an expander. A
 *    clinician must be able to see what a claim rests on without asking for it.
 *  - **Caveats render even when there are findings.** "Allergy list failed to
 *    load" alongside three medication findings is exactly the case where a
 *    partial answer could mislead.
 *  - **A retrieval failure is styled as an error, not a neutral note**, so it
 *    cannot be skimmed past as "nothing found".
 *  - **Critical findings are badged and sorted first**, so an allergy is not
 *    the row that scrolls off.
 */

import React, { useState } from 'react'

import { api } from '../api/client'
import type { AssistAnswer, AssistFinding } from '../api/types'
import { useAsyncAction } from '../hooks/useAsync'
import { Alert, Badge, Spinner } from '../ui/components'

/** Questions that demonstrate the supported shape without over-promising. */
export const EXAMPLE_QUESTIONS = [
  'What allergies are recorded?',
  'What was the last potassium?',
  'Current medications?',
] as const

/** True when a caveat reports a failed lookup rather than an absence. */
export function isFailureCaveat(caveat: string): boolean {
  return /could not be retrieved|not a statement/i.test(caveat)
}

const CitationList: React.FC<{ finding: AssistFinding }> = ({ finding }) => (
  <ul className="assist-citations">
    {finding.citations.map((citation, i) => (
      <li key={`${citation.resource_id}-${i}`}>
        <span className="mono">
          {citation.resource_type}/{citation.resource_id}
        </span>
        {citation.recorded ? (
          <span className="muted"> · {new Date(citation.recorded).toLocaleString()}</span>
        ) : null}
      </li>
    ))}
  </ul>
)

export const ChartAssistant: React.FC<{ patientId: string }> = ({ patientId }) => {
  const [question, setQuestion] = useState('')

  const ask = useAsyncAction<[string], AssistAnswer>((signal, q) =>
    api.assistAsk(patientId, q, null, signal),
  )

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const q = question.trim()
    if (!q) return
    await ask.run(q)
  }

  const answer = ask.state.status === 'success' ? ask.state.data : null

  return (
    <section className="chart-section">
      <h3>Ask about this chart</h3>
      <p className="muted" style={{ marginTop: 0, fontSize: '0.8rem' }}>
        Answers are read from this patient&rsquo;s recorded data and cite their source.
        No advice, no changes to the record.
      </p>

      <form onSubmit={onSubmit} className="stack" style={{ gap: 8 }}>
        <label htmlFor="assist-question" className="visually-hidden">
          Question about this patient&rsquo;s chart
        </label>
        <input
          id="assist-question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. what allergies are recorded?"
          maxLength={300}
        />
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <button type="submit" className="primary" disabled={ask.state.status === 'loading'}>
            {ask.state.status === 'loading' ? (
              <>
                <Spinner label="Asking" /> Asking…
              </>
            ) : (
              'Ask'
            )}
          </button>
          {EXAMPLE_QUESTIONS.map((example) => (
            <button
              key={example}
              type="button"
              className="ghost"
              onClick={() => setQuestion(example)}
            >
              {example}
            </button>
          ))}
        </div>
      </form>

      {ask.state.status === 'error' && (
        <div style={{ marginTop: 8 }}>
          <Alert kind="error">{ask.state.error}</Alert>
        </div>
      )}

      {answer && (
        <div className="stack" style={{ marginTop: 12, gap: 10 }}>
          {answer.findings.map((finding, i) => (
            <div key={i} className="assist-finding">
              <div>
                {finding.critical && <Badge tone="err">critical</Badge>}{' '}
                <span>{finding.text}</span>
              </div>
              <CitationList finding={finding} />
            </div>
          ))}

          {/* Rendered even alongside findings: a partial answer is exactly
              when an unnoticed caveat does harm. */}
          {answer.caveats.map((caveat, i) => (
            <Alert key={i} kind={isFailureCaveat(caveat) ? 'error' : 'info'}>
              {caveat}
            </Alert>
          ))}

          {!answer.answered && answer.caveats.length === 0 && (
            <Alert kind="info">No answer could be produced from the record.</Alert>
          )}

          <p className="muted" style={{ fontSize: '0.75rem', margin: 0 }}>
            {answer.disclaimer}
          </p>
        </div>
      )}
    </section>
  )
}
