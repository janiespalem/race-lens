import React, { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { getLiveSimulatePit, getSimulatePit, getWhatIf } from '../../api/client'
import type { DriverState, PitSim, PitSimEvidence, WhatIf, WhatIfDiff } from '../../api/types'
import { formatRaceTime } from '../../lib/format'
import { viewerError } from '../../lib/viewerError'
import { teamColor } from './teamColors'

type Lang = 'en' | 'ru'

type Props = {
  lang?: Lang
  selectedIds: string[]
  drivers: Record<string, DriverState>
  /** Session ID for pit-sim/what-if (replay only; null in live). */
  sessionId: string | null
  /** Live mode: PIT NOW uses /api/live/simulate-pit instead (WHAT IF has no live mirror). */
  live?: boolean
  atMs: number
  lap: number
  onStrategyRequest?: () => void
  onRemoveDriver: (id: string) => void
}

type PitSnapshot = { atMs: number; lap: number }

function fmtLap(ms: number | null | undefined): string {
  if (!ms || ms <= 0) return '—'
  const m = Math.floor(ms / 60000)
  const s = Math.floor((ms % 60000) / 1000)
  const t = Math.floor((ms % 1000) / 100)
  return `${m}:${String(s).padStart(2, '0')}.${t}`
}

function fmtGap(s: number | null, lang: Lang): string {
  if (s === null) return '—'
  return `+${s.toFixed(1)}${lang === 'ru' ? 'с' : 's'}`
}

function recentLaps(driver: DriverState): number[] {
  const raw = driver.recent_laps_ms
  if (Array.isArray(raw)) return raw.filter((v) => v > 0).slice(-3)
  if (driver.last_lap_ms && driver.last_lap_ms > 0) return [driver.last_lap_ms]
  return []
}

/** Detect if a lap time is an in/out-lap: 10+ seconds slower than median of recent laps */
function isPitLap(driver: DriverState): boolean {
  if (!driver.last_lap_ms || driver.last_lap_ms <= 0) return false
  const raw = driver.recent_laps_ms
  let medMs: number | null = null
  if (Array.isArray(raw) && raw.length > 0) {
    const valid = raw.filter((v) => v > 0)
    if (valid.length > 0) {
      const sorted = [...valid].sort((a, b) => a - b)
      const mid = Math.floor(sorted.length / 2)
      medMs = sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid]
    }
  }
  if (medMs === null) return false
  return (driver.last_lap_ms - medMs) >= 10000
}

/** Pit sim verdict card */
function PitSimCard({ ev, snapshot, lang }: { ev: PitSimEvidence; snapshot: PitSnapshot | null; lang: Lang }) {
  const verdictColor = ev.verdict === 'UNDERCUT_LIKELY' ? '#00c853' : ev.verdict === 'UNLIKELY' ? '#f2a900' : '#a0a0ac'
  const verdictText = ev.verdict === 'UNDERCUT_LIKELY' ? (lang === 'ru' ? 'АНДЕРКАТ ВЕРОЯТЕН' : 'UNDERCUT LIKELY')
    : ev.verdict === 'UNLIKELY' ? (lang === 'ru' ? 'МАЛОВЕРОЯТНО' : 'UNLIKELY')
      : (lang === 'ru' ? 'НЕТ СОПЕРНИКА' : 'NO RIVAL')
  return (
    <div className="pit-sim-card">
      {snapshot && (
        <div className="pit-sim-snapshot">
          {lang === 'ru' ? 'ЗАПРОШЕНО · К' : 'REQUESTED AT · L'}{snapshot.lap || '—'} · T+{formatRaceTime(snapshot.atMs)}
        </div>
      )}
      <div className="pit-sim-verdict" style={{ color: verdictColor }}>{verdictText}</div>
      <div className="pit-sim-rows">
        <span>{lang === 'ru' ? 'ВЕРНЁТСЯ НА P' : 'REJOINS P'}{ev.rejoin_pos}</span>
        {ev.key_rival && ev.margin_s !== null && (
          <span>{lang === 'ru' ? 'ПРОТИВ' : 'VS'} {ev.key_rival}: {lang === 'ru' ? 'ЗАПАС' : 'MARGIN'} {ev.margin_s.toFixed(1)}{lang === 'ru' ? 'с' : 's'}</span>
        )}
        <span>{lang === 'ru' ? 'ПОТЕРЯ НА ПИТЕ' : 'PIT LOSS'} {ev.pit_loss_s.toFixed(1)}{lang === 'ru' ? 'с' : 's'}</span>
      </div>
    </div>
  )
}

