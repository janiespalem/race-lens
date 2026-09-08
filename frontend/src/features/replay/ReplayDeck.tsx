import type { Lang } from './replayTypes'
import { useMemo, type CSSProperties } from 'react'
import type { RaceMarker, Timeline } from '../../api/types'
import { formatRaceTime, lapAtTime } from '../../lib/format'
import type { LivePhase } from '../../lib/liveStatus'

type Speed = 1 | 5 | 10
const SPEEDS: Speed[] = [1, 5, 10]

type Props = {
  lang?: Lang
  timeline: Timeline | null
  atMs: number
  playing: boolean
  speed: Speed
  markers?: RaceMarker[]
  /** When false (live mode) the scrub rail is disabled and speed controls hidden. */
  canScrub?: boolean
  /** Session clock string shown in live mode (e.g. "LAP 42"). */
  liveLabel?: string | null
  livePhase?: LivePhase
  liveBadge?: string
  liveDetail?: string
  onScrub: (ms: number) => void
  onPlay: () => void
  onPause: () => void
  onSpeed: (s: Speed) => void
}

type PhaseKind = 'green' | 'red' | 'amber' | 'vsc'

type PhaseSegment = {
  kind: PhaseKind
  pct: number
  label?: string
}

function buildPhase(markers: RaceMarker[], startMs: number, endMs: number): PhaseSegment[] {
  if (endMs <= startMs) return [{ kind: 'green', pct: 100 }]

  const statusEvents: { ms: number; kind: PhaseKind; label?: string }[] = [
    { ms: startMs, kind: 'green' },
  ]

  for (const marker of markers) {
    if (marker.kind === 'RED_FLAG') statusEvents.push({ ms: marker.at_ms, kind: 'red', label: 'RF' })
    else if (marker.kind === 'VSC') statusEvents.push({ ms: marker.at_ms, kind: 'vsc', label: 'VSC' })
    else if (marker.kind === 'SAFETY_CAR') statusEvents.push({ ms: marker.at_ms, kind: 'amber', label: 'SC' })
    else if (marker.kind === 'GREEN') statusEvents.push({ ms: marker.at_ms, kind: 'green' })
  }

  statusEvents.sort((a, b) => a.ms - b.ms)
  statusEvents.push({ ms: endMs, kind: 'green' })

  const duration = endMs - startMs
  const segments: PhaseSegment[] = []

  for (let i = 0; i < statusEvents.length - 1; i++) {
    const seg = statusEvents[i]
    const next = statusEvents[i + 1]
    const segPct = ((next.ms - seg.ms) / duration) * 100
    if (segPct > 0) {
      segments.push({ kind: seg.kind, pct: segPct, label: seg.label })
    }
  }

  return segments.length > 0 ? segments : [{ kind: 'green', pct: 100 }]
}

