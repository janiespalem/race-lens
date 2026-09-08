import { viewerError } from '../../lib/viewerError'
import { sessionTypeLabel } from '../../lib/format'
import type { Lang } from './replayTypes'
import { useCallback, useEffect, useRef, useState } from 'react'
import { getLiveSessions } from '../../api/client'
import type { LiveSessionInfo, LiveSource } from '../../api/client'
import { TrackMap } from './TrackMap'

// ── Types ─────────────────────────────────────────────────────────────────────

type LobbyPhase = 'LOBBY' | 'SESSIONS' | 'COUNTDOWN' | 'LIVE'

type Props = {
  lang?: Lang
  signalrAvailable: boolean
  onStart: (year: number, country: string, sessionName: string, source: LiveSource) => Promise<void>
  onStop: () => void
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function parseUtcMs(iso: string): number {
  // OpenF1 date_start may arrive without timezone; treat as UTC
  const s = iso.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(iso) ? iso : iso + 'Z'
  return Date.parse(s)
}

function formatCountdown(remainMs: number): string {
  if (remainMs <= 0) return '00:00:00'
  const totalSec = Math.floor(remainMs / 1000)
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  return [h, m, s].map((v) => String(v).padStart(2, '0')).join(':')
}

function localTimeLabel(iso: string): string {
  const ms = parseUtcMs(iso)
  if (Number.isNaN(ms)) return iso
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// ── Component ─────────────────────────────────────────────────────────────────

export function LiveLobby({ lang = 'en', signalrAvailable, onStart, onStop }: Props) {
  const [phase, setPhase] = useState<LobbyPhase>('LOBBY')

  // LOBBY inputs
  const [year, setYear] = useState(() => new Date().getFullYear())
  const [country, setCountry] = useState('')
  // signalr = free official F1 live-timing feed (default); openf1 realtime is paid.
  const [source, setSource] = useState<LiveSource>(signalrAvailable ? 'signalr' : 'openf1')
  // signalr only: FastF1 session name — no OpenF1 discovery involved.
  const [sessionName, setSessionName] = useState('Race')
  const [loadBusy, setLoadBusy] = useState(false)
  const [loadErr, setLoadErr] = useState<string | null>(null)

  // SESSIONS list
  const [sessions, setSessions] = useState<LiveSessionInfo[]>([])

  // COUNTDOWN target
  const [countdownTarget, setCountdownTarget] = useState<LiveSessionInfo | null>(null)
  const [remainMs, setRemainMs] = useState(0)
  const countdownRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Clear intervals on unmount
  useEffect(() => {
    return () => {
      if (countdownRef.current) clearInterval(countdownRef.current)
    }
  }, [])

  const handleLoad = useCallback(async () => {
    if (!country.trim()) { setLoadErr('Enter a country / event name'); return }
    setLoadBusy(true)
    setLoadErr(null)
    try {
      const list = await getLiveSessions(year, country.trim())
      setSessions(list)
      setPhase('SESSIONS')
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : 'Failed to load sessions')
    } finally {
      setLoadBusy(false)
    }
  }, [year, country])

  // F1 FEED path: connect straight to the official SignalR feed — no OpenF1
  // discovery (which 401/502s on race days), no session list, just start.
  const startSession = useCallback(async (name: string, nextSource: LiveSource) => {
    if (!country.trim()) { setLoadErr('Enter the Grand Prix name, e.g. Silverstone'); return }
    setLoadBusy(true)
    setLoadErr(null)
    try {
      await onStart(year, country.trim(), name, nextSource)
      setPhase('LIVE')
    } catch (error) {
      setLoadErr(error instanceof Error ? error.message : 'Failed to start live session')
    } finally {
      setLoadBusy(false)
    }
  }, [year, country, onStart])

  const handleDirectStart = useCallback(() => {
    void startSession(sessionName.trim() || 'Race', 'signalr')
  }, [sessionName, startSession])

  const handleSessionClick = useCallback((session: LiveSessionInfo) => {
    if (session.started) {
      void startSession(session.session_name, source)
    } else {
      // Enter countdown
      setCountdownTarget(session)
      const target = parseUtcMs(session.date_start)
      setRemainMs(Math.max(0, target - Date.now()))
      setPhase('COUNTDOWN')
    }
  }, [source, startSession])

  // Countdown tick + live-start polling
  useEffect(() => {
    if (phase !== 'COUNTDOWN' || !countdownTarget) return

    const target = parseUtcMs(countdownTarget.date_start)
    const tick = () => {
      const r = Math.max(0, target - Date.now())
      setRemainMs(r)
    }
    tick()
    countdownRef.current = setInterval(tick, 1000)

    return () => {
      if (countdownRef.current) { clearInterval(countdownRef.current); countdownRef.current = null }
    }
  }, [phase, countdownTarget])

  // Also auto-start when countdown hits zero
  useEffect(() => {
    if (phase === 'COUNTDOWN' && remainMs === 0 && countdownTarget) {
      void startSession(countdownTarget.session_name, source)
    }
  }, [remainMs, phase, countdownTarget, source, startSession])

  // ── Render ─────────────────────────────────────────────────────────────────

  if (phase === 'LOBBY') {
    return (
      <main className="live-lobby">
        <section className="live-lobby-card" aria-labelledby="live-lobby-title">
          <span className="live-lobby-kicker">{(lang === 'ru' ? 'ЗАПИСЬ ЭФИРА' : 'LIVE CAPTURE')}</span>
          <h1 id="live-lobby-title">{(lang === 'ru' ? 'Подключение к хронометражу F1' : 'Connect to F1 live timing')}</h1>
          <p className="live-lobby-intro">
            {lang === 'ru' ? 'Подключитесь к официальному потоку до начала сессии. Статус ОЖИДАНИЕ сохраняется до первого пакета хронометража.' : 'Start the official feed before the session. A WAITING state is normal until the first timing packet arrives.'}
          </p>
          <div className="live-lobby-controls">
            <label>
              <span>{(lang === 'ru' ? 'СЕЗОН' : 'SEASON')}</span>
              <input
                className="live-input live-year"
                type="number"
                value={year}
                min={2018}
                max={2030}
                onChange={(event) => setYear(Number(event.target.value))}
                inputMode="numeric"
              />
            </label>
            <label>
              <span>{(lang === 'ru' ? 'ГРАН-ПРИ' : 'GRAND PRIX')}</span>
              <input
                className="live-input live-event"
                type="text"
                value={country}
                placeholder={(lang === 'ru' ? 'Belgium или Spa' : 'Belgium or Spa')}
                onChange={(event) => setCountry(event.target.value)}
                onKeyDown={(event) => event.key === 'Enter' && (
                  source === 'signalr' ? handleDirectStart() : void handleLoad()
                )}
              />
            </label>
            {source === 'signalr' ? (
              <>
                <label>
                  <span>{(lang === 'ru' ? 'СЕССИЯ' : 'SESSION')}</span>
                  <select
                    className="live-input live-session"
                    value={sessionName}
                    onChange={(event) => setSessionName(event.target.value)}
                  >
                    <option value="Practice 1">{sessionTypeLabel('Practice 1', lang)}</option>
                    <option value="Practice 2">{sessionTypeLabel('Practice 2', lang)}</option>
                    <option value="Practice 3">{sessionTypeLabel('Practice 3', lang)}</option>
                    <option value="Sprint Qualifying">{sessionTypeLabel('Sprint Qualifying', lang)}</option>
                    <option value="Sprint">{sessionTypeLabel('Sprint', lang)}</option>
                    <option value="Qualifying">{sessionTypeLabel('Qualifying', lang)}</option>
                    <option value="Race">{sessionTypeLabel('Race', lang)}</option>
                  </select>
                </label>
                <button className="b primary" type="button" onClick={handleDirectStart} disabled={loadBusy}>
                  {lang === 'ru' ? (loadBusy ? 'ПОДКЛЮЧЕНИЕ…' : 'НАЧАТЬ ЭФИР') : (loadBusy ? 'CONNECTING…' : 'START LIVE')}
                </button>
              </>
            ) : (
              <button className="b primary" type="button" onClick={handleLoad} disabled={loadBusy}>
                {lang === 'ru' ? (loadBusy ? 'ЗАГРУЗКА…' : 'НАЙТИ СЕССИИ') : (loadBusy ? 'LOADING…' : 'FIND SESSIONS')}
              </button>
            )}
          </div>
          <div className="live-source-row">
            <div className="tog-group live-source-toggle" title={(lang === 'ru' ? 'Источник данных эфира' : 'Live data source')}>
              <button
                type="button"
                className={`tog${source === 'signalr' ? ' tog-on' : ''}`}
                onClick={() => setSource('signalr')}
                disabled={!signalrAvailable}
                title={(lang === 'ru' ? 'Официальный хронометраж F1 — бесплатно, прямое подключение' : 'Official F1 live-timing feed — free, direct connect')}
              >
                {lang === 'ru' ? 'ДАННЫЕ F1' : 'F1 FEED'}
              </button>
              <button
                type="button"
                className={`tog${source === 'openf1' ? ' tog-on' : ''}`}
                onClick={() => setSource('openf1')}
                title={(lang === 'ru' ? 'OpenF1 API — прямой эфир платный; бесплатные данные с задержкой' : 'OpenF1 API — realtime tier is paid; free tier is delayed')}
              >
                OPENF1
              </button>
            </div>
            <p>
              {source === 'signalr'
                ? (lang === 'ru' ? 'Бесплатный официальный поток · подключается сразу и ждёт выбранную сессию.' : 'Free official feed · connects immediately and waits for the selected session.')
                : (lang === 'ru' ? 'Поиск OpenF1 · для прямого эфира может потребоваться платный тариф.' : 'OpenF1 discovery · live timing may require its paid realtime tier.')}
            </p>
          </div>
          {loadErr && <div className="live-err" role="alert">{viewerError(loadErr, lang)}</div>}
        </section>
        <aside className="live-lobby-guide">
          <span>{(lang === 'ru' ? 'ПЕРЕД СТАРТОМ' : 'BEFORE LIGHTS OUT')}</span>
          <strong>{(lang === 'ru' ? '1 · Подключитесь' : '1 · Connect')}</strong>
          <p>{(lang === 'ru' ? 'Используйте названия этапа и сессии из расписания F1.' : 'Use the same event and session names as the F1 schedule.')}</p>
          <strong>{(lang === 'ru' ? '2 · Оставьте подключение открытым' : '2 · Leave it running')}</strong>
          <p>{(lang === 'ru' ? 'Race Lens пропускает предыдущую сессию и ждёт выбранную.' : 'Race Lens rejects the previous session and waits for the selected one.')}</p>
          <strong>{(lang === 'ru' ? '3 · Следите за статусом' : '3 · Watch the status')}</strong>
          <p>{(lang === 'ru' ? 'ОЖИДАНИЕ сменится ЭФИРОМ после первого корректного кадра хронометража.' : 'WAITING becomes LIVE after the first valid timing frame.')}</p>
        </aside>
      </main>
    )
  }

  if (phase === 'SESSIONS') {
    return (
      <main className="live-lobby live-lobby-sessions">
        <section className="live-lobby-card">
          <div className="live-sessions-head">
            <span>
              {year} {country.toUpperCase()}
            </span>
            <button className="b" type="button" onClick={() => setPhase('LOBBY')}>
              {lang === 'ru' ? 'НАЗАД' : 'BACK'}
            </button>
          </div>
          <div className="live-session-list">
            {sessions.map((s) => (
              <button
                key={s.session_key}
                type="button"
                className={`b${s.started ? ' primary' : ''}`}
                onClick={() => handleSessionClick(s)}
                disabled={loadBusy}
                title={s.started ? `${lang === 'ru' ? 'Начало в' : 'Started at'} ${localTimeLabel(s.date_start)}` : `${lang === 'ru' ? 'Начнётся в' : 'Starts at'} ${localTimeLabel(s.date_start)}`}
              >
                {sessionTypeLabel(s.session_name, lang)}
                <span>
                  {localTimeLabel(s.date_start)}{s.started ? '' : (lang === 'ru' ? ' (по расписанию)' : ' (scheduled)')}
                </span>
              </button>
            ))}
            {sessions.length === 0 && <p>{(lang === 'ru' ? 'Сессии для этого этапа не найдены.' : 'No sessions found for this event.')}</p>}
          </div>
          {loadErr && <div className="live-err" role="alert">{viewerError(loadErr, lang)}</div>}
        </section>
      </main>
    )
  }

  if (phase === 'COUNTDOWN' && countdownTarget) {
    return (
      <main className="live-countdown">
        <TrackMap
          lang={lang}
          sessionId={null}
          atMs={0}
          playing={false}
          playbackSpeed={1}
          drivers={{}}
          classification={[]}
          sessionStatus="started"
          positionsData={null}
        />
        <div className="live-countdown-overlay">
          <span>
            {sessionTypeLabel(countdownTarget.session_name, lang).toUpperCase()} · {lang === 'ru' ? 'НАЧАЛО ЧЕРЕЗ' : 'STARTS IN'}
          </span>
          <strong>
            {formatCountdown(remainMs)}
          </strong>
          <div className="live-countdown-actions">
            <button className="b" type="button" onClick={() => setPhase('SESSIONS')}>{(lang === 'ru' ? 'НАЗАД' : 'BACK')}</button>
            <button className="b danger" type="button" onClick={() => { setPhase('LOBBY'); onStop() }}>{(lang === 'ru' ? 'СТОП' : 'STOP')}</button>
          </div>
          <small>
            {lang === 'ru' ? 'ЗАПИСЬ ЭФИРА НАЧНЁТСЯ ПО РАСПИСАНИЮ' : 'LIVE CAPTURE STARTS AT THE SCHEDULED TIME'}
          </small>
        </div>
      </main>
    )
  }

  // LIVE phase — render nothing here; parent has taken over
  return null
}
