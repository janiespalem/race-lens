import type { Lang } from './replayTypes'
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { Battle, DriverState } from '../../api/types'
import { battlePair } from '../../lib/battles'
import { formatLapTime } from '../../lib/format'
import { teamColor } from './teamColors'

type DriverRow = { id: string } & DriverState

type Props = {
  lang?: Lang
  rows: DriverRow[]
  battles: Battle[]
  selectedIds: string[]
  onSelectDriver: (id: string) => void
}

/** Gained/lost since start: grid_position (baseline) vs current position (rank).
    Zero delta renders nothing — a badge on every row is noise, not signal. */
function gridDeltaBadge(row: DriverRow): { text: string; cls: 'up' | 'down' } | null {
  if (row.grid_position == null || row.position == null) return null
  const delta = row.grid_position - row.position
  if (delta > 0) return { text: `▲${delta}`, cls: 'up' }
  if (delta < 0) return { text: `▼${Math.abs(delta)}`, cls: 'down' }
  return null
}

/** Pace trend: compare last_lap_ms vs mean of recent_laps_ms (fallback: best_lap_ms). */
function paceTrend(row: DriverRow): 'up' | 'down' | null {
  const last = row.last_lap_ms
  if (!last || last <= 0) return null

  const recent = row.recent_laps_ms
  let avg: number | null = null

  if (Array.isArray(recent) && recent.length > 0) {
    const valid = recent.filter((v) => v > 0)
    if (valid.length > 0) avg = valid.reduce((a, b) => a + b, 0) / valid.length
  }

  if (avg === null) return null

  const delta = last - avg
  if (delta < -300) return 'up'   // faster (lower is better)
  if (delta > 300) return 'down'
  return null
}