/** What-If result card */
function WhatIfCard({ result, lang }: { result: WhatIf; lang: Lang }) {
  const summary = lang === 'ru' ? result.summary_text_ru : result.summary_text_en
  const topMovers = result.diff.filter((d) => d.delta !== 0).slice(0, 5)

  return (
    <div className="what-if-card">
      <div className="what-if-summary">{summary}</div>
      {topMovers.length > 0 && (
        <div className="what-if-diff">
          {topMovers.map((d: WhatIfDiff) => (
            <div key={d.driver} className="what-if-diff-row">
              <span className="what-if-driver" style={{ color: teamColor(d.driver) }}>{d.driver}</span>
              <span className="what-if-pos-from">P{d.baseline_pos}</span>
              <span className="what-if-arrow">→</span>
              <span className="what-if-pos-to">P{d.scenario_pos}</span>
              <span className={`what-if-delta ${d.delta > 0 ? 'up' : 'down'}`}>
                {d.delta > 0 ? `▲${d.delta}` : `▼${Math.abs(d.delta)}`}
              </span>
            </div>
          ))}
        </div>
      )}
      <div className="what-if-note">{lang === 'ru' ? 'чувствительность стратегии · модель не калибрована' : 'strategy sensitivity · uncalibrated'}</div>
    </div>
  )
}

