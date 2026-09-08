package com.fearlesstilted.racelens

import android.content.Intent
import android.media.MediaPlayer
import android.os.Bundle
import java.util.Locale
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner

class MainActivity : ComponentActivity() {
    private val viewModel: RaceViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        consumeDeepLink(intent)
        setContent { RaceLensApp(viewModel) }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        consumeDeepLink(intent)
        setIntent(Intent(this, MainActivity::class.java))
    }

    override fun onResume() { super.onResume(); viewModel.onForeground() }
    override fun onPause() { viewModel.onBackground(); super.onPause() }

    private fun consumeDeepLink(intent: Intent?) {
        val raw = intent?.data?.toString() ?: return
        intent.data = null
        viewModel.handleDeepLink(raw)
    }
}

private val Ink = Color(0xFF0A0C0D)
private val Paper = Color(0xFFF4F0E8)
private val Steel = Color(0xFF20262B)
private val Signal = Color(0xFFFF4E31)
private val Acid = Color(0xFFD5FF3F)
private val Dim = Color(0xFF9CA49E)

@Composable
fun RaceLensApp(viewModel: RaceViewModel) = MaterialTheme(
    colorScheme = darkColorScheme(primary = Signal, secondary = Acid, background = Ink, surface = Steel),
) {
    PocketScreen(viewModel.state, viewModel::chooseReplay, viewModel::enterLive, viewModel::seek, viewModel::beginSeek, viewModel::toggleReplay, viewModel::setReplaySpeed, viewModel::toggleDriver, viewModel::focusBattle, viewModel::retry)
}

@Composable
private fun PocketScreen(
    state: ScreenState,
    chooseReplay: (String) -> Unit,
    enterLive: () -> Unit,
    seek: (Long) -> Unit,
    beginSeek: () -> Unit,
    toggleReplay: () -> Unit,
    setReplaySpeed: (Int) -> Unit,
    toggleDriver: (String) -> Unit,
    focusBattle: (Battle) -> Unit,
    retry: () -> Unit,
) {
    val target = state.frame.target
    LazyColumn(
        modifier = Modifier.fillMaxSize().background(Ink).padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { Masthead(state) }
        item { ActionRail(state, chooseReplay, enterLive) }
        state.message?.let { message -> item { Notice(message, retry) } }
        if (target.sessionId.isBlank()) {
            item { EmptyReady(state, chooseReplay) }
        } else {
            item { SessionTitle(state) }
            if (target.mode == WatchMode.REPLAY) item { ReplayControl(state, seek, beginSeek, toggleReplay, setReplaySpeed) }
            item { NowArea(state, focusBattle) }
            if (state.feed.isNotEmpty()) item { FeedPanel(state.feed) }
            item { ClassificationLabel(state) }
            item { TimingList(state, toggleDriver) }
        }
        if (target.sessionId.isBlank()) item { Text("OPEN A RACE LENS POCKET LINK TO HAND OFF A SESSION.", color = Dim, fontSize = 10.sp, lineHeight = 14.sp, modifier = Modifier.padding(bottom = 16.dp)) }
    }
}

@Composable
private fun BattleNow(battle: Battle, onClick: () -> Unit) = Column(
    modifier = Modifier
        .fillMaxWidth()
        .heightIn(min = 48.dp)
        .clickable(onClick = onClick)
        .semantics {
            contentDescription = "Battle now: ${battle.driverOneId} versus ${battle.driverTwoId}. Tap to focus."
            role = Role.Button
        }
        .background(Signal)
        .padding(horizontal = 14.dp, vertical = 10.dp),
    verticalArrangement = Arrangement.spacedBy(2.dp),
) {
    Text("BATTLE NOW", color = Ink, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 10.sp)
    Text("${battle.driverOneId}  /  ${battle.driverTwoId}${battle.intervalSeconds?.let { "  ·  ${"%.1f".format(Locale.ROOT, it)}S" } ?: ""}", color = Ink, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 18.sp)
    Text("TAP TO FOCUS", color = Ink, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Bold, fontSize = 9.sp)
}