export const TimingTower = React.memo(function TimingTower({
  lang = 'en',
  rows,
  battles,
  selectedIds,
  onSelectDriver,
}: Props) {
  const battleSet = useMemo(() => {
    const s = new Set<string>()
    for (const b of battles) {
      const pair = battlePair(b)
      if (pair) {
        s.add(pair[0])
        s.add(pair[1])
      }
    }
    return s
  }, [battles])

  const rowCount = rows.length || 1

  // Gained/lost peek: clicking the POS header swaps the LAST column into big
  // ▲/▼ grid-deltas for a moment, then reverts — the always-on badge under the
  // position number is too small to actually read.
  const [deltaPeek, setDeltaPeek] = useState(false)
  const deltaPeekTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const peekDeltas = () => {
    setDeltaPeek(true)
    if (deltaPeekTimer.current) clearTimeout(deltaPeekTimer.current)
    deltaPeekTimer.current = setTimeout(() => setDeltaPeek(false), 3200)
  }
  useEffect(() => () => {
    if (deltaPeekTimer.current) clearTimeout(deltaPeekTimer.current)
  }, [])

  // ── Fastest lap across peloton ────────────────────────────────
  const fastestLapHolder = useMemo(() => {
    let best: number | null = null
    let bestId: string | null = null
    for (const row of rows) {
      if (row.best_lap_ms && row.best_lap_ms > 0) {
        if (best === null || row.best_lap_ms < best) {
          best = row.best_lap_ms
          bestId = row.id
        }
      }
    }
    return bestId
  }, [rows])

  // ── FLIP animation ────────────────────────────────────────────
  // Map driver_id → DOM element ref
  const rowRefs = useRef<Map<string, HTMLDivElement>>(new Map())
  // Map driver_id → last measured offsetTop (before render)
  const prevTopsRef = useRef<Map<string, number>>(new Map())

  // Track which drivers changed position direction for highlight
  const prevPositionRef = useRef<Map<string, number>>(new Map())
  const [posChanges, setPosChanges] = useState<Map<string, 'up' | 'down'>>(new Map())
  const highlightTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  // After paint: detect moves, run FLIP, detect position changes
  useLayoutEffect(() => {
    const prev = prevTopsRef.current
    // Honour reduced-motion: skip transform animation entirely if the user requested it.
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    for (const [id, el] of rowRefs.current) {
      const oldTop = prev.get(id)
      const newTop = el.offsetTop
      if (oldTop !== undefined && oldTop !== newTop) {
        const delta = oldTop - newTop
        if (reducedMotion) {
          // No motion — just snap to final position (no transform trickery).
          el.style.transform = ''
          el.style.transition = 'none'
          el.style.zIndex = ''
        } else {
          // Lift above neighbours during travel so rows glide over, not through.
          el.style.zIndex = '10'
          // Apply inverted transform (no transition) — the FLIP "First" step.
          el.style.transition = 'none'
          el.style.transform = `translateY(${delta}px)`
          // Force reflow to commit the starting transform before we release.
          void el.offsetTop
          // Release with broadcast-grade easing: fast start, silky settle.
          // 310ms is short enough to feel snappy at 60fps but long enough to
          // read clearly in a GIF/screen-record.
          el.style.transition = 'transform 310ms cubic-bezier(0.2, 1, 0.3, 1), z-index 0s 310ms'
          el.style.transform = ''
          // Drop z-index back after travel completes.
          const clearZ = setTimeout(() => {
            el.style.zIndex = ''
            el.style.transition = ''
          }, 320)
          // Track so we don't leak timers (overwrite if another swap fires sooner).
          const zEl = el as HTMLDivElement & { _zTimer?: ReturnType<typeof setTimeout> }
          if (zEl._zTimer) clearTimeout(zEl._zTimer)
          zEl._zTimer = clearZ
        }
      }
    }

    // Detect position changes for highlight + position-number flash
    const newChanges = new Map<string, 'up' | 'down'>()
    for (const row of rows) {
      if (row.position === null) continue
      const prevPos = prevPositionRef.current.get(row.id)
      if (prevPos !== undefined && prevPos !== row.position) {
        const dir = row.position < prevPos ? 'up' : 'down'
        newChanges.set(row.id, dir)

        // Flash the position number badge via Web Animations API.
        if (!reducedMotion) {
          const el = rowRefs.current.get(row.id)
          const posEl = el?.querySelector<HTMLElement>('.pos')
          if (posEl) {
            posEl.animate(
              [
                { transform: 'skewX(-8deg) scale(1)',    opacity: 1   },
                { transform: 'skewX(-8deg) scale(1.22)', opacity: 1   },
                { transform: 'skewX(-8deg) scale(1)',    opacity: 0.9 },
              ],
              { duration: 280, easing: 'ease-out', fill: 'none' },
            )
          }
        }

        // Clear accent bar after 2.2s
        const existing = highlightTimers.current.get(row.id)
        if (existing) clearTimeout(existing)
        const t = setTimeout(() => {
          setPosChanges((old) => {
            const next = new Map(old)
            next.delete(row.id)
            return next
          })
          highlightTimers.current.delete(row.id)
        }, 2200)
        highlightTimers.current.set(row.id, t)
      }
      prevPositionRef.current.set(row.id, row.position)
    }
    if (newChanges.size > 0) {
      setPosChanges((old) => {
        const next = new Map(old)
        for (const [k, v] of newChanges) next.set(k, v)
        return next
      })
    }

    // Capture current tops for the NEXT FLIP — must run after the comparison
    // above, otherwise it overwrites the old positions and every delta is 0
    // (that was the bug: rows never moved, the number just blinked in place).
    const tops = new Map<string, number>()
    for (const [id, el] of rowRefs.current) {
      tops.set(id, el.offsetTop)
    }
    prevTopsRef.current = tops
   
  }, [rows])

  return (
    <div
      className="col col-timing"
      style={{ '--row-count': rowCount } as React.CSSProperties}
    >
      <div className="label label-row">
        {lang === 'ru' ? 'ХРОНОМЕТРАЖ' : 'TIMING'}
        <button
          type="button"
          className={`delta-btn${deltaPeek ? ' on' : ''}`}
          title={(lang === 'ru' ? 'Выиграно и потеряно позиций со старта' : 'Positions gained/lost since the start')}
          onClick={peekDeltas}
        >{(lang === 'ru' ? '▲▼ СМЕНА ПОЗИЦИЙ' : '▲▼ GAINED/LOST')}</button>
      </div>
      {/* Column headers */}
      <div className="trow-hdr">
        <span>{(lang === 'ru' ? 'ПОЗ' : 'POS')}</span>
        <span />
        <span>{(lang === 'ru' ? 'ПИЛ' : 'DRV')}</span>
        <span title={(lang === 'ru' ? 'Состав шин' : 'Tyre compound')}>{(lang === 'ru' ? 'Ш.' : 'TYR')}</span>
        <span className="col-age" title={(lang === 'ru' ? 'Пробег шин в кругах' : 'Tyre age laps')}>{(lang === 'ru' ? 'КР' : 'AGE')}</span>
        <span title={(lang === 'ru' ? 'Время последнего круга' : 'Last lap time')}>{lang === 'ru' ? (deltaPeek ? '±СТАРТ' : 'КРУГ') : (deltaPeek ? '±START' : 'LAST')}</span>
        <span />
        <span title={(lang === 'ru' ? 'Отставание от лидера' : 'Gap to leader')}>{(lang === 'ru' ? 'ОТСТ' : 'GAP')}</span>
        <span className="col-int" title={(lang === 'ru' ? 'Интервал до машины впереди' : 'Gap to car ahead')}>{(lang === 'ru' ? 'ИНТ' : 'INT')}</span>
        <span className="col-pit">{(lang === 'ru' ? 'ПИТ' : 'PIT')}</span>
      </div>
      {rows.map((row) => {
        const isLead = row.position === 1
        const inBattle = battleSet.has(row.id)
        const isRetired = row.retired === true
        const isStopped = !isRetired && row.stopped === true
        const outLabel = lang === 'ru' ? (row.retirement_inferred ? 'СХОД?' : 'СХОД') : (row.retirement_inferred ? 'OUT?' : 'OUT')
        const color = teamColor(row.id)
        const isSelected = selectedIds.includes(row.id)
        const posChange = posChanges.get(row.id)
        const trend = paceTrend(row)
        const hasFastestLap = row.id === fastestLapHolder

        const displayInterval = row.interval_s
        const displayGap = row.gap_s

        const intDisplay = isRetired
          ? <span className="gap dim" title={row.retirement_inferred ? (lang === 'ru' ? 'Предположение по отставанию в кругах' : 'Inferred from lap deficit') : undefined}>{outLabel}</span>
          : isStopped
            ? <span className="gap dim">{(lang === 'ru' ? 'СТОИТ' : 'STOPPED')}</span>
          : isLead
            ? <span className="gap dim">—</span>
            : displayInterval !== null && displayInterval !== undefined
              ? <span className="gap dim">{`+${displayInterval.toFixed(1)}`}</span>
              : <span className="gap dim">—</span>

        const gapDisplay = isRetired
          ? <span className="gap dim" title={row.retirement_inferred ? (lang === 'ru' ? 'Предположение по отставанию в кругах' : 'Inferred from lap deficit') : undefined}>{outLabel}</span>
          : isStopped
            ? <span className="gap dim">{(lang === 'ru' ? 'СТОИТ' : 'STOPPED')}</span>
          : isLead
            ? <span className="gap dim">—</span>
            : <span className={`gap${displayGap === null || displayGap === undefined ? ' dim' : ''}`}>
                {displayGap !== null && displayGap !== undefined ? `+${displayGap.toFixed(1)}` : '—'}
              </span>

        const compound = row.tyre_compound?.charAt(0).toUpperCase() ?? '?'

        const trendEl = trend === 'up'
          ? <span className="pace-trend up" title={(lang === 'ru' ? 'В сравнении со своим недавним темпом' : 'vs own recent pace')}>▲</span>
          : trend === 'down'
            ? <span className="pace-trend down" title={(lang === 'ru' ? 'В сравнении со своим недавним темпом' : 'vs own recent pace')}>▼</span>
            : <span className="pace-trend" />

        const deltaBadge = isRetired ? null : gridDeltaBadge(row)

        return (
          <div
            key={row.id}
            ref={(el) => {
              if (el) rowRefs.current.set(row.id, el)
              else rowRefs.current.delete(row.id)
            }}
            className={[
              'trow',
              isLead ? 'lead' : '',
              inBattle ? 'battle-tick' : '',
              isRetired ? 'retired' : '',
              isSelected ? 'trow-selected' : '',
              posChange === 'up' ? 'trow-pos-up' : '',
              posChange === 'down' ? 'trow-pos-down' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            onClick={() => onSelectDriver(row.id)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                onSelectDriver(row.id)
              }
            }}
            role="button"
            tabIndex={0}
            aria-pressed={isSelected}
            aria-label={`${row.id}, ${lang === 'ru' ? 'позиция' : 'position'} ${row.position ?? (lang === 'ru' ? 'неизвестна' : 'unknown')}`}
            style={{ cursor: 'pointer' }}
          >
            <span className="pos">{isRetired ? '—' : (row.position ?? '—')}</span>
            <span className="tbar" style={{ background: color }} />
            <span className="code">
              {row.id}
              {row.in_pit && !isRetired && <span className="pit-tag">{(lang === 'ru' ? 'ПИТ' : 'PIT')}</span>}
            </span>
            <span className={`ty ${compound}`}>
              {isRetired ? '—' : compound}
            </span>
            <span className={`col-age tyre-age${!isRetired && row.tyre_age_laps != null && row.tyre_age_laps <= 2 ? ' fresh' : ''}`}>
              {isRetired ? '' : (row.tyre_age_laps ?? '—')}
            </span>
            <span className="last-lap last-swap">
              <span className={`swap-layer${deltaPeek ? ' swap-hidden' : ''}`}>
                {isRetired ? '—' : formatLapTime(row.last_lap_ms)}
                {hasFastestLap && !isRetired && <span className="fl-dot" title={(lang === 'ru' ? 'Лучший круг' : 'Fastest lap')}>●</span>}
              </span>
              <span
                className={`swap-layer swap-delta${deltaPeek ? '' : ' swap-hidden'}${deltaBadge ? ` ${deltaBadge.cls}` : ''}`}
                aria-hidden={!deltaPeek}
              >
                {isRetired ? '—' : (deltaBadge ? deltaBadge.text : '=')}
              </span>
            </span>
            {trendEl}
            {gapDisplay}
            {intDisplay}
            <span className="col-pit pits-count">{row.pit_count ?? 0}</span>
          </div>
        )
      })}
    </div>
  )
})