/** Single-driver card (non-H2H mode) */
function DriverCard({ lang, driverId, driver, sessionId, live, atMs, lap, onStrategyRequest }: { lang: Lang; driverId: string; driver: DriverState; sessionId: string | null; live?: boolean; atMs: number; lap: number; onStrategyRequest?: () => void }) {
  const color = teamColor(driverId)
  const laps = recentLaps(driver)
  const compound = driver.tyre_compound?.charAt(0).toUpperCase() ?? '?'
  const inPit = driver.in_pit
  const pitLap = !inPit && isPitLap(driver)
  const [pitSim, setPitSim] = useState<PitSim | null>(null)
  const [pitBusy, setPitBusy] = useState(false)
  const [pitError, setPitError] = useState<string | null>(null)
  const [pitSnapshot, setPitSnapshot] = useState<PitSnapshot | null>(null)
  const [whatIf, setWhatIf] = useState<WhatIf | null>(null)
  const [whatIfBusy, setWhatIfBusy] = useState(false)
  const [whatIfError, setWhatIfError] = useState<string | null>(null)
  const [whatIfScenario, setWhatIfScenario] = useState<string | null>(null)
  const pitRequestSeq = useRef(0)
  const whatIfRequestSeq = useRef(0)
  const pitInFlight = useRef(false)
  const whatIfInFlight = useRef(false)
  const replayAtMs = sessionId ? atMs : 0

  useLayoutEffect(() => {
    pitRequestSeq.current++
    whatIfRequestSeq.current++
    pitInFlight.current = false
    whatIfInFlight.current = false
    setPitSim(null)
    setPitBusy(false)
    setPitError(null)
    setPitSnapshot(null)
    setWhatIf(null)
    setWhatIfBusy(false)
    setWhatIfError(null)
    setWhatIfScenario(null)
  }, [driverId, sessionId, live, replayAtMs])

  const handlePitNow = useCallback(() => {
    if ((!sessionId && !live) || pitInFlight.current) return
    pitInFlight.current = true
    const seq = ++pitRequestSeq.current
    onStrategyRequest?.()
    setPitSim(null)
    setPitError(null)
    setPitSnapshot(live ? { atMs, lap } : null)
    setPitBusy(true)
    const req = sessionId ? getSimulatePit(sessionId, atMs, driverId) : getLiveSimulatePit(driverId)
    req
      .then((res) => {
        if (seq === pitRequestSeq.current) setPitSim(res)
      })
      .catch(() => {
        if (seq === pitRequestSeq.current) setPitError('Pit calculation failed · try again')
      })
      .finally(() => {
        if (seq !== pitRequestSeq.current) return
        pitInFlight.current = false
        setPitBusy(false)
      })
  }, [sessionId, live, atMs, lap, driverId, onStrategyRequest])

  const handleWhatIf = useCallback((scenario: string) => {
    if (!sessionId || whatIfInFlight.current) return
    whatIfInFlight.current = true
    const seq = ++whatIfRequestSeq.current
    onStrategyRequest?.()
    setWhatIf(null)
    setWhatIfError(null)
    setWhatIfBusy(true)
    setWhatIfScenario(scenario)
    getWhatIf(sessionId, atMs, scenario, driverId)
      .then((res) => {
        if (seq === whatIfRequestSeq.current) setWhatIf(res)
      })
      .catch(() => {
        if (seq === whatIfRequestSeq.current) setWhatIfError('Finish calculation failed · try again')
      })
      .finally(() => {
        if (seq !== whatIfRequestSeq.current) return
        whatIfInFlight.current = false
        setWhatIfBusy(false)
      })
  }, [sessionId, atMs, driverId, onStrategyRequest])

  return (
    <div className="focus-card">
      <div className="focus-head" style={{ borderLeftColor: color }}>
        <span className="focus-code" style={{ color }}>{driverId}</span>
        {inPit && <span className="focus-inpit-badge">{lang === 'ru' ? 'В БОКСАХ' : 'IN PIT'}</span>}
        <span className="focus-pos">P{driver.position ?? '—'}</span>
        <span className="focus-gap">
          {driver.position === 1 ? (lang === 'ru' ? 'ЛИДЕР' : 'LEADER') : fmtGap(driver.gap_s, lang)}
        </span>
        <span className={`focus-tyre ty ${compound}`}>
          {compound}<span className="age">{driver.tyre_age_laps ?? '—'}</span>
        </span>
        <span className="focus-pits" style={{ fontSize: 14 }}>{driver.pit_count ?? 0}×{lang === 'ru' ? 'ПИТ' : 'PIT'}</span>
      </div>
      <div className="focus-laps">
        {laps.length === 0 && <span className="focus-lap-cell dim">—</span>}
        {laps.map((ms, i) => {
          const prev = laps[i - 1]
          const delta = prev !== undefined ? ms - prev : null
          const isLast = i === laps.length - 1
          return (
            <span key={i} className="focus-lap-cell">
              <span className="focus-lap-time">{fmtLap(ms)}</span>
              {isLast && pitLap && (
                <span className="focus-pitlap-ann">{lang === 'ru' ? 'КРУГ С ПИТОМ' : 'PIT LAP'}</span>
              )}
              {delta !== null && (
                <span className={`focus-lap-delta ${delta < 0 ? 'up' : 'down'}`}>
                  {delta < 0 ? '▲' : '▼'}{Math.abs(delta / 1000).toFixed(2)}
                </span>
              )}
            </span>
          )
        })}
      </div>
      {driver.interval_s !== null && driver.position !== 1 && (
        <div className="focus-int">
          {lang === 'ru' ? 'ИНТ' : 'INT'} <b>+{driver.interval_s.toFixed(2)}{lang === 'ru' ? 'с' : 's'}</b> {lang === 'ru' ? 'до машины впереди' : 'to car ahead'}
        </div>
      )}
      {(sessionId || live) && (
        <div className="focus-pit-row" aria-busy={pitBusy}>
          <button className="b pit-now-btn" type="button" onClick={handlePitNow} disabled={pitBusy}>
            {lang === 'ru' ? 'В БОКСЫ СЕЙЧАС' : 'PIT NOW'}
          </button>
          {pitBusy && <div className="strategy-action-status" role="status">{lang === 'ru' ? 'РАСЧЁТ ПИТ-ОКНА…' : 'CALCULATING PIT WINDOW…'}</div>}
          {pitSim && !pitSim.error && pitSim.evidence && <PitSimCard lang={lang} ev={pitSim.evidence} snapshot={pitSnapshot} />}
          {(pitError || pitSim?.error) && <div className="strategy-action-error" role="alert">{pitError
            ? (lang === 'ru' ? 'Не удалось рассчитать пит-стоп · повторите попытку' : pitError)
            : viewerError(pitSim?.error ?? '', lang)}</div>}
        </div>
      )}
      {sessionId && (
        // WHAT IF has no live mirror (server-side) — replay only.
        <div className="focus-whatif-section" aria-busy={whatIfBusy}>
          <div className="focus-whatif-label">{lang === 'ru' ? 'ЧТО ЕСЛИ' : 'WHAT IF'}</div>
          <div className="focus-whatif-btns">
            <button
              className={`b whatif-btn${whatIfScenario === 'pit_now' ? ' active' : ''}`}
              type="button"
              onClick={() => handleWhatIf('pit_now')}
              disabled={whatIfBusy}
              title={lang === 'ru' ? 'Прогноз финиша при пит-стопе сейчас' : 'Full race projection if driver pits right now'}
            >
              {lang === 'ru' ? 'ФИНИШ С ПИТОМ' : 'PIT FINISH'}
            </button>
            <button
              className={`b whatif-btn${whatIfScenario === 'stay_out' ? ' active' : ''}`}
              type="button"
              onClick={() => handleWhatIf('stay_out')}
              disabled={whatIfBusy}
              title={lang === 'ru' ? 'Прогноз финиша на текущих шинах без пит-стопа' : 'Full race projection if driver stays out on current tyres'}
            >
              {lang === 'ru' ? 'ОСТАТЬСЯ НА ТРАССЕ' : 'STAY OUT'}
            </button>
          </div>
          {whatIfBusy && <div className="strategy-action-status" role="status">{lang === 'ru' ? 'РАСЧЁТ ФИНИША…' : 'CALCULATING FINISH…'}</div>}
          {whatIfError && <div className="strategy-action-error" role="alert">{lang === 'ru' ? 'Не удалось рассчитать финиш · повторите попытку' : whatIfError}</div>}
          {whatIf && !whatIfBusy && <WhatIfCard lang={lang} result={whatIf} />}
        </div>
      )}
    </div>
  )
}

