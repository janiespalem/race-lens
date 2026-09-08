import type { BattlesResponse, Capabilities, CatalogResponse, CommentaryResponse, DotdResponse, FeedItem, FeedResponse, Forecast, HighlightsResponse, InsightsResponse, MarkersResponse, Overtake, PitSim, Preparation, RaceState, SessionSummary, Timeline, StintsResponse, WhatIf, WinProb, WinProbSeriesPoint } from './types'
import { apiUrl } from './url'

const responseError = async (response: Response, path: string) => {
  const body = await response.json().catch(() => null) as { detail?: unknown } | null
  const detail = typeof body?.detail === 'string' ? body.detail : response.statusText
  return new Error(`${response.status} ${detail}: ${path}`)
}

const json = async <T>(path: string, retry = true): Promise<T> => {
  const response = await fetch(apiUrl(path))
  if (retry && [502, 503, 504].includes(response.status)) {
    await new Promise((resolve) => setTimeout(resolve, 1500))
    return json<T>(path, false)
  }
  if (!response.ok) {
    throw await responseError(response, path)
  }
  return (await response.json()) as T
}

export const listSessions = async (onWake?: () => void): Promise<SessionSummary[]> => {
  const deadline = Date.now() + 45_000
  while (true) {
    try {
      return await json<SessionSummary[]>('/api/sessions')
    } catch (error) {
      if (Date.now() >= deadline) throw error
      onWake?.()
      await new Promise((resolve) => setTimeout(resolve, 2500))
    }
  }
}
export const getCapabilities = () => json<Capabilities>('/api/capabilities')
export const getCatalog = (season?: number) =>
  json<CatalogResponse>(`/api/catalog${season ? `?season=${season}` : ''}`)

export const getTimeline = (sessionId: string) =>
  json<Timeline>(`/api/sessions/${encodeURIComponent(sessionId)}/timeline`)

export const getState = (sessionId: string, atMs: number) =>
  json<RaceState>(`/api/sessions/${encodeURIComponent(sessionId)}/state?at_ms=${atMs}`)

export const getInsights = (sessionId: string, atMs: number) =>
  json<InsightsResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/insights?at_ms=${atMs}`)

export const streamUrl = (sessionId: string, speed: number, fromMs: number, tickMs = 1000, lang = 'en', level = 'pro') =>
  apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/stream?speed=${speed}&from_ms=${fromMs}&tick_ms=${tickMs}&lang=${lang}&level=${level}`)

/** The backend returns a bare list; normalise to {items} for the rest of the app. */
export const getFeed = async (sessionId: string, untilMs: number, limit = 30, lang = 'en'): Promise<FeedResponse> => {
  const raw = await json<FeedItem[] | FeedResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/feed?until_ms=${untilMs}&lang=${lang}&limit=${limit}`,
  )
  if (Array.isArray(raw)) return { items: raw }
  return raw
}

export const getBattles = (sessionId: string, atMs: number) =>
  json<BattlesResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/battles?at_ms=${atMs}`)

export const getCommentary = (sessionId: string, atMs: number, lang = 'en', level = 'pro') =>
  json<CommentaryResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/commentary?at_ms=${atMs}&lang=${lang}&level=${level}`)

export type TrackCorner = { number: number; x: number; y: number }
export type TrackData = {
  session_id: string
  viewbox: [number, number]
  points: [number, number][]
  progress_points?: [number, number][]
  corners?: TrackCorner[]
}

export const getTrack = (sessionId: string) =>
  json<TrackData>(`/api/sessions/${encodeURIComponent(sessionId)}/track`)

export const getLiveTrack = () => json<TrackData>('/api/live/track')

export const getLiveStints = () => json<StintsResponse>('/api/live/stints')

// ── Predictive endpoints (replay) ──────────────────────────────────────────────
//
// NOTE: These endpoints are session-scoped (/sessions/{id}/…), for replay mode.
// Live mode uses the /api/live/* mirrors below instead (same response shapes).

export const getForecast = (sessionId: string, atMs: number, laps = 10) =>
  json<Forecast>(
    `/api/sessions/${encodeURIComponent(sessionId)}/forecast?at_ms=${atMs}&laps=${laps}`,
  )

export const getSimulatePit = (sessionId: string, atMs: number, driver: string) =>
  json<PitSim>(
    `/api/sessions/${encodeURIComponent(sessionId)}/simulate-pit?at_ms=${atMs}&driver=${encodeURIComponent(driver)}`,
  )

export const getOvertake = (sessionId: string, atMs: number, ahead: string, behind: string) =>
  json<Overtake>(
    `/api/sessions/${encodeURIComponent(sessionId)}/overtake?at_ms=${atMs}&ahead=${encodeURIComponent(ahead)}&behind=${encodeURIComponent(behind)}`,
  )

export const getWinProbSeries = (sessionId: string, untilMs: number, samples = 20) =>
  json<WinProbSeriesPoint[]>(
    `/api/sessions/${encodeURIComponent(sessionId)}/win-prob-series?until_ms=${untilMs}&samples=${samples}`,
  )

export const getWhatIf = (sessionId: string, atMs: number, scenario: string, driver?: string) => {
  const params = new URLSearchParams({ at_ms: String(atMs), scenario })
  if (driver) params.set('driver', driver)
  return json<WhatIf>(`/api/sessions/${encodeURIComponent(sessionId)}/what-if?${params.toString()}`)
}

export const getMarkers = (sessionId: string, untilMs?: number) =>
  json<MarkersResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/markers${untilMs !== undefined ? `?until_ms=${untilMs}` : ''}`,
  )

