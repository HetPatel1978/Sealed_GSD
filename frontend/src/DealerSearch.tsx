import { useEffect, useState } from 'react'
import { request } from './api'

export type DealerSource = {
  kind: 'discovery'; urls: string[]; discovery_id: string; candidate_id: string
}
export type FoundDealer = {
  name: string; website: string; address: string; email: string | null; phone: string | null
  distance_miles: number; distance_method: string; inventory_status: string; matching_listing_url: string | null
  source: DealerSource; flags: string[]
}
type Search = {
  id: string; status: string; progress: string; mode: 'sim' | 'live'; dealers: FoundDealer[]
  dropped: number; cached?: boolean; error?: string | null
}
export type Requirements = { item: string; spec: { quantity: 1; details: string }; zip: string }

export default function DealerSearch({ requirements, mode, selected, count, onSelect }: {
  requirements: Requirements; mode: 'sim' | 'live'; selected: string[]; count: number
  onSelect: (dealer: FoundDealer, checked: boolean) => void
}) {
  const [search, setSearch] = useState<Search | null>(null)
  const [consent, setConsent] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const pending = search !== null && ['queued', 'researching', 'validating'].includes(search.status)

  useEffect(() => {
    if (!search?.id || !pending) return
    let active = true
    const id = search.id
    const timer = window.setInterval(() => {
      request<Search>(`/dealers/discover/${id}`).then(next => {
        if (active) { setSearch(next); setError('') }
      }).catch(() => { if (active) setError('Could not refresh research progress. The saved search has not been restarted.') })
    }, 1500)
    return () => { active = false; window.clearInterval(timer) }
  }, [search?.id, pending])

  async function find() {
    setStarting(true); setError('')
    try {
      // Explicit public subset. Never serialize the intake form or private inputs.
      setSearch(await request<Search>('/dealers/discover', {
        item: requirements.item, spec: requirements.spec, zip: requirements.zip,
        radius_miles: 20, allow_fo_research: consent,
      }))
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : 'Dealer research could not start.')
    } finally { setStarting(false) }
  }

  const valid = requirements.item.trim() && requirements.spec.details.trim() && /^\d{5}$/.test(requirements.zip)
  return <section className="dealer-search" aria-label="Find dealers">
    <div className="section-title"><div><h3>Need help finding sellers?</h3><p className="small">Search within about 20 miles using ZIP-centroid distances.</p></div><button type="button" className="secondary" disabled={!valid || starting || pending || (mode === 'live' && !consent)} onClick={() => void find()}>{starting ? 'Starting research…' : pending ? 'Research in progress…' : 'Find dealers'}</button></div>
    {mode === 'live' ? <label className="check"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} />Use Fo to research public dealership websites for this specification and ZIP. This authorizes research only; no dealer outreach.</label> : <p className="small">Simulation shows synthetic dealers and makes no Fo or website requests.</p>}
    {error && <p role="alert" className="warning">{error}</p>}
    {search && <div className="research-progress" role="status"><strong>{search.status.replaceAll('_', ' ')}</strong><p>{search.progress}</p>{pending && <small>Fo research can take several minutes. You can keep reviewing the form.</small>}</div>}
    {search?.status === 'complete' && <>
      <p className="small">{search.dealers.length} candidates · {search.dropped} excluded by checks. Availability needs confirmation. Select up to five sellers total.</p>
      {search.dealers.length === 0 && <p>No candidates passed the source and distance checks. You can add sellers manually below.</p>}
      <div className="discovery-results">{search.dealers.map(dealer => {
        const id = `${dealer.source.discovery_id}:${dealer.source.candidate_id}`
        const checked = selected.includes(id)
        return <article className="discovery-card" key={id}>
          <label className="check"><input type="checkbox" checked={checked} disabled={!checked && count >= 5} onChange={event => onSelect(dealer, event.target.checked)} /><span><strong>{dealer.name}</strong><span className="small">{search.mode === 'sim' ? 'Synthetic candidate' : 'Published contacts checked'} · about {dealer.distance_miles} miles</span></span></label>
          <p>{dealer.address}</p><p>{dealer.email ?? 'No verified email'}{dealer.phone ? ` · ${dealer.phone}` : ''}</p>
          <p className="small">{dealer.inventory_status === 'listed_in_transit' ? 'Matching listing · in transit · unconfirmed' : dealer.inventory_status === 'listed_unconfirmed' ? 'Matching listing · availability unconfirmed' : 'Exact inventory unknown'}</p>
          <div className="discovery-links"><a href={dealer.website} target="_blank" rel="noopener noreferrer">Website ↗</a>{dealer.matching_listing_url && <a href={dealer.matching_listing_url} target="_blank" rel="noopener noreferrer">Vehicle listing ↗</a>}{dealer.source.urls.slice(0, 3).map((url, i) => <a key={url} href={url} target="_blank" rel="noopener noreferrer">Source {i + 1} ↗</a>)}</div>
        </article>
      })}</div>
    </>}
  </section>
}
