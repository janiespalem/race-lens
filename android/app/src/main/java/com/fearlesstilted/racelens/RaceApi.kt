package com.fearlesstilted.racelens

import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.concurrent.atomic.AtomicBoolean

const val DEFAULT_ORIGIN = "https://race-lens.onrender.com"

data class SessionSummary(val id: String)
data class Timeline(
    val startMs: Long,
    val endMs: Long,
    val lightsOutMs: Long = 0,
)
data class DriverTiming(
    val id: String,
    val position: Int?,
    val gapSeconds: Double?,
    val intervalSeconds: Double?,
    val tyre: String?,
    val tyreAge: Int?,
    val laps: Int,
    val sectors: List<SectorTime?> = emptyList(),
)
data class SectorTime(val lap: Int?, val timeMs: Int)
data class Weather(val rainfall: Boolean?, val trackTempC: Double?, val airTempC: Double?)
data class Battle(val driverOneId: String, val driverTwoId: String, val intervalSeconds: Double?)
data class RaceSnapshot(val atMs: Long, val lap: Int, val status: String, val drivers: List<DriverTiming>, val weather: Weather? = null, val sessionName: String? = null, val battle: Battle? = null)
data class LiveAvailability(
    val available: Boolean,
    val detail: String,
    val raceId: String?,
    val replaySessionId: String? = null,
    val status: String = "idle",
)
data class FeedItem(val id: String, val text: String, val lap: Int?, val audioUrl: String?)
enum class LiveStreamResult { ENDED, CLOSED }
class HttpStatusException(val status: Int, message: String) : Exception(message)

class RaceApi(private val origin: String) {
    private val streamCancelled = AtomicBoolean(false)
    @Volatile private var streamConnection: HttpURLConnection? = null

    fun sessions(): List<SessionSummary> = getJson("/api/sessions").let { array ->
        List(array.length()) { index ->
            val item = array.getJSONObject(index)
            SessionSummary(item.getString("session_id"))
        }
    }

    fun timeline(raceId: String): Timeline = getObject("/api/sessions/${encoded(raceId)}/timeline").let { body ->
        Timeline(body.getLong("start_ms"), body.getLong("end_ms"), body.optLong("lights_out_ms"))
    }

    fun replayState(raceId: String, atMs: Long): RaceSnapshot = parseRaceState(
        getObject("/api/sessions/${encoded(raceId)}/state?at_ms=${atMs.coerceAtLeast(0)}")
    )

    fun replayBattle(raceId: String, atMs: Long): Battle? = parseBattles(
        getObject("/api/sessions/${encoded(raceId)}/battles?at_ms=${atMs.coerceAtLeast(0)}").optJSONArray("battles")
    )

    fun liveStatus(): LiveAvailability {
        val body = getObject("/api/live/status")
        val status = nullableJsonString(body.opt("status")) ?: if (body.optBoolean("is_running")) "live" else "idle"
        val available = body.optBoolean("is_running") || status == "live"
        val detail = liveStatusDetail(status, available, nullableJsonString(body.opt("failure")))
        return LiveAvailability(
            available, detail, nullableJsonString(body.opt("canonical_session_id")),
            nullableJsonString(body.opt("replay_session_id")), status,
        )
    }

    fun feed(raceId: String, atMs: Long): List<FeedItem> = parseFeed(getJson("/api/sessions/${encoded(raceId)}/feed?until_ms=${atMs.coerceAtLeast(0)}&limit=8"))
    fun liveFeed(): List<FeedItem> = parseFeed(getJson("/api/live/feed?limit=8"))

    fun streamLive(onState: (RaceSnapshot) -> Unit) = stream("/api/live/stream?tick_s=2", onState)

    fun streamReplay(raceId: String, fromMs: Long, speed: Int, onState: (RaceSnapshot) -> Unit) =
        stream(replayStreamPath(raceId, fromMs, speed), onState)

    fun cancelStream() {
        streamCancelled.set(true)
        streamConnection?.disconnect()
    }

    private fun stream(path: String, onState: (RaceSnapshot) -> Unit): LiveStreamResult {
        if (streamCancelled.get()) return LiveStreamResult.CLOSED
        val connection = open(path)
        streamConnection = connection
        if (streamCancelled.get()) {
            connection.disconnect()
            streamConnection = null
            return LiveStreamResult.CLOSED
        }
        return try {
            connection.readTimeout = 35_000
            checkResponse(connection)
            connection.inputStream.bufferedReader().use { reader ->
                while (true) {
                    val line = reader.readLine() ?: return LiveStreamResult.CLOSED
                    if (line.startsWith("data:")) {
                        val payload = line.substringAfter("data:").trim()
                        if (payload != "{}") onState(parseRaceState(JSONObject(payload)))
                    }
                    if (line == "event: end") return LiveStreamResult.ENDED
                }
            }
            LiveStreamResult.CLOSED
        } finally {
            connection.disconnect()
            if (streamConnection === connection) streamConnection = null
        }
    }

    private fun getObject(path: String): JSONObject = JSONObject(request(path))
    private fun getJson(path: String): JSONArray = JSONArray(request(path))

    private fun request(path: String): String {
        val connection = open(path)
        return try {
            checkResponse(connection)
            connection.inputStream.bufferedReader().use { it.readText() }
        } finally {
            connection.disconnect()
        }
    }

