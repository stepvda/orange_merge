/* Explore — three best-of-breed views on one tab (docs/best_of_selection.md).
 *
 *   What-if    the scoring simulator from innovation_radar_orange: re-weight the
 *              five attractiveness components and watch the ranking move. The
 *              stored score is never changed; this is a lens, not a write.
 *   Compare    up to three spaces side by side on spider charts of their score
 *              components (innovation_radar_orange's overlaid polar compare).
 *   Landscape  every space placed by the language of its evidence — PCA of the
 *              centroid of its signals' embeddings (the topic data-map idea from
 *              innovation_radar_orange, computed live instead of a static PNG).
 */
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { ExploreScores, ExploreTopic, Landscape } from '../types'
import { CAT, Kpi } from './Charts'

type Sub = 'whatif' | 'compare' | 'landscape'

const COMPONENT_LABELS: Record<string, string> = {
  market_signal_strength: 'Market signal strength',
  source_diversity: 'Source diversity',
  evidence_quality: 'Evidence quality',
  novelty_momentum: 'Novelty & momentum',
  strategic_relevance: 'Strategic relevance',
  offer_match: 'Offer match',
  reference_density: 'Reference density',
  partner_coverage: 'Partner coverage',
  compliance_fit: 'Compliance fit',
  capability_depth: 'Capability depth',
  external_validation: 'External validation',
  technology_ownership: 'Technology ownership',
}
const label = (k: string) => COMPONENT_LABELS[k] ?? k.replace(/_/g, ' ')
const title = (t: ExploreTopic) => `${t.labels.use_case} · ${t.labels.technology} · ${t.labels.vertical}`

