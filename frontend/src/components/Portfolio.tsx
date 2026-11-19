/* Portfolio browser (/portfolio).
 *
 * Four cascading lists — SVP, value proposition, offer, customer reference —
 * and a mind map of the same hierarchy under them. The lists and the map are
 * one selection seen two ways: picking in a list redraws the map around it, and
 * clicking a node in the map sets the lists.
 *
 * The map always shows the path to the selection expanded and its siblings
 * collapsed, so it stays readable at any depth. Long sibling lists are capped
 * with a "+N more" node that opens the next batch in place.
 *
 * The value-proposition level is curated (config/portfolio.yaml): Expresso says
 * which SVPs an offer serves and which offers a reference used, but not which
 * offers make up a value proposition. A Salesforce extract, when imported, adds
 * offers by CRM product family (drawn dotted, tagged CRM), account figures on
 * matched references and opportunity totals on the SVPs.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type {
  Portfolio, PortfolioCrmAccount, PortfolioCrmSvp, PortfolioOffer, PortfolioReference, PortfolioSvp,
  PortfolioValueProposition,
} from '../types'
import '../portfolio.css'

type Kind = 'root' | 'svp' | 'vp' | 'offer' | 'ref' | 'more'

interface Sel { svp: string; vp: string; offer: string; ref: string }
const NONE: Sel = { svp: '', vp: '', offer: '', ref: '' }
const PARAMS: (keyof Sel)[] = ['svp', 'vp', 'offer', 'ref']

interface MapNode {
  key: string
  kind: Kind
  label: string
  count?: string
  title?: string
  svp?: string
  selected?: boolean
  onPath?: boolean
  muted?: boolean
  /** Placed by a CRM product family, not by the curation. */
  crm?: boolean
  pick?: Sel
  expandKey?: string
  children: MapNode[]
}

/** How many siblings a level shows before "+N more". */
const CAP: Record<'vp' | 'offer' | 'ref', number> = { vp: 40, offer: 12, ref: 15 }

const plural = (n: number, one: string, many = one + 's') => `${n} ${n === 1 ? one : many}`

function selFromUrl(): Sel {
  const p = new URLSearchParams(window.location.search)
  return { svp: p.get('svp') ?? '', vp: p.get('vp') ?? '', offer: p.get('offer') ?? '', ref: p.get('ref') ?? '' }
}

