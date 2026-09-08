import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getCommentary, getFeed, getMarkers, getTimeline } from '../../api/client'
import { apiUrl } from '../../api/url'
import type { Battle, CommentaryItem, FeedItem, Insight, RaceMarker, RaceState, RecentPass, Timeline } from '../../api/types'
import type { DataSource } from '../../api/dataSource'
import { buildStreamUrl } from '../../api/dataSource'
import type { PositionsData } from '../../lib/liveGaps'
import { isRetryablePositionsError } from '../../lib/liveTrack'
import { LANG_KEY, LEVEL_KEY, NEUTRAL_STATUSES, readLang, readLevel, writePersisted } from './replayTypes'
import type { Lang, Level, Speed } from './replayTypes'
import type { ReplaySetters } from './replaySetters'
import { useGreenFlag } from './useGreenFlag'
import { useReplayStream } from './useReplayStream'
import { useSnapshotLoader } from './useSnapshotLoader'

/** Debounce before loading a snapshot while the user is scrubbing. */
const SCRUB_DEBOUNCE_MS = 150
const POSITIONS_RETRY_MS = 3_000

export type { Lang, Level } from './replayTypes'

export type ReplayModel = {
  state: RaceState | null
  insights: Insight[]
  battles: Battle[]
  /** Overtakes in the last ~20s of session time — drives the on-map overtake flash. */
  recentPasses: RecentPass[]
  feed: FeedItem[]
  commentary: CommentaryItem[]
  timeline: Timeline | null
  /** All race markers for the session (full race, no until_ms filter). Filtered by atMs when spoilerFree. */
  markers: RaceMarker[]
  playing: boolean
  speed: Speed
  atMs: number
  loading: boolean
  error: string | null
  feedError: string | null
  lang: Lang
  level: Level
  /** Positions telemetry data (null if not available for this session). */
  positionsData: PositionsData | null
  /** True when the green flag strip should be shown. */
  greenFlag: boolean
  /** Text to display in the green flag strip. */
  greenFlagText: string
  /** session_time_ms of the event that established the current session_status (from backend state). */
  neutralizationStartMs: number | null
  /** Whether scrubbing is available (replay only — live is play-forward only). */
  canScrub: boolean
  scrub: (atMs: number) => void
  play: () => void
  pause: () => void
  setSpeed: (speed: Speed) => void
  setLang: (lang: Lang) => void
  setLevel: (level: Level) => void
}

/**
 * Core replay/live hook. Accepts a DataSource that determines whether we're
 * replaying a recorded session or tracking a live one.
 *
 * Replay: full scrub/speed controls, snapshot loader, timeline.
 * Live:   stream-only, scrub disabled, no timeline, speed controls hidden.
 */
