import type { Lang } from './replayTypes'
import { useEffect, useRef, useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { encodePocketAppLink, encodePocketLink, type PocketTarget } from '../../api/pocket'

export function CompanionLink({ target, lang = 'en' }: { target: PocketTarget | null; lang?: Lang }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)
  const [handoff, setHandoff] = useState<PocketTarget | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const dialog = useRef<HTMLElement>(null)
  const appLink = handoff ? encodePocketAppLink(handoff) : ''
  const browserLink = handoff ? encodePocketLink(handoff) : ''
  useEffect(() => {
    if (target == null && open) { setOpen(false); setHandoff(null); setCopied(null) }
  }, [open, target])
  useEffect(() => {
    if (!open) { setCopied(null); return }
    const opener = trigger.current
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
      if (event.key !== 'Tab' || !dialog.current) return
      const controls = Array.from(dialog.current.querySelectorAll<HTMLElement>('button:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'))
      if (!controls.length) return
      const first = controls[0]; const last = controls.at(-1)!
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    window.addEventListener('keydown', onKey)
    dialog.current?.querySelector<HTMLElement>('button')?.focus()
    return () => { window.removeEventListener('keydown', onKey); opener?.focus() }
  }, [open])
  const copy = async () => {
    try { await navigator.clipboard.writeText(browserLink); setCopied('LINK COPIED') }
    catch { setCopied('COPY FAILED') }
  }
  if (!target) return null
  return (
    <div className="companion-link">
      <button ref={trigger} type="button" className="companion-trigger" onClick={() => { setHandoff(target); setCopied(null); setOpen(true) }} aria-haspopup="dialog" aria-expanded={open}>
        <span className="companion-dot is-linked" aria-hidden="true" />
        <span>{(lang === 'ru' ? 'ОТКРЫТЬ В POCKET' : 'OPEN IN POCKET')}</span>
      </button>
      {open && <div className="companion-overlay">
        <button type="button" className="companion-backdrop" onClick={() => setOpen(false)} aria-label={(lang === 'ru' ? 'Закрыть передачу в Pocket' : 'Close Pocket handoff')} />
        <section ref={dialog} className="companion-panel" role="dialog" aria-modal="true" aria-labelledby="pocket-title">
          <header className="companion-header">
            <div><small>RACE LENS POCKET</small><h2 id="pocket-title">{(lang === 'ru' ? 'Открыть отдельно' : 'Open independently')}</h2></div>
            <button type="button" className="settings-close" onClick={() => { setOpen(false); setHandoff(null) }} aria-label={(lang === 'ru' ? 'Закрыть' : 'Close')}>×</button>
          </header>
          <div className="companion-share">
            <div className="companion-qr"><QRCodeSVG value={appLink} size={184} level="M" title={(lang === 'ru' ? 'QR-код для открытия в Race Lens Pocket' : 'Race Lens Pocket app handoff QR code')} /></div>
            <p>{(lang === 'ru' ? 'Сканируйте код в установленном Pocket, чтобы открыть эту сессию с выбранными пилотами. Pocket получает хронометраж напрямую; действия в нём не меняют эту панель.' : 'Scan with installed Pocket to hand off this session and focus. Pocket fetches timing directly; actions there do not change this dashboard.')}</p>
            <button type="button" className="b companion-primary" onClick={() => void copy()}>{(lang === 'ru' ? 'КОПИРОВАТЬ ССЫЛКУ ДЛЯ БРАУЗЕРА' : 'COPY BROWSER LINK')}</button>
            {copied && <small role="status">{lang === 'ru' ? (copied === 'LINK COPIED' ? 'ССЫЛКА СКОПИРОВАНА' : 'НЕ УДАЛОСЬ СКОПИРОВАТЬ') : copied}</small>}
          </div>
        </section>
      </div>}
    </div>
  )
}
