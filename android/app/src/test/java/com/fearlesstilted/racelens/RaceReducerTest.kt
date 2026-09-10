package com.fearlesstilted.racelens

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout

class RaceReducerTest {
    @Test
    fun repeatedPendingSeekKeepsItsOwnerButFailedOrInterruptedSeekCanRetry() {
        val target = WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 12_000, emptyList())
        val frame = WatchFrame(target, snapshot = RaceSnapshot(1_000, 1, "green", emptyList()), freshness = Freshness.STALE)

        assertTrue(canKeepWatchTarget(frame, target, requestActive = true, loading = true, hasError = false))
        // Keeping this request preserves the generation for both success and error completion.
        assertTrue(acceptsReplayCompletion(2, 2, target, frame.target))
        assertFalse(canKeepWatchTarget(frame, target, requestActive = false, loading = true, hasError = false))
        assertFalse(canKeepWatchTarget(frame, target, requestActive = false, loading = false, hasError = true))
        assertFalse(acceptsReplayCompletion(2, 3, target, target))
        assertFalse(acceptsReplayCompletion(2, 2, target, target.copy(replayMs = 24_000)))
        assertFalse(acceptsReplayCompletion(2, 2, target, target.copy(sessionId = "monza_2026_race")))
    }

    @Test
    fun foregroundDiscoveryRepeatsSeriallyAndStopsWhenCancelled() = runBlocking {
        val started = Channel<Int>(Channel.UNLIMITED)
        val releaseFirst = CompletableDeferred<Unit>()
        var requests = 0
        var active = 0
        var maxActive = 0
        var available = false
        val job = launch {
            refreshWhileActive(20) {
                launch {
                    active++
                    maxActive = maxOf(maxActive, active)
                    requests++
                    started.send(requests)
                    try {
                        if (requests == 1) releaseFirst.await()
                        available = requests > 1
                    } finally { active-- }
                }
            }
        }
        try {
            assertEquals(1, withTimeout(2_000) { started.receive() })
            delay(60)
            assertEquals(1, requests)
            assertFalse(available)
            releaseFirst.complete(Unit)
            assertEquals(2, withTimeout(2_000) { started.receive() })
            assertTrue(available)
            assertEquals(1, maxActive)
            job.cancelAndJoin()
            val stoppedAt = requests
            delay(60)
            assertEquals(stoppedAt, requests)
            assertEquals(0, active)
        } finally { job.cancelAndJoin() }
    }

    @Test
    fun appLinkParsesOneShotReplayHandoff() {
        val target = parseWatchLink(
            "https://race-lens.onrender.com/pocket?v=1&mode=replay&session=spa_2026_race&at=12000&drivers=VER,NOR",
        )

        assertEquals(WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 12_000, listOf("VER", "NOR")), target)
    }

    @Test
    fun appLinkRejectsInvalidHostAndLiveReplayTime() {
        assertNull(parseWatchLink("https://example.com/pocket?v=1&mode=live&session=live"))
        assertNull(parseWatchLink("https://race-lens.onrender.com/pocket?v=1&mode=live&session=live&at=3"))
    }

    @Test
    fun navigationClearsFrameAndLimitsFocusToTwoDrivers() {
        val current = WatchFrame(
            WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 1_000, listOf("VER")),
            snapshot = RaceSnapshot(1_000, 1, "green", emptyList()),
        )
        val moved = reduceWatch(current, WatchAction.Navigate(WatchTarget(1, WatchMode.LIVE, "live", null, emptyList())))

        assertNull(moved.snapshot)
        assertNull(moved.timeline)
        assertEquals(WatchMode.LIVE, moved.target.mode)
        assertEquals(listOf("NOR", "LEC"), reduceWatch(moved, WatchAction.Focus(listOf("NOR", "LEC", "VER", "NOR"))).target.focusedDrivers)
    }

    @Test
    fun sameSessionSeekKeepsUsefulDataButMarksTheOldFrameStale() {
        val timeline = Timeline(0, 60_000)
        val snapshot = RaceSnapshot(1_000, 1, "green", emptyList())
        val current = WatchFrame(
            WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 1_000, emptyList()),
            timeline = timeline,
            snapshot = snapshot,
        )

        val moved = navigateWatchFrame(current, current.target.copy(replayMs = 2_000))

        assertEquals(2_000L, moved.target.replayMs)
        assertEquals(timeline, moved.timeline)
        assertEquals(snapshot, moved.snapshot)
        assertEquals(Freshness.STALE, moved.freshness)
    }

    @Test
    fun acceptedReplayFrameRestoresFreshnessAfterASeek() {
        val stale = WatchFrame(
            WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 2_000, emptyList()),
            snapshot = RaceSnapshot(1_000, 1, "green", emptyList()),
            freshness = Freshness.STALE,
        )
        val accepted = RaceSnapshot(2_000, 1, "green", emptyList())

        val fresh = acceptSnapshot(stale, accepted)

        assertEquals(accepted, fresh.snapshot)
        assertEquals(Freshness.FRESH, fresh.freshness)
    }

    @Test
    fun driverTapsKeepTheLatestTwoSelections() {
        var selected = emptyList<String>()

        selected = toggleFocusedDriver(selected, "ANT")
        assertEquals(listOf("ANT"), selected)
        selected = toggleFocusedDriver(selected, "HAM")
        assertEquals(listOf("ANT", "HAM"), selected)
        selected = toggleFocusedDriver(selected, "RUS")
        assertEquals(listOf("HAM", "RUS"), selected)
        selected = toggleFocusedDriver(selected, "NOR")
        assertEquals(listOf("RUS", "NOR"), selected)
        selected = toggleFocusedDriver(selected, "RUS")
        assertEquals(listOf("NOR"), selected)
    }

    @Test
    fun staleReplayCompletionCannotReplaceNewTarget() {
        val target = WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 0, emptyList())
        assertEquals(true, acceptsReplayCompletion(2, 2, target, target))
        assertEquals(false, acceptsReplayCompletion(1, 2, target, target))
        assertEquals(false, acceptsReplayCompletion(2, 2, target, target.copy(sessionId = "monza_2026_race")))
    }

    @Test
    fun focusChangesDuringReplaySeekDoNotInvalidateTheRequestedFrame() {
        val requested = WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 12_000, listOf("VER"))
        assertEquals(true, acceptsReplayCompletion(2, 2, requested, requested.copy(focusedDrivers = listOf("NOR", "LEC"))))
    }

    @Test
    fun foregroundResumesOnlyAnInterruptedReplayLoad() {
        assertFalse(shouldResumeReplay(requestActive = true, loading = true))
        assertTrue(shouldResumeReplay(requestActive = false, loading = true))
        assertFalse(shouldResumeReplay(requestActive = false, loading = false))
    }

    @Test
    fun replayTimelineIsReusedOnlyForTheSameSession() {
        val target = WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 12_000, emptyList())
        val timeline = Timeline(1_000, 20_000)
        val frame = WatchFrame(target.copy(replayMs = 0), timeline = timeline)

        assertEquals(timeline, reusableReplayTimeline(frame, target))
        assertNull(reusableReplayTimeline(frame, target.copy(sessionId = "monza_2026_race")))
        assertNull(reusableReplayTimeline(frame, target.copy(mode = WatchMode.LIVE, replayMs = null)))
    }

    @Test
    fun recommendationMatchesTheWebRaceOrder() {
        val sessions = listOf(
            SessionSummary("monaco_2024_practice"),
            SessionSummary("bahrain_2021_race"),
            SessionSummary("hungarian_2026_race"),
        )
        assertEquals("hungarian_2026_race", recommendedReplayId(sessions))
        assertEquals("bahrain_2021_race", recommendedReplayId(sessions.dropLast(1)))
        assertEquals(null, recommendedReplayId(listOf(SessionSummary("spa_2026_practice"))))
    }

    @Test
    fun appUriParsesButHttpsRemainsTheBrowserFallback() {
        assertEquals(
            WatchTarget(1, WatchMode.LIVE, "2026-16-r", null, listOf("VER", "NOR")),
            parseWatchLink("racelens://pocket?v=1&mode=live&session=2026-16-r&drivers=VER,NOR"),
        )
    }

    @Test
    fun jsonNullDoesNotBecomeVisibleText() {
        assertNull(nullableJsonString(Any()))
        assertEquals("Dutch Grand Prix", nullableJsonString(" Dutch Grand Prix "))
    }

    @Test
    fun replayReadyIsNotPresentedAsStillPreparing() {
        assertEquals("Live ended; replay is preparing", liveStatusDetail("finishing", false, null))
        assertEquals("Live ended; replay ready", liveStatusDetail("replay_ready", false, null))
    }

    @Test
    fun battleParserSkipsMalformedEntriesAndKeepsTheFirstValidBattle() {
        val battle = parseBattleCandidates(listOf(
            Triple("BATTLE_DETECTED", listOf("NOR"), 0.2),
            Triple("OTHER", listOf("VER", "LEC"), 0.4),
            Triple("BATTLE_DETECTED", listOf("NOR", "ANT"), 0.8),
            Triple("BATTLE_DETECTED", listOf("HAM", "RUS"), 1.1),
        ))

        assertEquals(Battle("NOR", "ANT", 0.8), battle)
        assertNull(parseBattleCandidates(listOf(Triple("BATTLE_DETECTED", listOf("NOR"), null))))
    }

    @Test
    fun battleFocusReplacesTheExistingPair() {
        val frame = WatchFrame(WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 0, listOf("VER", "LEC")))
        val battle = Battle("NOR", "ANT", null)

        val focused = reduceWatch(frame, WatchAction.Focus(listOf(battle.driverOneId, battle.driverTwoId)))

        assertEquals(listOf("NOR", "ANT"), focused.target.focusedDrivers)
    }

    @Test
    fun feedKeepsRecentEventsAndTheNewestRadio() {
        val items = (1..6).map { index ->
            FeedItem("$index", "Event $index", index, if (index == 5) "https://livetiming.formula1.com/static/radio.mp3" else null)
        }

        assertEquals(listOf("1", "2", "3", "5"), visibleFeedItems(items).map(FeedItem::id))
    }

    @Test
    fun focusShowsWhichSelectedDriverHasTheGapAdvantage() {
        val verstappen = DriverTiming("VER", 4, 16.928, 2.1, "SOFT", 18, 55)
        val norris = DriverTiming("NOR", 1, null, null, "HARD", 18, 56)

        assertEquals(FocusEdge("NOR", 16.928), focusEdge(listOf(verstappen, norris)))
        assertEquals(FocusEdge(null, 0.0), focusEdge(listOf(norris, norris.copy(id = "PIA"))))
        assertNull(focusEdge(listOf(verstappen, norris.copy(id = "PIA", position = 2, gapSeconds = null))))
    }

    @Test
    fun replaySpeedIsLimitedToTheThreeVisibleControls() {
        assertEquals(1, normalizeReplaySpeed(1))
        assertEquals(5, normalizeReplaySpeed(5))
        assertEquals(10, normalizeReplaySpeed(10))
        assertEquals(1, normalizeReplaySpeed(2))
    }

    @Test
    fun emptyZeroFrameFallsForwardOnlyToTheSourceBackedStart() {
        val timeline = Timeline(-20_000, 120_000, 12_000)

        assertEquals(12_000L, resolvedReplayStart(0, timeline, hasDrivers = false))
        assertEquals(0L, resolvedReplayStart(0, timeline, hasDrivers = true))
        assertEquals(12_000L, resolvedReplayStart(12_000, timeline, hasDrivers = false))
    }

    @Test
    fun replayStreamUsesTheSelectedSpeedAndBoundedStart() {
        assertEquals(
            "/api/sessions/spa_2026_race/stream?speed=5&from_ms=0&tick_ms=1000",
            replayStreamPath("spa_2026_race", -50, 5),
        )
    }

    @Test
    fun replayStreamEventCannotCrossIntoLiveWithTheSameSessionId() {
        val replay = WatchTarget(1, WatchMode.REPLAY, "spa_2026_race", 0, emptyList())
        val live = replay.copy(mode = WatchMode.LIVE)

        assertTrue(acceptsStreamEvent(7, 7, replay.sessionId, replay))
        assertFalse(acceptsStreamEvent(7, 7, replay.sessionId, live))
    }

    @Test
    fun conditionsDoNotClaimDryWhenRainfallIsUnknown() {
        assertEquals("RAIN · TRACK 18° · AIR 12°", conditionsSummary(Weather(true, 18.9, 12.4)))
        assertEquals("DRY", conditionsSummary(Weather(false, null, null)))
        assertEquals("UNKNOWN · AIR 20°", conditionsSummary(Weather(null, null, 20.8)))
    }
}

