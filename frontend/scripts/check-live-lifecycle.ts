import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

import type { LiveStatusResult } from '../src/api/client.ts'
import {
  liveLifecycle,
  livePresentation,
  restartCountdown,
} from '../src/lib/liveStatus.ts'

const remoteLive: LiveStatusResult = {
  is_running: true,
  poll_count: 12,
  events_total: 200,
  last_poll_ok: true,
  last_poll_unix: 1_786_310_400,
  last_new_event_unix: 1_786_310_399,
  last_error: null,
  data_quality: 'good',
  source: 'remote',
  status: 'live',
  canonical_session_id: '2026-15-r',
  replay_session_id: 'dutch_2026_race',
  generated_at: '2026-08-10T12:00:00Z',
  expires_at: '2026-08-10T12:00:20Z',
  capture_freshness: {
    raw_size: 1024,
    raw_updated_at: '2026-08-10T11:59:59Z',
    seconds_since_growth: 1,
    transport_growing: true,
  },
  failure: null,
}

assert.equal(liveLifecycle(remoteLive, {
  readonly: true,
  explicitReplay: false,
  attachedToLive: false,
}).enterLive, true, 'no-query production visit adopts active Live')

const explicitReplay = liveLifecycle(remoteLive, {
  readonly: true,
  explicitReplay: true,
  attachedToLive: false,
})
assert.equal(explicitReplay.enterLive, false, 'explicit replay is not hijacked')
assert.equal(explicitReplay.showLiveNow, true, 'explicit replay offers LIVE NOW')

const localLive: LiveStatusResult = {
  is_running: true,
  poll_count: 4,
  events_total: 20,
  last_poll_ok: true,
  last_poll_unix: 1_786_310_400,
  last_new_event_unix: 1_786_310_399,
  last_error: null,
  data_quality: 'good',
}
const idle = { ...localLive, is_running: false, status: 'idle' as const }
assert.equal(livePresentation(idle, false, null).badge, 'LIVE OFF')
assert.equal(liveLifecycle(localLive, {
  readonly: false,
  explicitReplay: false,
  attachedToLive: false,
}).enterLive, true, 'F5 reattaches to local Live')

assert.equal(livePresentation(
  { ...remoteLive, expires_at: '2026-08-10T11:59:59Z' },
  true,
  'offline',
  Date.parse('2026-08-10T12:00:00Z'),
).phase, 'stalled', 'expired snapshots stay stalled while EventSource reconnects')
assert.equal(livePresentation(
  remoteLive,
  true,
  'offline',
  Date.parse('2026-08-10T12:00:10Z'),
).phase, 'reconnecting')

const finishing = { ...remoteLive, is_running: false, status: 'finishing' as const }
assert.equal(liveLifecycle(finishing, {
  readonly: true,
  explicitReplay: false,
  attachedToLive: false,
}).enterLive, true, 'F5 during finishing reattaches to preparing state')
assert.equal(livePresentation(finishing, true, null).phase, 'preparing')
assert.equal(livePresentation(finishing, true, null).badge, 'REPLAY PREPARING')

const replayReady = { ...remoteLive, is_running: false, status: 'replay_ready' as const }
assert.equal(liveLifecycle(replayReady, {
  readonly: true,
  explicitReplay: false,
  attachedToLive: true,
}).replaySessionId, 'dutch_2026_race')

const failed = {
  ...remoteLive,
  is_running: false,
  status: 'failed' as const,
  failure: 'Archive preparation failed',
}
assert.equal(liveLifecycle(failed, {
  readonly: true,
  explicitReplay: false,
  attachedToLive: true,
}).replaySessionId, null, 'failure never fakes replay readiness')
assert.equal(livePresentation(failed, true, null).phase, 'failed')

assert.equal(liveLifecycle(remoteLive, {
  readonly: true,
  explicitReplay: false,
  attachedToLive: true,
}).canManage, false, 'readonly deployments expose no management actions')
assert.equal(liveLifecycle(remoteLive, {
  readonly: false,
  explicitReplay: false,
  attachedToLive: true,
}).canManage, false, 'remote Live exposes no management actions on writable deployments')
assert.equal(liveLifecycle(localLive, {
  readonly: false,
  explicitReplay: false,
  attachedToLive: true,
}).canManage, true)

