import { useState, type ReactNode } from 'react'
import { sessionMeta, sessionTypeLabel } from '../../lib/format'
import type { Lang, Level } from './useReplay'
import { DriverOfDayPanel } from './DriverOfDayPanel'
import { HighlightsPanel } from './HighlightsPanel'
import type { DeskMode } from './workspace'

type AppMode = 'replay' | 'live'

type Props = {
  sessionId: string | null
  lap: number
  totalLaps: number | null
  lang: Lang
  level: Level
  mode: AppMode
  liveAvailable: boolean
  liveNowAvailable: boolean
  projection: boolean
  voice: boolean
  desk: DeskMode
  customEditing: boolean
  onModeChange: (mode: AppMode) => void
  onLang: (lang: Lang) => void
  onLevel: (level: Level) => void
  onProjection: (on: boolean) => void
  onVoice: (on: boolean) => void
  onDeskChange: (desk: DeskMode) => void
  onEditCustom: () => void
  onSeek?: (ms: number) => void
  onSettingsOpen?: () => void
  onCatalogOpen?: () => void
  sessionStatus?: string
  atMs?: number
  anchoredHighlights?: boolean
  anchoredDotd?: boolean
  /** Live-only session badge text, e.g. "SILVERSTONE · RACE". */
  sessionName?: string | null
  companion?: ReactNode
}

export function TopBar({ sessionId, lap, totalLaps, lang, level, mode, liveAvailable, liveNowAvailable, projection, voice, desk, customEditing, onModeChange, onLang, onLevel, onProjection, onVoice, onDeskChange, onEditCustom, onSeek, onSettingsOpen, onCatalogOpen, sessionStatus, atMs, anchoredHighlights = true, anchoredDotd = true, sessionName, companion }: Props) {
  const current = sessionId ? sessionMeta(sessionId) : null
  const sessionTriggerLabel = current?.year
    ? `${current.year} · ${current.event} · ${sessionTypeLabel(current.type)}`
    : 'YEAR · EVENT · SESSION'
  const [layersOpen, setLayersOpen] = useState(false)
  // LAYERS badge lights up when any optional layer is active.
  const anyLayer = projection || voice || level === 'beginner' || desk === 'custom' || customEditing
  return (
    <div className="top">
      <div className="ident">
        <span>RACE LENS</span>
      </div>

      {mode === 'replay' ? (
        <div className="sess">
          {onCatalogOpen && (
            <button type="button" className="catalog-open" onClick={onCatalogOpen}>
              {sessionTriggerLabel}
            </button>
          )}
        </div>
      ) : (
        <div className="sess">
          <b>LIVE</b>
          <i>{sessionName ? `${sessionName} · ` : 'Near-live · '}F1 feed</i>
        </div>
      )}

      <div className="top-toggles">
        {/* Mode toggle — always visible */}
        <div className="tog-group">
          <button
            type="button"
            className={`tog${mode === 'replay' ? ' tog-on' : ''}`}
            onClick={() => onModeChange('replay')}
          >REPLAY</button>
          {liveAvailable && (
            <button
              type="button"
              className={`tog${mode === 'live' ? ' tog-on' : ''}`}
              onClick={() => onModeChange('live')}
            >{liveNowAvailable ? 'LIVE NOW' : 'LIVE'}</button>
          )}
        </div>
        {/* LAYERS popover — collects the optional view layers so the bar stays clean. */}
        <div className="tog-group tog-group--secondary layers-wrap">
          <button
            type="button"
            className={`tog${anyLayer ? ' tog-on' : ''}`}
            aria-expanded={layersOpen}
            onClick={() => setLayersOpen((o) => !o)}
          >LAYERS ▾</button>
          {layersOpen && (
            <>
              <div className="layers-backdrop" onClick={() => setLayersOpen(false)} />
              <div className="layers-pop" role="menu">
                <div className="layer-row">
                  <span className="layer-name">DETAIL</span>
                  <div className="tog-group">
                    <button
                      type="button"
                      className={`tog${level === 'beginner' ? ' tog-on' : ''}`}
                      onClick={() => onLevel('beginner')}
                    >ROOKIE</button>
                    <button
                      type="button"
                      className={`tog${level === 'pro' ? ' tog-on' : ''}`}
                      onClick={() => onLevel('pro')}
                    >PRO</button>
                  </div>
                </div>
                <div className="layer-row">
                  <span className="layer-name">DESK</span>
                  <div className="tog-group">
                    {(['classic', 'custom'] as const).map((value) => (
                      <button
                        type="button"
                        className={`tog${desk === value ? ' tog-on' : ''}`}
                        key={value}
                        onClick={() => {
                          onDeskChange(value)
                          setLayersOpen(false)
                        }}
                      >{value.toUpperCase()}</button>
                    ))}
                  </div>
                </div>
                <button
                  type="button"
                  className={`layer-row layer-toggle${projection ? ' on' : ''}`}
                  onClick={() => onProjection(!projection)}
                >
                  <span className="layer-name">PACE OUTLOOK</span>
                  <span className="layer-state">{projection ? 'ON' : 'OFF'}</span>
                </button>
                <button
                  type="button"
                  className={`layer-row layer-toggle${voice ? ' on' : ''}`}
                  onClick={() => onVoice(!voice)}
                >
                  <span className="layer-name">VOICE</span>
                  <span className="layer-state">{voice ? 'ON' : 'OFF'}</span>
                </button>
                <button
                  type="button"
                  className={`layer-row layer-toggle${customEditing ? ' on' : ''}`}
                  onClick={() => {
                    setLayersOpen(false)
                    onEditCustom()
                  }}
                >
                  <span className="layer-name">EDIT CUSTOM</span>
                  <span className="layer-state">{customEditing ? 'EDITING' : '→'}</span>
                </button>
              </div>
            </>
          )}
        </div>
        <div className="tog-group tog-group--secondary" aria-label="Language">
          <button
            type="button"
            className={`tog${lang === 'en' ? ' tog-on' : ''}`}
            onClick={() => onLang('en')}
          >EN</button>
          <button
            type="button"
            className={`tog${lang === 'ru' ? ' tog-on' : ''}`}
            onClick={() => onLang('ru')}
          >RU</button>
        </div>
      </div>

      {/* Replay review panels; the catalog owns session selection. */}
      {mode === 'replay' && sessionId && onSeek && (anchoredHighlights || anchoredDotd) && (
        <div className="top-panels">
          {anchoredHighlights && <HighlightsPanel sessionId={sessionId} lang={lang} untilMs={atMs} onSeek={onSeek} />}
          {anchoredDotd && <DriverOfDayPanel sessionId={sessionId} lang={lang} sessionStatus={sessionStatus} lap={lap} totalLaps={totalLaps} atMs={atMs} />}
        </div>
      )}

      {/* Settings button — visible on tablet/mobile only (CSS controls display) */}
      <button
        type="button"
        className="settings-btn"
        onClick={onSettingsOpen}
        title="Settings"
        aria-label="Open settings"
      >&#9881;</button>

      {companion}

      <div className="lapbox">
        <span className="word">LAP</span>
        <span className="n">{lap || '—'}</span>
        <span className="of">/ {totalLaps ?? '—'}</span>
      </div>
    </div>
  )
}