@Composable
private fun NowArea(state: ScreenState, focusBattle: (Battle) -> Unit) = Column(
    modifier = Modifier.fillMaxWidth().background(Paper).padding(14.dp),
    verticalArrangement = Arrangement.spacedBy(8.dp),
) {
    Text("NOW", color = Ink, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 11.sp)
    val selected = state.frame.target.focusedDrivers
    val battle = state.frame.snapshot?.battle
    if (selected.size == 2) FocusArea(state)
    else if (battle != null) BattleNow(battle) { focusBattle(battle) }
    else FocusArea(state)
}

@Composable
private fun Masthead(state: ScreenState) {
    val largeText = LocalDensity.current.fontScale > 1.4f
    val modifier = Modifier.fillMaxWidth().padding(top = 20.dp)
    if (largeText) Column(modifier, verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Brand()
        Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.CenterEnd) { StatusStamp(state) }
    } else Row(modifier, horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.Top) {
        Brand()
        StatusStamp(state)
    }
}

@Composable
private fun Brand() = Column {
        Text("RACE LENS", color = Paper, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 22.sp, letterSpacing = 2.sp)
        Text("POCKET", color = Signal, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 10.sp, letterSpacing = 1.5.sp)
}

@Composable
private fun StatusStamp(state: ScreenState) {
    val live = state.frame.target.mode == WatchMode.LIVE
    val stale = state.frame.freshness == Freshness.STALE
    val detail = when {
        !live && state.loading -> "SEEKING"
        !live && state.replayPlaying -> "${state.replaySpeed}X · PLAYING"
        !live -> "${state.replaySpeed}X · PAUSED"
        stale && state.live.status in setOf("finishing", "replay_ready", "failed", "idle") -> state.live.detail
        stale -> "Last frame held"
        else -> state.live.detail
    }
    Column(horizontalAlignment = Alignment.End) {
        Text(
            if (live) when {
                state.connecting -> "LIVE / CONNECTING"
                stale -> "LIVE / STALE"
                else -> "LIVE / NOW"
            } else "REPLAY",
            color = if (stale) Signal else Acid,
            fontFamily = FontFamily.Monospace,
            fontWeight = FontWeight.Bold,
            fontSize = 12.sp,
        )
        Text(detail.uppercase(), color = Dim, fontSize = 9.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
private fun ActionRail(state: ScreenState, chooseReplay: (String) -> Unit, enterLive: () -> Unit) {
    val recommendation = recommendedReplayId(state.sessions)
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        Button(
            onClick = { recommendation?.let(chooseReplay) },
            enabled = recommendation != null,
            modifier = Modifier.weight(1f).heightIn(min = 48.dp),
            shape = RoundedCornerShape(2.dp),
            colors = ButtonDefaults.buttonColors(containerColor = Signal, contentColor = Ink),
        ) { Text("REPLAY", fontWeight = FontWeight.Black, fontSize = 12.sp, maxLines = 1) }
        Button(
            onClick = enterLive,
            enabled = state.live.available,
            modifier = Modifier.heightIn(min = 48.dp),
            shape = RoundedCornerShape(2.dp),
            colors = ButtonDefaults.buttonColors(containerColor = Steel, contentColor = Paper),
        ) { Text("● LIVE", fontWeight = FontWeight.Black, fontSize = 12.sp) }
    }
}

@Composable
private fun EmptyReady(state: ScreenState, chooseReplay: (String) -> Unit) = Column(
    modifier = Modifier.fillMaxWidth().background(Steel).padding(18.dp),
    verticalArrangement = Arrangement.spacedBy(8.dp),
) {
    Text("READY ON THE GRID", color = Acid, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 13.sp)
    Text(if (state.loading) "Checking available sessions…" else "Choose the recommended replay or scan a Pocket handoff link.", color = Paper)
    recommendedReplayId(state.sessions)?.let { Button(onClick = { chooseReplay(it) }, modifier = Modifier.heightIn(min = 48.dp)) { Text("OPEN ${shortName(it)}") } }
}

@Composable
private fun SessionTitle(state: ScreenState) = Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
    Text(if (state.frame.target.mode == WatchMode.LIVE) "LIVE SESSION" else "RACE REPLAY", color = Signal, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 11.sp, letterSpacing = 1.2.sp)
    Text(state.frame.snapshot?.sessionName ?: shortName(state.frame.target.sessionId), color = Paper, fontSize = 24.sp, fontWeight = FontWeight.Black, maxLines = 2)
    Text("LAP ${state.frame.snapshot?.lap ?: "—"}  /  ${state.frame.snapshot?.status?.uppercase() ?: "WAITING"}  /  ${formatTime(state.frame.snapshot?.atMs ?: state.frame.target.replayMs ?: 0)}", color = Dim, fontFamily = FontFamily.Monospace, fontSize = 11.sp)
    state.frame.snapshot?.weather?.let { weather -> Conditions(weather, state.frame.snapshot?.atMs ?: 0) }
}

@Composable
private fun Conditions(weather: Weather, atMs: Long) = Column(
    modifier = Modifier.fillMaxWidth().background(Steel).padding(horizontal = 10.dp, vertical = 8.dp),
    verticalArrangement = Arrangement.spacedBy(2.dp),
) {
    Text("CONDITIONS", color = Acid, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 9.sp)
    Text(conditionsSummary(weather), color = Paper, fontFamily = FontFamily.Monospace, fontSize = 10.sp, maxLines = 2)
    weatherAgeLabel(weather, atMs)?.let { label ->
        Text(label, color = if (label.startsWith("WEATHER STALE")) Signal else Dim, fontFamily = FontFamily.Monospace, fontSize = 9.sp, maxLines = 1)
    }
}

@Composable
private fun FocusArea(state: ScreenState) {
    val selected = state.frame.target.focusedDrivers
    val byId = state.frame.snapshot?.drivers?.associateBy { it.id }.orEmpty()
    val edge = focusEdge(selected.mapNotNull(byId::get))
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(5.dp)) {
        Text(if (selected.size == 2) "BATTLE FOCUS" else "FOCUS TWO DRIVERS", color = Ink, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 11.sp)
        if (selected.size == 2) Text(
            when {
                edge == null -> "GAP COMPARISON PENDING"
                edge.driverId == null -> "LEVEL ON GAP"
                else -> "${edge.driverId} ${"%.3f".format(Locale.ROOT, edge.seconds)}S AHEAD"
            },
            color = Signal, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 12.sp, maxLines = 2,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            if (selected.isEmpty()) Text("Tap up to two names in classification.", color = Steel)
            selected.forEach { id ->
                val driver = byId[id]
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Text(id, color = Ink, fontSize = 22.sp, fontWeight = FontWeight.Black, fontFamily = FontFamily.Monospace, maxLines = 1)
                    Text("P${driver?.position ?: "—"}  ${formatGap(driver?.gapSeconds)}", color = Steel, fontFamily = FontFamily.Monospace, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Text("${driver?.tyre?.take(1) ?: "—"}${driver?.tyreAge?.let { " L$it" } ?: ""}  ·  ${driver?.laps ?: "—"} LAPS", color = Steel, fontFamily = FontFamily.Monospace, fontSize = 10.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    driver?.let { SectorLine(it) }
                }
            }
        }
    }
}

@Composable
private fun SectorLine(driver: DriverTiming) {
    val parts = driver.sectors.take(3).mapIndexed { index, sector ->
        if (sector == null) "S${index + 1} —" else "S${index + 1} ${formatSector(sector.timeMs)} L${sector.lap ?: "?"}"
    }
    if (parts.all { it.endsWith("—") }) return
    Text(parts.joinToString("  "), color = Paper, fontFamily = FontFamily.Monospace, fontSize = 10.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
}

@Composable
private fun FeedPanel(items: List<FeedItem>) {
    var player by remember { mutableStateOf<MediaPlayer?>(null) }
    var playingId by remember { mutableStateOf<String?>(null) }
    var playbackError by remember { mutableStateOf<String?>(null) }
    val largeText = LocalDensity.current.fontScale > 1.4f
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_PAUSE) {
            releasePlayer(player); player = null; playingId = null
        } }
        lifecycle.addObserver(observer)
        onDispose {
            lifecycle.removeObserver(observer)
            releasePlayer(player)
        }
    }
    Column(modifier = Modifier.fillMaxWidth().background(Steel).padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text("FEED / RADIO", color = Acid, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 11.sp)
        visibleFeedItems(items).forEach { item ->
            val playRadio = { url: String ->
                playbackError = null
                releasePlayer(player); playingId = item.id
                val candidate = MediaPlayer(); player = candidate
                runCatching {
                    candidate.setOnErrorListener { failed, _, _ ->
                        if (player === failed) { failed.release(); player = null; playingId = null; playbackError = "Radio unavailable" }
                        true
                    }
                    candidate.setOnCompletionListener { finished -> if (player === finished) { finished.release(); player = null; playingId = null } }
                    candidate.setDataSource(url)
                    candidate.setOnPreparedListener { prepared -> if (player === prepared) prepared.start() }
                    candidate.prepareAsync()
                }.onFailure { if (player === candidate) { candidate.release(); player = null; playingId = null; playbackError = "Radio unavailable" } }
            }
            if (largeText && item.audioUrl != null) Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Row(verticalAlignment = Alignment.Top) { FeedText(item) }
                Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.CenterEnd) {
                    RadioButton(item, playingId) { playRadio(item.audioUrl) }
                }
            } else Row(verticalAlignment = Alignment.CenterVertically) {
                FeedText(item)
                item.audioUrl?.let { url -> RadioButton(item, playingId) { playRadio(url) } }
            }
        }
        playbackError?.let { Text(it, color = Signal, fontSize = 10.sp) }
    }
}

