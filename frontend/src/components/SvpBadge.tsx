import type { Topic } from '../types'

/** The portfolio SVP a space belongs to (radar.svp), as a solid chip.
 *
 * Solid rather than outlined so it reads before the outlined status badges
 * around it, and in the text colour rather than orange: orange already means
 * right-to-win on the radar and in the score blocks. The closest value
 * proposition rides along in the tooltip (or inline with `detailed`).
 */
export default function SvpBadge({ topic, detailed = false }:
  { topic: Pick<Topic, 'svp' | 'svp_label' | 'svp_value_proposition'>; detailed?: boolean }) {
  if (!topic.svp) return null
  const title = `Portfolio SVP: ${topic.svp_label}`
    + (topic.svp_value_proposition ? ` — closest value proposition: ${topic.svp_value_proposition}` : '')
  return (
    <span className="svp-chip" title={title}>
      <b>{topic.svp}</b>
      <span>{topic.svp_label}</span>
      {detailed && topic.svp_value_proposition && (
        <span className="svp-chip-vp">· {topic.svp_value_proposition}</span>
      )}
    </span>
  )
}
