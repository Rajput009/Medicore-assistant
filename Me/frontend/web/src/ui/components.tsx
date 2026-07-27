/** Small presentational primitives shared across pages. */

import React from 'react'

export const Card: React.FC<{
  title?: React.ReactNode
  actions?: React.ReactNode
  children: React.ReactNode
  className?: string
}> = ({ title, actions, children, className }) => (
  <section className={`card ${className ?? ''}`.trim()}>
    {(title || actions) && (
      <div className="card-title">
        {typeof title === 'string' ? <h3>{title}</h3> : title}
        {actions}
      </div>
    )}
    {children}
  </section>
)

export const Spinner: React.FC<{ label?: string }> = ({ label = 'Loading' }) => (
  <span className="spinner" role="status" aria-label={label} />
)

/**
 * Announces async results to screen readers. `role="alert"` (assertive) for
 * errors, polite status for everything else.
 */
export const Alert: React.FC<{
  kind: 'error' | 'success' | 'info'
  children: React.ReactNode
}> = ({ kind, children }) => (
  <div className={`alert ${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
    <span>{children}</span>
  </div>
)

export const Badge: React.FC<{
  tone: 'ok' | 'err' | 'warn' | 'neutral'
  children: React.ReactNode
  withDot?: boolean
}> = ({ tone, children, withDot }) => (
  <span className={`badge ${tone}`}>
    {withDot && <span className="dot" aria-hidden="true" />}
    {children}
  </span>
)

export const EmptyState: React.FC<{ message: string; hint?: string }> = ({ message, hint }) => (
  <div className="empty">
    <p style={{ margin: 0, fontWeight: 600 }}>{message}</p>
    {hint && <p style={{ margin: '4px 0 0' }}>{hint}</p>}
  </div>
)

export const SkeletonRows: React.FC<{ rows?: number }> = ({ rows = 3 }) => (
  <div className="stack" aria-hidden="true">
    {Array.from({ length: rows }, (_, i) => (
      <div key={i} className="skeleton" style={{ width: `${100 - i * 12}%` }} />
    ))}
  </div>
)

/** A labelled input wired up for accessible error reporting. */
export const Field: React.FC<{
  id: string
  label: string
  error?: string | null
  children: (props: {
    id: string
    'aria-invalid': boolean
    'aria-describedby': string | undefined
  }) => React.ReactNode
  hint?: string
}> = ({ id, label, error, children, hint }) => {
  const errorId = `${id}-error`
  const hintId = `${id}-hint`
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ')
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children({
        id,
        'aria-invalid': Boolean(error),
        'aria-describedby': describedBy || undefined,
      })}
      {hint && (
        <div id={hintId} className="muted" style={{ fontSize: '0.8rem', marginTop: 4 }}>
          {hint}
        </div>
      )}
      {error && (
        <div id={errorId} className="field-error" role="alert">
          {error}
        </div>
      )}
    </div>
  )
}

export const JsonBlock: React.FC<{ value: unknown; label?: string }> = ({ value, label }) => (
  <pre className="json" aria-label={label}>
    {JSON.stringify(value, null, 2)}
  </pre>
)