export default function PortfolioBrowser() {
  const [data, setData] = useState<Portfolio | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sel, setSel] = useState<Sel>(selFromUrl)
  const [shown, setShown] = useState<Record<string, number>>({})

  useEffect(() => {
    api.portfolio().then(setData).catch((e) => setError(String(e.message ?? e)))
  }, [])

  const vpById = useMemo(() => {
    const m: Record<string, PortfolioValueProposition> = {}
    for (const vp of data?.value_propositions ?? []) m[vp.id] = vp
    return m
  }, [data])

  // A selection read from a shared link may name something that no longer
  // exists, or a child that is not under its parent. Drop it from that level down
  // rather than draw a map of nothing.
  useEffect(() => {
    if (!data) return
    setSel((s) => {
      const svp = data.svps.some((x) => x.key === s.svp) ? s.svp : ''
      const vp = svp && vpById[s.vp]?.svp === svp ? s.vp : ''
      const offer = vp && vpById[vp].offers.includes(s.offer) ? s.offer : ''
      const ref = offer && data.offers[offer].references.includes(s.ref) ? s.ref : ''
      const next = { svp, vp, offer, ref }
      return PARAMS.every((k) => next[k] === s[k]) ? s : next
    })
  }, [data, vpById])

  // Keep the selection in the address bar, so a view can be sent to someone.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search)
    for (const k of PARAMS) sel[k] ? p.set(k, sel[k]) : p.delete(k)
    const q = p.toString()
    window.history.replaceState(null, '', `${window.location.pathname}${q ? `?${q}` : ''}`)
  }, [sel])
  useEffect(() => () => {
    const p = new URLSearchParams(window.location.search)
    PARAMS.forEach((k) => p.delete(k))
    const q = p.toString()
    window.history.replaceState(null, '', `${window.location.pathname}${q ? `?${q}` : ''}`)
  }, [])

  const choose = useCallback((next: Sel) => setSel(next), [])
  const more = useCallback((key: string, step: number) =>
    setShown((s) => ({ ...s, [key]: (s[key] ?? 0) + step })), [])

  const svp = data?.svps.find((s) => s.key === sel.svp)
  const vp = sel.vp ? vpById[sel.vp] : undefined
  const offer = sel.offer && data ? data.offers[sel.offer] : undefined
  const ref = sel.ref && data ? data.references[sel.ref] : undefined

  const tree = useMemo(() => (data ? buildTree(data, vpById, sel, shown) : null), [data, vpById, sel, shown])

  if (error) return <div className="panel"><div className="empty"><p>The portfolio could not be loaded.</p><p className="pf-mono">{error}</p></div></div>
  if (!data || !tree) return <div className="panel"><div className="empty">Loading the portfolio…</div></div>

  const vps = svp ? svp.value_propositions.map((id) => vpById[id]).filter(Boolean) : []
  const offers = vp ? vp.offers.map((id) => data.offers[id]).filter(Boolean) : []
  const refs = offer ? offer.references.map((id) => data.references[id]).filter(Boolean) : []

  return (
    <div className="panel portfolio">
      <div className="panel-head">
        <h2>Portfolio</h2>
        <span className="sub">
          {data.counts.svps} SVPs · {data.counts.value_propositions} value propositions · {data.counts.offers} offers · {data.counts.references} customer references
        </span>
      </div>
      <div className="panel-body">
        <div className="pf-pickers" role="group" aria-label="Portfolio selection">
          <Picker label="Strategic Value Proposition" value={sel.svp} placeholder="All SVPs"
                  onChange={(v) => choose({ ...NONE, svp: v })}
                  options={data.svps.map((s) => ({ value: s.key, text: `${s.key} · ${s.name}` }))} />
          <Picker label="Value proposition" value={sel.vp} disabled={!svp}
                  placeholder={svp ? 'All value propositions' : 'Choose an SVP first'}
                  onChange={(v) => choose({ ...NONE, svp: sel.svp, vp: v })}
                  options={vps.map((v) => ({ value: v.id, text: `${v.name} (${plural(v.offers.length, 'offer')})` }))} />
          <Picker label="Offer" value={sel.offer} disabled={!vp || !offers.length}
                  placeholder={!vp ? 'Choose a value proposition first' : offers.length ? 'All offers' : 'No offers in Expresso'}
                  onChange={(v) => choose({ ...sel, offer: v, ref: '' })}
                  options={offers.map((o) => ({ value: o.id, text: `${o.name} (${plural(o.references.length, 'reference')})${vp?.crm_offers?.includes(o.id) ? ' · CRM' : ''}` }))} />
          <Picker label="Customer reference" value={sel.ref} disabled={!offer || !refs.length}
                  placeholder={!offer ? 'Choose an offer first' : refs.length ? `All ${refs.length} references` : 'No references'}
                  onChange={(v) => choose({ ...sel, ref: v })}
                  options={refs.map((r) => ({ value: r.id, text: refLabel(r) }))} />
          <button className="pf-clear" onClick={() => choose(NONE)} disabled={!sel.svp}>Clear</button>
        </div>

        <MindMap root={tree} onPick={choose} onMore={more} />

        <Details svp={svp} vp={vp} offer={offer} reference={ref} offersById={data.offers} />
        <p className="pf-note">
          Value propositions are curated in config/portfolio.yaml; Expresso does not link offers to them.
          Offers, their SVP tags and the references that used them come from the Expresso crawl.
          {data.crm && <> Salesforce extract {data.crm.source}{data.crm.extracted ? ` (${data.crm.extracted})` : ''}:
            {' '}{plural(data.crm.offers_placed, 'offer')} placed by product family,
            {' '}{plural(data.crm.references_matched, 'reference')} matched to a CRM account by name,
            {' '}opportunity figures for {plural(data.crm.svps_with_figures, 'SVP')}. The extract covers
            international accounts only, so most French references have no CRM match.</>}
        </p>
      </div>
    </div>
  )
}

