import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getHighlights } from '../../api/client'
import type { Highlight, HighlightsResponse } from '../../api/types'
import { formatRaceTime } from '../../lib/format'
import { useAsync } from './useAsync'

type Lang = 'en' | 'ru'

type Props = {
  sessionId: string
  lang?: Lang
  untilMs?: number
  onSeek: (ms: number) => void
}

const KIND_ICON: Record<string, string> = {
  RED_FLAG: '🚩',
  SAFETY_CAR: '🟡',
  VSC: '🟡',
  GREEN: '🟢',
  CRASH: '💥',
  INCIDENT: '⚠',
  PENALTY: '⬛',
  LEAD_CHANGE: '⬆',
  PODIUM_CHANGE: '↕',
  FASTEST_LAP: '🟣',
  OVERTAKE: '⏩',
  UNDERCUT: '⤵',
}

const KIND_COLOR: Record<string, string> = {
  RED_FLAG: '#cc2222',
  SAFETY_CAR: '#f2a900',
  VSC: '#f2a900',
  GREEN: '#22cc44',
  CRASH: '#cc2222',
  INCIDENT: '#cc2222',
  PENALTY: '#f2a900',
  LEAD_CHANGE: '#ffffff',
  PODIUM_CHANGE: '#aaaacc',
  FASTEST_LAP: '#b388ff',
  OVERTAKE: '#00d2be',
  UNDERCUT: '#00d2be',
}

export function HighlightsPanel({ sessionId, lang = 'en', untilMs, onSeek }: Props) {
  const [open, setOpen] = useState(false)
  const [cutoffMs, setCutoffMs] = useState<number | undefined>(undefined)
  const [playing, setPlaying] = useState(false)
  const playRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const playIdxRef = useRef(0)

  const { data, loading } = useAsync<HighlightsResponse>(
    () => getHighlights(sessionId, 8, cutoffMs),
    [sessionId, cutoffMs],
    open,
  )
  const highlights: Highlight[] = useMemo(
    () => (data?.highlights ?? []).filter((highlight) => untilMs === undefined || highlight.at_ms <= untilMs),
    [data, untilMs],
  )

  const stopPlay = useCallback(() => {
    if (playRef.current) clearTimeout(playRef.current)
    playRef.current = null
    setPlaying(false)
    playIdxRef.current = 0
  }, [])

  // Stop playback when closed or session changes
  useEffect(() => {
    stopPlay()
  }, [sessionId, stopPlay])

  const startPlay = useCallback(() => {
    if (highlights.length === 0) return
    setPlaying(true)
    playIdxRef.current = 0

    const step = (idx: number) => {
      if (idx >= highlights.length) {
        setPlaying(false)
        return
      }
      onSeek(highlights[idx].at_ms)
      playIdxRef.current = idx
      playRef.current = setTimeout(() => step(idx + 1), 4000)
    }
    step(0)
  }, [highlights, onSeek])

  useEffect(() => {
    return () => { if (playRef.current) clearTimeout(playRef.current) }
  }, [])

  const toggle = () => {
    if (open) {
      stopPlay()
      setOpen(false)
      return
    }
    setCutoffMs(untilMs)
    setOpen(true)
  }

  return (
    <div className="hl-wrap">
      <button
        type="button"
        className={`tog${open ? ' tog-on' : ''}`}
        onClick={toggle}
        title={lang === 'ru' ? 'Главные моменты — гонка за 60 секунд' : 'Highlight reel — race in 60 seconds'}
      >
        {lang === 'ru' ? 'ГЛАВНЫЕ МОМЕНТЫ' : 'HIGHLIGHTS'}
      </button>

      {open && (
        <div className="hl-panel">
          <div className="hl-header">
            <span className="hl-title">{lang === 'ru' ? 'ГЛАВНЫЕ МОМЕНТЫ К ЭТОМУ ВРЕМЕНИ' : 'HIGHLIGHTS SO FAR'}</span>
            {!playing ? (
              <button
                type="button"
                className="b primary hl-play-btn"
                disabled={loading || highlights.length === 0}
                onClick={startPlay}
              >
                {lang === 'ru' ? 'СМОТРЕТЬ МОМЕНТЫ' : 'PLAY HIGHLIGHTS'}
              </button>
            ) : (
              <button
                type="button"
                className="b danger hl-play-btn"
                onClick={stopPlay}
              >
                {lang === 'ru' ? 'СТОП' : 'STOP'}
              </button>
            )}
          </div>

          {loading && <div className="hl-empty">{lang === 'ru' ? 'Загрузка…' : 'Loading…'}</div>}

          {!loading && highlights.length === 0 && (
            <div className="hl-empty">{lang === 'ru' ? 'Главных моментов пока нет' : 'No highlights available'}</div>
          )}

          <div className="hl-list">
            {highlights.map((h, i) => {
              const color = KIND_COLOR[h.kind] ?? '#888899'
              const icon = KIND_ICON[h.kind] ?? '·'
              const title = lang === 'ru' ? h.title_ru : h.title_en
              const isActive = playing && playIdxRef.current === i
              return (
                <button
                  key={`${h.at_ms}-${h.kind}`}
                  type="button"
                  className={`hl-item${isActive ? ' hl-item-active' : ''}`}
                  onClick={() => onSeek(h.at_ms)}
                  title={lang === 'ru' ? `Перейти к ${formatRaceTime(h.at_ms)}` : `Seek to ${formatRaceTime(h.at_ms)}`}
                >
                  <span className="hl-icon" style={{ color }}>{icon}</span>
                  <span className="hl-time">{formatRaceTime(h.at_ms)}</span>
                  {h.lap != null && <span className="hl-lap">{lang === 'ru' ? 'К' : 'L'}{h.lap}</span>}
                  <span className="hl-text">{title}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
