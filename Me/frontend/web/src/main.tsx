import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { AuthProvider } from './auth/AuthContext'
import './styles.css'
import { App } from './ui/App'
import { ErrorBoundary } from './ui/ErrorBoundary'

const container = document.getElementById('root')
if (!container) throw new Error('Root element #root not found')

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <ErrorBoundary>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </ErrorBoundary>
  </React.StrictMode>,
)
