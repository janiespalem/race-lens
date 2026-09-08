import { useEffect, useState } from 'react'
import { getDriverOfDay } from '../../api/client'
import type { DotdCandidate, DotdResponse } from '../../api/types'
import { dotdResultOrder } from '../../lib/driverOfDay'
import { teamColor } from './teamColors'
import { useAsync } from './useAsync'

type Lang = 'en' | 'ru'

type Props = {
  sessionId: string
  lang?: Lang
  sessionStatus?: string
  /** Current lap in progress + total, to unlock the panel in the final laps. */
  lap?: number
  totalLaps?: number | null
  /** Current session time — the pick is computed spoiler-free up to here. */
  atMs?: number
}

const DOTD_VOTE_KEY = (sessionId: string) => `racelens_dotd_vote_${sessionId}`
/** DOTD voting opens this many laps before the finish (as in real F1). */
const UNLOCK_LAPS_TO_GO = 10

function loadUserPick(sessionId: string): string | null {
  try {
    return localStorage.getItem(DOTD_VOTE_KEY(sessionId))
  } catch {
    return null
  }
}

export function DriverOfDayPanel({ sessionId, lang = 'en', sessionStatus, lap, totalLaps, atMs }: Props) {
  const [open, setOpen] = useState(false)
  const [userPick, setUserPick] = useState<string | null>(null)
  const isFinished = sessionStatus === 'finished'

  // Snapshot the pick at the moment the panel opens — spoiler-free (race so
  // far). Ordinary atMs changes stay excluded so it doesn't churn while reading;
  // the finish phase is included so the official result is fetched exactly once.
  const { data, loading } = useAsync<DotdResponse>(
    () => getDriverOfDay(sessionId, isFinished ? undefined : atMs),
    [sessionId, isFinished],
    open,
  )

  useEffect(() => {
    if (!open) return
    // Load saved vote from localStorage
    setUserPick(loadUserPick(sessionId))
  }, [open, sessionId])

  // Reset on session change
  useEffect(() => {
    setUserPick(loadUserPick(sessionId))
  }, [sessionId])

  const vote = (driver: string) => {
    const next = userPick === driver ? null : driver
    setUserPick(next)
    try {
      if (next) localStorage.setItem(DOTD_VOTE_KEY(sessionId), next)
      else localStorage.removeItem(DOTD_VOTE_KEY(sessionId))
    } catch { /* noop */ }
  }

  const maxScore = data?.candidates[0]?.score ?? 1
  const lapsToGo = totalLaps != null && lap != null ? totalLaps - lap : null
  const available = isFinished || (lapsToGo != null && lapsToGo <= UNLOCK_LAPS_TO_GO)
  const dotdLabel = lang === 'ru'
    ? `открывается за ${UNLOCK_LAPS_TO_GO} кругов до финиша`
    : `unlocks in the final ${UNLOCK_LAPS_TO_GO} laps`
  const dotdTitle = isFinished
    ? (lang === 'ru' ? 'Официальное голосование и отдельные выборы' : 'Official fan vote and distinct picks')
    : (lang === 'ru' ? 'Предварительный выбор — по гонке пока' : 'Provisional pick — race so far')

  return (
    <div className="dotd-wrap">
      <button
        type="button"
        className={`tog${open ? ' tog-on' : ''}`}
        onClick={() => { if (available) setOpen((v) => !v) }}
        disabled={!available}
        title={available ? dotdTitle : dotdLabel}
        style={!available ? { opacity: 0.45, cursor: 'not-allowed' } : undefined}
        aria-disabled={!available}
      >
        DOTD
      </button>
      {!available && (
        <span className="dotd-gate-hint">{dotdLabel}</span>
      )}

      {open && (
        <div className="dotd-panel">
          <div className="dotd-header">
            <span className="dotd-title">{lang === 'ru' ? 'ГОНЩИК ДНЯ' : 'DRIVER OF THE DAY'}</span>
            <span className="dotd-sub">{lang === 'ru' ? 'официальное голосование · ваш выбор · Race Lens' : 'official fan vote · your pick · Race Lens'}</span>
          </div>
          <div className="dotd-footer" style={{ borderTop: 0, paddingTop: 0 }}>
            {lang === 'ru'
              ? 'Оценка: +3 за отыгранную позицию · +15 быстрейший круг · +10 камбэк (5+ поз.) · −0.5 за лишний пит'
              : 'Score: +3 per position gained · +15 fastest lap · +10 comeback (5+) · −0.5 per extra pit'}
          </div>
          {!isFinished && lapsToGo != null && (
            <div className="dotd-footer" style={{ borderTop: 0, paddingTop: 0, color: 'var(--red)' }}>
              {lang === 'ru'
                ? `предварительно — по гонке пока (осталось ${lapsToGo} кр.)`
                : `provisional — race so far (${lapsToGo} laps to go)`}
            </div>
          )}

          {loading && <div className="dotd-empty">{lang === 'ru' ? 'Загрузка…' : 'Loading…'}</div>}
          {!loading && !data && <div className="dotd-empty">{lang === 'ru' ? 'Нет данных' : 'No data'}</div>}

          {data && (
            <>
              <div className="dotd-results">
                {dotdResultOrder(isFinished, data.official_result, userPick, data.computed_pick).map((kind) => {
                  if (kind === 'official-pending') {
                    return (
                      <div className="dotd-result dotd-result-pending" key={kind}>
                        <span className="dotd-hero-label">{lang === 'ru' ? 'ОФИЦИАЛЬНЫЙ ВЫБОР БОЛЕЛЬЩИКОВ' : 'OFFICIAL FAN VOTE'}</span>
                        <span className="dotd-hero-note">{lang === 'ru' ? 'Ожидается официальный результат' : 'Official result pending'}</span>
                      </div>
                    )
                  }
                  const driver = kind === 'official'
                    ? data.official_result?.driver
                    : kind === 'user' ? userPick : data.computed_pick
                  if (!driver) return null
                  const candidate = data.candidates.find((item) => item.driver === driver)
                  return (
                    <div className="dotd-result" style={{ borderColor: teamColor(driver) }} key={kind}>
                      <span className="dotd-hero-code" style={{ color: teamColor(driver) }}>{driver}</span>
                      <span className="dotd-hero-label">
                        {kind === 'official'
                          ? (lang === 'ru' ? 'ОФИЦИАЛЬНЫЙ ВЫБОР БОЛЕЛЬЩИКОВ' : 'OFFICIAL FAN VOTE')
                          : kind === 'user'
                            ? (lang === 'ru' ? 'ВАШ ВЫБОР' : 'YOUR PICK')
                            : isFinished
                              ? (lang === 'ru' ? 'ВЫБОР RACE LENS' : 'RACE LENS PICK')
                              : (lang === 'ru' ? 'ПРЕДВАРИТЕЛЬНЫЙ ВЫБОР RACE LENS' : 'PROVISIONAL RACE LENS PICK')}
                      </span>
                      {kind === 'official' && data.official_result && (
                        <a href={data.official_result.source_url} target="_blank" rel="noreferrer" className="dotd-hero-note">
                          {data.official_result.percentage.toFixed(0)}% · {lang === 'ru' ? 'голосование болельщиков Formula 1' : data.official_result.provider}
                        </a>
                      )}
                      {kind === 'race-lens' && candidate && (
                        <span className="dotd-hero-note">
                          {lang === 'ru' ? candidate.note_ru : candidate.note_en}
                        </span>
                      )}
                    </div>
                  )
                })}
              </div>

              {/* Top-5 list */}
              <div className="dotd-list">
                {data.candidates.map((c: DotdCandidate) => {
                  const color = teamColor(c.driver)
                  const barPct = maxScore > 0 ? Math.max(0, (c.score / maxScore) * 100) : 0
                  const isUserVote = userPick === c.driver
                  return (
                    <button
                      key={c.driver}
                      type="button"
                      className={`dotd-row${isUserVote ? ' dotd-row-voted' : ''}`}
                      onClick={() => vote(c.driver)}
                      title={lang === 'ru' ? c.note_ru : c.note_en}
                    >
                      <span className="dotd-code" style={{ color }}>{c.driver}</span>
                      <div className="dotd-bar-wrap">
                        <div className="dotd-bar" style={{ width: `${barPct}%`, background: color }} />
                      </div>
                      <span className="dotd-score">{c.score.toFixed(1)}</span>
                      {isUserVote && <span className="dotd-check">{lang === 'ru' ? '✓ ВАШ ВЫБОР' : '✓ YOUR PICK'}</span>}
                    </button>
                  )
                })}
              </div>

              <div className="dotd-footer">
                {lang === 'ru' ? 'ваш выбор сохранён на устройстве · выбор Race Lens рассчитан алгоритмом' : 'your pick is saved locally · Race Lens pick is algorithmic'}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