function refLabel(r: PortfolioReference) {
  return r.project ? `${r.customer} — ${r.project}` : r.customer
}

function Picker({ label, value, options, placeholder, disabled, onChange }: {
  label: string; value: string; placeholder: string; disabled?: boolean
  options: { value: string; text: string }[]; onChange: (v: string) => void
}) {
  return (
    <label className="pf-picker">
      <span>{label}</span>
      <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        <option value="">{placeholder}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.text}</option>)}
      </select>
    </label>
  )
}

/* ------------------------------------------------------------------ tree */

function capped<T>(items: T[], keep: (t: T) => boolean, limit: number): { head: T[]; rest: number } {
  if (items.length <= limit) return { head: items, rest: 0 }
  const head = items.slice(0, limit)
  const pinned = items.slice(limit).filter(keep)
  return { head: [...head, ...pinned], rest: items.length - limit - pinned.length }
}

export function buildTree(data: Portfolio, vpById: Record<string, PortfolioValueProposition>, sel: Sel,
                   shown: Record<string, number>): MapNode {
  const moreNode = (parentKey: string, rest: number, step: number): MapNode => ({
    key: `${parentKey}:more`, kind: 'more', label: `+${rest} more`, title: 'Show the next ones',
    expandKey: parentKey, count: String(step), children: [],
  })

  const refNode = (r: PortfolioReference, base: Sel, svpKey: string): MapNode => ({
    key: `ref:${r.id}`, kind: 'ref', label: r.customer, title: refLabel(r), svp: svpKey,
    count: r.signed ? r.signed.slice(0, 4) : undefined,
    selected: sel.ref === r.id, onPath: sel.ref === r.id, muted: !!sel.ref && sel.ref !== r.id,
    pick: { ...base, ref: r.id }, children: [],
  })

  const offerNode = (o: PortfolioOffer, v: PortfolioValueProposition, expand: boolean): MapNode => {
    const base: Sel = { svp: v.svp, vp: v.id, offer: o.id, ref: '' }
    const crm = !!v.crm_offers?.includes(o.id)
    const node: MapNode = {
      key: `offer:${v.id}:${o.id}`, kind: 'offer', label: o.name, svp: v.svp,
      title: (o.headline || o.name) + (crm ? '\nPlaced here by its CRM product family' : ''),
      count: String(o.references.length), selected: sel.offer === o.id && !sel.ref, onPath: sel.offer === o.id,
      muted: !!sel.offer && sel.offer !== o.id, crm, pick: base, children: [],
    }
    if (expand) {
      const refs = o.references.map((id) => data.references[id]).filter(Boolean)
      const limit = CAP.ref + (shown[node.key] ?? 0)
      const { head, rest } = capped(refs, (r) => r.id === sel.ref, limit)
      node.children = head.map((r) => refNode(r, base, v.svp))
      if (rest) node.children.push(moreNode(node.key, rest, Math.min(rest, 25)))
    }
    return node
  }

  const vpNode = (v: PortfolioValueProposition, expand: boolean): MapNode => {
    const node: MapNode = {
      key: `vp:${v.id}`, kind: 'vp', label: v.name, title: v.description, svp: v.svp,
      count: String(v.offers.length), selected: sel.vp === v.id && !sel.offer, onPath: sel.vp === v.id,
      muted: (!!sel.vp && sel.vp !== v.id) || !v.curated,
      pick: { svp: v.svp, vp: v.id, offer: '', ref: '' }, children: [],
    }
    if (expand) {
      const offers = v.offers.map((id) => data.offers[id]).filter(Boolean)
      // A chosen value proposition lists all its offers (in batches); with no
      // value proposition chosen, each one shows a short preview.
      const limit = (sel.vp === v.id ? 30 : CAP.offer) + (shown[node.key] ?? 0)
      const { head, rest } = capped(offers, (o) => o.id === sel.offer, limit)
      node.children = head.map((o) => offerNode(o, v, sel.offer === o.id))
      if (rest) node.children.push(moreNode(node.key, rest, Math.min(rest, 25)))
      if (!offers.length) {
        node.children.push({ key: `${node.key}:none`, kind: 'more', label: 'No Expresso offers',
                             title: v.unmatched_offers.length ? `Named but not found: ${v.unmatched_offers.join(', ')}` : undefined,
                             children: [] })
      }
    }
    return node
  }

  const svpNode = (key: string, expandVps: boolean): MapNode => {
    const s = data.svps.find((x) => x.key === key)!
    const vps = s.value_propositions.map((id) => vpById[id]).filter(Boolean)
    return {
      key: `svp:${s.key}`, kind: 'svp', label: `${s.key} · ${s.name}`, title: s.summary, svp: s.key,
      count: String(vps.length), selected: sel.svp === s.key && !sel.vp, onPath: sel.svp === s.key,
      pick: { ...NONE, svp: s.key },
      children: expandVps
        ? vps.map((v) => vpNode(v, sel.vp ? sel.vp === v.id : true))
        : [],
    }
  }

  if (!sel.svp) {
    return {
      key: 'root', kind: 'root', label: 'Orange Business portfolio', pick: NONE, selected: true,
      children: data.svps.map((s) => {
        const n = svpNode(s.key, false)
        n.children = s.value_propositions.map((id) => vpById[id]).filter(Boolean).map((v) => vpNode(v, false))
        return n
      }),
    }
  }
  return svpNode(sel.svp, true)
}

