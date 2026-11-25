import type { Topic } from '../types'

/** The subtitle under every title (2026-11-25): why now, and who signs.
 *
 *  Before this the subtitle was the three taxonomy labels, which come from
 *  closed lists and so can never say anything specific. They are still shown,
 *  as chips (`LabelChips`), but they are no longer what describes the space.
 *  Silent for a space written before the subtitle existed. */
export function TopicSubtitle({ topic }: { topic: Pick<Topic, 'why_now' | 'who_signs'> }) {
  if (!topic.why_now && !topic.who_signs) return null
  return (
    <div className="topic-subtitle">
      {topic.why_now && (
        <span><span className="topic-subtitle-key">Why now</span> {topic.why_now}</span>
      )}
      {topic.why_now && topic.who_signs && <span aria-hidden="true"> · </span>}
      {topic.who_signs && (
        <span><span className="topic-subtitle-key">Who signs</span> {topic.who_signs}</span>
      )}
    </div>
  )
}

/** Vertical, use case and technology as three small chips. */
export function LabelChips({ labels }: { labels: Topic['labels'] }) {
  return (
    <span className="label-chips">
      <span className="label-chip" title="Vertical">{labels.vertical}</span>
      <span className="label-chip" title="Use case">{labels.use_case}</span>
      <span className="label-chip" title="Technology">{labels.technology}</span>
    </span>
  )
}
