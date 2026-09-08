import type { FeedItem, RaceMarker } from '../api/types'

export type BroadcastCandidate = {
  id: string
  atMs: number
  lap: number | null
  tone: 'race' | 'strategy' | 'incident' | 'radio'
  priority: number
  kicker: string
  title: string
}

export const BROADCAST_DISPLAY_MS = 4_500
export const BROADCAST_EXIT_MS = 300

export function canPresentBroadcastCandidate(
  candidateId: string | null,
  dismissedId: string | null,
): boolean {
  return candidateId !== null && candidateId !== dismissedId
}

type Args = {
  atMs: number
  playing: boolean
  speed: number
  lang: 'en' | 'ru'
  markers: RaceMarker[]
  feed: FeedItem[]
}

const MARKER_PRIORITY: Partial<Record<RaceMarker['kind'], number>> = {
  RED_FLAG: 6,
  CRASH: 6,
  SAFETY_CAR: 5,
  VSC: 5,
  INCIDENT: 5,
  OFF_TRACK: 5,
  PENALTY: 4,
  LEAD_CHANGE: 3,
  OVERTAKE: 3,
  UNDERCUT: 2,
}

const MARKER_LABEL_RU: Partial<Record<RaceMarker['kind'], string>> = {
  RED_FLAG: 'КРАСНЫЙ ФЛАГ', CRASH: 'АВАРИЯ', SAFETY_CAR: 'МАШИНА БЕЗОПАСНОСТИ', VSC: 'VSC',
  INCIDENT: 'ИНЦИДЕНТ', OFF_TRACK: 'ВЫЕЗД С ТРАССЫ', PENALTY: 'ШТРАФ', LEAD_CHANGE: 'СМЕНА ЛИДЕРА',
  OVERTAKE: 'ОБГОН', UNDERCUT: 'АНДЕРКАТ',
}

export function selectBroadcastCandidate({
  atMs, playing, speed, lang, markers, feed,
}: Args): BroadcastCandidate | null {
  const candidates: BroadcastCandidate[] = []
  for (const marker of markers) {
    const priority = MARKER_PRIORITY[marker.kind]
    if (!priority) continue
    const tone = priority >= 4
      ? 'incident'
      : marker.kind === 'UNDERCUT' ? 'strategy' : 'race'
    candidates.push({
      id: `marker:${marker.kind}:${marker.at_ms}:${marker.driver_ids.join(':')}`,
      atMs: marker.at_ms,
      lap: marker.lap,
      tone,
      priority,
      kicker: lang === 'ru' ? (MARKER_LABEL_RU[marker.kind] ?? 'СОБЫТИЕ ГОНКИ') : marker.kind.replaceAll('_', ' '),
      title: lang === 'ru' ? marker.text_ru : marker.text_en,
    })
  }
  for (const item of feed) {
    const radio = Boolean(item.audio_url) || item.text.startsWith('RADIO:')
    if (item.tag !== 'PIT' && !radio) continue
    candidates.push({
      id: item.id,
      atMs: item.at_ms,
      lap: item.lap,
      tone: radio ? 'radio' : 'strategy',
      priority: radio ? 1 : 2,
      kicker: radio
        ? (lang === 'ru'
          ? `РАДИО КОМАНДЫ${item.transcript ? ' · АВТОРАСШИФРОВКА' : ''}`
          : `TEAM RADIO${item.transcript ? ' · MACHINE TRANSCRIPT' : ''}`)
        : (lang === 'ru' ? 'ПИТ-СТРАТЕГИЯ' : 'PIT STRATEGY'),
      title: radio && item.transcript ? item.transcript : item.text,
    })
  }

  const windowMs = playing ? 8_000 * speed : 12_000
  return candidates
    .filter((item) => item.atMs <= atMs && atMs - item.atMs <= windowMs)
    .sort((a, b) => b.priority - a.priority || b.atMs - a.atMs)[0] ?? null
}