export const useReplay = (source: DataSource | null): ReplayModel => {
  const [state, setState] = useState<RaceState | null>(null)
  const [insights, setInsights] = useState<Insight[]>([])
  const [battles, setBattles] = useState<Battle[]>([])
  const [recentPasses, setRecentPasses] = useState<RecentPass[]>([])
  const [feed, setFeed] = useState<FeedItem[]>([])
  const [commentary, setCommentary] = useState<CommentaryItem[]>([])
  const [timeline, setTimeline] = useState<Timeline | null>(null)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeedValue] = useState<Speed>(10)
  const [atMs, setAtMs] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [feedError, setFeedError] = useState<string | null>(null)
  const [lang, setLangState] = useState<Lang>(readLang)
  const [level, setLevelState] = useState<Level>(readLevel)
  const [positionsData, setPositionsData] = useState<PositionsData | null>(null)
  const [markers, setMarkers] = useState<RaceMarker[]>([])
  const scrubTimeoutRef = useRef<number | null>(null)
  const languageRequestSeq = useRef(0)
  const positionsRequestSeq = useRef(0)
  const positionsLoadingRef = useRef(false)
  const positionsRetryRef = useRef<number | null>(null)

  const cancelPositionsRequest = useCallback(() => {
    positionsRequestSeq.current++
    positionsLoadingRef.current = false
    if (positionsRetryRef.current !== null) {
      window.clearTimeout(positionsRetryRef.current)
      positionsRetryRef.current = null
    }
  }, [])

  const set = useMemo<ReplaySetters>(() => ({
    setState, setInsights, setBattles, setRecentPasses, setFeed, setCommentary,
    setAtMs, setLoading, setError, setFeedError, setPlaying,
  }), [])

  // Derive stable values from source
  const isReplay = source?.kind === 'replay'
  const sessionId = source?.kind === 'replay' ? source.sessionId : null
  const active = source !== null

  // Stream URL factory — stable per source+lang+level to avoid spurious reconnects
  const getStreamUrl = useCallback(
    (s: Speed, ms: number, l: Lang, lv: Level) => {
      if (!source) return ''
      return buildStreamUrl(source, l, lv, s, ms)
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [source?.kind, sessionId],
  )

  const { loadSnapshot, cancelSnapshot } = useSnapshotLoader(sessionId, set)
  const { closeStream, openStream: startStream } = useReplayStream(active, getStreamUrl, sessionId, set)
  const openStream = useCallback((s: Speed, ms: number, l: Lang, lv: Level) => {
    cancelSnapshot()
    if (scrubTimeoutRef.current !== null) {
      window.clearTimeout(scrubTimeoutRef.current)
      scrubTimeoutRef.current = null
    }
    startStream(s, ms, l, lv)
  }, [cancelSnapshot, startStream])
  const { greenFlag, greenFlagText } = useGreenFlag(atMs, markers, timeline?.lights_out_ms ?? 0)

  const loadPositionsWindow = useCallback((sid: string, centerMs: number, supersedeRetry = false) => {
    if (positionsLoadingRef.current) return
    if (positionsRetryRef.current !== null) {
      if (!supersedeRetry) return
      window.clearTimeout(positionsRetryRef.current)
      positionsRetryRef.current = null
    }
    const seq = ++positionsRequestSeq.current
    const url = apiUrl(`/api/sessions/${encodeURIComponent(sid)}/positions?at_ms=${Math.max(0, Math.round(centerMs))}`)
    const attempt = () => {
      positionsLoadingRef.current = true
      fetch(url)
        .then((response) => {
          if (response.ok) return response.json() as Promise<PositionsData>
          if (response.status >= 500) throw new Error(`${response.status} positions unavailable`)
          return null
        })
        .then((data) => {
          if (seq !== positionsRequestSeq.current) return
          setPositionsData(data)
          positionsLoadingRef.current = false
        })
        .catch((error: unknown) => {
          if (seq !== positionsRequestSeq.current) return
          positionsLoadingRef.current = false
          if (!isRetryablePositionsError(error)) {
            setPositionsData(null)
            return
          }
          positionsRetryRef.current = window.setTimeout(() => {
            positionsRetryRef.current = null
            attempt()
          }, POSITIONS_RETRY_MS)
        })
    }
    attempt()
  }, [])

  // Source change: reset everything, then load appropriately
  useEffect(() => {
    languageRequestSeq.current++
    cancelPositionsRequest()
    if (scrubTimeoutRef.current !== null) {
      window.clearTimeout(scrubTimeoutRef.current)
      scrubTimeoutRef.current = null
    }
    closeStream()
    setPlaying(false)
    setState(null)
    setInsights([])
    setBattles([])
    setRecentPasses([])
    setFeed([])
    setCommentary([])
    setTimeline(null)
    setAtMs(0)
    setError(null)
    setFeedError(null)
    setPositionsData(null)
    setMarkers([])
    if (!source) return

    if (source.kind === 'live') {
      // Live: open stream immediately (backend already started by UI)
      openStream(speed, 0, lang, level)
      return () => { closeStream() }
    }

    // Replay path: load timeline + first snapshot
    const sid = source.sessionId
    let cancelled = false

    // Fetch markers for full race (non-critical, best-effort)
    getMarkers(sid)
      .then((r) => { if (!cancelled) setMarkers(r.markers) })
      .catch(() => { if (!cancelled) setMarkers([]) })

    setLoading(true)
    getTimeline(sid)
      .then((nextTimeline) => {
        if (cancelled) return undefined
        setTimeline(nextTimeline)
        setAtMs(nextTimeline.start_ms)
        loadPositionsWindow(sid, nextTimeline.start_ms)
        return loadSnapshot(nextTimeline.start_ms, lang, level)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Could not load replay timeline')
        setLoading(false)
      })

    return () => {
      cancelled = true
      cancelPositionsRequest()
      if (scrubTimeoutRef.current !== null) {
        window.clearTimeout(scrubTimeoutRef.current)
        scrubTimeoutRef.current = null
      }
      closeStream()
    }
    // lang/level intentionally NOT in deps — session change resets; lang/level trigger own effect
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source?.kind, sessionId])

  // Refresh the small telemetry window before playback reaches its end, or
  // immediately after a scrub outside the current window.
  useEffect(() => {
    if (!sessionId || !positionsData || positionsData.session_id !== sessionId) return
    const frameCount = Math.max(
      0,
      ...Object.values(positionsData.drivers).map((frames) => frames.length),
    )
    if (frameCount === 0) return
    const windowEndMs = positionsData.start_ms + (frameCount - 1) * positionsData.tick_ms
    if (atMs < positionsData.start_ms || atMs >= windowEndMs - 30_000) {
      loadPositionsWindow(sessionId, atMs, !playing)
    }
  }, [atMs, loadPositionsWindow, playing, positionsData, sessionId])

  // Re-fetch feed + commentary when lang/level change (without resetting position).
  useEffect(() => {
    const seq = ++languageRequestSeq.current
    if (sessionId) {
      setFeedError(null)
      getFeed(sessionId, atMs, 30, lang)
        .then((r) => {
          if (seq === languageRequestSeq.current) {
            setFeed(r.items)
            setFeedError(null)
          }
        })
        .catch((err: unknown) => {
          if (seq === languageRequestSeq.current)
            setFeedError(err instanceof Error ? err.message : 'Feed unavailable')
        })
      getCommentary(sessionId, atMs, lang, level)
        .then((r) => {
          if (seq === languageRequestSeq.current) setCommentary(r.items)
        })
        .catch(() => undefined)
    }

    // Reopen replay or live stream with the new language/detail settings.
    if (playing) {
      openStream(speed, atMs, lang, level)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lang, level])

  const scrub = useCallback(
    (nextAtMs: number) => {
      if (!isReplay) return // live: scrub disabled
      closeStream()
      cancelSnapshot()
      setPlaying(false)
      setAtMs(nextAtMs)
      setRecentPasses([]) // stream-only data — stale once we jump off the live frame sequence
      if (scrubTimeoutRef.current !== null) window.clearTimeout(scrubTimeoutRef.current)
      scrubTimeoutRef.current = window.setTimeout(() => {
        scrubTimeoutRef.current = null
        if (sessionId && !positionsData) loadPositionsWindow(sessionId, nextAtMs, true)
        void loadSnapshot(nextAtMs, lang, level)
      }, SCRUB_DEBOUNCE_MS)
    },
    [cancelSnapshot, closeStream, isReplay, lang, level, loadPositionsWindow, loadSnapshot, positionsData, sessionId],
  )

  const play = useCallback(() => {
    if (!source) return
    openStream(speed, atMs, lang, level)
  }, [atMs, lang, level, openStream, source, speed])

  const pause = useCallback(() => {
    closeStream()
    setPlaying(false)
  }, [closeStream])

  const setSpeed = useCallback(
    (nextSpeed: Speed) => {
      setSpeedValue(nextSpeed)
      if (playing && isReplay) openStream(nextSpeed, atMs, lang, level)
    },
    [atMs, isReplay, lang, level, openStream, playing],
  )

  const setLang = useCallback((nextLang: Lang) => {
    writePersisted(LANG_KEY, nextLang)
    setLangState(nextLang)
  }, [])

  const setLevel = useCallback((nextLevel: Level) => {
    writePersisted(LEVEL_KEY, nextLevel)
    setLevelState(nextLevel)
  }, [])

  // Derive neutralization start from backend state (works correctly after scrub)
  const neutralizationStartMs =
    state !== null && NEUTRAL_STATUSES.has(state.session_status)
      ? state.status_since_ms
      : null

  return {
    state, insights, battles, recentPasses, feed, commentary, timeline, markers,
    playing, speed, atMs, loading, error, feedError,
    lang, level, positionsData,
    greenFlag, greenFlagText, neutralizationStartMs,
    canScrub: isReplay,
    scrub, play, pause, setSpeed, setLang, setLevel,
  }
}
