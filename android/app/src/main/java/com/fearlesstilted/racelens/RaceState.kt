package com.fearlesstilted.racelens

import java.net.URI
import kotlin.math.abs
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive

private val sessionIdPattern = Regex("^[a-z0-9][a-z0-9_-]{0,79}$")
private val driverIdPattern = Regex("^[A-Z0-9]{2,5}$")
internal fun isValidDriverId(value: String) = driverIdPattern.matches(value)

enum class WatchMode { REPLAY, LIVE }
enum class Freshness { FRESH, STALE }

data class WatchTarget(
    val version: Int,
    val mode: WatchMode,
    val sessionId: String,
    val replayMs: Long?,
    val focusedDrivers: List<String>,
)

data class WatchFrame(
    val target: WatchTarget = WatchTarget(1, WatchMode.REPLAY, "", 0, emptyList()),
    val timeline: Timeline? = null,
    val snapshot: RaceSnapshot? = null,
    val freshness: Freshness = Freshness.FRESH,
)

data class FocusEdge(val driverId: String?, val seconds: Double)

sealed interface WatchAction {
    data class Navigate(val target: WatchTarget) : WatchAction
    data class Focus(val drivers: List<String>) : WatchAction
}

fun reduceWatch(frame: WatchFrame, action: WatchAction): WatchFrame = when (action) {
    is WatchAction.Navigate -> frame.copy(
        target = action.target.normalized(),
        timeline = null,
        snapshot = null,
        freshness = Freshness.FRESH,
    )
    is WatchAction.Focus -> frame.copy(target = frame.target.copy(focusedDrivers = action.drivers.focused()))
}

fun navigateWatchFrame(frame: WatchFrame, target: WatchTarget): WatchFrame {
    val next = target.normalized()
    val changedSession = next.sessionId != frame.target.sessionId || next.mode != frame.target.mode
    return if (changedSession) reduceWatch(frame, WatchAction.Navigate(next))
    else frame.copy(target = next, freshness = Freshness.STALE)
}

fun acceptSnapshot(frame: WatchFrame, snapshot: RaceSnapshot) = frame.copy(
    snapshot = snapshot,
    freshness = Freshness.FRESH,
)

fun acceptsReplayCompletion(generation: Long, currentGeneration: Long, requested: WatchTarget, current: WatchTarget) =
    generation == currentGeneration && requested.replayIdentity() == current.replayIdentity() && current.mode == WatchMode.REPLAY

fun shouldResumeReplay(requestActive: Boolean, loading: Boolean) = !requestActive && loading

internal fun canKeepWatchTarget(frame: WatchFrame, next: WatchTarget, requestActive: Boolean, loading: Boolean, hasError: Boolean) =
    next == frame.target && (
        (loading && requestActive) ||
            (!loading && !hasError && frame.snapshot != null && frame.freshness == Freshness.FRESH)
        )

internal suspend fun refreshWhileActive(intervalMs: Long, refresh: suspend CoroutineScope.() -> Unit) = coroutineScope {
    while (isActive) {
        coroutineScope(refresh)
        delay(intervalMs)
    }
}

fun normalizeReplaySpeed(speed: Int) = speed.takeIf { it in setOf(1, 5, 10) } ?: 1

fun conditionsSummary(weather: Weather) = buildList {
    add(when (weather.rainfall) { true -> "RAIN"; false -> "DRY"; null -> "UNKNOWN" })
    weather.trackTempC?.let { add("TRACK ${it.toInt()}°") }
    weather.airTempC?.let { add("AIR ${it.toInt()}°") }
}.joinToString(" · ")

const val WEATHER_STALE_MS = 300_000L

/** Age/staleness of the OLDEST source-reported weather field; null when unknown.
 *
 * A fresh air reading must not hide a stale track/rain value: the label
 * reflects the worst displayed field, never the newest.
 */
fun weatherAgeLabel(weather: Weather, atMs: Long): String? {
    val observed = weather.observedAtMs
    if (observed.isEmpty()) return null
    val worst = observed.values.maxOf { (atMs - it).coerceAtLeast(0) }
    val minutes = worst / 60_000
    return if (worst >= WEATHER_STALE_MS) "WEATHER STALE · ${minutes}M" else "SOURCE AGE ${minutes}M"
}

