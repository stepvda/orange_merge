/* Taxonomy watchlist — terms the model keeps reaching for outside the closed
 * vocabulary, for a curator to approve into it or reject. Ported from the
 * Orange_use_case team's watchlist/proposal/review loop (CLI-only there). */
import { Fragment, useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { WatchItem, WatchList } from '../types'

type Filter = 'proposed' | 'pending' | 'approved' | 'rejected'

const DIM_LABEL: Record<string, string> = { use_cases: 'Use case', technologies: 'Technology', verticals: 'Vertical' }

export default function TaxonomyWatch() {
  const [filter, setFilter] = useState<Filter>('proposed')
  const [data, setData] = useState<WatchList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<number | null>(null)

  const load = useCallback(() => {
    api.watchlist(filter).then(setData).catch((e) => setError(String(e.message ?? e)))
  }, [filter])
  useEffect(load, [load])

  const decide = async (item: WatchItem, decision: 'approve' | 'reject' | 'reopen', extra: Record<string, unknown> = {}) => {
    try {
      setError(null)
      await api.watchDecision(item.id, { decision, ...extra })
      setEditing(null)
      load()
    } catch (e: any) { setError(String(e.message ?? e)) }
  }

  return (
    <div className="panel taxonomy-watch">
      <div className="panel-head">
        <h2>Taxonomy watchlist</h2>
        <span className="sub">
          Use cases and technologies the model proposed outside the closed vocabulary. A term seen
          {' '}{data?.threshold ?? 2}+ times is <b>proposed</b>; approving it adds it to
          {' '}<code>{data?.extensions_file ?? 'config/taxonomy/extensions.yaml'}</code> and it is valid from the next generation run.
        </span>
      </div>
      <div className="panel-body">
        <div className="seg" role="tablist" aria-label="Watchlist status">
          {(['proposed', 'pending', 'approved', 'rejected'] as Filter[]).map((f) => (
            <button key={f} role="tab" aria-selected={filter === f} className="seg-btn" onClick={() => setFilter(f)}>
              {f[0].toUpperCase() + f.slice(1)} <small>{data?.counts?.[f] ?? 0}</small>
            </button>
          ))}
        </div>
        {error && <div className="form-error" role="alert">{error}</div>}
        {data && data.items.length === 0 && <div className="empty">Nothing here.</div>}
        {data && data.items.length > 0 && (
          <table className="data-table">
            <thead><tr><th>Term</th><th>Dimension</th><th className="num">Seen</th><th>Origin</th><th>Last seen</th><th></th></tr></thead>
            <tbody>
              {data.items.map((item) => (
                <Fragment key={item.id}>
                  <tr>
                    <td><b>{item.term}</b>
                      {item.examples.length > 0 && (
                        <details><summary>{item.examples.length} example(s)</summary>
                          <ul className="examples">{item.examples.map((ex, i) => (
                            <li key={i}>{ex.statement || <i>no statement</i>}{ex.vertical && <small> — {ex.vertical}</small>}</li>))}
                          </ul></details>)}
                      {item.status === 'approved' && <div className="muted">added as <code>{item.approved_id}</code> by {item.decided_by}</div>}
                      {item.status === 'rejected' && <div className="muted">rejected by {item.decided_by}{item.note ? ` — ${item.note}` : ''}</div>}
                    </td>
                    <td>{DIM_LABEL[item.dimension] ?? item.dimension}</td>
                    <td className="num">{item.frequency}</td>
                    <td className="muted">{item.origin}</td>
                    <td className="muted">{item.last_seen.slice(0, 10)}</td>
                    <td className="actions">
                      {item.status === 'pending' && item.can_approve && (
                        <button className="btn" onClick={() => setEditing(editing === item.id ? null : item.id)}>Approve…</button>)}
                      {item.status === 'pending' && (
                        <button className="btn btn-ghost" onClick={() => decide(item, 'reject')}>Reject</button>)}
                      {item.status === 'rejected' && (
                        <button className="btn btn-ghost" onClick={() => decide(item, 'reopen')}>Reopen</button>)}
                    </td>
                  </tr>
                  {editing === item.id && data && (
                    <tr className="approve-row">
                      <td colSpan={6}>
                        <ApproveForm item={item} domains={data.domains}
                                     onCancel={() => setEditing(null)}
                                     onApprove={(extra) => decide(item, 'approve', extra)} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function ApproveForm({ item, domains, onApprove, onCancel }: {
  item: WatchItem
  domains: { id: string; label: string }[]
  onApprove: (extra: Record<string, unknown>) => void
  onCancel: () => void
}) {
  const [label, setLabel] = useState(item.term)
  const [definition, setDefinition] = useState('')
  const [synonyms, setSynonyms] = useState('')
  const [chosen, setChosen] = useState<string[]>([])
  const needsDomain = item.dimension === 'use_cases'
  return (
    <form className="approve-form" onSubmit={(e) => {
      e.preventDefault()
      onApprove({ label, definition, domains: chosen,
                  synonyms: synonyms.split(',').map((s) => s.trim()).filter(Boolean) })
    }}>
      <label>Label <input value={label} onChange={(e) => setLabel(e.target.value)} required maxLength={120} /></label>
      <label>Definition <textarea value={definition} onChange={(e) => setDefinition(e.target.value)} maxLength={600}
                                  placeholder="One sentence the model will be shown, including what it is NOT." /></label>
      <label>Synonyms <input value={synonyms} onChange={(e) => setSynonyms(e.target.value)}
                             placeholder="comma-separated" /></label>
      <fieldset><legend>Business domains{needsDomain ? ' (at least one)' : ''}</legend>
        {domains.map((d) => (
          <label key={d.id} className="check">
            <input type="checkbox" checked={chosen.includes(d.id)}
                   onChange={(e) => setChosen(e.target.checked ? [...chosen, d.id] : chosen.filter((x) => x !== d.id))} />
            {d.label}
          </label>
        ))}
      </fieldset>
      <div className="form-actions">
        <button className="btn btn-primary" type="submit" disabled={needsDomain && chosen.length === 0}>Add to vocabulary</button>
        <button className="btn btn-ghost" type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  )
}