/* ---------------------------------------------------------------- layout */

const COL = 310
const NODE_W = 276
const NODE_H = 28
const ROW = 36
const PAD = 12

interface Placed { node: MapNode; x: number; y: number; depth: number }

export function layout(root: MapNode) {
  const placed: Placed[] = []
  const links: { from: Placed; to: Placed }[] = []
  let leaf = 0
  let maxDepth = 0
  const walk = (node: MapNode, depth: number): Placed => {
    maxDepth = Math.max(maxDepth, depth)
    const kids = node.children.map((c) => walk(c, depth + 1))
    const y = kids.length ? (kids[0].y + kids[kids.length - 1].y) / 2 : PAD + (leaf++) * ROW + NODE_H / 2
    const p = { node, x: PAD + depth * COL, y, depth }
    placed.push(p)
    kids.forEach((k) => links.push({ from: p, to: k }))
    return p
  }
  walk(root, 0)
  return { placed, links, width: PAD * 2 + maxDepth * COL + NODE_W, height: PAD * 2 + Math.max(1, leaf) * ROW }
}

function fit(text: string, width: number, px = 6.6) {
  const max = Math.floor(width / px)
  return text.length <= max ? text : text.slice(0, Math.max(1, max - 1)).trimEnd() + '…'
}

export function MindMap({ root, onPick, onMore }: {
  root: MapNode; onPick: (s: Sel) => void; onMore: (key: string, step: number) => void
}) {
  const { placed, links, width, height } = useMemo(() => layout(root), [root])
  const act = (n: MapNode) => {
    if (n.kind === 'more') { if (n.expandKey) onMore(n.expandKey, Number(n.count) || 25) }
    else if (n.pick) onPick(n.pick)
  }
  return (
    <div className="pf-map" role="region" aria-label="Portfolio mind map">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        {links.map(({ from, to }) => {
          const x1 = from.x + NODE_W, x2 = to.x, mid = (x1 + x2) / 2
          return <path key={`${from.node.key}>${to.node.key}`}
                       className={`pf-link${to.node.onPath ? ' on' : ''}`}
                       d={`M${x1},${from.y} C${mid},${from.y} ${mid},${to.y} ${x2},${to.y}`} />
        })}
        {placed.map(({ node, x, y }) => {
          const countW = node.count && node.kind !== 'more' ? 34 : 0
          const crmW = node.crm ? 30 : 0
          const cls = ['pf-node', `k-${node.kind}`, node.svp ? `svp-${node.svp}` : '',
            node.selected ? 'sel' : '', node.onPath ? 'on' : '', node.muted ? 'muted' : '',
            node.crm ? 'crm' : ''].join(' ')
          const clickable = node.kind !== 'more' || !!node.expandKey
          return (
            <g key={node.key} className={cls} transform={`translate(${x},${y - NODE_H / 2})`}
               role={clickable ? 'button' : undefined} tabIndex={clickable ? 0 : undefined}
               aria-label={node.title ? `${node.label}: ${node.title}` : node.label}
               onClick={clickable ? () => act(node) : undefined}
               onKeyDown={clickable ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); act(node) } } : undefined}>
              <title>{node.title ? `${node.label}\n${node.title}` : node.label}</title>
              <rect className="pf-box" width={NODE_W} height={NODE_H} rx={7} />
              {node.kind !== 'root' && node.kind !== 'more' && <rect className="pf-bar" width={5} height={NODE_H} rx={2} />}
              <text className="pf-label" x={12} y={NODE_H / 2 + 4}>{fit(node.label, NODE_W - 20 - countW - crmW)}</text>
              {crmW > 0 && <text className="pf-crm-tag" x={NODE_W - 9 - countW} y={NODE_H / 2 + 3.5} textAnchor="end">CRM</text>}
              {countW > 0 && <text className="pf-count" x={NODE_W - 9} y={NODE_H / 2 + 4} textAnchor="end">{node.count}</text>}
            </g>
          )
        })}
      </svg>
      <p className="pf-legend">
        Click a node to select it. Numbers count what sits one level down: value propositions per SVP, offers per value
        proposition, references per offer, and the signing year on a reference. Greyed branches are outside the selection
        or not curated. Dotted offers marked CRM were placed by their Salesforce product family, not by the curation.
      </p>
    </div>
  )
}