fun resolvedReplayStart(requestedMs: Long, timeline: Timeline, hasDrivers: Boolean): Long {
    if (requestedMs != 0L || hasDrivers) return requestedMs
    return timeline.lightsOutMs.coerceIn(timeline.startMs.coerceAtLeast(0), timeline.endMs)
}

fun acceptsStreamEvent(generation: Long, currentGeneration: Long, sessionId: String, current: WatchTarget) =
    generation == currentGeneration && current.mode == WatchMode.REPLAY && current.sessionId == sessionId

fun reusableReplayTimeline(frame: WatchFrame, target: WatchTarget) = frame.timeline.takeIf {
    frame.target.mode == WatchMode.REPLAY && target.mode == WatchMode.REPLAY && frame.target.sessionId == target.sessionId
}

fun toggleFocusedDriver(drivers: List<String>, driverId: String) =
    if (driverId in drivers) drivers - driverId else (drivers + driverId).takeLast(2)

fun visibleFeedItems(items: List<FeedItem>): List<FeedItem> {
    val visible = items.take(3)
    val radio = items.firstOrNull { it.audioUrl != null && visible.none { shown -> shown.id == it.id } }
    return if (radio == null) visible else visible + radio
}

fun focusEdge(drivers: List<DriverTiming>): FocusEdge? {
    if (drivers.size != 2) return null
    val firstGap = drivers[0].gapSeconds ?: if (drivers[0].position == 1) 0.0 else return null
    val secondGap = drivers[1].gapSeconds ?: if (drivers[1].position == 1) 0.0 else return null
    val delta = firstGap - secondGap
    return FocusEdge(
        driverId = when {
            abs(delta) < .001 -> null
            delta < 0 -> drivers[0].id
            else -> drivers[1].id
        },
        seconds = abs(delta),
    )
}

fun recommendedReplayId(sessions: List<SessionSummary>): String? = sessions.map { it.id }.firstOrNull { it == "hungarian_2026_race" }
    ?: sessions.map { it.id }.firstOrNull { it == "bahrain_2021_race" }
    ?: sessions.map { it.id }.firstOrNull { it.endsWith("_race") }

fun parseWatchLink(raw: String): WatchTarget? = runCatching {
    val uri = URI(raw)
    val https = uri.scheme == "https" && uri.host == "race-lens.onrender.com" && uri.path == "/pocket"
    val app = uri.scheme == "racelens" && uri.host == "pocket" && uri.path.isEmpty()
    if (!https && !app) return null
    val query = uri.rawQuery.orEmpty().split('&').associate {
        val (key, value) = it.split('=', limit = 2).let { pair -> pair[0] to pair.getOrElse(1) { "" } }
        java.net.URLDecoder.decode(key, Charsets.UTF_8) to java.net.URLDecoder.decode(value, Charsets.UTF_8)
    }
    val mode = when (query["mode"]) {
        "replay" -> WatchMode.REPLAY
        "live" -> WatchMode.LIVE
        else -> return null
    }
    val version = query["v"]?.toIntOrNull() ?: return null
    val sessionId = query["session"]?.trim().orEmpty()
    if (version != 1 || !sessionIdPattern.matches(sessionId)) return null
    val replayMs = query["at"]?.toLongOrNull()?.takeIf { it >= 0 }
    if ((mode == WatchMode.LIVE && replayMs != null) || (mode == WatchMode.REPLAY && replayMs == null)) return null
    WatchTarget(version, mode, sessionId, replayMs, query["drivers"].orEmpty().split(',').focused()).normalized()
}.getOrNull()

fun WatchTarget.normalized() = copy(
    sessionId = sessionId.trim(),
    replayMs = if (mode == WatchMode.LIVE) null else (replayMs ?: 0).coerceAtLeast(0),
    focusedDrivers = focusedDrivers.focused(),
)

private fun List<String>.focused() = map(String::trim).filter(::isValidDriverId).distinct().take(2)
fun WatchTarget.replayIdentity() = listOf(version, mode, sessionId, replayMs)
