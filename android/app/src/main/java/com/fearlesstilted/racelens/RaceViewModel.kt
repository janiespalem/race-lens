package com.fearlesstilted.racelens

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class ScreenState(
    val sessions: List<SessionSummary> = emptyList(),
    val live: LiveAvailability = LiveAvailability(false, "Checking Live…", null),
    val frame: WatchFrame = WatchFrame(),
    val feed: List<FeedItem> = emptyList(),
    val loading: Boolean = false,
    val connecting: Boolean = false,
    val replayPlaying: Boolean = false,
    val replaySpeed: Int = 1,
    val message: String? = null,
)

class RaceViewModel : ViewModel() {
    private val origin = DEFAULT_ORIGIN
    private var refreshJob: Job? = null
    private var replayJob: Job? = null
    private var replayGeneration = 0L
    private var streamJob: Job? = null
    private var streamApi: RaceApi? = null
    private var feedJob: Job? = null
    private var liveGeneration = 0L
    private var liveReconnects = 0
    private var lastLiveFeedAt = 0L
    private var lastReplaySideAt = 0L
    private var foreground = true

    var state by mutableStateOf(ScreenState())
        private set

    init { refresh() }

    fun refresh() {
        refreshJob?.cancel()
        refreshJob = viewModelScope.launch {
            if (state.frame.target.sessionId.isBlank()) state = state.copy(loading = true, message = null)
            val api = RaceApi(origin)
            val sessions = async(Dispatchers.IO) { runCatching { api.sessions() } }
            val live = async(Dispatchers.IO) { runCatching { api.liveStatus() } }
            launch {
                val loadedSessions = sessions.await()
                state = state.copy(sessions = loadedSessions.getOrElse { state.sessions })
                if (state.frame.target.sessionId.isBlank()) {
                    recommendedReplayId(state.sessions)?.let(::chooseReplay) ?: run {
                        state = state.copy(loading = false, message = loadedSessions.exceptionOrNull()?.let { "Replay catalog unavailable" })
                    }
                }
            }
            launch {
                val loadedLive = live.await()
                state = state.copy(live = loadedLive.getOrElse { LiveAvailability(false, "Live status unavailable", null) })
                if (foreground && state.frame.target.mode == WatchMode.LIVE && state.live.available) startLiveStream()
            }
        }
    }

    fun chooseReplay(sessionId: String) = navigate(WatchTarget(1, WatchMode.REPLAY, sessionId, 0, emptyList()))

    fun seek(atMs: Long) {
        val target = state.frame.target
        if (target.mode == WatchMode.REPLAY) {
            pauseReplay()
            navigate(target.copy(replayMs = atMs))
        }
    }

    fun toggleReplay() {
        if (state.frame.target.mode != WatchMode.REPLAY || state.frame.snapshot == null) return
        if (state.replayPlaying) pauseReplay() else startReplayStream()
    }

    fun beginSeek() {
        if (state.replayPlaying) pauseReplay()
    }

    fun setReplaySpeed(speed: Int) {
        val normalized = normalizeReplaySpeed(speed)
        if (normalized == state.replaySpeed) return
        val wasPlaying = state.replayPlaying
        state = state.copy(replaySpeed = normalized)
        if (wasPlaying) startReplayStream()
    }

    fun toggleDriver(driverId: String) {
        val focused = state.frame.target.focusedDrivers
        state = state.copy(frame = reduceWatch(state.frame, WatchAction.Focus(toggleFocusedDriver(focused, driverId))))
    }

    fun focusBattle(battle: Battle) {
        state = state.copy(frame = reduceWatch(state.frame, WatchAction.Focus(listOf(battle.driverOneId, battle.driverTwoId))))
    }

    fun enterLive() {
        if (!state.live.available) {
            state = state.copy(message = state.live.detail)
            return
        }
        navigate(WatchTarget(1, WatchMode.LIVE, state.live.raceId ?: "live", null, emptyList()))
    }

    fun retry() = when {
        state.frame.target.sessionId.isBlank() -> refresh()
        state.frame.target.mode == WatchMode.LIVE -> {
            cancelActiveStream()
            startLiveStream()
        }
        else -> loadReplay(state.frame.target)
    }

    fun handleDeepLink(raw: String) {
        parseWatchLink(raw)?.let(::navigate) ?: run {
            state = state.copy(message = "Invalid Race Lens Pocket link")
        }
    }

    fun onForeground() {
        foreground = true
        if (refreshJob?.isActive != true) refresh()
        if (state.frame.target.mode == WatchMode.LIVE) startLiveStream()
        else if (
            state.frame.target.sessionId.isNotBlank() &&
            shouldResumeReplay(replayJob?.isActive == true, state.loading, state.frame.snapshot)
        ) loadReplay(state.frame.target)
    }

    fun onBackground() {
        foreground = false
        state = state.copy(replayPlaying = false)
        refreshJob?.cancel()
        refreshJob = null
        liveGeneration++
        cancelActiveStream()
        replayGeneration++
        replayJob?.cancel()
        replayJob = null
        feedJob?.cancel()
        feedJob = null
    }

