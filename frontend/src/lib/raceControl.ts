import type { FinishCondition } from '../api/types'

type SectorFlags = Record<string, 'yellow' | 'double_yellow'>

export function formatLocalFlags(flags: SectorFlags | null | undefined): string {
  return Object.entries(flags ?? {})
    .filter(([sector, severity]) => /^[1-9]\d*$/.test(sector) && (severity === 'yellow' || severity === 'double_yellow'))
    .sort(([left], [right]) => Number(left) - Number(right))
    .map(([sector, severity]) => `${severity === 'double_yellow' ? 'DOUBLE ' : ''}S${sector}`)
    .join(' · ')
}

export function localYellowSummary(flags: SectorFlags | null | undefined): string | null {
  const sectors = formatLocalFlags(flags)
  return sectors ? `LOCAL YELLOW · ${sectors}` : null
}

export function finishSummary(condition: FinishCondition | null | undefined): string {
  if (!condition) return 'CHEQUERED FLAG'
  const mode = {
    safety_car: 'SAFETY CAR',
    vsc: 'VSC',
    red_flag: 'RED FLAG',
    green: '',
  }[condition.control_mode]
  const sectors = formatLocalFlags(condition.sector_flags)
  const description = mode || (sectors ? 'LOCAL YELLOW' : '')
  return description
    ? `CHEQUERED FLAG · FINISHED UNDER ${description}${sectors ? ` · ${sectors}` : ''}`
    : 'CHEQUERED FLAG'
}
