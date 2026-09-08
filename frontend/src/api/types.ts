export type SessionSummary = {
  session_id: string
  source: string
}

export type Capabilities = {
  readonly: boolean
  signalr_available: boolean
  catalog_available?: boolean
  preparation_enabled?: boolean
}

export type CatalogSessionType = 'FP1' | 'FP2' | 'FP3' | 'SQ' | 'Sprint' | 'Q' | 'R'
export type CatalogSessionStatus = 'ready' | 'prepare' | 'queued' | 'processing' | 'failed'

export type CatalogSession = {
  session_id: string
  type: CatalogSessionType
  name: string
  starts_at: string
  status: CatalogSessionStatus
  replay_session_id: string | null
  job_id: string | null
}

export type CatalogEvent = {
  round: number
  name: string
  sessions: CatalogSession[]
}

export type CatalogResponse = {
  season: number
  seasons: number[]
  catalog_available: boolean
  preparation_enabled: boolean
  events: CatalogEvent[]
}

export type Preparation = {
  job_id: string
  session_id: string
  status: 'ready' | 'queued' | 'processing' | 'failed'
  created_at: string
  updated_at: string
  replay_session_id: string | null
  error: string | null
}

export type Timeline = {
  session_id: string
  start_ms: number
  end_ms: number
  /** Display time of lights-out. Formation lap occupies [start_ms, lights_out_ms). */
  lights_out_ms: number
  events_total: number
  lap_marks: Record<string, number>
}

export type DriverState = {
  position: number | null
  /** 1-based ordering truth (= classification index). Render this; never re-sort. */
  rank: number | null
  /** Baseline position = first-known PositionChanged value (grid, or join-time for mid-join recordings). */
  grid_position: number | null
  laps_completed: number
  last_lap_ms: number | null
  best_lap_ms: number | null
  gap_s: number | null
  interval_s: number | null
  tyre_compound: string | null
  tyre_age_laps: number | null
  pit_count: number
  in_pit: boolean
  retired: boolean
  /** True only for the conservative five-lap fallback, not an official OUT signal. */
  retirement_inferred?: boolean
  /** Official live timing Stopped flag; distinct from a confirmed retirement. */
  stopped?: boolean
  /** Track telemetry for this frame. null in live mode (map dead-reckons). */
  x: number | null
  y: number | null
  /** Cumulative track progress (laps + arc). Animation only — NOT ordering. */
  progress: number | null
  recent_laps_ms: number[]
  /** Latest observation per timing sector (S1..S3). Each slot carries its own lap; null = unknown. */
  sectors?: (SectorTime | null)[]
}

export type SectorTime = {
  /** Lap the observation belongs to; null when the source cannot say. */
  lap: number | null
  /** Sector duration in whole milliseconds. */
  time_ms: number
  /** Session time the observation was recorded at. */
  at_ms: number
}

export type DataQuality = {
  status: string
  last_event_ms: number | null
  events_applied: number
  duplicates_dropped: number
}

export type Insight = {
  insight_id: string
  type: string
  severity: 'medium' | 'high' | string
  confidence: string
  created_at_ms: number
  lap: number
  driver_ids: string[]
  evidence: Record<string, number | string | boolean | null>
}

/** One overtake event within the "recent" attention window (see RaceState.recent_passes). */
export type RecentPass = {
  ahead: string
  behind: string
  kind: string
  at_ms: number
}

export type WeatherState = {
  air_temp_c?: number
  track_temp_c?: number
  humidity_percent?: number
  pressure_mbar?: number
  rainfall?: boolean
  wind_direction_deg?: number
  wind_speed_mps?: number
}

export type RaceState = {
  session_id: string | null
  at_ms: number
  lap: number
  session_status: string
  /** Live-only badge text, e.g. "SILVERSTONE · RACE" (null in replay / before SessionInfo arrives). */
  session_name?: string | null
  status_since_ms: number
  /** Source-backed scheduled restart point, relative to session start. */
  restart_at_ms?: number | null
  total_laps: number | null
  /** Latest source-backed F1 weather sample at this point in session time. */
  weather?: WeatherState | null
  classification: string[]
  drivers: Record<string, DriverState>
  data_quality: DataQuality
  active_insights?: Insight[]
  /** Map viewbox [w,h] from telemetry, or null in live mode. */
  viewbox?: [number, number] | null
  /** "replay" (positions.json telemetry) or "live". */
  frame_source?: string
  /** Passes with at_ms in the last ~20s of session time — drives the on-map overtake flash. */
  recent_passes?: RecentPass[]
  /** Live-only: battles embedded directly in the stream frame (replay fetches via /battles). */
  battles?: Battle[]
  /** Stream-rendered WTW copy for the selected language and detail level. */
  commentary?: CommentaryItem[]
  /** Live-only tyre strategy embedded in each remote snapshot. */
  stints?: StintsResponse | null
}

