import type { Insight } from '../api/types'

export type EvidenceDatum = { label: string; value: string }

export function evidenceData(insight: Pick<Insight, 'evidence'>): EvidenceDatum[] {
  const items: EvidenceDatum[] = []
  const ev = insight.evidence
  if (typeof ev.gap_s === 'number') items.push({ label: 'GAP', value: `${ev.gap_s.toFixed(1)}s` })
  if (typeof ev.interval_s === 'number') items.push({ label: 'INT', value: `${ev.interval_s.toFixed(2)}s` })
  if (typeof ev.pace_delta_ms === 'number')
    items.push({ label: 'Δ PACE', value: `+${(ev.pace_delta_ms / 1000).toFixed(1)}/lap` })
  if (typeof ev.margin_s === 'number')
    items.push({ label: 'MARGIN', value: `+${ev.margin_s.toFixed(1)}s` })
  const tyreAge = [ev.tyre_age_laps, ev.attacker_tyre_age_laps, ev.tyre_age]
    .find((value): value is number => typeof value === 'number')
  if (tyreAge !== undefined) items.push({ label: 'TYRES', value: `${tyreAge} LAPS` })
  return items
}