    private fun navigate(target: WatchTarget) {
        val next = target.normalized()
        if (next == state.frame.target && state.frame.snapshot != null) return
        liveGeneration++
        cancelActiveStream()
        replayGeneration++
        replayJob?.cancel()
        feedJob?.cancel()
        val changedSession = next.sessionId != state.frame.target.sessionId || next.mode != state.frame.target.mode
        val frame = navigateWatchFrame(state.frame, next)
        state = state.copy(
            frame = frame,
            feed = if (changedSession) emptyList() else state.feed,
            loading = next.mode == WatchMode.REPLAY,
            connecting = false,
            replayPlaying = false,
            message = null,
        )
        if (next.mode == WatchMode.REPLAY) loadReplay(next) else if (foreground) startLiveStream()
    }

    private fun loadReplay(target: WatchTarget) {
        if (target.sessionId.isBlank()) return
        replayJob?.cancel()
        val generation = ++replayGeneration
        val knownTimeline = reusableReplayTimeline(state.frame, target)
        state = state.copy(loading = true, message = null)
        replayJob = viewModelScope.launch {
            try {
                val api = RaceApi(origin)
                val requestedAt = (target.replayMs ?: 0).coerceAtLeast(0)
                val timelineRequest = if (knownTimeline == null) async(Dispatchers.IO) { api.timeline(target.sessionId) } else null
                val snapshotRequest = if (knownTimeline == null) async(Dispatchers.IO) { api.replayState(target.sessionId, requestedAt) } else null
                val timeline = knownTimeline ?: requireNotNull(timelineRequest).await()
                val atMs = requestedAt.coerceIn(timeline.startMs.coerceAtLeast(0), timeline.endMs)
                var snapshot = if (snapshotRequest != null && atMs == requestedAt) snapshotRequest.await()
                else withContext(Dispatchers.IO) { api.replayState(target.sessionId, atMs) }
                val resolvedAt = resolvedReplayStart(atMs, timeline, snapshot.drivers.isNotEmpty())
                if (resolvedAt != atMs) snapshot = withContext(Dispatchers.IO) { api.replayState(target.sessionId, resolvedAt) }
                if (acceptsReplayCompletion(generation, replayGeneration, target, state.frame.target)) {
                    state = state.copy(frame = acceptSnapshot(state.frame, snapshot).copy(timeline = timeline), loading = false, message = null)
                    loadReplayFeed(generation, target, snapshot.atMs)
                    val battle = withContext(Dispatchers.IO) { runCatching { api.replayBattle(target.sessionId, snapshot.atMs) }.getOrNull() }
                    if (acceptsReplayCompletion(generation, replayGeneration, target, state.frame.target)) {
                        state = state.copy(frame = state.frame.copy(snapshot = state.frame.snapshot?.copy(battle = battle)))
                    }
                }
            } catch (_: CancellationException) {
                throw CancellationException()
            } catch (error: Exception) {
                if (acceptsReplayCompletion(generation, replayGeneration, target, state.frame.target)) {
                    state = state.copy(loading = false, message = safeMessage(error))
                }
            }
        }
    }

    private fun startLiveStream() {
        if (!foreground || state.frame.target.mode != WatchMode.LIVE) return
        if (streamJob?.isActive == true) return
        cancelActiveStream()
        val generation = ++liveGeneration
        val api = RaceApi(origin)
        streamApi = api
        streamJob = viewModelScope.launch {
            state = state.copy(connecting = true, message = null, frame = state.frame.copy(timeline = null))
            while (foreground && generation == liveGeneration && state.frame.target.mode == WatchMode.LIVE && isActive) {
                val result = runCatching { withContext(Dispatchers.IO) {
                    api.streamLive { snapshot -> viewModelScope.launch {
                        if (foreground && generation == liveGeneration && state.frame.target.mode == WatchMode.LIVE) {
                            liveReconnects = 0
                            state = state.copy(frame = state.frame.copy(snapshot = snapshot, freshness = Freshness.FRESH), connecting = false, message = null)
                            if (System.currentTimeMillis() - lastLiveFeedAt > 15_000) loadLiveFeed(generation, state.frame.target)
                        }
                    } }
                } }
                if (!foreground || generation != liveGeneration || state.frame.target.mode != WatchMode.LIVE) return@launch
                if (result.getOrNull() == LiveStreamResult.ENDED) {
                    val live = withContext(Dispatchers.IO) { runCatching { RaceApi(origin).liveStatus() } }.getOrNull()
                    if (!foreground || generation != liveGeneration || state.frame.target.mode != WatchMode.LIVE) return@launch
                    if (live?.status == "replay_ready" && live.replaySessionId != null) {
                        navigate(WatchTarget(1, WatchMode.REPLAY, live.replaySessionId, state.frame.snapshot?.atMs ?: 0, state.frame.target.focusedDrivers)); return@launch
                    }
                    if (live?.status in setOf("failed", "idle")) {
                        val stopped = requireNotNull(live)
                        state = state.copy(live = stopped, frame = state.frame.copy(freshness = Freshness.STALE), connecting = false, message = stopped.detail)
                        return@launch
                    }
                    state = state.copy(live = live ?: state.live, frame = state.frame.copy(freshness = Freshness.STALE), connecting = false, message = if (live?.status == "finishing") "Live ended; replay is preparing" else "Live connection closed")
                } else state = state.copy(frame = state.frame.copy(freshness = Freshness.STALE), connecting = false, message = "Live connection lost")
                delay((1_000L shl liveReconnects.coerceAtMost(4)).coerceAtMost(15_000L))
                liveReconnects = (liveReconnects + 1).coerceAtMost(4)
            }
        }
    }