export const getHighlights = (sessionId: string, topN = 8, untilMs?: number) =>
  json<HighlightsResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/highlights?top_n=${topN}` +
    (untilMs !== undefined ? `&until_ms=${Math.max(0, Math.round(untilMs))}` : ''),
  )

export const getDriverOfDay = (sessionId: string, atMs?: number) =>
  json<DotdResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/driver-of-day` +
    (atMs != null ? `?at_ms=${Math.max(0, Math.round(atMs))}` : ''),
  )

// ── Live endpoints ────────────────────────────────────────────────────────────

export type LiveStartResult = { session_key: number; poll_interval_s: number; status: string }
export type LiveCaptureFreshness = {
  raw_size: number
  raw_updated_at: string
  seconds_since_growth: number
  transport_growing: boolean
}
export type LiveStatusResult = {
  is_running: boolean
  poll_count: number
  events_total: number
  last_poll_ok: boolean
  last_poll_unix: number | null
  last_new_event_unix: number | null
  last_error: string | null
  capture_alive?: boolean
  data_quality: 'good' | 'degraded' | 'stalled'
  source?: string
  status?: 'live' | 'finishing' | 'replay_ready' | 'failed' | 'idle'
  canonical_session_id?: string
  replay_session_id?: string
  generated_at?: string
  expires_at?: string | null
  capture_freshness?: LiveCaptureFreshness | null
  failure?: string | null
}

const post = async <T>(path: string): Promise<T> => {
  const response = await fetch(apiUrl(path), { method: 'POST' })
  if (!response.ok) throw await responseError(response, path)
  return (await response.json()) as T
}

export const prepareSession = (sessionId: string) =>
  post<Preparation>(`/api/catalog/${encodeURIComponent(sessionId)}/prepare`)

export const getPreparation = (sessionId: string) =>
  json<Preparation>(`/api/preparations/${encodeURIComponent(sessionId)}`)

export type LiveSource = 'openf1' | 'signalr'

export const liveStart = (year: number, country: string, session: string, pollS = 2, source: LiveSource = 'openf1') =>
  post<LiveStartResult>(
    `/api/live/start?year=${year}&country=${encodeURIComponent(country)}&session=${encodeURIComponent(session)}&poll_s=${pollS}&source=${source}`,
  )

export const liveStatus = () => json<LiveStatusResult>('/api/live/status')

export const liveStop = () => post<LiveStatusResult>('/api/live/stop')

export const liveStreamUrl = (lang: string, level: string, tickS = 2) =>
  apiUrl(`/api/live/stream?tick_s=${tickS}&lang=${lang}&level=${level}`)

/** The backend returns a bare list; normalise to {items} for the rest of the app. */
export const getLiveFeed = async (limit = 30, lang = 'en'): Promise<FeedResponse> => {
  const raw = await json<FeedItem[] | FeedResponse>(`/api/live/feed?lang=${lang}&limit=${limit}`)
  if (Array.isArray(raw)) return { items: raw }
  return raw
}

// ── Live mirrors of the predictive endpoints ────────────────────────────────────
//
// Same response shapes as the session-scoped ones above, fed by the live
// runner's current state instead of a replay snapshot at at_ms.

export const getLiveForecast = (laps = 10) =>
  json<Forecast>(`/api/live/forecast?laps=${laps}`)

export const getLiveWinProb = () =>
  json<WinProb>('/api/live/win-prob')

export const getLiveSimulatePit = (driver: string) =>
  json<PitSim>(`/api/live/simulate-pit?driver=${encodeURIComponent(driver)}`)

// ── Live lobby ────────────────────────────────────────────────────────────────

export interface LiveSessionInfo {
  session_name: string
  session_key: number
  session_type: string
  date_start: string
  started: boolean
}

export const getLiveSessions = (year: number, country?: string): Promise<LiveSessionInfo[]> => {
  const params = new URLSearchParams({ year: String(year) })
  if (country) params.set('country', country)
  return json<LiveSessionInfo[]>(`/api/live/sessions?${params.toString()}`)
}

export const getStints = (sessionId: string) =>
  json<StintsResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/stints`)