class SectorParsingTest {
    @Test
    fun sectorsKeepEachSlotsOwnLapAndDropMalformedValues() {
        val json = org.json.JSONArray(
            "[{\"lap\":2,\"time_ms\":27795,\"at_ms\":12000}," +
                "{\"lap\":2,\"time_ms\":32161,\"at_ms\":19000}," +
                "null," +
                "{\"lap\":null,\"time_ms\":0,\"at_ms\":1}]",
        )
        assertEquals(
            listOf(SectorTime(2, 27795), SectorTime(2, 32161), null, null),
            parseSectors(json),
        )
        assertEquals(emptyList<SectorTime?>(), parseSectors(null))
    }
}

class WeatherFreshnessTest {
    @Test
    fun weatherAgeFollowsTheOldestSourceReportedField() {
        // Air was re-reported at 100s; track/rain stopped 90s earlier. The
        // label must reflect the stale fields, not the fresh air reading.
        val weather = Weather(true, 32.9, 18.7, mapOf("air_temp_c" to 100_000L, "track_temp_c" to 10_000L, "rainfall" to 10_000L))
        assertEquals("SOURCE AGE 1M", weatherAgeLabel(weather, 100_000))
        assertEquals("SOURCE AGE 3M", weatherAgeLabel(weather, 220_000))
        assertEquals("WEATHER STALE · 7M", weatherAgeLabel(weather, 460_000))
    }

