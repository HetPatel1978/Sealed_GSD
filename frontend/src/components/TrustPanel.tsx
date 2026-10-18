// The trust counters (docs/api.md TrustSummary). The ones that should stay at zero are shown as such.

const COUNTERS: { key: string; label: string; zeroIsGood?: boolean }[] = [
  { key: 'budget_revealed_count', label: 'ceiling mentions in any message', zeroIsGood: true },
  { key: 'messages_checked_before', label: 'messages checked before sending' },
  { key: 'messages_checked_after', label: 'sent copies checked after sending' },
  { key: 'false_claims', label: 'unsupported claims', zeroIsGood: true },
  { key: 'commitments_without_approval', label: 'commitments without your approval', zeroIsGood: true },
  { key: 'suspicious_instructions_flagged', label: 'suspicious dealer instructions flagged' },
]

export default function TrustPanel({ trust }: { trust: Record<string, number> | null | undefined }) {
  return <div className="trust-grid">{COUNTERS.map(c => {
    const value = trust?.[c.key] ?? 0
    const tone = c.zeroIsGood ? (value === 0 ? 'ok' : 'bad') : ''
    return <div key={c.key} className={`trust-item ${tone}`}><strong>{value}</strong><span>{c.label}</span></div>
  })}</div>
}
