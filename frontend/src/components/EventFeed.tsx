// The raw event log, newest first, with one readable line per event. Expand an event for its full data.

import type { SealedEvent } from '../api'
import { describe } from '../activity'

export default function EventFeed({ events, names }: { events: SealedEvent[]; names: Map<string, string> }) {
  return <div className="event-feed">{events.length === 0 ? <p className="muted">Waiting for job events…</p> : [...events].reverse().map(event =>
    <details key={event.id} className="event">
      <summary>
        <span className="event-id">{String(event.id).padStart(2, '0')}</span>
        <strong>{describe(event, names)}</strong>
        <span className={`chip ${event.mode === 'live' ? 'live' : 'sim'}`}>{event.mode}</span>
        <span className="event-meta">{new Date(event.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
      </summary>
      <pre>{JSON.stringify(event.data, null, 2)}</pre>
    </details>)}</div>
}
