import type { FinishCondition } from '../api/types'

type SectorFlags = Record<string, 'yellow' | 'double_yellow'>
type RaceControlLang = 'en' | 'ru'

export function formatLocalFlags(
  flags: SectorFlags | null | undefined,
  lang: RaceControlLang = 'en',
): string {
  return Object.entries(flags ?? {})
    .filter(([sector, severity]) => /^[1-9]\d*$/.test(sector) && (severity === 'yellow' || severity === 'double_yellow'))
    .sort(([left], [right]) => Number(left) - Number(right))
    .map(([sector, severity]) => (
      `${severity === 'double_yellow' ? (lang === 'ru' ? 'ДВОЙНОЙ ' : 'DOUBLE ') : ''}S${sector}`
    ))
    .join(' · ')
}

export function localYellowSummary(
  flags: SectorFlags | null | undefined,
  lang: RaceControlLang = 'en',
): string | null {
  const sectors = formatLocalFlags(flags, lang)
  const label = lang === 'ru' ? 'ЛОКАЛЬНЫЙ ЖЁЛТЫЙ' : 'LOCAL YELLOW'
  return sectors ? `${label} · ${sectors}` : null
}

export function finishSummary(
  condition: FinishCondition | null | undefined,
  lang: RaceControlLang = 'en',
): string {
  const chequered = lang === 'ru' ? 'КЛЕТЧАТЫЙ ФЛАГ' : 'CHEQUERED FLAG'
  if (!condition) return chequered
  const mode = {
    safety_car: lang === 'ru' ? 'МАШИНОЙ БЕЗОПАСНОСТИ' : 'SAFETY CAR',
    vsc: 'VSC',
    red_flag: lang === 'ru' ? 'КРАСНЫМ ФЛАГОМ' : 'RED FLAG',
    green: '',
  }[condition.control_mode]
  const sectors = formatLocalFlags(condition.sector_flags, lang)
  const description = mode || (sectors ? (lang === 'ru' ? 'ЛОКАЛЬНЫМ ЖЁЛТЫМ' : 'LOCAL YELLOW') : '')
  return description
    ? `${chequered} · ${lang === 'ru' ? 'ФИНИШ ПОД' : 'FINISHED UNDER'} ${description}${sectors ? ` · ${sectors}` : ''}`
    : chequered
}
