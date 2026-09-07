import React, { useEffect, useRef, useState } from 'react'
import type { FeedItem } from '../../api/types'
import { formatRaceTime } from '../../lib/format'
import { isDirectActivation } from './workspace'

function fmtFeedTime(ms: number, clockOriginMs?: number): string {
  if (clockOriginMs === undefined) return formatRaceTime(ms)
  return ms < clockOriginMs ? 'FORMATION' : formatRaceTime(ms - clockOriginMs)
}

type Tag = 'PIT' | 'FLAG' | 'FASTEST' | 'FINISH' | 'PASS' | 'INFO' | 'STEWARDS'

const TAG_LABELS: Record<Tag, string> = {
  FLAG: 'FLAG',
  PIT: 'PIT',
  FASTEST: 'FAST',
  FINISH: 'FIN',
  PASS: 'PASS',
  INFO: 'INFO',
  STEWARDS: 'FIA',
}

// One shared <audio> element: only ever one team-radio clip plays at a time.
const radioAudio = typeof Audio !== 'undefined' ? new Audio() : null

const FeedRow = React.memo(function FeedRow({
  item,
  flash,
  playingUrl,
  onToggleRadio,
  onActivate,
  clockOriginMs,
}: {
  item: FeedItem
  flash: boolean
  playingUrl: string | null
  onToggleRadio: (url: string) => void
  onActivate?: (item: FeedItem) => void
  clockOriginMs?: number
}) {
  const isStatus = item.kind === 'status' || item.kind === 'red_flag' || item.kind === 'safety_car'
  const isFastest = item.kind === 'fastest_lap' || item.kind === 'LapCompleted'
  const tag = (item.tag ?? 'INFO') as Tag
  const isPlaying = !!item.audio_url && item.audio_url === playingUrl
  return (
    <div
      className={[
        'ev',
        isStatus ? 'crit' : '',
        isFastest ? 'fast' : '',
        flash ? 'ev-flash' : '',
      ].filter(Boolean).join(' ')}
    >
      {onActivate && (
        <button
          type="button"
          className="ev-row-action"
          aria-label={`Open feed event: ${item.text}`}
          onClick={(event) => {
            if (isDirectActivation(event.currentTarget, event.target)) onActivate(item)
          }}
        />
      )}
      {item.lap !== null ? (
        <span className="ev-lap">L{item.lap}</span>
      ) : (
        <span className="ev-lap" />
      )}
      <span className="t">{fmtFeedTime(item.at_ms, clockOriginMs)}</span>
      <span className="x">
        <span className={`ev-tag ev-tag-${tag.toLowerCase()}`}>{TAG_LABELS[tag]}</span>
        {item.audio_url && (
          <button
            type="button"
            className={`ev-radio-btn${isPlaying ? ' playing' : ''}`}
            onClick={() => onToggleRadio(item.audio_url!)}
            aria-label={isPlaying ? 'Stop team radio' : 'Play team radio'}
            title={isPlaying ? 'Stop team radio' : 'Play team radio'}
          >
            {isPlaying ? '■' : '▶'}
          </button>
        )}
        {item.text}
        {item.transcript && (
          <span className="ev-transcript">
            <small>AUTO TRANSCRIPT · MAY BE INACCURATE</small>
            “{item.transcript.replace(/([.!?])\s+/g, '$1\n')}”
          </span>
        )}
      </span>
    </div>
  )
})

function itemKey(item: FeedItem): string {
  return item.id
}

export function RaceFeed({
  items,
  loading = false,
  clockOriginMs,
  onActivate,
  isActionable,
}: {
  items: FeedItem[]
  loading?: boolean
  clockOriginMs?: number
  onActivate?: (item: FeedItem) => void
  isActionable?: (item: FeedItem) => boolean
}) {
  const prevKeysRef = useRef<Set<string>>(new Set())
  const [flashKeys, setFlashKeys] = useState<Set<string>>(new Set())
  const [playingUrl, setPlayingUrl] = useState<string | null>(null)

  // Stop-and-clear when the clip finishes on its own.
  useEffect(() => {
    if (!radioAudio) return
    const onEnded = () => setPlayingUrl(null)
    radioAudio.addEventListener('ended', onEnded)
    return () => {
      radioAudio.removeEventListener('ended', onEnded)
      radioAudio.pause()
      radioAudio.removeAttribute('src')
      radioAudio.load()
    }
  }, [])

  const handleToggleRadio = (url: string) => {
    if (!radioAudio) return
    if (playingUrl === url) {
      radioAudio.pause()
      setPlayingUrl(null)
      return
    }
    radioAudio.src = url
    setPlayingUrl(null)
    void radioAudio.play()
      .then(() => {
        if (radioAudio.src === url) setPlayingUrl(url)
      })
      .catch(() => {
        if (radioAudio.src === url) setPlayingUrl(null)
      })
  }

  useEffect(() => {
    const newKeys = new Set<string>()
    for (const item of items) {
      const k = itemKey(item)
      if (!prevKeysRef.current.has(k)) {
        newKeys.add(k)
      }
    }
    prevKeysRef.current = new Set(items.map(itemKey))
    if (newKeys.size > 0) {
      setFlashKeys(newKeys)
      const t = window.setTimeout(() => setFlashKeys(new Set()), 2000)
      return () => clearTimeout(t)
    }
  }, [items])

  return (
    <div className="ev-scroll">
      <div className="label">RACE FEED</div>
      <div className="ev-list">
        {items.map((item) => {
          const k = itemKey(item)
          return (
            <FeedRow
              key={k}
              item={item}
              flash={flashKeys.has(k)}
              playingUrl={playingUrl}
              onToggleRadio={handleToggleRadio}
              onActivate={onActivate && (isActionable?.(item) ?? true) ? onActivate : undefined}
              clockOriginMs={clockOriginMs}
            />
          )
        })}
        {items.length === 0 && (
          <div className="ev">
            <span className="ev-lap" />
            <span className="t">—</span>
            <span className="x">{loading ? 'Loading events…' : 'No events yet'}</span>
          </div>
        )}
      </div>
    </div>
  )
}
