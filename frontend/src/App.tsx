// Placeholder screen for the project setup: shows the data source and whether the backend answers.
// The real dashboard screens replace this (see frontend/README.md).

import { useEffect, useState } from 'react'
import { API_BASE, DATA_SOURCE } from './config'

type Health = { status: string; version: string; mode: string; fo: string; gmail: string }

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch(`${API_BASE}/health`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setHealth)
      .catch((e: Error) => setError(e.message))
  }, [])

  return (
    <main>
      <h1>Sealed</h1>
      <p>
        Data source: <strong>{DATA_SOURCE}</strong>
      </p>
      <p>
        Backend ({API_BASE}):{' '}
        {health ? (
          <strong>
            {health.status}, v{health.version}, mode {health.mode}
          </strong>
        ) : error ? (
          <strong>not reachable ({error}). Start it with python -m app.run</strong>
        ) : (
          'checking…'
        )}
      </p>
    </main>
  )
}
