// Ceiling entry: visible by default so the user can see what they type, with a formatted preview and a check
// against the target. "Hide" masks it for screen sharing. The value is never stored in the browser.

import { useState } from 'react'
import { money } from './format'

export default function CeilingInput({ name, value, onChange, target, currency, label = 'Private ceiling' }: {
  name?: string; value: string; onChange: (value: string) => void; target?: number | null; currency: string; label?: string
}) {
  const [hidden, setHidden] = useState(false)
  const amount = value.trim() === '' ? null : Number(value)
  const valid = amount != null && Number.isFinite(amount) && amount > 0
  const belowTarget = valid && target != null && Number.isFinite(target) && amount <= target
  return <label className="ceiling-input">
    <span className="label-row">{label}<button type="button" className="text-button reveal" onClick={() => setHidden(h => !h)}>{hidden ? 'Show' : 'Hide'}</button></span>
    <input name={name} type={hidden ? 'password' : 'text'} inputMode="decimal" autoComplete="off" spellCheck={false}
      placeholder="Most you would pay, e.g. 32000" value={value} onChange={e => onChange(e.target.value.replace(/[^\d.]/g, ''))}
      aria-invalid={belowTarget || (value !== '' && !valid)} required />
    <small className={belowTarget || (value !== '' && !valid) ? 'warning' : ''}>
      {value === '' ? 'Only you see this. Never sent to Fo, dealers or any AI.'
        : !valid ? 'Enter a number.'
        : belowTarget ? `Must be higher than your target (${money(target, currency)}).`
        : hidden ? 'Hidden on screen.' : `${money(amount, currency)} · only you see this`}
    </small>
  </label>
}