/** H2H half-panel for one driver */
function H2HDriver({ lang, driverId, driver }: { lang: Lang; driverId: string; driver: DriverState }) {
  const color = teamColor(driverId)
  const laps = recentLaps(driver)
  const compound = driver.tyre_compound?.charAt(0).toUpperCase() ?? '?'

  return (
    <div className="h2h-half">
      <div className="h2h-code" style={{ color }}>{driverId}</div>
      <div className="h2h-meta">
        <span className="h2h-pos">P{driver.position ?? '—'}</span>
        <span className="h2h-gap">{driver.position === 1 ? (lang === 'ru' ? 'ЛИДЕР' : 'LEADER') : fmtGap(driver.gap_s, lang)}</span>
      </div>
      <div className="h2h-laps">
        {laps.length === 0 && <span className="h2h-lap-cell"><span className="h2h-lap-time">—</span></span>}
        {laps.map((ms, i) => {
          const prev = laps[i - 1]
          const delta = prev !== undefined ? ms - prev : null
          return (
            <span key={i} className="h2h-lap-cell">
              <span className="h2h-lap-time">{fmtLap(ms)}</span>
              {delta !== null && (
                <span className={`h2h-lap-delta ${delta < 0 ? 'up' : 'down'}`}>
                  {delta < 0 ? '▲' : '▼'}{Math.abs(delta / 1000).toFixed(2)}
                </span>
              )}
            </span>
          )
        })}
      </div>
      <div className="h2h-tyre-row">
        <span className={`ty ${compound}`}>{compound}<span className="age">{driver.tyre_age_laps ?? '—'}</span></span>
        <span className="h2h-pits">{driver.pit_count ?? 0}×{lang === 'ru' ? 'ПИТ' : 'PIT'}</span>
      </div>
    </div>
  )
}