export default function Explore({ selectedId, onSelect }: {
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const [sub, setSub] = useState<Sub>('whatif')
  const [scores, setScores] = useState<ExploreScores | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.exploreScores().then(setScores).catch((e) => setError(String(e.message ?? e)))
  }, [])

  return (
    <div className="panel explore">
      <div className="panel-head">
        <h2>Explore</h2>
        <div className="seg" role="tablist" aria-label="Explore view">
          {([['whatif', 'What-if weights'], ['compare', 'Compare'], ['landscape', 'Landscape map']] as [Sub, string][])
            .map(([id, text]) => (
              <button key={id} role="tab" aria-selected={sub === id} className="seg-btn"
                      onClick={() => setSub(id)}>{text}</button>
            ))}
        </div>
      </div>
      <div className="panel-body">
        {error && <div className="empty">Could not load: {error}</div>}
        {!error && !scores && <div className="empty">Loading…</div>}
        {scores && sub === 'whatif' && <WhatIf data={scores} onSelect={onSelect} />}
        {scores && sub === 'compare' && <Compare data={scores} selectedId={selectedId} onSelect={onSelect} />}
        {sub === 'landscape' && <LandscapeMap onSelect={onSelect} selectedId={selectedId} />}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ What-if */

function WhatIf({ data, onSelect }: { data: ExploreScores; onSelect: (id: string) => void }) {
  const keys = Object.keys(data.attractiveness_weights)
  const stored = useMemo(() => Object.fromEntries(
    keys.map((k) => [k, Math.round(data.attractiveness_weights[k] * 100)])), [data]) // eslint-disable-line
  const [weights, setWeights] = useState<Record<string, number>>(stored)
  const [showAll, setShowAll] = useState(false)

  const rows = useMemo(() => {
    const sum = keys.reduce((s, k) => s + (weights[k] ?? 0), 0)
    const norm = (k: string) => (sum > 0 ? (weights[k] ?? 0) / sum : 1 / keys.length)
    const scored = data.topics.map((t) => {
      const adjusted = Math.max(0, Math.min(100,
        keys.reduce((s, k) => s + norm(k) * (t.attractiveness_components[k] ?? 0), 0)))
      return { t, stored: t.attractiveness ?? 0, adjusted: Math.round(adjusted * 100) / 100 }
    })
    const byStored = [...scored].sort((a, b) => b.stored - a.stored)
    const storedRank = new Map(byStored.map((r, i) => [r.t.id, i + 1]))
    const byAdjusted = [...scored].sort((a, b) => b.adjusted - a.adjusted)
    return byAdjusted.map((r, i) => ({ ...r, rank: i + 1, storedRank: storedRank.get(r.t.id)!, }))
  }, [data, weights]) // eslint-disable-line

  const moved = rows.filter((r) => r.rank !== r.storedRank)
  const up = rows.filter((r) => r.rank < r.storedRank).length
  const down = rows.filter((r) => r.rank > r.storedRank).length
  const avgShift = rows.length ? rows.reduce((s, r) => s + Math.abs(r.rank - r.storedRank), 0) / rows.length : 0
  const top10Kept = rows.slice(0, 10).filter((r) => r.storedRank <= 10).length
  const sum = keys.reduce((s, k) => s + (weights[k] ?? 0), 0)
  const changed = keys.some((k) => weights[k] !== stored[k])

  return (
    <div className="whatif">
      <p className="explore-note">
        Re-weight the five attractiveness components and see how the ranking would move. Weights are
        normalised to sum to 100%. The stored scores (weight set <code>{data.weight_set}</code>) are never
        changed — a weight change for real needs a new weight set in <code>config/settings.yaml</code>.
      </p>
      <div className="whatif-grid">
        <div className="whatif-sliders">
          {keys.map((k) => (
            <label key={k} className="slider-row">
              <span className="slider-label">{label(k)}</span>
              <input type="range" min={0} max={100} step={5} value={weights[k]}
                     onChange={(e) => setWeights({ ...weights, [k]: Number(e.target.value) })}
                     aria-label={`${label(k)} weight`} />
              <span className="slider-value">
                {sum > 0 ? Math.round((weights[k] / sum) * 100) : 20}%
                {weights[k] !== stored[k] && <small> (was {stored[k]}%)</small>}
              </span>
            </label>
          ))}
          <div className="slider-actions">
            <button className="btn" disabled={!changed} onClick={() => setWeights(stored)}>Reset to stored weights</button>
            {keys.map((k) => (
              <button key={k} className="btn btn-ghost" title={`Only ${label(k)}`}
                      onClick={() => setWeights(Object.fromEntries(keys.map((x) => [x, x === k ? 100 : 0])))}>
                Only {label(k).split(' ')[0].toLowerCase()}
              </button>
            ))}
          </div>
        </div>
        <div className="whatif-side">
          <div className="kpi-row">
            <Kpi value={up} label="Moved up" />
            <Kpi value={down} label="Moved down" />
            <Kpi value={avgShift.toFixed(1)} label="Avg. rank shift" />
            <Kpi value={`${top10Kept}/10`} label="Top 10 kept" />
          </div>
          <Scatter rows={rows} onSelect={onSelect} />
        </div>
      </div>

      <table className="data-table">
        <thead>
          <tr><th>#</th><th>Move</th><th>Opportunity space</th><th className="num">Stored</th>
            <th className="num">What-if</th><th className="num">Δ</th></tr>
        </thead>
        <tbody>
          {(showAll ? rows : rows.slice(0, 25)).map((r) => {
            const delta = r.storedRank - r.rank
            return (
              <tr key={r.t.id} onClick={() => onSelect(r.t.id)} className="clickable">
                <td>{r.rank}</td>
                <td className={delta > 0 ? 'up' : delta < 0 ? 'down' : 'same'}>
                  {delta > 0 ? `▲ ${delta}` : delta < 0 ? `▼ ${-delta}` : '='}
                  <small> (was {r.storedRank})</small>
                </td>
                <td><span className="mono">{r.t.id}</span> {title(r.t)}</td>
                <td className="num">{r.stored.toFixed(1)}</td>
                <td className="num"><b>{r.adjusted.toFixed(1)}</b></td>
                <td className="num">{(r.adjusted - r.stored) >= 0 ? '+' : ''}{(r.adjusted - r.stored).toFixed(1)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {rows.length > 25 && (
        <button className="btn btn-ghost" onClick={() => setShowAll(!showAll)}>
          {showAll ? 'Show top 25' : `Show all ${rows.length} (${moved.length} moved)`}
        </button>
      )}
    </div>
  )
}

function Scatter({ rows, onSelect }: {
  rows: { t: ExploreTopic; stored: number; adjusted: number; rank: number; storedRank: number }[]
  onSelect: (id: string) => void
}) {
  const W = 320, H = 260, P = 32
  const lo = Math.max(0, Math.floor(Math.min(...rows.map((r) => Math.min(r.stored, r.adjusted)), 100) / 10) * 10)
  const hi = Math.min(100, Math.ceil(Math.max(...rows.map((r) => Math.max(r.stored, r.adjusted)), 0) / 10) * 10)
  const sx = (v: number) => P + ((v - lo) / Math.max(1, hi - lo)) * (W - P - 8)
  const sy = (v: number) => H - P - ((v - lo) / Math.max(1, hi - lo)) * (H - P - 8)
  return (
    <figure className="scatter">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Stored score against what-if score">
        <line x1={sx(lo)} y1={sy(lo)} x2={sx(hi)} y2={sy(hi)} className="diag" />
        <line x1={P} y1={H - P} x2={W - 8} y2={H - P} className="axis" />
        <line x1={P} y1={8} x2={P} y2={H - P} className="axis" />
        <text x={(W + P) / 2} y={H - 6} className="axis-label" textAnchor="middle">Stored attractiveness</text>
        <text x={10} y={(H - P) / 2} className="axis-label" textAnchor="middle"
              transform={`rotate(-90 10 ${(H - P) / 2})`}>What-if</text>
        <text x={P - 4} y={H - P + 12} className="tick" textAnchor="end">{lo}</text>
        <text x={W - 8} y={H - P + 12} className="tick" textAnchor="end">{hi}</text>
        {rows.map((r) => (
          <circle key={r.t.id} cx={sx(r.stored)} cy={sy(r.adjusted)} r={3.2}
                  className={r.adjusted > r.stored + 0.05 ? 'pt up' : r.adjusted < r.stored - 0.05 ? 'pt down' : 'pt'}
                  onClick={() => onSelect(r.t.id)}>
            <title>{`${r.t.id} ${title(r.t)}\nstored ${r.stored.toFixed(1)} → what-if ${r.adjusted.toFixed(1)}`}</title>
          </circle>
        ))}
      </svg>
      <figcaption>Above the diagonal: gains under your weights. Below: loses.</figcaption>
    </figure>
  )
}

/* ------------------------------------------------------------------ Compare */

function Compare({ data, selectedId, onSelect }: {
  data: ExploreScores; selectedId: string | null; onSelect: (id: string) => void
}) {
  const byId = useMemo(() => new Map(data.topics.map((t) => [t.id, t])), [data])
  const initial = useMemo(() => {
    const top = [...data.topics].sort((a, b) => (b.attractiveness ?? 0) - (a.attractiveness ?? 0))
    const picks = selectedId && byId.has(selectedId) ? [selectedId] : []
    for (const t of top) { if (picks.length >= 2) break; if (!picks.includes(t.id)) picks.push(t.id) }
    return picks
  }, [data]) // eslint-disable-line
  const [ids, setIds] = useState<string[]>(initial)
  const [query, setQuery] = useState('')
  const chosen = ids.map((i) => byId.get(i)).filter(Boolean) as ExploreTopic[]

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return []
    return data.topics.filter((t) => !ids.includes(t.id)
      && `${t.id} ${title(t)} ${t.statement}`.toLowerCase().includes(q)).slice(0, 8)
  }, [query, data, ids])

  return (
    <div className="compare">
      <p className="explore-note">Pick up to three spaces. Attractiveness and right to win stay on separate charts —
        the radar never folds them into one number.</p>
      <div className="compare-pick">
        {chosen.map((t, i) => (
          <span key={t.id} className="chip" style={{ borderColor: CAT[i] }}>
            <span className="swatch" style={{ background: CAT[i] }} />
            <span className="mono">{t.id}</span> {t.labels.use_case}
            <button aria-label={`Remove ${t.id}`} onClick={() => setIds(ids.filter((x) => x !== t.id))}>×</button>
          </span>
        ))}
        {ids.length < 3 && (
          <span className="compare-search">
            <input placeholder="Add a space — search id, use case, technology…" value={query}
                   onChange={(e) => setQuery(e.target.value)} aria-label="Search spaces to compare" />
            {matches.length > 0 && (
              <ul className="compare-results">
                {matches.map((t) => (
                  <li key={t.id}><button onClick={() => { setIds([...ids, t.id]); setQuery('') }}>
                    <span className="mono">{t.id}</span> {title(t)}</button></li>
                ))}
              </ul>
            )}
          </span>
        )}
      </div>
      {chosen.length === 0 ? <div className="empty">Add a space to compare.</div> : (
        <>
          <div className="compare-charts">
            <Spider title="Attractiveness components" topics={chosen}
                    keys={Object.keys(data.attractiveness_weights)} pick={(t) => t.attractiveness_components} />
            <Spider title="Right-to-win components" topics={chosen}
                    keys={Object.keys(data.right_to_win_weights)} pick={(t) => t.right_to_win_components} />
          </div>
          <table className="data-table">
            <thead><tr><th></th>{chosen.map((t, i) => (
              <th key={t.id}><span className="swatch" style={{ background: CAT[i] }} />
                <button className="link" onClick={() => onSelect(t.id)}>{t.id}</button></th>))}</tr></thead>
            <tbody>
              <Row name="Statement" cells={chosen.map((t) => t.statement)} />
              <Row name="Vertical" cells={chosen.map((t) => t.labels.vertical)} />
              <Row name="Use case" cells={chosen.map((t) => t.labels.use_case)} />
              <Row name="Technology" cells={chosen.map((t) => t.labels.technology)} />
              <Row name="Domain" cells={chosen.map((t) => t.domain_labels.join(', '))} />
              <Row name="State · horizon" cells={chosen.map((t) => `${t.state} · ${t.horizon ?? '—'}`)} />
              <Row name="Attractiveness" cells={chosen.map((t) => t.attractiveness?.toFixed(1) ?? '—')} best={chosen.map((t) => t.attractiveness ?? -1)} />
              <Row name="Right to win" cells={chosen.map((t) => t.right_to_win?.toFixed(1) ?? '—')} best={chosen.map((t) => t.right_to_win ?? -1)} />
              <Row name="Portfolio distance" cells={chosen.map((t) => t.portfolio_distance != null ? `L${t.portfolio_distance}` : '—')} />
              <Row name="Signals" cells={chosen.map((t) => String(t.signal_count))} best={chosen.map((t) => t.signal_count)} />
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}

function Row({ name, cells, best }: { name: string; cells: string[]; best?: number[] }) {
  const max = best ? Math.max(...best) : null
  return (
    <tr><th scope="row">{name}</th>
      {cells.map((c, i) => <td key={i} className={best && best[i] === max && cells.length > 1 ? 'best' : ''}>{c}</td>)}
    </tr>
  )
}

function Spider({ title: heading, topics, keys, pick }: {
  title: string; topics: ExploreTopic[]; keys: string[]; pick: (t: ExploreTopic) => Record<string, number>
}) {
  const S = 300, C = S / 2, R = 100
  const angle = (i: number) => (Math.PI * 2 * i) / keys.length - Math.PI / 2
  const pt = (i: number, v: number) => [C + Math.cos(angle(i)) * R * v / 100, C + Math.sin(angle(i)) * R * v / 100]
  return (
    <figure className="spider">
      <figcaption>{heading}</figcaption>
      <svg viewBox={`-70 -10 ${S + 140} ${S + 20}`} role="img" aria-label={heading}>
        {[25, 50, 75, 100].map((ring) => (
          <polygon key={ring} className="ring"
                   points={keys.map((_, i) => pt(i, ring).join(',')).join(' ')} />
        ))}
        {keys.map((k, i) => {
          const [x, y] = pt(i, 100); const [lx, ly] = pt(i, 122)
          return (
            <g key={k}>
              <line x1={C} y1={C} x2={x} y2={y} className="spoke" />
              <text x={lx} y={ly} className="spider-label"
                    textAnchor={Math.abs(lx - C) < 8 ? 'middle' : lx > C ? 'start' : 'end'}
                    dominantBaseline="middle">{label(k)}</text>
            </g>
          )
        })}
        {topics.map((t, ti) => {
          const values = pick(t)
          return (
            <polygon key={t.id} points={keys.map((k, i) => pt(i, values[k] ?? 0).join(',')).join(' ')}
                     style={{ fill: CAT[ti], stroke: CAT[ti] }} className="series">
              <title>{`${t.id}\n${keys.map((k) => `${label(k)}: ${(values[k] ?? 0).toFixed(1)}`).join('\n')}`}</title>
            </polygon>
          )
        })}
      </svg>
    </figure>
  )
}

/* ---------------------------------------------------------------- Landscape */

function LandscapeMap({ onSelect, selectedId }: { onSelect: (id: string) => void; selectedId: string | null }) {
  const [data, setData] = useState<Landscape | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [hover, setHover] = useState<string | null>(null)
  useEffect(() => { api.landscape().then(setData).catch((e) => setError(String(e.message ?? e))) }, [])

  if (error) return <div className="empty">Could not load the map: {error}</div>
  if (!data) return <div className="empty">Computing the map…</div>
  if (data.points.length < 3) return <div className="empty">Not enough embedded evidence to draw a map yet.</div>

  const color = new Map(data.domains.map((d, i) => [d.id, CAT[i % CAT.length]]))
  const W = 760, H = 520, P = 20
  const sx = (x: number) => P + ((x + 1) / 2) * (W - 2 * P)
  const sy = (y: number) => H - P - ((y + 1) / 2) * (H - 2 * P)
  const r = (a: number | null) => 2.2 + Math.sqrt(Math.max(0, a ?? 0)) * 0.55
  const visible = data.points.filter((p) => !hidden.has(p.domain ?? ''))
  const hovered = data.points.find((p) => p.id === hover)
  const ev = data.explained_variance

  return (
    <div className="landscape">
      <p className="explore-note">
        Each dot is a space, placed by the language of its evidence: the average multilingual embedding of
        its attached signals, projected to two dimensions (PCA). Close dots share vocabulary in their evidence,
        even across verticals. Size is attractiveness; colour is the primary domain. The two axes capture
        {' '}{Math.round((ev[0] + ev[1]) * 100)}% of the spread — read clusters, not distances.
      </p>
      <div className="legend" role="group" aria-label="Domains">
        {data.domains.map((d) => (
          <button key={d.id} className={`legend-item ${hidden.has(d.id) ? 'off' : ''}`}
                  aria-pressed={!hidden.has(d.id)}
                  onClick={() => { const n = new Set(hidden); n.has(d.id) ? n.delete(d.id) : n.add(d.id); setHidden(n) }}>
            <span className="swatch" style={{ background: color.get(d.id) }} />{d.label}
            <small> {data.points.filter((p) => p.domain === d.id).length}</small>
          </button>
        ))}
      </div>
      <div className="landscape-wrap">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Landscape map of opportunity spaces">
          <line x1={sx(0)} y1={P} x2={sx(0)} y2={H - P} className="axis faint" />
          <line x1={P} y1={sy(0)} x2={W - P} y2={sy(0)} className="axis faint" />
          {visible.map((p) => (
            <circle key={p.id} cx={sx(p.x)} cy={sy(p.y)} r={r(p.attractiveness)}
                    className={`dot ${p.id === selectedId ? 'selected' : ''}`}
                    style={{ fill: color.get(p.domain ?? '') ?? 'var(--text-muted)' }}
                    onMouseEnter={() => setHover(p.id)} onMouseLeave={() => setHover(null)}
                    onClick={() => onSelect(p.id)}>
              <title>{`${p.id} — ${p.labels.use_case} · ${p.labels.technology} · ${p.labels.vertical}`}</title>
            </circle>
          ))}
        </svg>
        {hovered && (
          <div className="landscape-tip">
            <b className="mono">{hovered.id}</b> {hovered.labels.use_case} · {hovered.labels.technology}
            <div>{hovered.labels.vertical} · {hovered.domain_label} · {hovered.horizon ?? '—'}</div>
            <div className="tip-statement">{hovered.statement}</div>
            <div>Attractiveness {hovered.attractiveness?.toFixed(1) ?? '—'} · Right to win {hovered.right_to_win?.toFixed(1) ?? '—'} · {hovered.n_embedded} embedded signals</div>
          </div>
        )}
      </div>
      <p className="explore-note small">
        {data.embedded_spaces} spaces with embedded evidence · axis 1 explains {(ev[0] * 100).toFixed(1)}%,
        axis 2 {(ev[1] * 100).toFixed(1)}% · click a dot to open it.
      </p>
    </div>
  )
}
