/**
 * DataSource — minimal abstraction that tells hooks which backend to target.
 *
 * kind='replay' → /api/sessions/{id}/* endpoints (scrub, stream by speed)
 * kind='live'   → /api/live/* endpoints (stream only, no scrub)
 *
 * Keeping this as a plain discriminated union avoids class hierarchy overhead
 * and lets the hooks branch on a single field.
 */
import { liveStreamUrl, streamUrl as replayStreamUrl } from './client'
import type { Lang, Level, Speed } from '../features/replay/replayTypes'

export type DataSource =
  | { kind: 'replay'; sessionId: string }
  | { kind: 'live' }

/** Build the SSE stream URL appropriate for the data source. */
export function buildStreamUrl(source: DataSource, lang: Lang, level: Level, speed?: Speed, atMs?: number): string {
  if (source.kind === 'live') {
    return liveStreamUrl(lang, level, 2)
  }
  const s = speed ?? 10
  const tick = s === 1 ? 500 : s === 5 ? 1000 : 2000
  return replayStreamUrl(source.sessionId, s, atMs ?? 0, tick, lang, level)
}