function H2HDeltas({
  lang, idA, idB, driverA, driverB,
}: {
  lang: Lang; idA: string; idB: string; driverA: DriverState; driverB: DriverState
}) {
  const gapDiff = driverA.gap_s !== null && driverB.gap_s !== null
    ? Math.abs(driverA.gap_s - driverB.gap_s)
    : null
  const lastDiff = driverA.last_lap_ms !== null && driverB.last_lap_ms !== null
    ? driverA.last_lap_ms - driverB.last_lap_ms
    : null
  const tyreDiff = driverA.tyre_age_laps !== null && driverB.tyre_age_laps !== null
    ? driverA.tyre_age_laps - driverB.tyre_age_laps
    : null

  return (
    <div className="h2h-deltas">
      <div className="h2h-delta-cell">
        <span className="h2h-delta-label">{lang === 'ru' ? 'Δ ОТРЫВ' : 'Δ GAP'}</span>
        <span className="h2h-delta-val">{gapDiff !== null ? `${gapDiff.toFixed(1)}${lang === 'ru' ? 'с' : 's'}` : '—'}</span>
      </div>
      <div className="h2h-delta-cell">
        <span className="h2h-delta-label">{lang === 'ru' ? 'Δ ПОСЛ. КРУГ' : 'Δ LAST'}</span>
        <span className="h2h-delta-val">
          {lastDiff !== null
            ? `${(Math.abs(lastDiff) / 1000).toFixed(2)}${lang === 'ru' ? 'с' : 's'}`
            : '—'}
        </span>
        {lastDiff !== null && (() => {
          const fasterId = lastDiff < 0 ? idA : idB
          return (
            <span
              className="h2h-delta-who h2h-delta-faster"
              style={{ color: teamColor(fasterId) }}
            >{fasterId} {lang === 'ru' ? 'быстрее' : 'faster'}</span>
          )
        })()}
      </div>
      <div className="h2h-delta-cell">
        <span className="h2h-delta-label">{lang === 'ru' ? 'Δ ШИНЫ' : 'Δ TYRE'}</span>
        <span className="h2h-delta-val">
          {tyreDiff !== null ? `${Math.abs(tyreDiff)}${lang === 'ru' ? 'К' : 'L'}` : '—'}
        </span>
        {tyreDiff !== null && tyreDiff !== 0 && (
          <span className="h2h-delta-who">{tyreDiff > 0 ? idA : idB} {lang === 'ru' ? 'старше' : 'older'}</span>
        )}
      </div>
    </div>
  )
}

export const FocusPanel = React.memo(function FocusPanel({ lang = 'en', selectedIds, drivers, sessionId, live, atMs, lap, onStrategyRequest, onRemoveDriver }: Props) {
  if (selectedIds.length === 0) return null

  const [idA, idB] = selectedIds
  const driverA = drivers[idA]
  const driverB = idB ? drivers[idB] : null

  if (!driverA) return null

  const isH2H = driverB !== null && idB !== undefined
  const selection = (
    <div className="focus-selection" aria-label={lang === 'ru' ? 'Выбранные гонщики' : 'Selected drivers'}>
      {selectedIds.map((id) => (
        <button type="button" key={id} onClick={() => onRemoveDriver(id)} aria-label={lang === 'ru' ? `Убрать ${id} из выбранных гонщиков` : `Remove ${id} from driver focus`}>
          {id}<span aria-hidden="true"> ×</span>
        </button>
      ))}
    </div>
  )

  if (isH2H && driverB) {
    return (
      <div className="focus-panel focus-panel-h2h">
        {selection}
        <div className="focus-h2h-label">{lang === 'ru' ? 'СРАВНЕНИЕ ГОНЩИКОВ' : 'HEAD TO HEAD'}</div>
        <div className="h2h-body">
          <H2HDriver lang={lang} driverId={idA} driver={driverA} />
          <div className="h2h-divider" />
          <H2HDriver lang={lang} driverId={idB!} driver={driverB} />
        </div>
        <H2HDeltas lang={lang} idA={idA} idB={idB!} driverA={driverA} driverB={driverB} />
      </div>
    )
  }

  return (
    <div className="focus-panel">
      {selection}
      <div className="focus-cards">
        <DriverCard lang={lang} key={idA} driverId={idA} driver={driverA} sessionId={sessionId} live={live} atMs={atMs} lap={lap} onStrategyRequest={onStrategyRequest} />
      </div>
    </div>
  )
})
