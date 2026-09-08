import type { Lang } from '../features/replay/replayTypes'
import type { Timeline } from '../api/types'

export const formatRaceTime = (ms: number) => {
  const totalSeconds = Math.floor(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

export const lapAtTime = (timeline: Timeline | null, atMs: number): number => {
  if (!timeline || atMs < timeline.lights_out_ms) return 0
  const marks = Object.values(timeline.lap_marks)
  return Math.min(marks.filter((mark) => mark <= atMs).length + 1, marks.length || 1)
}

export const formatLapTime = (ms: number | null | undefined) => {
  if (!ms || ms <= 0) return '—'
  const minutes = Math.floor(ms / 60000)
  const seconds = Math.floor((ms % 60000) / 1000)
  const millis = Math.floor(ms % 1000)
  return `${minutes}:${String(seconds).padStart(2, '0')}.${String(millis).padStart(3, '0')}`
}

export const sessionLabel = (sessionId: string, lang: Lang = 'en'): string => {
  const session = sessionMeta(sessionId)
  return `${session.event} ${session.year} — ${sessionTypeLabel(session.type, lang)}`
}

const title = (value: string) => value.charAt(0).toUpperCase() + value.slice(1)

const SESSION_TYPES: Record<string, string> = {
  fp1: 'Practice 1', fp2: 'Practice 2', fp3: 'Practice 3',
  practice_1: 'Practice 1', practice_2: 'Practice 2', practice_3: 'Practice 3',
  q: 'Qualifying', qualifying: 'Qualifying', sq: 'Sprint Qualifying',
  sprint_qualifying: 'Sprint Qualifying', sprint: 'Sprint', r: 'Race', race: 'Race',
}

const SESSION_TYPES_RU: Record<string, string> = {
  'Practice 1': 'Практика 1', 'Practice 2': 'Практика 2', 'Practice 3': 'Практика 3',
  Qualifying: 'Квалификация', 'Sprint Qualifying': 'Спринт-квалификация',
  Sprint: 'Спринт', Race: 'Гонка',
}

export const sessionTypeLabel = (type: string, lang: Lang = 'en'): string => {
  const label = SESSION_TYPES[type.toLowerCase().replaceAll(' ', '_')] ?? type.split('_').map(title).join(' ')
  return lang === 'ru' ? SESSION_TYPES_RU[label] ?? label : label
}

export const sessionMeta = (sessionId: string) => {
  const parts = sessionId.split('_')
  const yearIndex = parts.findIndex((part) => /^\d{4}$/.test(part))
  if (yearIndex <= 0 || yearIndex === parts.length - 1) {
    return { year: '', event: sessionId.split('_').map(title).join(' '), type: '' }
  }
  return {
    year: parts[yearIndex],
    event: parts.slice(0, yearIndex).map(title).join(' '),
    type: parts.slice(yearIndex + 1).join('_'),
  }
}