    @Test
    fun weatherAgeClampsFramesAheadOfTheObservation() {
        val weather = Weather(true, 32.9, 18.7, mapOf("air_temp_c" to 100_000L, "track_temp_c" to 100_000L))
        assertEquals("SOURCE AGE 0M", weatherAgeLabel(weather, 5_000))
    }

    @Test
    fun weatherAgeIsUnknownForLegacySnapshotsWithoutObservationTimes() {
        assertNull(weatherAgeLabel(Weather(null, null, null, emptyMap()), 100_000))
    }

    @Test
    fun weatherAgeIgnoresHiddenFieldsLikeHumidity() {
        val weather = Weather(
            true, 32.9, 18.7,
            mapOf(
                "rainfall" to 115_000L,
                "track_temp_c" to 115_000L,
                "air_temp_c" to 110_000L,
                "humidity_percent" to 0L,
            ),
        )
        assertEquals("SOURCE AGE 0M", weatherAgeLabel(weather, 120_000))
    }

    @Test
    fun weatherAgeKeepsStaleRainfallEvenWhenAirIsFresh() {
        val weather = Weather(
            true, null, 18.7,
            mapOf("rainfall" to 0L, "air_temp_c" to 460_000L),
        )
        assertEquals("WEATHER STALE · 7M", weatherAgeLabel(weather, 460_000))
    }
}

class WeatherObservedParsingTest {
    @Test
    fun observationAtSessionStartIsAValidTimestamp() {
        val json = org.json.JSONObject(
            "{\"rainfall\": 0, \"air_temp_c\": 15000, \"missing\": null}",
        )
        // Rainfall observed at t=0 must survive; null/absent entries are dropped.
        assertEquals(mapOf("rainfall" to 0L, "air_temp_c" to 15_000L), parseWeatherObserved(json))
        assertEquals(emptyMap<String, Long>(), parseWeatherObserved(null))
    }
}
