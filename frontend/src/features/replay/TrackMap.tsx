import React, { useEffect, useRef, useState } from 'react'
import type { TrackData } from '../../api/client'
import { getLiveTrack, getTrack } from '../../api/client'
import type { Battle, DriverState, RecentPass } from '../../api/types'
import { battlePair } from '../../lib/battles'
import type { PositionsData } from '../../lib/liveGaps'
import { buildPathD, startFinishLine } from '../../lib/trackGeometry'
import { hasFinishedRace } from '../../lib/trackInterpolation'
import { isRetryableLiveTrackError, LIVE_TRACK_RETRY_MS } from '../../lib/liveTrack'
import { teamColor } from './teamColors'
import { useTrackAnimation } from './useTrackAnimation'
import type { Lang } from './replayTypes'

type Props = {
  lang?: Lang
  sessionId: string | null
  atMs: number
  playing: boolean
  playbackSpeed: number
  drivers: Record<string, DriverState>
  classification: string[]
  totalLaps?: number | null
  sessionStatus?: string
  /** Session time (ms) when current neutralisation started — for elapsed timer in badge. */
  neutralizationStartMs?: number | null
  selectedIds?: string[]
  /** Positions telemetry data lifted from parent (useReplay). If null, schematic mode is used. */
  positionsData: PositionsData | null
  /** Active battles for on-map highlights */
  battles?: Battle[]
  /** Overtakes in the last ~20s of session time — drives the on-map overtake flash. */
  recentPasses?: RecentPass[]
  live?: boolean
}

/** How long the overtake flash ring stays on a driver after their pass. */
const OVERTAKE_FLASH_MS = 3_000

// Pit lane overlay — fixed position in bottom-left of SVG coordinate space
// These are SVG units, sized relative to typical 600x400 viewbox
const PIT_BOX_X = 10
const PIT_BOX_Y_TOP = 320
const PIT_BOX_HEIGHT = 60
const PIT_BOX_CAR_Y = PIT_BOX_Y_TOP + 38
const PIT_CAR_SPACING = 26

function statusWatermark(status: string, lang: Lang): { text: string; color: string } | null {
  if (status === 'red_flag') return { text: lang === 'ru' ? 'КРАСНЫЙ ФЛАГ' : 'RED FLAG', color: '#cc0000' }
  if (status === 'safety_car') return { text: lang === 'ru' ? 'SC' : 'SAFETY CAR', color: '#f2a900' }
  if (status === 'vsc') return { text: lang === 'ru' ? 'VSC' : 'VIRTUAL SC', color: '#f2a900' }
  return null
}

function trackStrokeColor(status: string): string {
  if (status === 'safety_car' || status === 'vsc') return '#f2a900'
  if (status === 'red_flag') return '#cc0000'
  return '#26262e'
}

function trackShadow(status: string): string | undefined {
  if (status === 'safety_car' || status === 'vsc')
    return '0 0 18px 6px #f2a900aa'
  if (status === 'red_flag') return '0 0 18px 6px #cc0000aa'
  return undefined
}

