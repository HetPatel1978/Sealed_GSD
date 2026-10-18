import { describe as group, expect, it } from 'vitest'
import mockEvents from '../../mock/events.json'
import type { SealedEvent } from './api'
import { buildActivity, describe } from './activity'

const events = mockEvents as SealedEvent[]
const activity = buildActivity(events)

group('buildActivity on mock/events.json', () => {
  it('lists every dealer from job.created with its contact details', () => {
    expect(activity.dealers.map(d => d.name)).toEqual(['Northline Motors', 'Harbor Auto Group', 'Summit Auto'])
    expect(activity.dealers.every(d => d.email || d.phone)).toBe(true)
    expect(activity.job?.target).toBe(29500)
  })

  it('puts every message, reply and quote on the right dealer', () => {
    const counted = activity.dealers.flatMap(d => d.items)
    expect(counted.filter(i => i.kind === 'sent')).toHaveLength(events.filter(e => e.type === 'message.sent').length)
    expect(counted.filter(i => i.kind === 'reply')).toHaveLength(events.filter(e => e.type === 'reply.received').length)
    expect(counted.filter(i => i.kind === 'quote')).toHaveLength(events.filter(e => e.type === 'quote.parsed').length)
  })

  it('attaches guard and sent-copy results to each sent message', () => {
    const sent = activity.dealers.flatMap(d => d.items).filter(i => i.kind === 'sent')
    expect(sent.every(m => m.guard_passed !== null)).toBe(true)
    expect(sent.filter(m => m.channel === 'email').every(m => m.audit !== null)).toBe(true)
  })

  it('keeps spoken prices not firm and marks verification', () => {
    const quotes = [...activity.quotes.values()]
    expect(quotes.filter(q => q.source === 'call').every(q => !q.firm)).toBe(true)
    expect(quotes.some(q => q.verification === 'VERIFIED')).toBe(true)
  })

  it('collects review alerts and flags the dealer with a suspicious instruction', () => {
    expect(activity.alerts.map(a => a.reason_code)).toContain('suspicious_instruction')
    expect(activity.dealers.find(d => d.name === 'Summit Auto')?.flagged).toBe(true)
  })

  it('reads the final fee comparison and trust counters', () => {
    expect(activity.fee_comparison?.[0].out_the_door_total).toBe(30450)
    expect(activity.trust?.budget_revealed_count).toBe(0)
  })

  it('never exposes a ceiling field anywhere it builds', () => {
    const seen = JSON.stringify({ ...activity, quotes: [...activity.quotes.values()], replies: [...activity.replies.values()] })
    expect(seen).not.toMatch(/ceil/i)
  })

  it('has a readable line for every event in the mock', () => {
    const names = new Map(activity.dealers.map(d => [d.id, d.name]))
    for (const e of events) expect(describe(e, names)).not.toMatch(/: [a-z]+\.[a-z_]+$/)
  })
})
