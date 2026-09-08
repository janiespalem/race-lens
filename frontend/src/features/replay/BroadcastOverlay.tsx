import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FeedItem, RaceMarker } from '../../api/types'
import {
  BROADCAST_DISPLAY_MS,
  BROADCAST_EXIT_MS,
  canPresentBroadcastCandidate,
  selectBroadcastCandidate,
} from '../../lib/broadcastOverlay'
import type { BroadcastCandidate } from '../../lib/broadcastOverlay'

type Props = {
  atMs: number
  playing: boolean
  speed: number
  lang: 'en' | 'ru'
  markers: RaceMarker[]
  feed: FeedItem[]
}

export function BroadcastOverlay({ atMs, playing, speed, lang, markers, feed }: Props) {
  const current = useMemo(
    () => selectBroadcastCandidate({ atMs, playing, speed, lang, markers, feed }),
    [atMs, feed, lang, markers, playing, speed],
  )
  const [displayed, setDisplayed] = useState<BroadcastCandidate | null>(null)
  const [dismissedId, setDismissedId] = useState<string | null>(null)
  const [leaving, setLeaving] = useState(false)
  const displayedId = displayed?.id ?? null

  useEffect(() => {
    if (!current) {
      if (dismissedId !== null) setDismissedId(null)
      return
    }
    if (!canPresentBroadcastCandidate(current.id, dismissedId)) return
    setDismissedId(null)
    setLeaving(false)
    setDisplayed(current)
  }, [current, dismissedId])

  const dismiss = useCallback(() => {
    if (!displayedId) return
    setDismissedId(displayedId)
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setDisplayed(null)
      setLeaving(false)
    } else {
      setLeaving(true)
    }
  }, [displayedId])

  useEffect(() => {
    if (!displayedId || leaving) return
    const timer = window.setTimeout(dismiss, BROADCAST_DISPLAY_MS)
    return () => window.clearTimeout(timer)
  }, [dismiss, displayedId, leaving])

  useEffect(() => {
    if (!leaving || !displayedId) return
    const timer = window.setTimeout(() => {
      setDisplayed((value) => value?.id === displayedId ? null : value)
      setLeaving(false)
    }, BROADCAST_EXIT_MS)
    return () => window.clearTimeout(timer)
  }, [displayedId, leaving])

  useEffect(() => {
    if (!displayedId) return
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') dismiss()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [dismiss, displayedId])

  if (!displayed) return null
  return (
    <aside
      key={displayed.id}
      className={`broadcast-overlay broadcast-overlay--${displayed.tone}${leaving ? ' is-leaving' : ''}`}
      role="status"
      aria-atomic="true"
    >
      <span className="broadcast-overlay__kicker">
        <i aria-hidden="true" />
        {displayed.kicker}
      </span>
      <button
        type="button"
        className="broadcast-overlay__close"
        onClick={dismiss}
        aria-label={lang === 'ru' ? 'Закрыть сообщение о гонке' : 'Dismiss race update'}
      >×</button>
      <div className="broadcast-overlay__body">
        <strong>{displayed.title}</strong>
        <small>{displayed.lap ? `${lang === 'ru' ? 'КРУГ' : 'LAP'} ${displayed.lap}` : (lang === 'ru' ? 'СОБЫТИЕ ГОНКИ' : 'RACE UPDATE')} · {lang === 'ru' ? 'СОБЫТИЕ ПОВТОРА' : 'REPLAY EVENT'}</small>
      </div>
    </aside>
  )
}
