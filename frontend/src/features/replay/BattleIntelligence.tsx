import { useMemo } from 'react'
import type { Battle, DriverState, WeatherState } from '../../api/types'
import { battleGap, battlePair } from '../../lib/battles'
import { formatLapTime } from '../../lib/format'
import { compoundLabel } from '../../lib/stints'
import { formatWeather, formatWeatherAge } from '../../lib/weather'
import { teamColor } from './teamColors'

type DriverRow = { id: string } & DriverState

type Props = {
  lang?: 'en' | 'ru'
  rows: DriverRow[]
  battles: Battle[]
  currentLap: number
  totalLaps: number | null
  weather?: WeatherState | null
  weatherObservedAtMs?: Record<string, number>
  atMs: number
  onSelectDriver: (id: string) => void
  onSelectBattle: (ids: string[]) => void
}

const recentPace = (driver: DriverRow): number | null => {
  const laps = driver.recent_laps_ms.filter((lap) => lap > 0).slice(-5)
  if (laps.length === 0) return driver.last_lap_ms && driver.last_lap_ms > 0
    ? driver.last_lap_ms
    : null
  return laps.reduce((sum, lap) => sum + lap, 0) / laps.length
}

export function BattleIntelligence({
  lang = 'en',
  rows,
  battles,
  currentLap,
  totalLaps,
  weather,
  weatherObservedAtMs,
  atMs,
  onSelectDriver,
  onSelectBattle,
}: Props) {
  const leadFlow = rows.slice(0, 3)
  const byId = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows])
  const fastest = useMemo(() => rows.reduce<DriverRow | null>((best, row) => {
    if (!row.best_lap_ms || row.best_lap_ms <= 0) return best
    return !best || !best.best_lap_ms || row.best_lap_ms < best.best_lap_ms ? row : best
  }, null), [rows])
  const pace = useMemo(() => rows
    .filter((row) => !row.retired)
    .map((row) => ({ row, average: recentPace(row) }))
    .filter((item): item is { row: DriverRow; average: number } => item.average !== null)
    .sort((a, b) => a.average - b.average)
    .slice(0, 7), [rows])
  const fastestPace = pace[0]?.average
  const weatherSummary = formatWeather(weather, lang)
  const weatherAge = formatWeatherAge(weather, weatherObservedAtMs, atMs, lang)

  return (
    <section className="battle-intelligence" aria-label={lang === 'ru' ? 'Анализ борьбы' : 'Battle intelligence'}>
      <div className="bi-grid">
        {rows.length === 0 ? (
          <div className="bi-formation">
            <small>{lang === 'ru' ? 'СЕССИЯ ГОТОВА' : 'SESSION READY'}</small>
            <strong>{lang === 'ru' ? 'ПРОГРЕВОЧНЫЙ КРУГ' : 'FORMATION LAP'}</strong>
          </div>
        ) : (
          <>
        <article className="bi-card bi-flow">
          <div className="bi-card-head">
            <b>{lang === 'ru' ? 'ТРОЙКА ЛИДЕРОВ' : 'TOP 3'}</b>
          </div>
          <div className="bi-flow-list">
            {leadFlow.map((driver, index) => (
              <div key={driver.id}>
                {index > 0 && (
                  <div className={`bi-gap-link${(driver.interval_s ?? 99) <= 1 ? ' hot' : ''}`}>
                    {driver.interval_s != null ? `${driver.interval_s.toFixed(2)}${lang === 'ru' ? 'с' : 's'}` : '—'}
                  </div>
                )}
                <button
                  type="button"
                  className="bi-driver"
                  onClick={() => onSelectDriver(driver.id)}
                >
                  <span className="bi-rank">P{driver.position ?? index + 1}</span>
                  <i style={{ background: teamColor(driver.id) }} />
                  <strong>{driver.id}</strong>
                  <span className="bi-driver-meta">
                    <b>{driver.retired ? (lang === 'ru' ? 'СХОД' : 'OUT') : `${compoundLabel(driver.tyre_compound, lang)} · ${driver.tyre_age_laps ?? '—'} ${lang === 'ru' ? 'кр.' : 'laps'}`}</b>
                    <small>{formatLapTime(driver.last_lap_ms)}</small>
                  </span>
                  <span className="bi-driver-gap">
                    {index === 0 ? (lang === 'ru' ? 'ЛИДЕР' : 'LEADER') : driver.gap_s != null ? `+${driver.gap_s.toFixed(2)}` : '—'}
                  </span>
                </button>
              </div>
            ))}
            {leadFlow.length === 0 && <div className="bi-empty">{lang === 'ru' ? 'Ожидание классификации…' : 'Waiting for classification…'}</div>}
          </div>
        </article>

        <article className="bi-card bi-state">
          <div className="bi-card-head">
            <b>{lang === 'ru' ? 'ГОНКА' : 'RACE'}</b>
            {weatherSummary && (
              <span>
                {weatherSummary}
                {weatherAge && <small className={`bi-weather-age${weatherAge.startsWith(lang === 'ru' ? 'УСТАРЕЛО' : 'STALE') ? ' stale' : ''}`}>{weatherAge}</small>}
              </span>
            )}
          </div>
          <div className="bi-lap">
            {currentLap || '—'} <small>/ {totalLaps ?? '—'} {lang === 'ru' ? 'КР.' : 'LAPS'}</small>
          </div>
          <div className="bi-state-grid">
            <div><span>{lang === 'ru' ? 'ЛИДЕР' : 'LEADER'}</span><strong>{rows[0]?.id ?? '—'}</strong></div>
            <div><span>{lang === 'ru' ? 'БЫСТРЕЙШИЙ' : 'FASTEST'}</span><strong>{fastest?.id ?? '—'}</strong></div>
            <div><span>{lang === 'ru' ? 'НА ТРАССЕ' : 'RUNNING'}</span><strong>{rows.filter((row) => !row.retired).length}</strong></div>
          </div>
        </article>

        <article className="bi-card bi-battles">
          <div className="bi-card-head">
            <b>{lang === 'ru' ? 'БОРЬБА НА ТРАССЕ' : 'ACTIVE BATTLES'} · {battles.length}</b>
          </div>
          <div className="bi-battle-list">
            {battles.slice(0, 5).map((battle) => {
              const pair = battlePair(battle)
              const gap = battleGap(battle)
              if (!pair || gap === null) return null
              const [leaderId, chaserId] = pair
              const leader = byId.get(leaderId)
              const strength = Math.max(8, 100 - Math.min(100, gap * 45))
              return (
                <button
                  type="button"
                  className="bi-battle-row"
                  key={`${leaderId}-${chaserId}`}
                  onClick={() => onSelectBattle([leaderId, chaserId])}
                >
                  <span>P{leader?.position ?? '—'}</span>
                  <strong>{leaderId} / {chaserId}</strong>
                  <i><b style={{ width: `${strength}%` }} /></i>
                  <em>{gap.toFixed(2)}{lang === 'ru' ? 'с' : 's'}</em>
                </button>
              )
            })}
            {battles.length === 0 && <div className="bi-empty">{lang === 'ru' ? 'Сейчас активной борьбы нет.' : 'No active battles right now.'}</div>}
          </div>
        </article>

        <article className="bi-card bi-pace">
          <div className="bi-card-head">
            <b>{lang === 'ru' ? 'ТЕМП ЗА 5 КРУГОВ' : '5-LAP PACE'}</b>
          </div>
          <div className="bi-pace-list">
            {pace.map(({ row, average }) => {
              const delta = average - (fastestPace ?? average)
              return (
                <button
                  type="button"
                  className="bi-pace-row"
                  key={row.id}
                  onClick={() => onSelectDriver(row.id)}
                  title={`${row.id} · ${formatLapTime(average)}`}
                >
                  <i style={{ background: teamColor(row.id) }} />
                  <strong>{row.id}</strong>
                  <span>{formatLapTime(average)}</span>
                  <em>{delta === 0 ? (lang === 'ru' ? 'ЛУЧШИЙ' : 'FASTEST') : `+${(delta / 1000).toFixed(3)}${lang === 'ru' ? 'с' : 's'}`}</em>
                </button>
              )
            })}
            {pace.length === 0 && <div className="bi-empty">{lang === 'ru' ? 'Для сравнения темпа нужен завершённый круг.' : 'Complete a lap to compare pace.'}</div>}
          </div>
        </article>
          </>
        )}
      </div>
    </section>
  )
}
