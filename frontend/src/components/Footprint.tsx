/* AI footprint — energy and carbon of the model calls behind the radar.
 * Ported idea from orange_opportunity_spaces_HuMaSoo (CodeCarbon / EcoLogits);
 * see src/radar/footprint.py for why it is estimated from tokens instead. */
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { FootprintReport } from '../types'
import { BarList, ChartCard, Kpi } from './Charts'

const fmtWh = (wh: number) => (wh >= 1000 ? `${(wh / 1000).toFixed(1)} kWh` : `${wh.toFixed(0)} Wh`)
const fmtG = (g: number) => (g >= 1000 ? `${(g / 1000).toFixed(1)} kg` : `${g.toFixed(0)} g`)
const fmtTok = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : n >= 1e3 ? `${(n / 1e3).toFixed(0)} k` : String(n))

export default function FootprintCard() {
  const [data, setData] = useState<FootprintReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { api.footprint().then(setData).catch((e) => setError(String(e.message ?? e))) }, [])

  const f = data?.factors
  return (
    <ChartCard wide title="AI footprint — energy and CO₂e of the model calls behind this radar"
               note={f ? `Estimated, not metered. Exact token counts × ${f.wh_per_1k_prompt_tokens} Wh per 1k prompt tokens and ${f.wh_per_1k_completion_tokens} Wh per 1k generated tokens × PUE ${f.pue} × ${f.grid_gco2e_per_kwh} g CO₂e/kWh (factor set ${f.version}, config/settings.yaml → footprint). ${data?.historic_runs ?? 0} runs before the per-call ledger are counted from their run totals.` : undefined}>
      {error && <div className="empty">Could not load: {error}</div>}
      {!data && !error && <div className="empty">Loading…</div>}
      {data && (
        <div className="footprint">
          <div className="kpi-row">
            <Kpi value={fmtWh(data.totals.energy_wh)} label="Energy (est.)"
                 sub={`≈ ${data.equivalents.phone_charges.toLocaleString()} phone charges`} />
            <Kpi value={fmtG(data.totals.co2e_g)} label="CO₂e (est.)"
                 sub={`≈ ${data.equivalents.car_km} km by car`} />
            <Kpi value={data.totals.calls.toLocaleString()} label="Model calls" />
            <Kpi value={fmtTok(data.totals.prompt_tokens + data.totals.completion_tokens)} label="Tokens"
                 sub={`${fmtTok(data.totals.completion_tokens)} generated`} />
            <Kpi value={data.per_space.energy_wh != null ? `${data.per_space.energy_wh} Wh` : '—'}
                 label="Per opportunity space" sub={data.per_space.co2e_g != null ? `${data.per_space.co2e_g} g CO₂e` : undefined} />
          </div>
          <div className="footprint-grid">
            <div>
              <h4>By month (Wh)</h4>
              <BarList data={data.by_month.map((m) => ({ label: m.month, value: Math.round(m.energy_wh),
                hint: `${m.calls} calls, ${fmtTok(m.prompt_tokens)} prompt + ${fmtTok(m.completion_tokens)} generated tokens` }))} />
            </div>
            <div>
              <h4>By purpose (Wh)</h4>
              <BarList data={data.by_purpose.slice(0, 8).map((m) => ({ label: m.purpose, value: Math.round(m.energy_wh),
                hint: `${m.calls} calls` }))} />
            </div>
          </div>
        </div>
      )}
    </ChartCard>
  )
}