/* --------------------------------------------------------------- details */

/** €1.2bn / €34.5M / €120k / €900 */
export function eur(n: number) {
  const a = Math.abs(n)
  if (a >= 1e9) return `€${(n / 1e9).toFixed(1)}bn`
  if (a >= 1e6) return `€${(n / 1e6).toFixed(1)}M`
  if (a >= 1e3) return `€${Math.round(n / 1e3)}k`
  return `€${Math.round(n)}`
}

function CrmSvp({ crm }: { crm: PortfolioCrmSvp }) {
  return (
    <div className="pf-crm">
      <h4>Salesforce · {crm.name}</h4>
      <dl>
        <dt>Opportunities</dt><dd>{crm.opportunities.toLocaleString()}</dd>
        <dt>Won</dt><dd>{crm.won.toLocaleString()} · {eur(crm.won_tcv_eur)} TCV</dd>
        <dt>Win rate</dt><dd>{crm.win_rate == null ? '—' : `${Math.round(crm.win_rate * 100)}% of decided`}</dd>
        <dt>Open pipeline</dt><dd>{crm.open.toLocaleString()} · {eur(crm.open_tcv_eur)} TCV</dd>
      </dl>
    </div>
  )
}

function CrmAccount({ crm }: { crm: PortfolioCrmAccount }) {
  const where = [crm.country, crm.region].filter(Boolean).join(', ')
  return (
    <div className="pf-crm">
      <h4>Salesforce account · {crm.name}</h4>
      <dl>
        {crm.type && <><dt>Type</dt><dd>{crm.type}{crm.segment ? ` · ${crm.segment}` : ''}</dd></>}
        {crm.industry && <><dt>Industry</dt><dd>{crm.industry}</dd></>}
        {where && <><dt>Location</dt><dd>{where}</dd></>}
        <dt>Won deals</dt><dd>{crm.won} · {eur(crm.won_tcv_eur)} TCV{crm.last_won ? ` · last ${crm.last_won.slice(0, 7)}` : ''}</dd>
        <dt>Open</dt><dd>{crm.open} · {eur(crm.open_tcv_eur)} TCV</dd>
        <dt>Lost / cancelled</dt><dd>{crm.lost}</dd>
      </dl>
      <p className="pf-muted">Matched by company name; check it is the same legal entity before relying on it.</p>
    </div>
  )
}