@Composable
private fun RowScope.FeedText(item: FeedItem) {
    Text(item.lap?.let { "L$it" } ?: "•", color = Signal, fontFamily = FontFamily.Monospace, fontSize = 10.sp, modifier = Modifier.width(44.dp), maxLines = 1)
    Text(item.text, color = Paper, fontSize = 11.sp, modifier = Modifier.weight(1f), maxLines = 2, overflow = TextOverflow.Ellipsis)
}

@Composable
private fun RadioButton(item: FeedItem, playingId: String?, onClick: () -> Unit) = Button(
    onClick = onClick,
    modifier = Modifier.heightIn(min = 48.dp).semantics { contentDescription = "Play radio: ${item.text}" },
) { Text(if (playingId == item.id) "PLAYING" else "RADIO", fontSize = 9.sp) }

@Composable
private fun ReplayControl(state: ScreenState, seek: (Long) -> Unit, beginSeek: () -> Unit, toggleReplay: () -> Unit, setReplaySpeed: (Int) -> Unit) {
    val timeline = state.frame.timeline ?: return
    val start = timeline.startMs.coerceAtLeast(0)
    val end = timeline.endMs.coerceAtLeast(start + 1)
    var scrub by remember(state.frame.target.sessionId, state.frame.snapshot?.atMs) { mutableFloatStateOf((state.frame.snapshot?.atMs ?: start).coerceIn(start, end).toFloat()) }
    Column(modifier = Modifier.fillMaxWidth().background(Steel).padding(12.dp)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(onClick = toggleReplay, enabled = !state.loading, modifier = Modifier.heightIn(min = 48.dp)) {
                Text(if (state.replayPlaying) "PAUSE" else "PLAY")
            }
            Column(Modifier.weight(1f), horizontalAlignment = Alignment.End) {
                Text(if (state.connecting) "BUFFERING" else "REPLAY POSITION", color = Dim, fontFamily = FontFamily.Monospace, fontSize = 10.sp)
                Text("${formatTime(scrub.toLong())} / ${formatTime(end)}", color = Paper, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Bold, fontSize = 11.sp)
            }
        }
        Slider(value = scrub, onValueChange = { beginSeek(); scrub = it }, onValueChangeFinished = { seek(scrub.toLong()) }, valueRange = start.toFloat()..end.toFloat(), modifier = Modifier.semantics { contentDescription = "Replay position"; stateDescription = formatTime(scrub.toLong()) })
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf(1, 5, 10).forEach { speed ->
                Button(
                    onClick = { setReplaySpeed(speed) },
                    modifier = Modifier.weight(1f).heightIn(min = 48.dp).semantics {
                        selected = state.replaySpeed == speed
                        contentDescription = "Replay speed ${speed}x"
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = if (state.replaySpeed == speed) Signal else Ink, contentColor = if (state.replaySpeed == speed) Ink else Paper),
                ) { Text("${speed}×", fontSize = 11.sp, maxLines = 1) }
            }
        }
    }
}

