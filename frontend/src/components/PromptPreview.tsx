import { useEffect, useRef, useState } from 'react'
import { api, type GenerationPrompt } from '../api'
import { useFocusTrap } from './Help'
import type { GenerationConstraints } from '../types'

/** The full prompt a generation run with the current settings sends to the
 *  model: the system prompt and the user prompt for the first evidence cluster.
 *  Every later call in the run differs only in its cluster's evidence and lens. */
export default function PromptPreview({ count, constraints, onClose }: {
  count: number
  constraints: GenerationConstraints
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  useFocusTrap(true, ref, onClose)
  const [data, setData] = useState<GenerationPrompt | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    api.generationPrompt(count, constraints).then(setData).catch((e) => setError(String(e.message ?? e)))
  }, [count, constraints])

  const full = data ? `### SYSTEM\n\n${data.system}\n\n### USER\n\n${data.user ?? ''}` : ''
  const copy = () => {
    navigator.clipboard?.writeText(full).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) })
  }

  return (
    <div className="help-backdrop" onClick={onClose} role="presentation">
      <div className="help-modal prompt-modal" role="dialog" aria-modal="true"
           aria-labelledby="prompt-title" tabIndex={-1} ref={ref} onClick={(e) => e.stopPropagation()}>
        <div className="help-head">
          <h3 id="prompt-title">Prompt sent to the LLM</h3>
          <span className="spacer" />
          {data && <button type="button" onClick={copy}>{copied ? 'Copied' : 'Copy'}</button>}
          <button type="button" onClick={onClose} aria-label="Close prompt">✕</button>
        </div>
        <div className="help-body">
          {error && <p className="error">{error}</p>}
          {!data && !error && <p>Building the prompt…</p>}
          {data && (
            <>
              <p>
                {data.model && <>Model <code>{data.model}</code> · </>}
                temperature {data.temperature} · {data.clusters_in_scope} evidence cluster
                {data.clusters_in_scope === 1 ? '' : 's'} in scope · {data.passes_per_cluster} call
                {data.passes_per_cluster === 1 ? '' : 's'} per cluster.
                {data.cluster && <> Shown for the first cluster, <b>{data.cluster.label || `#${data.cluster.id}`}</b> ({data.cluster.signals} signals); every other call uses the same system prompt and differs only in the evidence and the lens.</>}
              </p>
              <h4>System prompt</h4>
              <pre className="prompt-text">{data.system}</pre>
              <h4>User prompt</h4>
              {data.user
                ? <pre className="prompt-text">{data.user}</pre>
                : <p>No evidence cluster matches these settings, so a run would make no model calls.</p>}
              {data.lenses.length > 0 && (
                <>
                  <h4>Lenses</h4>
                  <p>Each pass over a cluster uses a different lens in place of the one shown above:</p>
                  <ol>{data.lenses.map((lens) => <li key={lens}>{lens}</li>)}</ol>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