export type InsightsResponse = {
  at_ms: number
  insights: Insight[]
}

export type FeedItem = {
  id: string
  at_ms: number
  lap: number | null
  driver_id?: string | null
  text: string
  kind: string // 'status' | 'fastest_lap' | 'pit' | 'info' | ...
  tag?: 'PIT' | 'FLAG' | 'FASTEST' | 'FINISH' | 'PASS' | 'INFO' | 'STEWARDS'
  audio_url?: string
  /** Whisper transcript of the team-radio clip, when available. */
  transcript?: string
}

export type FeedResponse = {
  items: FeedItem[]
}

export type Battle = Insight

export type BattlesResponse = {
  battles: Battle[]
}

export type CommentaryItem = {
  at_ms: number
  text: string
  driver_ids: string[]
  insight_id: string | null
  level: string
}

export type CommentaryResponse = {
  items: CommentaryItem[]
}

// ── Predictive / forecast types ───────────────────────────────────────────────

export type ForecastDriver = {
  projected_gap_s: number | null
  current_pos: number
  projected_pos: number
  delta_pos: number
}

export type Forecast = {
  at_ms: number
  laps_ahead: number
  effective_laps?: number
  model?: string
  calibrated?: boolean
  projected_order: string[]
  projected: Record<string, ForecastDriver>
}

export type PitSimEvidence = {
  pit_loss_s: number
  rejoin_gap_s: number
  rejoin_pos: number
  key_rival: string | null
  margin_s: number | null
  verdict: 'UNDERCUT_LIKELY' | 'UNLIKELY' | 'NO_RIVAL'
}

export type PitSim = {
  driver: string
  confidence: string
  evidence?: PitSimEvidence
  error?: string
}

export type Overtake = {
  ahead: string
  behind: string
  probability: number
  attack_score?: number
  calibrated?: boolean
  factors: Record<string, number | string | boolean>
  error?: string
}

// ── Win probability ───────────────────────────────────────────────────────────

export type WinProbEntry = { driver: string; prob: number }

export type WinProb = {
  at_ms: number
  laps_remaining: number
  win_prob: Record<string, number>
  win_score?: Record<string, number>
  calibrated?: boolean
  leader: string | null
  top: WinProbEntry[]
}

export type WinProbSeriesPoint = {
  at_ms: number
  probs: Record<string, number>
}

// ── Race markers ──────────────────────────────────────────────────────────────

export type MarkerKind =
  | 'RED_FLAG'
  | 'SAFETY_CAR'
  | 'VSC'
  | 'GREEN'
  | 'INCIDENT'
  | 'CRASH'
  | 'PENALTY'
  | 'LEAD_CHANGE'
  | 'PODIUM_CHANGE'
  | 'FASTEST_LAP'
  | 'OFF_TRACK'
  | 'OVERTAKE'
  | 'UNDERCUT'

export type MarkerSeverity = 'critical' | 'high' | 'medium' | 'low'

export type RaceMarker = {
  at_ms: number
  lap: number
  kind: MarkerKind
  severity: MarkerSeverity
  driver_ids: string[]
  text_en: string
  text_ru: string
}

export type MarkersResponse = {
  markers: RaceMarker[]
}

// ── Highlights ────────────────────────────────────────────────────────────────

export type Highlight = {
  at_ms: number
  lap: number | null
  kind: MarkerKind
  title_en: string
  title_ru: string
  drivers: string[]
}

export type HighlightsResponse = {
  highlights: Highlight[]
}

// ── Driver of the Day ─────────────────────────────────────────────────────────

export type DotdCandidate = {
  driver: string
  score: number
  positions_gained: number
  had_fastest_lap: boolean
  note_en: string
  note_ru: string
}

export type DotdResponse = {
  candidates: DotdCandidate[]
  computed_pick: string | null
  official_result: {
    driver: string
    percentage: number
    provider: 'Formula 1 fan vote'
    source_url: string
    fetched_at: string
  } | null
}

// ── What-If counterfactual ────────────────────────────────────────────────────

export type WhatIfDiff = {
  driver: string
  baseline_pos: number
  scenario_pos: number
  delta: number  // positive = gained positions
}

export type WhatIf = {
  scenario: string
  driver: string | null
  baseline_order: string[]
  scenario_order: string[]
  diff: WhatIfDiff[]
  summary_text_en: string
  summary_text_ru: string
  assumptions: string[]
  model?: string
  calibrated?: boolean
}

// ── Tyre strategy ─────────────────────────────────────────────────────────────
export type Stint = {
  compound: string
  start_lap: number
  end_lap: number
  laps: number
}

export type StintsResponse = {
  session_id: string
  total_laps: number
  stints: Record<string, Stint[]>
}
