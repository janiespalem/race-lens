import type { Insight } from '../api/types'

export type EvidenceDatum = { label: string; value: string }

export function evidenceData(insight: Pick<Insight, 'evidence'>, lang: 'en' | 'ru' = 'en'): EvidenceDatum[] {
  const items: EvidenceDatum[] = []
  const ev = insight.evidence
  const seconds = lang === 'ru' ? 'с' : 's'
  if (typeof ev.gap_s === 'number') items.push({ label: lang === 'ru' ? 'ОТРЫВ' : 'GAP', value: `${ev.gap_s.toFixed(1)}${seconds}` })
  if (typeof ev.interval_s === 'number') items.push({ label: lang === 'ru' ? 'ИНТ' : 'INT', value: `${ev.interval_s.toFixed(2)}${seconds}` })
  if (typeof ev.pace_delta_ms === 'number')
    items.push({ label: lang === 'ru' ? 'Δ ТЕМП' : 'Δ PACE', value: `+${(ev.pace_delta_ms / 1000).toFixed(1)}/${lang === 'ru' ? 'круг' : 'lap'}` })
  if (typeof ev.margin_s === 'number')
    items.push({ label: lang === 'ru' ? 'ЗАПАС' : 'MARGIN', value: `+${ev.margin_s.toFixed(1)}${seconds}` })
  const tyreAge = [ev.tyre_age_laps, ev.attacker_tyre_age_laps, ev.tyre_age]
    .find((value): value is number => typeof value === 'number')
  if (tyreAge !== undefined) items.push({ label: lang === 'ru' ? 'ШИНЫ' : 'TYRES', value: `${tyreAge} ${lang === 'ru' ? 'КР.' : 'LAPS'}` })
  return items
}