function Details({ svp, vp, offer, reference, offersById }: {
  svp?: PortfolioSvp; vp?: PortfolioValueProposition
  offer?: PortfolioOffer; reference?: PortfolioReference; offersById: Record<string, PortfolioOffer>
}) {
  if (!svp) return null
  const crmPlaced = vp?.crm_offers?.length ?? 0
  return (
    <div className="pf-details">
      <section>
        <h3>{svp.name}</h3>
        <p className="pf-muted">{svp.role}</p>
        <p>{svp.summary}</p>
        {svp.crm && <CrmSvp crm={svp.crm} />}
      </section>
      {vp && (
        <section>
          <h3>{vp.name}</h3>
          <p>{vp.description}</p>
          {vp.ideas.length > 0 && <p className="pf-muted">Opportunity ideas: {vp.ideas.join(', ')}</p>}
          {crmPlaced > 0 && (
            <p className="pf-muted">{plural(crmPlaced, 'offer')} placed here by Salesforce product family:
              {' '}{vp.crm_offers!.map((id) => offersById[id]?.name).filter(Boolean).join(', ')}</p>
          )}
          {vp.unmatched_offers.length > 0 && (
            <p className="pf-muted">Named in the curation but not found in Expresso: {vp.unmatched_offers.join(', ')}</p>
          )}
        </section>
      )}
      {offer && (
        <section>
          <h3>{offer.name}</h3>
          {offer.headline && <p>{offer.headline}</p>}
          <p className="pf-muted">
            SVP tags: {offer.svps.join(', ') || 'none'}
            {offer.svps_secondary.length > 0 && ` (secondary: ${offer.svps_secondary.join(', ')})`}
            {' · '}{offer.languages.join(' / ').toUpperCase()}
          </p>
          {offer.crm_products && offer.crm_products.length > 0 && (
            <p className="pf-muted">
              Salesforce products: {offer.crm_products.join(', ')}
              {vp?.crm_offers?.includes(offer.id) && ' (their product family placed this offer here)'}
            </p>
          )}
          <a href={offer.url} target="_blank" rel="noreferrer">Open in Expresso</a>
        </section>
      )}
      {reference && (
        <section>
          <h3>{reference.customer}</h3>
          {reference.project && <p><strong>{reference.project}</strong></p>}
          {reference.headline && <p>{reference.headline}</p>}
          <p className="pf-muted">
            {[reference.sectors.join(', '), reference.regions.join(', '), reference.signed && `signed ${reference.signed}`]
              .filter(Boolean).join(' · ')}
          </p>
          {!reference.communication_agreement && (
            <p className="pf-flag">No communication agreement: internal use only, do not name this customer externally.</p>
          )}
          {reference.offers.length > 1 && (
            <p className="pf-muted">Also used: {reference.offers.filter((id) => id !== offer?.id)
              .map((id) => offersById[id]?.name).filter(Boolean).join(', ')}</p>
          )}
          {reference.crm && <CrmAccount crm={reference.crm} />}
          <a href={reference.url} target="_blank" rel="noreferrer">Open in Expresso</a>
        </section>
      )}
    </div>
  )
}
