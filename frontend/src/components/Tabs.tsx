// A plain tab bar. Tabs never switch on their own: the user clicks, or follows a link from the next-step card.

export type TabId = 'overview' | 'dealers' | 'activity' | 'receipt'
export type TabSpec = { id: TabId; label: string; count?: number; fresh?: number; alert?: boolean; hidden?: boolean }

export default function Tabs({ tabs, active, onSelect }: { tabs: TabSpec[]; active: TabId; onSelect: (id: TabId) => void }) {
  return <div className="tabs" role="tablist" aria-label="Job details">{tabs.filter(t => !t.hidden).map(t =>
    <button key={t.id} type="button" role="tab" id={`tab-${t.id}`} aria-selected={active === t.id} aria-controls={`panel-${t.id}`}
      className={`tab ${active === t.id ? 'active' : ''}`} onClick={() => onSelect(t.id)}>
      {t.label}
      {t.count != null && <span className="tab-count">{t.count}</span>}
      {t.alert && <span className="tab-dot alert" aria-label="needs review" />}
      {!t.alert && !!t.fresh && active !== t.id && <span className="tab-new">{t.fresh} new</span>}
    </button>)}</div>
}
