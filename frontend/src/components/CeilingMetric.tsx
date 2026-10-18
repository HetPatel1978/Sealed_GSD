// The private ceiling, hidden until the user asks to see it (screen shares, recorded demos).
// The value lives only in React state passed in from App; it is never stored or sent anywhere from here.

import { useState } from 'react'
import { money } from './format'

export default function CeilingMetric({ ceiling, currency }: { ceiling: number | null; currency: string }) {
  const [shown, setShown] = useState(false)
  return <div className="metric">
    <span>Your private ceiling <button type="button" className="text-button reveal" onClick={() => setShown(s => !s)} disabled={ceiling == null}>{shown ? 'Hide' : 'Show'}</button></span>
    <strong>{ceiling == null ? '—' : shown ? money(ceiling, currency) : '••••••'}</strong>
  </div>
}