assert.equal(restartCountdown(1_349_000, 1_980_000), '10:31')
assert.equal(restartCountdown(1_979_001, 1_980_000), '00:01')
assert.equal(restartCountdown(1_980_000, 1_980_000), null)

console.log('production Live lifecycle checks passed')

// Exercise the actual stream hook with controlled transport/timers; no browser
// or new test dependency is needed for disconnect ownership.
const timers = new Map<number, () => void>()
let timerId = 0
const clock = {
  setTimeout(fn: () => void) { const id = ++timerId; timers.set(id, fn); return id },
  clearTimeout(id: number) { timers.delete(id) },
}
const streams: FakeStream[] = []
class FakeStream {
  url: string
  closed = false
  onmessage: any
  onerror: any
  onopen: any
  end: any
  constructor(url: string) { this.url = url; streams.push(this) }
  close() { this.closed = true }
  addEventListener(name: string, fn: () => void) { if (name === 'end') this.end = fn }
  frame(atMs: number) { this.onmessage({ data: JSON.stringify({ at_ms: atMs }) }) }
}
const module = { exports: {} as any }
const api = new Proxy({}, { get: () => async () => ({ items: [], battles: [] }) })
const js = ts.transpileModule(readFileSync(new URL('../src/features/replay/useReplayStream.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
new Function('require', 'module', 'exports', 'EventSource', 'setTimeout', 'clearTimeout', 'performance', js)(
  (name: string) => name === 'react' ? {
    useRef: (current: unknown) => ({ current }),
    useCallback: (fn: unknown) => fn,
  } : api,
  module, module.exports, FakeStream, clock.setTimeout, clock.clearTimeout, { now: () => 1000 },
)
let acceptedMs = 0
let playing = false
const set = new Proxy({}, { get: (_target, name) => {
  if (name === 'setAtMs') return (ms: number) => { acceptedMs = ms }
  if (name === 'setPlaying') return (value: boolean) => { playing = value }
  return () => undefined
} })
const { openStream, closeStream } = module.exports.useReplayStream(true,
  (speed: number, atMs: number, lang: string, level: string) => `/race/stream?speed=${speed}&from_ms=${atMs}&lang=${lang}&level=${level}`,
  'race', set,
)
const runTimers = () => {
  const pending = [...timers.values()]
  timers.clear()
  pending.forEach((fn) => fn())
}
openStream(5, 1000, 'ru', 'beginner')
const first = streams.at(-1)!
first.frame(9000)
first.onerror()
assert.equal(first.closed, true, 'disconnect closes native reconnect at the old URL')
assert.equal(timers.size, 1, 'one controlled reconnect is scheduled')
first.onerror()
assert.equal(timers.size, 1, 'repeated errors cannot schedule duplicate streams')
first.frame(2000)
assert.equal(acceptedMs, 9000, 'disconnected source cannot rewind accepted state')
runTimers()
const resumed = streams.at(-1)!
assert.notEqual(resumed, first)
assert.equal(resumed.url, '/race/stream?speed=5&from_ms=9000&lang=ru&level=beginner')
assert.equal(streams.filter((stream) => !stream.closed).length, 1)
resumed.frame(12_000)
resumed.onerror()
runTimers()
assert.match(streams.at(-1)!.url, /from_ms=12000&/, 'each reconnect advances its cursor')

// pause/seek/session cleanup/unmount all use this same cancellation interface.
streams.at(-1)!.onerror()
const obsoleteRetry = [...timers.values()].at(-1)!
const beforeClose = streams.length
closeStream()
assert.equal(timers.size, 0)
obsoleteRetry()
assert.equal(streams.length, beforeClose, 'cancelled callback cannot reopen an obsolete stream')
openStream(10, 30_000, 'en', 'pro')
obsoleteRetry()
assert.equal(streams.length, beforeClose + 1, 'new seek owns its stream')
const final = streams.at(-1)!
final.end()
final.onerror()
runTimers()
assert.equal(final.closed, true)
assert.equal(playing, false)
assert.equal(streams.length, beforeClose + 1, 'explicit end never reconnects')
console.log('replay disconnect/resume ownership checks passed')