@Composable
private fun ClassificationLabel(state: ScreenState) = Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
    Text("CLASSIFICATION", color = Paper, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, fontSize = 13.sp, modifier = Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
    Text(if (state.frame.freshness == Freshness.STALE) "STALE FRAME" else "TAP TO FOCUS", color = if (state.frame.freshness == Freshness.STALE) Signal else Dim, fontSize = 10.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
}

@Composable
private fun TimingList(state: ScreenState, toggleDriver: (String) -> Unit) {
    val drivers = state.frame.snapshot?.drivers.orEmpty()
    if (drivers.isEmpty()) {
        Box(Modifier.fillMaxWidth().heightIn(min = 96.dp).background(Steel), contentAlignment = Alignment.Center) { Text(if (state.loading) "Loading timing…" else "No timing frame yet", color = Dim) }
        return
    }
    Column {
        drivers.forEach { driver ->
            val selected = driver.id in state.frame.target.focusedDrivers
            Row(
                modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp).clickable { toggleDriver(driver.id) }.semantics {
                    contentDescription = "Driver ${driver.id}, position ${driver.position ?: "unknown"}"
                    this.selected = selected
                    role = Role.Button
                }.padding(vertical = 8.dp, horizontal = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("${driver.position ?: "—"}", color = if (selected) Acid else Paper, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, modifier = Modifier.width(36.dp), maxLines = 1)
                Text(driver.id, color = Paper, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Black, modifier = Modifier.width(56.dp), maxLines = 1)
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TimingCell("GAP", formatGap(driver.gapSeconds), Modifier.weight(1f))
                        TimingCell("INT", formatGap(driver.intervalSeconds), Modifier.weight(1f))
                    }
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TimingCell("TYRE", "${driver.tyre?.take(1) ?: "—"}${driver.tyreAge?.let { " L$it" } ?: ""}", Modifier.weight(1f))
                        TimingCell("LAPS", driver.laps.toString(), Modifier.weight(1f))
                    }
                }
            }
            HorizontalDivider(color = Color.White.copy(alpha = .1f))
        }
    }
}