export function ReplayDeck({ lang = 'en', timeline, atMs, playing, speed, markers = [], canScrub = true, liveLabel, livePhase = 'connecting', liveBadge = lang === 'ru' ? 'ПОДКЛЮЧЕНИЕ' : 'CONNECTING', liveDetail = lang === 'ru' ? 'ПОДКЛЮЧЕНИЕ К ХРОНОМЕТРАЖУ' : 'OPENING LIVE TIMING', onScrub, onPlay, onPause, onSpeed }: Props) {

  const startMs = timeline?.start_ms ?? 0
  const endMs = timeline?.end_ms ?? 0
  const duration = endMs - startMs || 1

  const progress = Math.min(Math.max((atMs - startMs) / duration, 0), 1)
  const cursorPct = progress * 100
  const phases = useMemo(() => {
    if (!timeline) return [{ kind: 'green' as const, pct: 100 }]
    return buildPhase(markers, startMs, endMs)
  }, [markers, startMs, endMs, timeline])

  // Clock is race-relative: 0:00 at lights-out. During the formation lap
  // (before lights-out) show a label instead of a time.
  const lightsOutMs = timeline?.lights_out_ms ?? 0
  const inFormation = atMs < lightsOutMs
  const positionLabel = !timeline
    ? (lang === 'ru' ? 'ПОЗИЦИЯ ПОВТОРА' : 'REPLAY POSITION')
    : inFormation
      ? (lang === 'ru' ? 'ПРОГРЕВОЧНЫЙ КРУГ' : 'FORMATION LAP')
      : `${lang === 'ru' ? 'КРУГ' : 'LAP'} ${lapAtTime(timeline, atMs)} · ${formatRaceTime(atMs - lightsOutMs)}`
  const visiblePhases = useMemo(() => {
    const result: { kind: PhaseKind; pct: number; key: string; neutral: boolean; label?: string }[] = []
    let accumulated = 0
    for (let i = 0; i < phases.length; i++) {
      const seg = phases[i]
      const segEnd = accumulated + seg.pct
      if (accumulated >= cursorPct) {
        result.push({ ...seg, key: `phase-${i}-future`, neutral: true })
      } else if (segEnd > cursorPct) {
        const pastPct = cursorPct - accumulated
        const futurePct = segEnd - cursorPct
        result.push({ kind: seg.kind, pct: pastPct, key: `phase-${i}-past`, neutral: false, label: seg.label })
        result.push({ kind: seg.kind, pct: futurePct, key: `phase-${i}-future`, neutral: true, label: seg.label })
      } else {
        result.push({ ...seg, key: `phase-${i}-whole`, neutral: false })
      }
      accumulated = segEnd
    }
    return result
  }, [phases, cursorPct])

  return (
    <div className={`deck${canScrub ? '' : ' deck-live'}`}>
      {canScrub ? (
        <div className="deck-shell">
          <button
            className={`deck-transport${playing ? ' is-playing' : ''}`}
            type="button"
            onClick={playing ? onPause : onPlay}
            disabled={!timeline}
            aria-label={lang === 'ru' ? (playing ? 'Приостановить повтор' : 'Воспроизвести повтор') : (playing ? 'Pause replay' : 'Play replay')}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              {playing
                ? <path d="M7 5h3v14H7zm7 0h3v14h-3z" />
                : <path d="m8 5 11 7-11 7z" />}
            </svg>
          </button>

          <div className="deck-timeline">
            <output className="deck-position">{positionLabel}</output>
            <div className="phase" aria-hidden="true">
              {visiblePhases.map((seg) => {
                let cls: string
                if (seg.neutral) {
                  cls = 'ph-neutral'
                } else if (seg.kind === 'red') {
                  cls = 'ph-r'
                } else if (seg.kind === 'amber') {
                  cls = 'ph-s'
                } else if (seg.kind === 'vsc') {
                  cls = 'ph-vsc'
                } else {
                  cls = 'ph-g'
                }
                return (
                  <i
                    key={seg.key}
                    className={cls}
                    style={{ width: `${seg.pct}%` }}
                    title={seg.neutral ? undefined : seg.label}
                  />
                )
              })}
            </div>
            <input
              className="deck-range"
              type="range"
              min={startMs}
              max={endMs}
              step={1000}
              value={atMs}
              style={{ '--deck-progress': `${cursorPct}%` } as CSSProperties}
              onChange={(event) => onScrub(Number(event.currentTarget.value))}
              disabled={!timeline}
              aria-label={(lang === 'ru' ? 'Позиция повтора' : 'Replay position')}
              aria-valuetext={positionLabel}
            />
          </div>

          <div className="deck-options">
            <div className="deck-speeds" role="group" aria-label={lang === 'ru' ? 'Скорость повтора' : 'Replay speed'}>
              {SPEEDS.map((s) => (
                <button
                  key={s}
                  type="button"
                  className={`deck-speed${speed === s ? ' on' : ''}`}
                  onClick={() => onSpeed(s)}
                  aria-pressed={speed === s}
                >
                  {s}×
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : (
        <div className="deck-live-state" role="status">
          <span className={`live-badge live-badge--${livePhase}`}>{liveBadge}</span>
          <strong>{liveLabel ?? liveDetail}</strong>
          <small>{liveLabel ? liveDetail : (lang === 'ru' ? 'ВОСПРОИЗВЕДЕНИЕ ВПЕРЁД · БЕЗ ПЕРЕМОТКИ' : 'PLAY-FORWARD · NO REPLAY SCRUB')}</small>
        </div>
      )}
    </div>
  )
}
