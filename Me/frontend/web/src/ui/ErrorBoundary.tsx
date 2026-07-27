import React from 'react'

type Props = { children: React.ReactNode }
type State = { error: Error | null }

/**
 * Catches render-time exceptions so a single broken component shows a recovery
 * card instead of unmounting the whole app into a blank white page.
 */
export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    // Replace with a real telemetry sink in production.
    console.error('Unhandled UI error', error, info)
  }

  private reset = () => this.setState({ error: null })

  render(): React.ReactNode {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <main className="login-page">
        <section className="card login-card">
          <h1 style={{ fontSize: '1.15rem' }}>Something went wrong</h1>
          <div className="alert error" role="alert" style={{ marginTop: 12 }}>
            <span>{error.message}</span>
          </div>
          <div className="row" style={{ marginTop: 14 }}>
            <button type="button" className="primary" onClick={this.reset}>
              Try again
            </button>
            <button type="button" onClick={() => window.location.assign('/')}>
              Reload console
            </button>
          </div>
        </section>
      </main>
    )
  }
}
