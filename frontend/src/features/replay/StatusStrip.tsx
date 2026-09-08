import type { Lang } from './replayTypes'
import { restartCountdown } from '../../lib/liveStatus'

type Props = {
  lang?: Lang
  status: string
  lap?: number | null
  atMs?: number
  neutralizationStartMs?: number | null
  greenFlag?: boolean
  greenFlagText?: string
  restartAnnouncement?: string | null
  restartAtMs?: number | null
}

function formatNeutralTimer(atMs: number, startMs: number): string {
  const elapsed = Math.max(0, atMs - startMs)
  const totalSec = Math.floor(elapsed / 1000)
  const mm = Math.floor(totalSec / 60).toString().padStart(2, '0')
  const ss = (totalSec % 60).toString().padStart(2, '0')
  return `${mm}:${ss}`
}

export function StatusStrip({
  lang = 'en',
  status,
  lap = null,
  atMs = 0,
  neutralizationStartMs = null,
  greenFlag = false,
  greenFlagText = '',
  restartAnnouncement = null,
  restartAtMs = null,
}: Props) {
  const lapStr = lap != null ? `${lang === 'ru' ? 'КРУГ' : 'LAP'} ${lap}` : ''
  const timerStr =
    neutralizationStartMs != null
      ? formatNeutralTimer(atMs, neutralizationStartMs)
      : '00:00'
  const restartTimer = restartCountdown(atMs, restartAtMs)

  if (status === 'formation') {
    return (
      <div className="hazard hazard-formation">
        <span>{(lang === 'ru' ? 'ПРОГРЕВОЧНЫЙ КРУГ' : 'FORMATION LAP')}</span>
      </div>
    )
  }

  // Pre-start banners (formation lap / on the grid) show regardless of session
  // status — there are no events before lights-out.
  if (greenFlag && (greenFlagText === 'FORMATION LAP' || greenFlagText === 'ON THE GRID')) {
    return (
      <div className="hazard hazard-formation">
        <span>{lang === 'ru' ? ({ 'FORMATION LAP': 'ПРОГРЕВОЧНЫЙ КРУГ', 'ON THE GRID': 'НА СТАРТОВОЙ РЕШЁТКЕ', 'LIGHTS OUT': 'СТАРТ', 'GREEN FLAG · RACING RESUMED': 'ЗЕЛЁНЫЙ ФЛАГ · ГОНКА ВОЗОБНОВЛЕНА' }[greenFlagText] ?? greenFlagText) : greenFlagText}</span>
      </div>
    )
  }

  if (greenFlag && status === 'started') {
    return (
      <div className="hazard hazard-green">
        <span>{lang === 'ru' ? ({ 'FORMATION LAP': 'ПРОГРЕВОЧНЫЙ КРУГ', 'ON THE GRID': 'НА СТАРТОВОЙ РЕШЁТКЕ', 'LIGHTS OUT': 'СТАРТ', 'GREEN FLAG · RACING RESUMED': 'ЗЕЛЁНЫЙ ФЛАГ · ГОНКА ВОЗОБНОВЛЕНА' }[greenFlagText] ?? greenFlagText) : greenFlagText}</span>
      </div>
    )
  }

  if (status === 'started') return null

  if (status === 'red_flag') {
    return (
      <div className="hazard hazard-red">
        <span>
          {lang === 'ru' ? 'КРАСНЫЙ ФЛАГ · СЕССИЯ ОСТАНОВЛЕНА' : 'RED FLAG · SESSION STOPPED'} · {timerStr}
          {restartAnnouncement ? ` · ${restartAnnouncement}` : ''}
          {restartTimer ? ` · ${lang === 'ru' ? 'ВОЗОБНОВЛЕНИЕ ЧЕРЕЗ' : 'RESUME IN'} ${restartTimer}` : ''}
        </span>
      </div>
    )
  }

  if (status === 'safety_car') {
    return (
      <div className="hazard hazard-amber">
        <span>{lang === 'ru' ? 'МАШИНА БЕЗОПАСНОСТИ' : 'SAFETY CAR'}{lapStr ? ` · ${lapStr}` : ''} · {timerStr}</span>
      </div>
    )
  }

  if (status === 'vsc') {
    return (
      <div className="hazard hazard-amber">
        <span>{lang === 'ru' ? 'ВИРТУАЛЬНАЯ МАШИНА БЕЗОПАСНОСТИ' : 'VIRTUAL SAFETY CAR'}{lapStr ? ` · ${lapStr}` : ''} · {timerStr}</span>
      </div>
    )
  }

  if (status === 'finished') {
    return (
      <div className="hazard hazard-chequered">
        <span>{(lang === 'ru' ? 'КЛЕТЧАТЫЙ ФЛАГ' : 'CHEQUERED FLAG')}</span>
      </div>
    )
  }

  return null
}