@Composable
private fun TimingCell(label: String, value: String, modifier: Modifier = Modifier) = Column(modifier) {
    Text(label, color = Dim, fontFamily = FontFamily.Monospace, fontSize = 8.sp)
    Text(value, color = Paper, fontFamily = FontFamily.Monospace, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
}

@Composable
private fun Notice(message: String, retry: () -> Unit) = Row(
    modifier = Modifier.fillMaxWidth().background(Signal.copy(alpha = .18f)).padding(12.dp),
    verticalAlignment = Alignment.CenterVertically,
) {
    Text(message, color = Paper, modifier = Modifier.weight(1f), fontSize = 12.sp)
    Button(onClick = retry, modifier = Modifier.heightIn(min = 48.dp)) { Text("RETRY") }
}

private fun shortName(id: String): String {
    val canonical = Regex("(\\d{4})-(\\d+)-r").matchEntire(id)
    return canonical?.let { "${it.groupValues[1]} · RACE ${it.groupValues[2]}" }
        ?: id.replace('_', ' ').replaceFirstChar { it.uppercase() }
}
private fun formatGap(seconds: Double?) = when { seconds == null -> "—"; seconds == 0.0 -> "LEADER"; else -> "+%.3f".format(Locale.ROOT, seconds) }
private fun formatSector(ms: Int) = "%.3f".format(Locale.ROOT, ms / 1000.0)
private fun formatTime(ms: Long): String { val total = ms.coerceAtLeast(0) / 1_000; return "%d:%02d".format(total / 60, total % 60) }
private fun releasePlayer(player: MediaPlayer?) {
    player?.setOnPreparedListener(null)
    player?.setOnErrorListener(null)
    player?.setOnCompletionListener(null)
    player?.release()
}