    private fun startReplayStream() {
        val target = state.frame.target
        val snapshot = state.frame.snapshot ?: return
        if (!foreground || target.mode != WatchMode.REPLAY) return
        cancelActiveStream()
        replayJob?.cancel()
        val generation = ++replayGeneration
        val sessionId = target.sessionId
        val api = RaceApi(origin)
        streamApi = api
        state = state.copy(replayPlaying = true, connecting = true, message = null)
        lastReplaySideAt = 0L
        streamJob = viewModelScope.launch {
            val result = runCatching { withContext(Dispatchers.IO) {
                api.streamReplay(sessionId, snapshot.atMs, state.replaySpeed) { next -> viewModelScope.launch {
                    if (foreground && state.replayPlaying && acceptsStreamEvent(generation, replayGeneration, sessionId, state.frame.target)) {
                        state = state.copy(
                            frame = acceptSnapshot(state.frame.copy(target = state.frame.target.copy(replayMs = next.atMs)), next),
                            connecting = false,
                            message = null,
                        )
                        if (System.currentTimeMillis() - lastReplaySideAt >= 5_000) {
                            lastReplaySideAt = System.currentTimeMillis()
                            loadReplaySideData(generation, sessionId, next.atMs)
                        }
                    }
                } }
            } }
            if (!acceptsStreamEvent(generation, replayGeneration, sessionId, state.frame.target)) return@launch
            state = if (result.getOrNull() == LiveStreamResult.ENDED) {
                state.copy(replayPlaying = false, connecting = false)
            } else {
                state.copy(replayPlaying = false, connecting = false, message = "Replay connection lost")
            }
        }
    }

    private fun pauseReplay() {
        if (state.frame.target.mode != WatchMode.REPLAY) return
        state = state.copy(replayPlaying = false, connecting = false)
        replayGeneration++
        cancelActiveStream()
    }

    private fun cancelActiveStream() {
        streamApi?.cancelStream()
        streamApi = null
        streamJob?.cancel()
        streamJob = null
        state = state.copy(connecting = false)
    }

    private fun loadReplayFeed(generation: Long, target: WatchTarget, atMs: Long) {
        feedJob?.cancel()
        feedJob = viewModelScope.launch {
            val raceId = target.sessionId
            val items = withContext(Dispatchers.IO) { runCatching { RaceApi(origin).feed(raceId, atMs) } }.getOrNull() ?: return@launch
            if (generation == replayGeneration && target.replayIdentity() == state.frame.target.replayIdentity()) state = state.copy(feed = items)
        }
    }

    private fun loadReplaySideData(generation: Long, sessionId: String, atMs: Long) {
        feedJob?.cancel()
        feedJob = viewModelScope.launch {
            val api = RaceApi(origin)
            val items = withContext(Dispatchers.IO) { runCatching { api.feed(sessionId, atMs) }.getOrNull() }
            val battle = withContext(Dispatchers.IO) { runCatching { api.replayBattle(sessionId, atMs) } }
            val current = state.frame
            val currentSnapshot = current.snapshot ?: return@launch
            if (
                acceptsStreamEvent(generation, replayGeneration, sessionId, current.target) &&
                state.replayPlaying
            ) {
                state = state.copy(
                    feed = items ?: state.feed,
                    frame = current.copy(snapshot = currentSnapshot.copy(battle = if (battle.isSuccess) battle.getOrNull() else currentSnapshot.battle)),
                )
            }
        }
    }

    private fun loadLiveFeed(generation: Long, target: WatchTarget) {
        feedJob?.cancel()
        feedJob = viewModelScope.launch {
            lastLiveFeedAt = System.currentTimeMillis()
            val items = withContext(Dispatchers.IO) { runCatching { RaceApi(origin).liveFeed() } }.getOrNull() ?: return@launch
            if (foreground && generation == liveGeneration && target == state.frame.target && target.mode == WatchMode.LIVE) state = state.copy(feed = items)
        }
    }

    private fun safeMessage(error: Exception) = when (error) {
        is HttpStatusException -> "HTTP ${error.status}"
        else -> "Connection failed"
    }
}
