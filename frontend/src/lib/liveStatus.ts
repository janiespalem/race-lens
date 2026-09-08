import type { Lang } from '../features/replay/replayTypes'
import type { LiveStatusResult } from '../api/client'

export type LivePhase =
  | 'connecting'
  | 'waiting'
  | 'live'
  | 'reconnecting'
  | 'degraded'
  | 'stalled'
  | 'preparing'
  | 'failed'
  | 'ended'

export type LivePresentation = {
  phase: LivePhase
  badge: string
  detail: string
}

type LiveLifecycleOptions = {
  readonly: boolean
  explicitReplay: boolean
  attachedToLive: boolean
}

export type LiveLifecycle = {
  canManage: boolean
  remoteAvailable: boolean
  enterLive: boolean
  showLiveNow: boolean
  replaySessionId: string | null
}

export function restartCountdown(atMs: number, restartAtMs?: number | null): string | null {
  if (restartAtMs == null || restartAtMs <= atMs) return null
  const totalSec = Math.ceil((restartAtMs - atMs) / 1000)
  const mm = Math.floor(totalSec / 60).toString().padStart(2, '0')
  const ss = (totalSec % 60).toString().padStart(2, '0')
  return `${mm}:${ss}`
}

export function liveLifecycle(
  status: LiveStatusResult | null,
  options: LiveLifecycleOptions,
): LiveLifecycle {
  const remote = status?.source === 'remote'
  const remoteAttachable = remote && (
    status.status === 'live' || status.status === 'finishing'
  )
  const attachable = status?.is_running === true || remoteAttachable

  return {
    canManage: !options.readonly && !remote,
    remoteAvailable: remote && status.status === 'live',
    enterLive: !options.explicitReplay && !options.attachedToLive && attachable,
    showLiveNow: options.explicitReplay && !options.attachedToLive && remote && status.status === 'live',
    replaySessionId: !options.explicitReplay && options.attachedToLive
      && status?.status === 'replay_ready'
      ? status.replay_session_id ?? null
      : null,
  }
}

