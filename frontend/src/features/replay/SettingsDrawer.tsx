import { useEffect, useRef } from 'react'
import { DriverOfDayPanel } from './DriverOfDayPanel'
import { HighlightsPanel } from './HighlightsPanel'
import type { Lang, Level } from './replayTypes'

type Props = {
  open: boolean
  onClose: () => void
  lang: Lang
  level: Level
  mode: 'replay' | 'live'
  liveAvailable: boolean
  liveNowAvailable: boolean
  onLang: (lang: Lang) => void
  onLevel: (level: Level) => void
  onModeChange: (mode: 'replay' | 'live') => void
  sessionId?: string | null
  onSeek?: (ms: number) => void
  sessionStatus?: string
  lap?: number
  totalLaps?: number | null
  atMs?: number
}

export function SettingsDrawer({
  open, onClose, lang, level, mode, liveAvailable, liveNowAvailable,
  onLang, onLevel, onModeChange,
  sessionId, onSeek, sessionStatus, lap, totalLaps, atMs,
}: Props) {
  const closeRef = useRef<HTMLButtonElement>(null)
  const drawerRef = useRef<HTMLDivElement>(null)
  const previousFocus = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) return
    previousFocus.current = document.activeElement as HTMLElement | null
    closeRef.current?.focus()
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      if (event.key !== 'Tab' || !drawerRef.current) return
      const focusable = Array.from(drawerRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), select:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
      ))
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => {
      window.removeEventListener('keydown', handleKey)
      previousFocus.current?.focus()
    }
  }, [open, onClose])

  return (
    <div className={`settings-overlay${open ? ' open' : ''}`} aria-hidden={!open}>
      <div className="settings-backdrop" onClick={onClose} />
      <div ref={drawerRef} className="settings-drawer" role="dialog" aria-modal="true" aria-label={(lang === 'ru' ? 'Настройки' : 'Settings')}>
        <div className="settings-drawer-hdr">
          <span>{(lang === 'ru' ? 'НАСТРОЙКИ' : 'SETTINGS')}</span>
          <button ref={closeRef} type="button" className="settings-close" onClick={onClose} aria-label={(lang === 'ru' ? 'Закрыть' : 'Close')}>&#215;</button>
        </div>

        <div className="settings-group">
          <div className="settings-group-label">{(lang === 'ru' ? 'РЕЖИМ' : 'MODE')}</div>
          <div className="tog-group">
            <button type="button" className={`tog${mode === 'replay' ? ' tog-on' : ''}`} onClick={() => { onModeChange('replay'); onClose() }}>{(lang === 'ru' ? 'ПОВТОР' : 'REPLAY')}</button>
            <button type="button" className={`tog${mode === 'live' ? ' tog-on' : ''}`} disabled={!liveAvailable} onClick={() => { onModeChange('live'); onClose() }}>{lang === 'ru' ? (liveNowAvailable ? 'СЕЙЧАС В ЭФИРЕ' : 'ЭФИР') : (liveNowAvailable ? 'LIVE NOW' : 'LIVE')}</button>
          </div>
        </div>

        <div className="settings-group">
          <div className="settings-group-label">{(lang === 'ru' ? 'ЯЗЫК' : 'LANGUAGE')}</div>
          <div className="tog-group">
            <button type="button" className={`tog${lang === 'en' ? ' tog-on' : ''}`} onClick={() => onLang('en')}>EN</button>
            <button type="button" className={`tog${lang === 'ru' ? ' tog-on' : ''}`} onClick={() => onLang('ru')}>RU</button>
          </div>
        </div>

        <div className="settings-group">
          <div className="settings-group-label">{(lang === 'ru' ? 'УРОВЕНЬ' : 'LEVEL')}</div>
          <div className="tog-group">
            <button type="button" className={`tog${level === 'beginner' ? ' tog-on' : ''}`} onClick={() => onLevel('beginner')}>{(lang === 'ru' ? 'НОВИЧОК' : 'ROOKIE')}</button>
            <button type="button" className={`tog${level === 'pro' ? ' tog-on' : ''}`} onClick={() => onLevel('pro')}>{(lang === 'ru' ? 'ЭКСПЕРТ' : 'PRO')}</button>
          </div>
        </div>

        {mode === 'replay' && sessionId && onSeek && (
          <div className="settings-group drawer-panels-group">
            <div className="settings-group-label">{(lang === 'ru' ? 'МОМЕНТЫ И DOTD' : 'HIGHLIGHTS & DOTD')}</div>
            <div className="drawer-panels-inner">
              <HighlightsPanel sessionId={sessionId} lang={lang} untilMs={atMs} onSeek={(ms) => { onSeek(ms); onClose() }} />
              <DriverOfDayPanel sessionId={sessionId} lang={lang} sessionStatus={sessionStatus} lap={lap} totalLaps={totalLaps} atMs={atMs} />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
