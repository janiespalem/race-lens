/**
 * EventSource (SSE) playback stream. Owns the connection lifecycle and pushes
 * each streamed frame into shared state via the setters bundle.
 *
 * The caller supplies a `getStreamUrl` factory so replay and live can share
 * this hook without duplicating SSE logic.
 */
import { useCallback, useRef } from 'react'
import { getBattles, getCommentary, getFeed, getLiveFeed } from '../../api/client'
import type { RaceState } from '../../api/types'
import type { Lang, Level, Speed } from './replayTypes'
import type { ReplaySetters } from './replaySetters'

export type StreamUrlFactory = (speed: Speed, atMs: number, lang: Lang, level: Level) => string

export function useReplayStream(
  /** null = no active session, stream stays closed */
  active: boolean,
  /** Factory returns the SSE URL; called each time we (re-)open. */
  getStreamUrl: StreamUrlFactory,
  /** Used for side-data (feed/battles/commentary) — null in live mode with no sessionId. */
  sessionId: string | null,
  set: ReplaySetters,
) {
  const sourceRef = useRef<EventSource | null>(null)
  const streamSeqRef = useRef(0)
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Throttle ONLY the network side-data (feed / battles / commentary) so we don't
  // flood the API at high speed. The table + scrubber + map are NOT throttled —
  // they must render the same frame, else the tower lags the track by up to
  // DATA_THROTTLE_MS * speed of session time (the map/table desync bug).
  const lastDataRef = useRef(0)
  const pendingRef = useRef<RaceState | null>(null)
  const trailRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const DATA_THROTTLE_MS = 600

  const closeStream = useCallback(() => {
    streamSeqRef.current++
    if (reconnectRef.current !== null) {
      clearTimeout(reconnectRef.current)
      reconnectRef.current = null
    }
    sourceRef.current?.close()
    sourceRef.current = null
    pendingRef.current = null
    if (trailRef.current) {
      clearTimeout(trailRef.current)
      trailRef.current = null
    }
  }, [])

  const openStream = useCallback(
    (nextSpeed: Speed, startMs: number, nextLang: Lang, nextLevel: Level) => {
      if (!active) return
      closeStream()
      set.setError(null)
      set.setPlaying(true)

      const seq = streamSeqRef.current
      let resumeMs = startMs
      const connect = () => {
        if (streamSeqRef.current !== seq) return
        const url = getStreamUrl(nextSpeed, resumeMs, nextLang, nextLevel)
        const source = new EventSource(url)
        const isCurrent = () => streamSeqRef.current === seq && sourceRef.current === source
        sourceRef.current = source
        source.onopen = () => { if (isCurrent()) set.setError(null) }

        // Network side-data (feed / battles / commentary). Throttled — these are
        // API round-trips, not the on-screen frame. The table/map are NOT here.
        const flushSideData = (st: RaceState) => {
          lastDataRef.current = performance.now()
          const ms = st.at_ms
          if (sessionId) {
            void getBattles(sessionId, ms)
              .then((r) => { if (isCurrent()) set.setBattles(r.battles) })
              .catch(() => undefined)
            void getFeed(sessionId, ms, 30, nextLang)
              .then((r) => {
                if (isCurrent()) { set.setFeed(r.items); set.setFeedError(null) }
              })
              .catch((err: unknown) => {
                if (isCurrent())
                  set.setFeedError(err instanceof Error ? err.message : 'Feed unavailable')
              })
            void getCommentary(sessionId, ms, nextLang, nextLevel)
              .then((r) => { if (isCurrent()) set.setCommentary(r.items) })
              .catch(() => undefined)
          } else {
            // Live mode has no session_id to scope by — feed comes from the live
            // engine's own event log. Commentary stays replay-only. Battles are
            // embedded directly in every live frame (see onmessage) — no REST
            // round-trip needed, so they are NOT fetched here.
            void getLiveFeed(30, nextLang)
              .then((r) => {
                if (isCurrent()) { set.setFeed(r.items); set.setFeedError(null) }
              })
              .catch((err: unknown) => {
                if (isCurrent())
                  set.setFeedError(err instanceof Error ? err.message : 'Feed unavailable')
              })
          }
        }

        source.onmessage = (event) => {
          if (!isCurrent()) return
          const raw = event.data as string
          // Live stream can send empty heartbeat `{}` while no data yet
          if (!raw || raw === '{}') return
          let nextState: RaceState
          try {
            nextState = JSON.parse(raw) as RaceState
          } catch {
            if (isCurrent()) set.setError('Stream sent invalid data')
            return
          }
          if (typeof nextState !== 'object' || nextState === null || Array.isArray(nextState)) {
            if (isCurrent()) set.setError('Stream sent invalid data')
            return
          }
          // at_ms can legitimately be 0 (live joins anchor early events at t=0).
          if (!Number.isFinite(nextState.at_ms)) return
          resumeMs = nextState.at_ms
          set.setError(null)
          // Table, scrubber and map all read the SAME frame — push every frame so
          // the tower can never lag the track. rank is stable (Step 1), so the
          // FLIP tower no longer strobes when updated per frame.
          set.setAtMs(nextState.at_ms)
          set.setState(nextState)
          set.setInsights(nextState.active_insights ?? [])
          set.setCommentary(nextState.commentary ?? [])
          // recent_passes drives the on-map overtake flash — pushed every frame
          // (not throttled) so the flash triggers promptly, same as insights.
          set.setRecentPasses(nextState.recent_passes ?? [])
          if (!sessionId) {
            // Live: the backend embeds battles in every frame — read them straight
            // off the frame instead of a throttled REST round-trip (replay path below).
            set.setBattles(nextState.battles ?? [])
          }
          // Only the network side-data is throttled (leading + trailing).
          pendingRef.current = nextState
          const elapsed = performance.now() - lastDataRef.current
          if (elapsed >= DATA_THROTTLE_MS) {
            if (trailRef.current) { clearTimeout(trailRef.current); trailRef.current = null }
            flushSideData(nextState)
          } else if (!trailRef.current) {
            trailRef.current = setTimeout(() => {
              trailRef.current = null
              if (pendingRef.current) flushSideData(pendingRef.current)
            }, DATA_THROTTLE_MS - elapsed)
          }
        }

        source.addEventListener('end', () => {
          if (!isCurrent()) return
          closeStream()
          set.setPlaying(false)
        })

        source.onerror = () => {
          if (!isCurrent()) return
          // Native reconnect reuses the original from_ms. Retire this transport
          // (including its side-data callbacks) and resume from the accepted frame.
          source.close()
          sourceRef.current = null
          pendingRef.current = null
          if (trailRef.current !== null) {
            clearTimeout(trailRef.current)
            trailRef.current = null
          }
          set.setError('Stream disconnected · reconnecting')
          reconnectRef.current = setTimeout(() => {
            if (streamSeqRef.current !== seq) return
            reconnectRef.current = null
            connect()
          }, 1500)
        }
      }
      connect()
    },
    [active, closeStream, getStreamUrl, sessionId, set],
  )

  return { closeStream, openStream }
}