export function livePresentation(
  status: LiveStatusResult | null,
  hasState: boolean,
  streamError: string | null,
  now = Date.now(),
  lang: Lang = 'en',
): LivePresentation {
  if (status?.status === 'idle') {
    return { phase: 'connecting', badge: (lang === 'ru' ? 'ЭФИР ВЫКЛЮЧЕН' : 'LIVE OFF'), detail: (lang === 'ru' ? 'НЕТ ПРЯМОГО ЭФИРА' : 'NO LIVE SESSION') }
  }
  if (status?.status === 'failed') {
    return {
      phase: 'failed',
      badge: (lang === 'ru' ? 'ОШИБКА ЭФИРА' : 'LIVE FAILED'),
      detail: lang === 'ru' ? 'НЕ УДАЛОСЬ ПОДГОТОВИТЬ ПОВТОР' : status.failure ?? 'REPLAY PREPARATION FAILED',
    }
  }
  if (status?.status === 'finishing' || status?.status === 'replay_ready') {
    return {
      phase: 'preparing',
      badge: (lang === 'ru' ? 'ПОДГОТОВКА ПОВТОРА' : 'REPLAY PREPARING'),
      detail: (lang === 'ru' ? 'ЭФИР ЗАВЕРШЁН · ПУБЛИКУЕМ ПОВТОР' : 'LIVE ENDED · PUBLISHING REPLAY'),
    }
  }
  if (status?.capture_alive === false) {
    return { phase: 'stalled', badge: (lang === 'ru' ? 'НЕТ ОБНОВЛЕНИЙ' : 'STALLED'), detail: (lang === 'ru' ? 'ЗАПИСЬ ОСТАНОВЛЕНА · ПЕРЕЗАПУСТИТЕ ЭФИР' : 'CAPTURE STOPPED · RESTART LIVE') }
  }
  const expiresAt = status?.expires_at ? Date.parse(status.expires_at) : Number.NaN
  if (!Number.isNaN(expiresAt) && expiresAt <= now) {
    return {
      phase: 'stalled',
      badge: (lang === 'ru' ? 'НЕТ ОБНОВЛЕНИЙ' : 'STALLED'),
      detail: (lang === 'ru' ? 'УСТАРЕВШИЙ КАДР · СЕРВЕР НЕ ОБНОВЛЯЕТСЯ' : 'SNAPSHOT STALE · BACKEND STALLED'),
    }
  }
  if (status?.data_quality === 'stalled') {
    return { phase: 'stalled', badge: (lang === 'ru' ? 'НЕТ ОБНОВЛЕНИЙ' : 'STALLED'), detail: (lang === 'ru' ? 'НЕТ НОВЫХ ДАННЫХ · СЕРВЕР НЕ ОБНОВЛЯЕТСЯ' : 'NO NEW DATA · BACKEND STALLED') }
  }
  if (streamError) {
    return {
      phase: 'reconnecting',
      badge: (lang === 'ru' ? 'ПЕРЕПОДКЛЮЧЕНИЕ' : 'RECONNECTING'),
      detail: (lang === 'ru' ? 'ПОТОК ПРЕРВАН · ПОВТОРЯЕМ ПОДКЛЮЧЕНИЕ' : 'STREAM LOST · RETRYING AUTOMATICALLY'),
    }
  }
  if (status && !status.is_running) {
    return { phase: 'ended', badge: (lang === 'ru' ? 'ЗАВЕРШЁН' : 'ENDED'), detail: (lang === 'ru' ? 'ПОТОК СЕССИИ ЗАВЕРШЁН' : 'SESSION FEED ENDED') }
  }
  if (!hasState) {
    if (status?.is_running && status.events_total === 0) {
      return {
        phase: 'waiting',
        badge: (lang === 'ru' ? 'ОЖИДАНИЕ' : 'WAITING'),
        detail: (lang === 'ru' ? 'ПОДКЛЮЧЕНО · ЖДЁМ ПЕРВЫЙ ПАКЕТ ХРОНОМЕТРАЖА' : 'FEED CONNECTED · WAITING FOR FIRST TIMING PACKET'),
      }
    }
    return { phase: 'connecting', badge: (lang === 'ru' ? 'ПОДКЛЮЧЕНИЕ' : 'CONNECTING'), detail: (lang === 'ru' ? 'ПОДКЛЮЧЕНИЕ К ХРОНОМЕТРАЖУ' : 'OPENING LIVE TIMING') }
  }
  if (status?.data_quality === 'degraded') {
    return { phase: 'degraded', badge: (lang === 'ru' ? 'С ЗАДЕРЖКОЙ' : 'DELAYED'), detail: (lang === 'ru' ? 'ДАННЫЕ ЭФИРА ПОСТУПАЮТ С ЗАДЕРЖКОЙ' : 'LIVE DATA IS ARRIVING LATE') }
  }
  const generatedAt = status?.generated_at ? Date.parse(status.generated_at) : Number.NaN
  const snapshotAge = Number.isNaN(generatedAt) ? null : Math.max(0, Math.round((now - generatedAt) / 1000))
  return {
    phase: 'live',
    badge: (lang === 'ru' ? '● ЭФИР' : '● LIVE'),
    detail: status?.source === 'remote'
      ? lang === 'ru' ? `ВОЗРАСТ КАДРА: ${snapshotAge ?? '—'} С · ВОСПРОИЗВЕДЕНИЕ ВПЕРЁД` : `SNAPSHOT ${snapshotAge ?? '—'}S OLD · PLAY-FORWARD`
      : status ? (lang === 'ru' ? `ХРОНОМЕТРАЖ АКТИВЕН · ОПРОС №${status.poll_count}` : `TIMING ACTIVE · POLL #${status.poll_count}`) : (lang === 'ru' ? 'ХРОНОМЕТРАЖ АКТИВЕН' : 'TIMING ACTIVE'),
  }
}
