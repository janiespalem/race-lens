/**
 * REST snapshot loading for scrub / initial load. Fetches state + insights as a
 * critical batch, then feed/battles/commentary as best-effort. Uses a request
 * sequence guard so stale responses never overwrite newer ones.
 */
import { useCallback, useLayoutEffect, useRef } from 'react'
import { getBattles, getCommentary, getFeed, getInsights, getState } from '../../api/client'
import type { Insight, RaceState } from '../../api/types'
import type { Lang, Level } from './replayTypes'
import type { ReplaySetters } from './replaySetters'

export function useSnapshotLoader(sessionId: string | null, set: ReplaySetters) {
  const requestSeq = useRef(0)

  useLayoutEffect(() => {
    requestSeq.current++
  }, [sessionId])

  const cancelSnapshot = useCallback(() => {
    requestSeq.current++
    set.setLoading(false)
  }, [set])

  const loadSnapshot = useCallback(
    async (nextAtMs: number, nextLang: Lang, nextLevel: Level) => {
      if (!sessionId) return
      const seq = ++requestSeq.current
      set.setLoading(true)
      set.setError(null)
      set.setFeedError(null)

      let nextState: RaceState
      let nextInsights: { insights: Insight[] }
      try {
        ;[nextState, nextInsights] = await Promise.all([
          getState(sessionId, nextAtMs),
          getInsights(sessionId, nextAtMs),
        ])
      } catch (err) {
        if (seq !== requestSeq.current) return
        set.setError(err instanceof Error ? err.message : 'Could not load replay state')
        set.setLoading(false)
        return
      }

      if (seq !== requestSeq.current) return
      // Update atomically — never clear before new data arrives to avoid flicker
      set.setState(nextState)
      set.setInsights(nextInsights.insights)
      set.setAtMs(nextState.at_ms)

      const ms = nextState.at_ms
      const feedProm = getFeed(sessionId, ms, 30, nextLang)
        .then((r) => { if (seq === requestSeq.current) { set.setFeed(r.items); set.setFeedError(null) } })
        .catch((err: unknown) => {
          if (seq === requestSeq.current)
            set.setFeedError(err instanceof Error ? err.message : 'Feed unavailable')
        })
      const battlesProm = getBattles(sessionId, ms)
        .then((r) => { if (seq === requestSeq.current) set.setBattles(r.battles) })
        .catch(() => undefined)
      const commentaryProm = getCommentary(sessionId, ms, nextLang, nextLevel)
        .then((r) => { if (seq === requestSeq.current) set.setCommentary(r.items) })
        .catch(() => undefined)

      await Promise.allSettled([feedProm, battlesProm, commentaryProm])
      if (seq === requestSeq.current) set.setLoading(false)
    },
    [sessionId, set],
  )

  return { loadSnapshot, cancelSnapshot }
}
