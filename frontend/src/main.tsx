import type { Lang } from './features/replay/replayTypes'
import { viewerError } from './lib/viewerError'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { getCapabilities, listSessions, liveStart, liveStatus, liveStop } from './api/client'
import type { LiveStatusResult } from './api/client'
import type { DataSource } from './api/dataSource'
import type { PocketTarget } from './api/pocket'
import { parsePocketLink, pocketBootstrap } from './api/pocket'
import { LiveLobby } from './features/replay/LiveLobby'
import { BattleIntelligence } from './features/replay/BattleIntelligence'
import { BroadcastOverlay } from './features/replay/BroadcastOverlay'
import { ForecastStrip } from './features/replay/ForecastStrip'
import { StintTimeline } from './features/replay/StintTimeline'
import { FocusPanel } from './features/replay/FocusPanel'
import { InsightPanel } from './features/replay/InsightPanel'
import { RaceFeed } from './features/replay/RaceFeed'
import { ReplayDeck } from './features/replay/ReplayDeck'
import { SessionCatalog } from './features/replay/SessionCatalog'
import { SettingsDrawer } from './features/replay/SettingsDrawer'
import { StatusStrip } from './features/replay/StatusStrip'
import { TimingTower } from './features/replay/TimingTower'
import { TopBar } from './features/replay/TopBar'
import { CompanionLink } from './features/replay/CompanionLink'
import { useVoiceAlerts } from './features/replay/useVoiceAlerts'
import { TrackMap } from './features/replay/TrackMap'
import { DriverOfDayPanel } from './features/replay/DriverOfDayPanel'
import { HighlightsPanel } from './features/replay/HighlightsPanel'
import { WorkspaceGrid } from './features/replay/WorkspaceGrid'
import {
  defaultWorkspace,
  readDeskPreferences,
  readMobileCenter,
  readWorkspaces,
  saveCustomWorkspace,
  toggleDriverFocus,
  updateWorkspaceWidget,
  workspaceAction,
  writeDeskPreference,
  writeMobileCenter,
  writeWorkspace,
} from './features/replay/workspace'
import type { DeskMode, MobileCenter, WorkspaceLayout } from './features/replay/workspace'
import { useReplay } from './features/replay/useReplay'
import { lapAtTime, sessionLabel } from './lib/format'
import { focusDriverIds } from './lib/insightFocus'
import { liveLifecycle, livePresentation } from './lib/liveStatus'
import './style.css'
import './styles/dashboard.css'
import './styles/responsive.css'
import './styles/features.css'
import 'react-grid-layout/css/styles.css'
import 'react-resizable/css/styles.css'

// ── Live status pill ──────────────────────────────────────────────────────────

function LiveStatusPill({
  presentation,
}: {
  presentation: ReturnType<typeof livePresentation>
}) {
  return (
    <>
      <span className={`live-pill live-pill-${presentation.phase}`}>
        {presentation.badge}
      </span>
      <span className="live-status-detail">{presentation.detail}</span>
    </>
  )
}

// ── Center bottom segment tabs ────────────────────────────────────────────────

type CenterTab = 'FEED' | 'PACE' | 'STRATEGY'

type CenterTabsProps = {
  lang: Lang
  activeTab: CenterTab
  showForecast: boolean
  showStrategy: boolean
  onTab: (t: CenterTab) => void
}

function CenterTabs({ lang, activeTab, showForecast, showStrategy, onTab }: CenterTabsProps) {
  const tabs: CenterTab[] = ['FEED']
  if (showStrategy) tabs.push('STRATEGY')
  if (showForecast) tabs.push('PACE')
  if (tabs.length <= 1) return null
  return (
    <div className="ctr-tabs">
      {tabs.map((t) => (
        <button
          key={t}
          type="button"
          className={`ctr-tab${activeTab === t ? ' ctr-tab-on' : ''}`}
          onClick={() => onTab(t)}
        >{tabLabel(t, lang)}</button>
      ))}
    </div>
  )
}

// ── App ───────────────────────────────────────────────────────────────────────

type AppMode = 'replay' | 'live'
type MobTab = 'TIMING' | 'MAP' | 'INSIGHTS' | 'FEED'
const MOB_TABS: MobTab[] = ['TIMING', 'MAP', 'INSIGHTS', 'FEED']
const tabLabel = (tab: string, lang: Lang): string => lang === 'ru' ? ({
  TIMING: 'ХРОНОМЕТРАЖ', MAP: 'КАРТА', INSIGHTS: 'АНАЛИТИКА', FEED: 'ЛЕНТА',
  PACE: 'ТЕМП', STRATEGY: 'СТРАТЕГИЯ', BATTLES: 'БОРЬБА', TRACK: 'ТРАССА',
}[tab] ?? tab) : tab

