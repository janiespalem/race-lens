import type { Stint } from '../api/types'

export function clipStints(stints: Stint[], currentLap: number): Stint[] {
  return stints.flatMap((stint) => {
    if (stint.start_lap > currentLap) return []
    if (stint.end_lap <= currentLap) return [stint]
    const end_lap = currentLap
    return [{ ...stint, end_lap, laps: end_lap - stint.start_lap + 1 }]
  })
}

export function showStintLabel(laps: number, totalLaps: number): boolean {
  return totalLaps > 0 && laps / totalLaps >= 0.08
}

/** Localized display names; raw compound codes remain the styling/data keys. */
export function compoundLabel(compound: string | null | undefined, lang: 'en' | 'ru' = 'en'): string {
  if (!compound || compound.toUpperCase() === 'UNKNOWN') return lang === 'ru' ? 'НЕИЗВЕСТНО' : 'UNKNOWN'
  const labels: Record<string, string> = {
    SOFT: 'МЯГКИЕ', MEDIUM: 'СРЕДНИЕ', HARD: 'ЖЁСТКИЕ', INTERMEDIATE: 'ПРОМЕЖУТОЧНЫЕ', WET: 'ДОЖДЕВЫЕ',
  }
  return lang === 'ru' ? (labels[compound.toUpperCase()] ?? compound) : compound
}