    private fun open(path: String) = (URL(origin + path).openConnection() as HttpURLConnection).apply {
        connectTimeout = 8_000
        readTimeout = 30_000
        setRequestProperty("Accept", "application/json, text/event-stream")
    }
}

private fun parseRaceState(json: JSONObject): RaceSnapshot {
    val order = json.optJSONArray("classification") ?: JSONArray()
    val drivers = json.optJSONObject("drivers") ?: JSONObject()
    val weather = json.optJSONObject("weather")?.let { value ->
        Weather(value.opt("rainfall") as? Boolean, value.optNullableDouble("track_temp_c"), value.optNullableDouble("air_temp_c"))
            .takeIf { it.rainfall != null || it.trackTempC != null || it.airTempC != null }
    }
    return RaceSnapshot(
        atMs = json.optLong("at_ms"),
        lap = json.optInt("lap"),
        status = nullableJsonString(json.opt("session_status")) ?: "unknown",
        drivers = List(order.length()) { index ->
            val id = order.getString(index)
            val driver = drivers.optJSONObject(id) ?: JSONObject()
            DriverTiming(
                id = id,
                position = driver.optNullableInt("rank") ?: driver.optNullableInt("position"),
                gapSeconds = driver.optNullableDouble("gap_s"),
                intervalSeconds = driver.optNullableDouble("interval_s"),
                tyre = nullableJsonString(driver.opt("tyre_compound")),
                tyreAge = driver.optNullableInt("tyre_age_laps"),
                laps = driver.optInt("laps_completed"),
                sectors = parseSectors(driver.optJSONArray("sectors")),
            )
        },
        weather = weather,
        sessionName = nullableJsonString(json.opt("session_name")),
        battle = parseBattles(json.optJSONArray("battles")),
    )
}

internal fun parseSectors(items: JSONArray?): List<SectorTime?> =
    if (items == null) emptyList() else List(items.length()) { index ->
        val slot = items.optJSONObject(index)
        if (slot == null) {
            null
        } else {
            val timeMs = slot.optInt("time_ms")
            SectorTime(slot.optNullableInt("lap"), timeMs).takeIf { timeMs > 0 }
        }
    }

private fun parseBattles(items: JSONArray?): Battle? {
    if (items == null) return null
    return parseBattleCandidates(buildList {
        for (index in 0 until items.length()) {
            val item = items.optJSONObject(index) ?: continue
            val ids = item.optJSONArray("driver_ids")?.let { values ->
                List(values.length()) { nullableJsonString(values.opt(it)).orEmpty() }
            }.orEmpty()
            val interval = (item.optJSONObject("evidence")?.opt("interval_s") as? Number)?.toDouble()
            add(Triple(nullableJsonString(item.opt("type")), ids, interval))
        }
    })
}

internal fun parseBattleCandidates(candidates: List<Triple<String?, List<String>, Double?>>): Battle? =
    candidates.firstNotNullOfOrNull { (type, drivers, interval) ->
        drivers.takeIf { type == "BATTLE_DETECTED" && it.size == 2 && it.distinct().size == 2 && it.all(::isValidDriverId) }
            ?.let { Battle(it[0], it[1], interval?.takeIf { value -> value.isFinite() && value >= 0 }) }
    }

private fun parseFeed(items: JSONArray): List<FeedItem> = buildList {
    for (index in 0 until items.length()) {
        val item = items.optJSONObject(index) ?: continue
        val text = nullableJsonString(item.opt("text")) ?: continue
        add(FeedItem(nullableJsonString(item.opt("id")) ?: "feed-$index", text, item.optNullableInt("lap"), safeRadioUrl(nullableJsonString(item.opt("audio_url")).orEmpty())))
    }
}

private fun JSONObject.optNullableInt(name: String) = if (isNull(name) || !has(name)) null else optInt(name)
private fun JSONObject.optNullableDouble(name: String) = if (isNull(name) || !has(name)) null else optDouble(name)
internal fun nullableJsonString(value: Any?) = (value as? String)?.trim()?.takeIf(String::isNotEmpty)
internal fun liveStatusDetail(status: String, available: Boolean, failure: String?) = when {
    status == "failed" -> failure ?: "Live failed"
    available -> "Live timing active"
    status == "finishing" -> "Live ended; replay is preparing"
    status == "replay_ready" -> "Live ended; replay ready"
    else -> "No live session"
}
private fun safeRadioUrl(value: String): String? = runCatching {
    val uri = java.net.URI(value)
    value.takeIf { value.length <= 2048 && uri.scheme == "https" && uri.host == "livetiming.formula1.com" && uri.path.startsWith("/static/") }
}.getOrNull()
private fun encoded(value: String) = URLEncoder.encode(value, StandardCharsets.UTF_8.name())
internal fun replayStreamPath(raceId: String, fromMs: Long, speed: Int) =
    "/api/sessions/${encoded(raceId)}/stream?speed=${normalizeReplaySpeed(speed)}&from_ms=${fromMs.coerceAtLeast(0)}&tick_ms=1000"

private fun checkResponse(connection: HttpURLConnection) {
    val status = connection.responseCode
    if (status !in 200..299) {
        val detail = connection.errorStream?.bufferedReader()?.use(BufferedReader::readText)?.take(200)
        throw HttpStatusException(status, detail ?: "HTTP $status")
    }
}