function useDesktopWorkspace() {
  const [desktop, setDesktop] = useState(() => window.matchMedia('(min-width: 1025px)').matches)
  useEffect(() => {
    const query = window.matchMedia('(min-width: 1025px)')
    const update = () => setDesktop(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  return desktop
}

function App() {
  const initialPocket = useMemo(() => parsePocketLink(new URL(window.location.href)), [])
  const initialPocketBoot = useMemo(() => pocketBootstrap(initialPocket), [initialPocket])
  const initialPocketLive = initialPocketBoot.attachLive
  const initialParams = useMemo(() => new URLSearchParams(window.location.search), [])
  const initialCatalogId = initialParams.get('catalog')
  const initialSessionId = initialPocketLive ? null : initialPocketBoot.initialSessionId ?? initialParams.get('session')
  const initialCatalogSeason = initialCatalogId && /^\d{4}-/.test(initialCatalogId)
    ? Number(initialCatalogId.slice(0, 4))
    : undefined
  const [mode, setMode] = useState<AppMode>(initialPocket?.mode ?? 'replay')
  const [mobTab, setMobTab] = useState<MobTab>('MAP')
  const [workspaces, setWorkspaces] = useState(readWorkspaces)
  const [deskPreferences, setDeskPreferences] = useState(readDeskPreferences)
  const [workspaceDraft, setWorkspaceDraft] = useState<WorkspaceLayout | null>(null)
  const [mobileCenter, setMobileCenter] = useState<MobileCenter>(() => readMobileCenter())
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [catalogOpen, setCatalogOpen] = useState(Boolean(initialCatalogId) && !initialPocketLive)
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId)
  const [readyReplayIds, setReadyReplayIds] = useState<string[]>([])
  const [replayPinned, setReplayPinned] = useState(initialPocketLive ? false : initialPocketBoot.replayPinned || Boolean(initialParams.get('session')))
  const [sessionNotice, setSessionNotice] = useState<string | null>(null)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [backendPhase, setBackendPhase] = useState<'connecting' | 'waking' | 'ready'>('connecting')
  const [startupReady, setStartupReady] = useState(false)
  const [readonlyDeployment, setReadonlyDeployment] = useState<boolean | null>(null)
  const [isLiveActive, setIsLiveActive] = useState(initialPocket?.mode === 'live')
  const [liveStatusData, setLiveStatusData] = useState<LiveStatusResult | null>(null)
  const [liveError, setLiveError] = useState<string | null>(null)
  const [liveStopping, setLiveStopping] = useState(false)
  const [signalrAvailable, setSignalrAvailable] = useState(false)
  const closeSettings = useCallback(() => setSettingsOpen(false), [])
  const closeCatalog = useCallback(() => setCatalogOpen(false), [])
  const desktopWorkspace = useDesktopWorkspace()
  const workspace = workspaces[mode]
  const desk = deskPreferences[mode]
  const customEditing = workspaceDraft !== null
  const customWorkspace = workspaceDraft ?? workspace
  const handleWorkspace = useCallback((layout: typeof workspace) => {
    setWorkspaces(writeWorkspace(mode, layout))
  }, [mode])
  const handleDeskChange = useCallback((next: DeskMode) => {
    if (next === desk) return
    setWorkspaceDraft(null)
    setDeskPreferences(writeDeskPreference(mode, next))
  }, [desk, mode])
  const handleEditCustom = useCallback(() => {
    if (workspaceDraft) return
    setWorkspaceDraft(workspace)
  }, [workspace, workspaceDraft])
  const handleCustomDone = useCallback(() => {
    if (!workspaceDraft) return
    const saved = saveCustomWorkspace(mode, workspaceDraft)
    setWorkspaces(saved.workspaces)
    setDeskPreferences(saved.desks)
    setWorkspaceDraft(null)
  }, [mode, workspaceDraft])
  const handleMobileCenter = useCallback((center: MobileCenter) => {
    setMobileCenter(writeMobileCenter(center))
  }, [])

  // Driver focus: up to 2 selected IDs; survives scrub/play; resets on session change
  const [selectedIds, setSelectedIds] = useState<string[]>(initialPocket?.focusedDrivers ?? [])

  // PROJECTION toggle — replay only
  const [projection, setProjection] = useState(false)
  // VOICE alerts — speak flags/fastest laps/passes from the feed
  const [voice, setVoice] = useState(false)
  // Center bottom segment tab
  const [centerTab, setCenterTab] = useState<CenterTab>('FEED')

  useEffect(() => {
    if (centerTab === 'PACE' && !projection) {
      setCenterTab('FEED')
    }
  }, [centerTab, projection])

  // Build DataSource from current mode
  const source = useMemo<DataSource | null>(() => {
    if (mode === 'replay') {
      return sessionId ? { kind: 'replay', sessionId } : null
    }
    return isLiveActive ? { kind: 'live' } : null
  }, [mode, sessionId, isLiveActive])

  const replay = useReplay(source)
  const lang = replay.lang
  const scrubReplay = replay.scrub
  const pauseReplay = replay.pause
  const [pocketApplied, setPocketApplied] = useState(false)
  useEffect(() => {
    if (pocketApplied || initialPocket?.mode !== 'replay' || replay.timeline?.session_id !== initialPocket.sessionId) return
    scrubReplay(initialPocket.atMs ?? 0)
    setSelectedIds(initialPocket.focusedDrivers)
    setPocketApplied(true)
  }, [initialPocket, pocketApplied, replay.timeline?.session_id, scrubReplay])
  useVoiceAlerts(replay.feed, voice, replay.lang, mode === 'replay' ? sessionId : 'live')

  const liveDecision = liveLifecycle(liveStatusData, {
    readonly: readonlyDeployment !== false,
    explicitReplay: replayPinned,
    attachedToLive: mode === 'live' && isLiveActive,
  })
  const liveAvailable = liveDecision.canManage || liveDecision.remoteAvailable || isLiveActive

  const adoptReplay = useCallback((id: string) => {
    pauseReplay()
    setMode('replay')
    setIsLiveActive(false)
    setReplayPinned(true)
    setSelectedIds([])
    setWorkspaceDraft(null)
    setCenterTab('FEED')
    setSessionNotice(null)
    setSessionId(id)
    setCatalogOpen(false)
    const url = new URL(window.location.href)
    url.searchParams.set('session', id)
    url.searchParams.delete('catalog')
    window.history.replaceState(null, '', url)
  }, [pauseReplay])

  const adoptLive = useCallback(() => {
    pauseReplay()
    setMode('live')
    setIsLiveActive(true)
    setReplayPinned(false)
    setCatalogOpen(false)
    setLiveError(null)
    setCenterTab('FEED')
    setSelectedIds([])
    setWorkspaceDraft(null)
  }, [pauseReplay])

  const pocketTarget = useMemo<PocketTarget | null>(() => {
    const activeSession = mode === 'replay' ? sessionId : replay.state?.session_id ?? liveStatusData?.canonical_session_id
    if (!activeSession) return null
    return { version: 1, mode, sessionId: activeSession, atMs: mode === 'replay' ? replay.atMs : null, focusedDrivers: selectedIds }
  }, [liveStatusData?.canonical_session_id, mode, replay.atMs, replay.state?.session_id, selectedIds, sessionId])

  const loadSessions = useCallback(() => {
    let cancelled = false
    const requestedAtStart = new URLSearchParams(window.location.search).get('session')
    setSessionError(null)
    setBackendPhase('connecting')
    const wakeTimer = window.setTimeout(() => {
      if (!cancelled) setBackendPhase('waking')
    }, 1200)
    listSessions(() => setBackendPhase('waking'))
      .then((items) => {
        if (cancelled) return
        window.clearTimeout(wakeTimer)
        setReadyReplayIds(items.map((item) => item.session_id))
        const requested = new URLSearchParams(window.location.search).get('session')
        if (requested !== requestedAtStart) {
          setBackendPhase('ready')
          return
        }
        if (initialPocketLive) { setBackendPhase('ready'); return }
        const requestedSession = items.find((item) => item.session_id === requested)
        const initialSession = requestedSession?.session_id ?? null
        setSessionId((current) => (
          items.some((item) => item.session_id === current) ? current : initialSession
        ))
        if (requested && !requestedSession) {
          setSessionNotice(
            requested,
          )
          const url = new URL(window.location.href)
          url.searchParams.delete('session')
          window.history.replaceState(null, '', url)
          setCatalogOpen(true)
        } else {
          setSessionNotice(null)
        }
        setBackendPhase('ready')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        window.clearTimeout(wakeTimer)
        setSessionError(err instanceof Error ? err.message : 'Could not load sessions')
      })
    return () => {
      cancelled = true
      window.clearTimeout(wakeTimer)
    }
  }, [initialPocketLive])

  // Load replay sessions once; the bounded retry covers a sleeping free Render instance.
  useEffect(() => {
    return loadSessions()
  }, [loadSessions])

  // Resolve public/live routing before opening the catalog, so production Live
  // never flashes the replay picker on first load.
  useEffect(() => {
    let cancelled = false
    void Promise.allSettled([getCapabilities(), liveStatus()]).then(([capabilities, status]) => {
      if (cancelled) return
      const readonly = capabilities.status === 'fulfilled' ? capabilities.value.readonly : true
      setReadonlyDeployment(readonly)
      if (capabilities.status === 'fulfilled') {
        setSignalrAvailable(capabilities.value.signalr_available)
      }
      if (status.status === 'fulfilled') {
        setLiveStatusData(status.value)
        const decision = liveLifecycle(status.value, {
          readonly,
          explicitReplay: Boolean(initialSessionId) && !initialPocketLive,
          attachedToLive: initialPocketLive,
        })
        if (!initialPocketLive) {
          if (decision.replaySessionId) adoptReplay(decision.replaySessionId)
          else if (decision.enterLive) adoptLive()
          else if (!initialSessionId) setCatalogOpen(true)
        }
      } else if (!initialSessionId && !initialPocketLive) {
        setCatalogOpen(true)
      }
      setStartupReady(true)
    })
    return () => { cancelled = true }
  }, [adoptLive, adoptReplay, initialPocketLive, initialSessionId])

  // Public deployments keep discovering lifecycle changes; attached local Live
  // uses the same five-second poll. Failed polls retain the last truthful state.
  useEffect(() => {
    if (!startupReady || !(readonlyDeployment === true || mode === 'live' || isLiveActive)) return
    let cancelled = false
    let inFlight = false
    const poll = () => {
      if (inFlight) return
      inFlight = true
      liveStatus()
        .then((status) => {
          if (cancelled) return
          setLiveStatusData(status)
          const decision = liveLifecycle(status, {
            readonly: readonlyDeployment !== false,
            explicitReplay: replayPinned,
            attachedToLive: mode === 'live' && isLiveActive,
          })
          if (decision.replaySessionId) adoptReplay(decision.replaySessionId)
          else if (decision.enterLive) adoptLive()
        })
        .catch(() => undefined)
        .finally(() => { inFlight = false })
    }
    const id = window.setInterval(poll, 5000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [adoptLive, adoptReplay, isLiveActive, mode, readonlyDeployment, replayPinned, startupReady])

  // Esc to clear selection
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !document.querySelector('[aria-modal="true"]')) {
        setSelectedIds([])
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const handleWidgetAction = useCallback((
    source: 'battle' | 'strategy' | 'feed',
    ids: string[],
    atMs?: number,
  ) => {
    const action = workspaceAction(mode, source, ids, atMs)
    if (action.focusIds.length > 0) {
      setSelectedIds(action.focusIds)
      setMobTab('INSIGHTS')
    }
    if (action.seekMs !== null) {
      scrubReplay(action.seekMs)
    }
  }, [mode, scrubReplay])

  const handleSelectDriver = useCallback((id: string) => {
    const next = toggleDriverFocus(selectedIds, id)
    setMobTab('INSIGHTS')
    setSelectedIds(next)
  }, [selectedIds])

  const handleFocusDrivers = useCallback((ids: string[]) => {
    const focused = focusDriverIds(ids)
    if (focused.length === 0) return
    setSelectedIds(focused)
    setMobTab('INSIGHTS')
  }, [])

  const handleReplaySeek = useCallback((atMs: number) => {
    scrubReplay(atMs)
  }, [scrubReplay])

  const handleModeSwitch = (next: AppMode) => {
    if (next === mode) return
    if (next === 'live' && !liveAvailable) return
    if (next === 'live' && (liveDecision.remoteAvailable || liveStatusData?.is_running)) {
      adoptLive()
      return
    }
    replay.pause()
    setWorkspaceDraft(null)
    setMode(next)
    setIsLiveActive(false)
    setReplayPinned(next === 'replay')
    setLiveError(null)
    setCenterTab('FEED')
    setSelectedIds([])
    if (next === 'replay' && !sessionId) {
      // Switching away from Live without a chosen session must land on the
      // catalog, not an empty stage.
      setCatalogOpen(true)
    }
  }

  const handleSessionChange = (id: string) => {
    adoptReplay(id)
  }

  const state = replay.state
  const timeline = replay.timeline

  const effectivePositionsData = mode === 'live' ? null : replay.positionsData

  const rows = useMemo(() => {
    if (!state) return []
    return state.classification.map((driverId) => {
      const d = state.drivers[driverId]
      return { id: driverId, ...d, position: d.rank ?? d.position }
    })
  }, [state])

  const sessionStatus = state?.session_status ?? 'started'
  const liveView = livePresentation(liveStatusData, state !== null, replay.error, Date.now(), lang)
  const restartAnnouncement = replay.feed.find((item) =>
    item.text.toUpperCase().includes('RACE WILL RESUME AT'),
  )?.text ?? null

  if (!startupReady) {
    return (
      <div className="error-screen wake-screen" role="status" aria-live="polite">
        <div>
          <span className="wake-pulse" />
          <h2>{(lang === 'ru' ? 'Подключение' : 'Connecting')}</h2>
          <p>{(lang === 'ru' ? 'Проверяем доступность повторов и прямого эфира.' : 'Checking replay and Live availability.')}</p>
        </div>
      </div>
    )
  }

  if (sessionError && mode === 'replay') {
    return (
      <div className="error-screen">
        <div>
          <h2>{(lang === 'ru' ? 'Сервер гонок ещё не проснулся' : 'Race server is still asleep')}</h2>
          <p>{(lang === 'ru' ? 'Демоверсия не успела запуститься. Выбранная гонка сохранена.' : 'The free demo did not wake in time. Your selected race is safe.')}</p>
          <button type="button" className="b" onClick={loadSessions}>{(lang === 'ru' ? 'ПОВТОРИТЬ' : 'RETRY')}</button>
          <small>{viewerError(sessionError, lang)}</small>
        </div>
      </div>
    )
  }

  const hasFocus = selectedIds.length > 0

  // Replay follows the range immediately; live follows the latest stream state.
  let currentLap = 0
  if (mode === 'replay') {
    currentLap = timeline?.session_id === sessionId ? lapAtTime(timeline, replay.atMs) : 0
  } else if (state) {
    const inProgress = state.session_status === 'formation' ? 0 : (state.lap ?? 0) + 1
    currentLap = state.total_laps ? Math.min(inProgress, state.total_laps) : inProgress
  }

  // Build liveLabel for deck clock from current state lap
  const liveLabel = state && currentLap > 0 ? `${lang === 'ru' ? 'КРУГ' : 'LAP'} ${currentLap}` : null
  const liveSessionName = state?.session_name ?? (
    liveStatusData?.replay_session_id ? sessionLabel(liveStatusData.replay_session_id, lang) : null
  )
  const customDeskVisible = desktopWorkspace && (desk === 'custom' || customEditing)
  const projectionOn = customDeskVisible
    ? customWorkspace.widgets.pace.visible
    : projection
  const handleProjection = (value: boolean) => {
    setProjection(value)
    if (customDeskVisible) {
      const next = updateWorkspaceWidget(customWorkspace, mode, 'pace', { visible: value })
      if (customEditing) setWorkspaceDraft(next)
      else handleWorkspace(next)
    }
  }

  const workspaceWidgets = {
    timing: (
      <TimingTower lang={lang}
        rows={rows}
        battles={replay.battles}
        selectedIds={selectedIds}
        onSelectDriver={handleSelectDriver}
      />
    ),
    battles: (
      <>
        {mode === 'replay' && (
          <BroadcastOverlay
            atMs={replay.atMs}
            playing={replay.playing}
            speed={replay.speed}
            lang={replay.lang}
            markers={replay.markers}
            feed={replay.feed}
          />
        )}
        <BattleIntelligence lang={lang}
          rows={rows}
          battles={replay.battles}
          currentLap={currentLap}
          totalLaps={state?.total_laps ?? null}
          weather={state?.weather}
          onSelectDriver={handleSelectDriver}
          onSelectBattle={(ids) => handleWidgetAction('battle', ids)}
        />
      </>
    ),
    track: (
      <TrackMap lang={lang}
        key={mode === 'replay' ? sessionId ?? 'replay' : state?.session_id ?? 'live'}
        sessionId={mode === 'replay' ? sessionId : (state?.session_id ?? null)}
        atMs={replay.atMs}
        playing={replay.playing}
        playbackSpeed={mode === 'live' ? 1 : replay.speed}
        drivers={state?.drivers ?? {}}
        classification={state?.classification ?? []}
        totalLaps={state?.total_laps}
        sessionStatus={sessionStatus}
        neutralizationStartMs={replay.neutralizationStartMs}
        selectedIds={selectedIds}
        positionsData={effectivePositionsData}
        battles={replay.battles}
        recentPasses={replay.recentPasses}
        live={mode === 'live'}
      />
    ),
    insights: hasFocus ? (
      <div className="col col-insights col-focus">
        <div className="label">{(lang === 'ru' ? 'ФОКУС НА ПИЛОТЕ' : 'DRIVER FOCUS')}</div>
        <FocusPanel lang={lang}
          selectedIds={selectedIds}
          drivers={state?.drivers ?? {}}
          sessionId={mode === 'replay' ? sessionId : null}
          live={mode === 'live' && isLiveActive}
          atMs={replay.atMs}
          lap={currentLap}
          onStrategyRequest={mode === 'replay' ? replay.pause : undefined}
          onRemoveDriver={handleSelectDriver}
        />
      </div>
    ) : (
      <InsightPanel lang={lang}
        key={mode === 'replay' ? sessionId ?? 'replay' : 'live'}
        insights={mode !== 'replay' || timeline?.session_id === sessionId ? replay.insights : []}
        commentary={mode !== 'replay' || timeline?.session_id === sessionId ? replay.commentary : []}
        selectedIds={selectedIds}
        sessionStatus={sessionStatus}
        sessionId={mode === 'replay' ? sessionId : null}
        atMs={replay.atMs}
        onFocusDrivers={handleFocusDrivers}
      />
    ),
    feed: (
      <RaceFeed lang={lang}
        key={mode === 'replay' ? sessionId : 'live'}
        items={replay.feed}
        loading={replay.loading}
        clockOriginMs={mode === 'replay' && timeline?.session_id === sessionId ? timeline.lights_out_ms : undefined}
        onActivate={(item) => handleWidgetAction('feed', item.driver_id ? [item.driver_id] : [], item.at_ms)}
        isActionable={(item) => mode === 'replay' || Boolean(item.driver_id)}
      />
    ),
    strategy: (mode === 'replay' ? !!sessionId : isLiveActive) ? (
      <StintTimeline lang={lang}
        sessionId={mode === 'replay' ? sessionId : null}
        live={mode === 'live'}
        liveData={mode === 'live' ? state?.stints : null}
        currentLap={currentLap}
        order={state?.classification}
        onSelectDriver={(id) => handleWidgetAction('strategy', [id])}
      />
    ) : null,
    pace: mode === 'replay'
      ? sessionId && <ForecastStrip lang={lang} sessionId={sessionId} atMs={replay.atMs} />
      : isLiveActive && <ForecastStrip lang={lang} live atMs={replay.atMs} />,
    highlights: mode === 'replay' && sessionId ? (
      <HighlightsPanel sessionId={sessionId} lang={replay.lang} untilMs={replay.atMs} onSeek={handleReplaySeek} />
    ) : null,
    dotd: mode === 'replay' && sessionId ? (
      <DriverOfDayPanel
        sessionId={sessionId}
        lang={replay.lang}
        sessionStatus={sessionStatus}
        lap={currentLap}
        totalLaps={state?.total_laps ?? null}
        atMs={replay.atMs}
      />
    ) : null,
  }

  return (
    <>
      <SessionCatalog lang={lang}
        open={catalogOpen}
        landing={mode === 'replay' && !sessionId}
        initialSeason={initialCatalogSeason}
        readyReplayIds={readyReplayIds}
        onClose={closeCatalog}
        onOpenReplay={handleSessionChange}
      />
      <SettingsDrawer
        open={settingsOpen}
        onClose={closeSettings}
        lang={replay.lang}
        level={replay.level}
        mode={mode}
        liveAvailable={liveAvailable}
        liveNowAvailable={liveDecision.showLiveNow}
        onLang={replay.setLang}
        onLevel={replay.setLevel}
        onModeChange={handleModeSwitch}
        sessionId={mode === 'replay' ? sessionId : null}
        onSeek={mode === 'replay' ? handleReplaySeek : undefined}
        sessionStatus={sessionStatus}
        lap={currentLap}
        totalLaps={state?.total_laps ?? null}
        atMs={replay.atMs}
      />
      <TopBar
        sessionId={mode === 'replay' ? sessionId : null}
        lap={currentLap}
        totalLaps={state?.total_laps ?? null}
        lang={replay.lang}
        level={replay.level}
        mode={mode}
        liveAvailable={liveAvailable}
        liveNowAvailable={liveDecision.showLiveNow}
        projection={projectionOn}
        voice={voice}
        desk={desk}
        customEditing={customEditing}
        onModeChange={handleModeSwitch}
        onLang={replay.setLang}
        onLevel={replay.setLevel}
        onVoice={setVoice}
        onDeskChange={handleDeskChange}
        onEditCustom={handleEditCustom}
        onProjection={handleProjection}
        onSeek={mode === 'replay' ? handleReplaySeek : undefined}
        onSettingsOpen={() => setSettingsOpen(true)}
        onCatalogOpen={() => setCatalogOpen(true)}
        sessionStatus={sessionStatus}
        atMs={replay.atMs}
        anchoredHighlights={!customDeskVisible || !customWorkspace.widgets.highlights.visible}
        anchoredDotd={!customDeskVisible || !customWorkspace.widgets.dotd.visible}
        sessionName={mode === 'live' ? liveSessionName : null}
        companion={<CompanionLink lang={lang} target={pocketTarget} />}
      />

      {liveDecision.canManage && mode === 'live' && !isLiveActive && (
        <LiveLobby lang={lang}
          signalrAvailable={signalrAvailable}
          onStart={async (y, c, sessionName, source) => {
            setLiveError(null)
            await liveStart(y, c, sessionName, 6, source)
            setIsLiveActive(true)
            const status = await liveStatus()
            setLiveStatusData(status)
          }}
          onStop={() => { setIsLiveActive(false); setLiveStatusData(null) }}
        />
      )}
      {mode === 'live' && isLiveActive && (
        <div className="live-bar">
          <LiveStatusPill presentation={liveView} />
          {liveError && <span className="live-err">{viewerError(liveError, lang)}</span>}
          {liveDecision.canManage && (
            <button
              className="b danger"
              type="button"
              disabled={liveStopping}
              onClick={async () => {
                setLiveStopping(true)
                try {
                  await liveStop()
                  setIsLiveActive(false)
                  setLiveStatusData(null)
                  setLiveError(null)
                } catch (error) {
                  setLiveError(error instanceof Error ? error.message : 'Failed to stop live session')
                } finally {
                  setLiveStopping(false)
                }
              }}
            >
              {lang === 'ru' ? (liveStopping ? 'ОСТАНОВКА…' : 'СТОП') : (liveStopping ? 'STOPPING…' : 'STOP')}
            </button>
          )}
        </div>
      )}

      {backendPhase !== 'ready' && mode === 'replay' && (
        <div className="error-screen wake-screen" role="status" aria-live="polite">
          <div>
            <span className="wake-pulse" />
            <h2>{lang === 'ru' ? (backendPhase === 'waking' ? 'Пробуждаем сервер гонок' : 'Подключение') : (backendPhase === 'waking' ? 'Waking race server' : 'Connecting')}</h2>
            <p>{(lang === 'ru' ? 'После простоя бесплатному хостингу может потребоваться до минуты.' : 'Free hosting may need up to a minute after inactivity.')}</p>
          </div>
        </div>
      )}

      {((mode === 'replay' && sessionId) || isLiveActive) && (
        <>
          <StatusStrip lang={lang}
            status={sessionStatus}
            lap={state?.lap ?? null}
            atMs={replay.atMs}
            neutralizationStartMs={replay.neutralizationStartMs}
            greenFlag={replay.greenFlag}
            greenFlagText={replay.greenFlagText}
            restartAnnouncement={restartAnnouncement}
            restartAtMs={state?.restart_at_ms ?? null}
          />
          {sessionNotice && (
            <div className="feed-error" role="status">{lang === 'ru' ? `Повтор «${sessionNotice}» недоступен · выберите другую сессию` : `Replay "${sessionNotice}" is unavailable · choose another session`}</div>
          )}
          {(replay.error || replay.feedError) && (
            <div className="feed-error">{viewerError(replay.error || replay.feedError || '', lang)}</div>
          )}

          {/* Mobile tab bar — CSS shows only on <768px */}
          <div className="mob-tabbar">
            <div className="mob-tabbar-inner">
              {MOB_TABS.map((tab) => (
                <button
                  key={tab}
                  type="button"
                  className={`mob-tab${mobTab === tab ? ' mob-tab-on' : ''}`}
                  onClick={() => setMobTab(tab)}
                >{tabLabel(tab === 'MAP' ? mobileCenter.toUpperCase() : tab, lang)}</button>
              ))}
            </div>
          </div>

          {customDeskVisible ? (
            <WorkspaceGrid lang={lang}
              mode={mode}
              workspace={customWorkspace}
              editing={customEditing}
              widgets={workspaceWidgets}
              onChange={setWorkspaceDraft}
              onDone={handleCustomDone}
              onCancel={() => setWorkspaceDraft(null)}
              onReset={() => setWorkspaceDraft(defaultWorkspace(mode))}
            />
          ) : (
          <div className="wrap" data-mob-tab={mobTab}>
            <TimingTower lang={lang}
              rows={rows}
              battles={replay.battles}
              selectedIds={selectedIds}
              onSelectDriver={handleSelectDriver}
            />

            <div className="col col-center">
              {mode === 'replay' && (
                <BroadcastOverlay
                  atMs={replay.atMs}
                  playing={replay.playing}
                  speed={replay.speed}
                  lang={replay.lang}
                  markers={replay.markers}
                  feed={replay.feed}
                />
              )}
              <div className="center-heading">
                <span>{(lang === 'ru' ? 'РАБОЧАЯ ОБЛАСТЬ' : 'WORKSPACE')}</span>
                <div className="center-switch" role="group" aria-label={(lang === 'ru' ? 'Центральная рабочая область' : 'Center workspace')}>
                  {(['battles', 'track'] as const).map((center) => (
                    <button
                      key={center}
                      type="button"
                      className={mobileCenter === center ? 'on' : ''}
                      aria-pressed={mobileCenter === center}
                      onClick={() => handleMobileCenter(center)}
                    >
                      {tabLabel(center.toUpperCase(), lang)}
                    </button>
                  ))}
                </div>
              </div>
              {mobileCenter === 'battles' ? (
                <BattleIntelligence lang={lang}
                  rows={rows}
                  battles={replay.battles}
                  currentLap={currentLap}
                  totalLaps={state?.total_laps ?? null}
                  weather={state?.weather}
                  onSelectDriver={handleSelectDriver}
                  onSelectBattle={(ids) => handleWidgetAction('battle', ids)}
                />
              ) : (
                <TrackMap lang={lang}
                  key={mode === 'replay' ? sessionId ?? 'replay' : state?.session_id ?? 'live'}
                  sessionId={mode === 'replay' ? sessionId : (state?.session_id ?? null)}
                  atMs={replay.atMs}
                  playing={replay.playing}
                  playbackSpeed={mode === 'live' ? 1 : replay.speed}
                  drivers={state?.drivers ?? {}}
                  classification={state?.classification ?? []}
                  totalLaps={state?.total_laps}
                  sessionStatus={sessionStatus}
                  neutralizationStartMs={replay.neutralizationStartMs}
                  selectedIds={selectedIds}
                  positionsData={effectivePositionsData}
                  battles={replay.battles}
                  recentPasses={replay.recentPasses}
                  live={mode === 'live'}
                />
              )}
              {/* Center keeps its workspace and tabs while driver focus moves right. */}
              <div className="ctr-bottom">
                <CenterTabs lang={lang}
                  activeTab={centerTab}
                  showForecast={projection && (mode === 'replay' ? !!sessionId : isLiveActive)}
                  showStrategy={mode === 'replay' ? !!sessionId : isLiveActive}
                  onTab={setCenterTab}
                />
                <div className="ctr-pane">
                  {centerTab === 'FEED' && (
                    <RaceFeed lang={lang}
                      key={mode === 'replay' ? sessionId : 'live'}
                      items={replay.feed}
                      loading={replay.loading}
                      clockOriginMs={mode === 'replay' && timeline?.session_id === sessionId ? timeline.lights_out_ms : undefined}
                      onActivate={(item) => handleWidgetAction('feed', item.driver_id ? [item.driver_id] : [], item.at_ms)}
                      isActionable={(item) => mode === 'replay' || Boolean(item.driver_id)}
                    />
                  )}
                  {centerTab === 'STRATEGY' && (mode === 'replay' ? !!sessionId : isLiveActive) && (
                    <StintTimeline lang={lang}
                      sessionId={mode === 'replay' ? sessionId : null}
                      live={mode === 'live'}
                      liveData={mode === 'live' ? state?.stints : null}
                      currentLap={currentLap}
                      order={state?.classification}
                      onSelectDriver={(id) => handleWidgetAction('strategy', [id])}
                    />
                  )}
                  {centerTab === 'PACE' && projection && (
                    mode === 'replay'
                      ? sessionId && <ForecastStrip lang={lang} sessionId={sessionId} atMs={replay.atMs} />
                      : isLiveActive && <ForecastStrip lang={lang} live atMs={replay.atMs} />
                  )}
                </div>
              </div>
            </div>

            {/* Right column: driver focus when 1-2 selected, else insights feed. */}
            {hasFocus ? (
              <div className="col col-insights col-focus">
                <div className="label">{(lang === 'ru' ? 'ФОКУС НА ПИЛОТЕ' : 'DRIVER FOCUS')}</div>
                <FocusPanel lang={lang}
                  selectedIds={selectedIds}
                  drivers={state?.drivers ?? {}}
                  sessionId={mode === 'replay' ? sessionId : null}
                  live={mode === 'live' && isLiveActive}
                  atMs={replay.atMs}
                  lap={currentLap}
                  onStrategyRequest={mode === 'replay' ? replay.pause : undefined}
                  onRemoveDriver={handleSelectDriver}
                />
              </div>
            ) : (
              <InsightPanel lang={lang}
                key={mode === 'replay' ? sessionId ?? 'replay' : 'live'}
                insights={mode !== 'replay' || timeline?.session_id === sessionId ? replay.insights : []}
                commentary={mode !== 'replay' || timeline?.session_id === sessionId ? replay.commentary : []}
                selectedIds={selectedIds}
                sessionStatus={sessionStatus}
                sessionId={mode === 'replay' ? sessionId : null}
                atMs={replay.atMs}
                onFocusDrivers={handleFocusDrivers}
              />
            )}
          </div>
          )}

          <ReplayDeck lang={lang}
            timeline={timeline}
            atMs={replay.atMs}
            playing={replay.playing}
            speed={replay.speed}
            markers={replay.markers}
            canScrub={replay.canScrub}
            liveLabel={mode === 'live' ? liveLabel : null}
            livePhase={liveView.phase}
            liveBadge={liveView.badge}
            liveDetail={liveView.detail}
            onScrub={handleReplaySeek}
            onPlay={replay.play}
            onPause={replay.pause}
            onSpeed={replay.setSpeed}
          />
        </>
      )}
    </>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