/** Format elapsed ms as M:SS */
function fmtElapsed(ms: number): string {
  const totalSec = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

export const TrackMap = React.memo(function TrackMap({
  lang = 'en', sessionId, atMs, playing, playbackSpeed, drivers, classification, totalLaps, sessionStatus, neutralizationStartMs,
  selectedIds = [], positionsData, battles = [], recentPasses = [], live = false,
}: Props) {
  const [trackData, setTrackData] = useState<TrackData | null>(null)
  const [trackError, setTrackError] = useState(false)

  // Overtake flash: a white ring pulses on the `ahead` driver for OVERTAKE_FLASH_MS
  // after their pass. Keyed by ahead+at_ms so each pass event triggers the flash
  // exactly once, even though the same recentPasses entry arrives on every frame
  // for ~20s (the backend's attention window).
  const [flashingIds, setFlashingIds] = useState<Set<string>>(new Set())
  const seenPassKeysRef = useRef<Set<string>>(new Set())
  const flashTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  useEffect(() => {
    for (const p of recentPasses) {
      const key = `${p.ahead}:${p.at_ms}`
      if (seenPassKeysRef.current.has(key)) continue
      seenPassKeysRef.current.add(key)

      setFlashingIds((prev) => {
        if (prev.has(p.ahead)) return prev
        const next = new Set(prev)
        next.add(p.ahead)
        return next
      })

      const existingTimer = flashTimersRef.current.get(p.ahead)
      if (existingTimer) clearTimeout(existingTimer)
      const timer = setTimeout(() => {
        setFlashingIds((prev) => {
          if (!prev.has(p.ahead)) return prev
          const next = new Set(prev)
          next.delete(p.ahead)
          return next
        })
        flashTimersRef.current.delete(p.ahead)
      }, OVERTAKE_FLASH_MS)
      flashTimersRef.current.set(p.ahead, timer)
    }
  }, [recentPasses])

  // Clear any pending flash timers on unmount so they never fire against a dead component.
  useEffect(() => {
    const timers = flashTimersRef.current
    return () => {
      for (const t of timers.values()) clearTimeout(t)
    }
  }, [])

  const { pathRef, registerCar } = useTrackAnimation({
    atMs, playing, playbackSpeed, drivers, classification, sessionStatus, positionsData,
    progressPath: trackData?.progress_points,
  })

  // Fetch track data whenever session changes
  useEffect(() => {
    let cancelled = false
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    setTrackData(null)
    setTrackError(false)
    if (!live && !sessionId) return

    const loadTrack = () => {
      const request = live ? getLiveTrack() : getTrack(sessionId!)
      request
        .then((d) => { if (!cancelled) setTrackData(d) })
        .catch((error: unknown) => {
          if (cancelled) return
          if (live && isRetryableLiveTrackError(error)) {
            retryTimer = setTimeout(loadTrack, LIVE_TRACK_RETRY_MS)
            return
          }
          setTrackError(true)
        })
    }

    loadTrack()
    return () => {
      cancelled = true
      if (retryTimer) clearTimeout(retryTimer)
    }
  }, [live, sessionId])

  const status = sessionStatus ?? ''
  const watermark = statusWatermark(status, lang)
  const trackStroke = trackStrokeColor(status)
  const trackShadowFilter = trackShadow(status)
  const elapsedTimer = watermark && neutralizationStartMs != null
    ? fmtElapsed(atMs - neutralizationStartMs)
    : null
  const top3 = classification.slice(0, 3)
  const hasFocus = selectedIds.length > 0

  const [vw, vh] = trackData?.viewbox ?? [600, 400]
  const pathD = trackData ? buildPathD(trackData.points) : ''
  const sf = trackData ? startFinishLine(trackData.points) : null

  const pitDrivers = classification.filter(id => drivers[id]?.in_pit)

  // Cars to draw: union of state classification and telemetry drivers, so the
  // formation lap (before any timing events exist) still shows cars from the
  // position telemetry.
  const carIds = positionsData
    ? Array.from(new Set<string>([...classification, ...Object.keys(positionsData.drivers)]))
    : classification

  return (
    <div className="map">
      <svg viewBox={`0 0 ${vw} ${vh}`} preserveAspectRatio="xMidYMid meet" fill="none" style={{ width: '100%', height: '100%' }}>
        {trackError && (
          <text
            x={vw / 2}
            y={vh / 2}
            textAnchor="middle"
            dominantBaseline="middle"
            fill="#55555f"
            fontSize={18}
            fontFamily="'Barlow Condensed', sans-serif"
            letterSpacing="0.2em"
          >
            {lang === 'ru' ? 'НЕТ ДАННЫХ ТРАССЫ' : 'NO TRACK DATA'}
          </text>
        )}

        {trackData && (
          <>
            {trackShadowFilter && (
              <defs>
                <filter id="track-glow" x="-20%" y="-20%" width="140%" height="140%">
                  <feDropShadow dx="0" dy="0" stdDeviation="5" floodColor={watermark?.color ?? '#f2a900'} floodOpacity="0.7" />
                </filter>
              </defs>
            )}
            {/* Track base */}
            <path
              d={pathD}
              stroke={trackStroke}
              strokeWidth={12}
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
              filter={trackShadowFilter ? 'url(#track-glow)' : undefined}
            />
            {/* Invisible measurement path */}
            <path ref={pathRef} d={pathD} stroke="none" fill="none" />
            {/* Start/finish line */}
            {sf && (
              <line
                x1={sf.x1}
                y1={sf.y1}
                x2={sf.x2}
                y2={sf.y2}
                stroke="#ffffff"
                strokeWidth={1.5}
                strokeLinecap="round"
              />
            )}
            {/* Corner numbers */}
            {(trackData.corners ?? []).map((c) => (
              <text
                key={`c${c.number}`}
                x={c.x + 4}
                y={c.y - 4}
                textAnchor="start"
                dominantBaseline="auto"
                fill="#8a8a9a"
                fontSize={10}
                fontFamily="'Barlow Condensed', sans-serif"
                letterSpacing="0"
                pointerEvents="none"
              >
                {c.number}
              </text>
            ))}
          </>
        )}

        {/* Pit lane pocket */}
        {trackData && (
          <g>
            {/* Background rect */}
            <rect
              x={PIT_BOX_X - 6}
              y={PIT_BOX_Y_TOP}
              width={Math.max(80, pitDrivers.length * PIT_CAR_SPACING + 20)}
              height={PIT_BOX_HEIGHT}
              fill="#0c0c0fcc"
              stroke="#f2a90066"
              strokeWidth={1}
              rx={2}
            />
            {/* PIT LANE label */}
            <text
              x={PIT_BOX_X}
              y={PIT_BOX_Y_TOP + 13}
              fill="#f2a900bb"
              fontSize={12}
              fontFamily="'Barlow Condensed', sans-serif"
              fontWeight={700}
              letterSpacing="0.18em"
            >
              {lang === 'ru' ? 'ПИТ-ЛЕЙН' : 'PIT LANE'}
            </text>
            {/* Count badge */}
            {pitDrivers.length > 0 && (
              <text
                x={PIT_BOX_X}
                y={PIT_BOX_Y_TOP + 26}
                fill="#f2a90077"
                fontSize={9}
                fontFamily="'Barlow Condensed', sans-serif"
                fontWeight={600}
                letterSpacing="0.08em"
              >
                {lang === 'ru' ? `В БОКСАХ: ${pitDrivers.length}` : `${pitDrivers.length} IN PITS`}
              </text>
            )}
          </g>
        )}

        {/* Status badge — prominent overlay for SC/VSC/red flag */}
        {watermark && (
          <g>
            {/* Badge background pill */}
            <rect
              x={vw / 2 - 96}
              y={vh / 2 - 22}
              width={192}
              height={44}
              rx={6}
              fill={watermark.color}
              opacity={0.92}
            />
            <text
              x={vw / 2}
              y={elapsedTimer ? vh / 2 - 5 : vh / 2}
              textAnchor="middle"
              dominantBaseline="middle"
              fill="#000"
              fontSize={elapsedTimer ? 20 : 24}
              fontStyle="italic"
              fontWeight={900}
              fontFamily="'Barlow Condensed', sans-serif"
              letterSpacing="0.08em"
            >
              {watermark.text}
            </text>
            {elapsedTimer && (
              <text
                x={vw / 2}
                y={vh / 2 + 13}
                textAnchor="middle"
                dominantBaseline="middle"
                fill="#000"
                fontSize={16}
                fontWeight={700}
                fontFamily="'Barlow Condensed', sans-serif"
                letterSpacing="0.04em"
                opacity={0.8}
              >
                {elapsedTimer}
              </text>
            )}
          </g>
        )}

        {/* On-track cars — initial transform is 0,0; rAF loop updates imperatively */}
        {carIds
          .filter(id => !drivers[id]?.in_pit && !drivers[id]?.retired && !hasFinishedRace(drivers[id]?.laps_completed, totalLaps))
          .map((driverId) => {
            const color = teamColor(driverId)
            const isTop3 = top3.includes(driverId)
            const isSelected = selectedIds.includes(driverId)
            const isDimmed = hasFocus && !isSelected
            const r = isSelected ? 9 : 7
            const showLabel = isSelected || isTop3
            const inBattle = battles.some((battle) => battlePair(battle)?.includes(driverId))
            const isFlashing = flashingIds.has(driverId)
            return (
              <g
                key={driverId}
                ref={registerCar(driverId)}
                transform="translate(0,0)"
                opacity={isDimmed ? 0.5 : 1}
              >
                {/* Overtake flash — white pulsing ring on the driver who just passed */}
                {isFlashing && (
                  <circle
                    className="overtake-ring"
                    r={r + 8}
                    fill="none"
                    stroke="#ffffff"
                    strokeWidth={2}
                  />
                )}
                <circle
                  r={r}
                  fill={color}
                  stroke={isSelected ? '#fff' : inBattle ? '#f2a900' : isTop3 ? '#fff' : 'none'}
                  strokeWidth={isSelected || inBattle ? 2 : isTop3 ? 1.5 : 0}
                />
                {showLabel && (
                  <text
                    x={0}
                    y={-13}
                    textAnchor="middle"
                    fill="#fff"
                    fontSize={isSelected ? 15 : 14}
                    fontStyle="italic"
                    fontWeight={700}
                    fontFamily="'Barlow Condensed', sans-serif"
                    letterSpacing="0.04em"
                  >
                    {driverId}
                  </text>
                )}
              </g>
            )
          })}

        {/* Pit lane cars — in the pit pocket */}
        {pitDrivers.map((driverId, i) => {
          const color = teamColor(driverId)
          const cx = PIT_BOX_X + 7 + i * PIT_CAR_SPACING
          return (
            <g key={driverId} transform={`translate(${cx},${PIT_BOX_CAR_Y})`}>
              <circle r={7} fill={color} opacity={0.9} />
              <text
                x={0}
                y={-11}
                textAnchor="middle"
                fill="#ffffff"
                fontSize={9}
                fontFamily="'Barlow Condensed', sans-serif"
                fontWeight={700}
                letterSpacing="0.04em"
              >
                {driverId}
              </text>
            </g>
          )
        })}
      </svg>

      <span className="note">{positionsData
        ? (lang === 'ru' ? 'ЗАПИСАННАЯ ТЕЛЕМЕТРИЯ' : 'RECORDED TELEMETRY')
        : (lang === 'ru' ? 'СХЕМА · ИНТЕРПОЛЯЦИЯ' : 'SCHEMATIC · INTERPOLATED')}</span>
    </div>
  )
})
