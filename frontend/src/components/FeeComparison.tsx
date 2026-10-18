// job.completed.fee_comparison: each dealer's latest firm written quote, itemised, lowest total first.

import type { FeeRow } from '../activity'
import { label, money } from './format'

const itemised = (r: FeeRow) => r.vehicle_price != null || r.taxes != null || r.title_registration != null || r.dealer_fees_total > 0 || r.add_ons_total > 0

export default function FeeComparison({ rows, names, currency }: { rows: FeeRow[]; names: Map<string, string>; currency: string }) {
  if (rows.length === 0) return null
  return <section className="panel">
    <div className="section-title"><h3>Fee comparison</h3><span className="small">Firm written quotes, as each dealer itemised them</span></div>
    <div className="table-scroll"><table><thead><tr>
      <th>Dealer</th><th>Vehicle</th><th>Dealer fees</th><th>Add-ons</th><th>Taxes</th><th>Title &amp; reg.</th><th>Out-the-door</th>
    </tr></thead><tbody>{rows.map(r => <tr key={r.quote_id}>
      <td><strong>{names.get(r.vendor_id) ?? r.vendor_id}</strong>{r.flags.length > 0 && <small className="warning">{r.flags.map(label).join(', ')}</small>}{r.breakdown_flags.length > 0 && <small className="warning">parts don't add up</small>}</td>
      {itemised(r) ? <>
        <td>{money(r.vehicle_price, currency)}</td>
        <td>{money(r.dealer_fees_total, currency)}{r.dealer_fees.map(f => <small key={f.label}>{f.label}</small>)}</td>
        <td>{money(r.add_ons_total, currency)}{r.add_ons.map(a => <small key={a.label}>{a.label}</small>)}</td>
        <td>{money(r.taxes, currency)}</td>
        <td>{money(r.title_registration, currency)}</td>
      </> : <td colSpan={5} className="muted">Total only: this quote wasn't itemised</td>}
      <td><strong>{money(r.out_the_door_total, currency)}</strong></td>
    </tr>)}</tbody></table></div>
  </section>
}
