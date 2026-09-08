import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import ts from 'typescript'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../src')

// Run the production hooks without a browser/test-renderer dependency. Only
// React scheduling and transport are controlled; replay request logic is real.
function hookHost() {
  const slots: any[] = []
  let cursor = 0
  let dirty = true
  let effects: (() => void)[] = []
  const same = (a: unknown[] | undefined, b: unknown[]) => a?.length === b.length && a.every((v, i) => Object.is(v, b[i]))
  const memo = (fn: () => any, deps: unknown[]) => {
    const index = cursor++
    if (!same(slots[index]?.deps, deps)) slots[index] = { deps, value: fn() }
    return slots[index].value
  }
  const effect = (fn: () => any, deps: unknown[]) => {
    const index = cursor++
    if (same(slots[index]?.deps, deps)) return
    const previous = slots[index]
    slots[index] = { deps }
    effects.push(() => { previous?.cleanup?.(); slots[index].cleanup = fn() })
  }
  return {
    react: {
      useState(initial: any) {
        const index = cursor++
        if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial
        return [slots[index], (value: any) => {
          const next = typeof value === 'function' ? value(slots[index]) : value
          if (!Object.is(slots[index], next)) { slots[index] = next; dirty = true }
        }]
      },
      useRef: (value: any) => memo(() => ({ current: value }), []),
      useMemo: memo,
      useCallback: (fn: () => any, deps: unknown[]) => memo(() => fn, deps),
      useEffect: effect,
      useLayoutEffect: effect,
    },
    render(fn: () => any) {
      let result
      do {
        dirty = false
        cursor = 0
        result = fn()
        const pending = effects
        effects = []
        pending.forEach((run) => run())
      } while (dirty)
      return result
    },
  }
}

function moduleLoader(overrides: Record<string, any>, globals: Record<string, any> = {}) {
  const cache = new Map<string, any>()
  const load = (path: string): any => {
    if (path in overrides) return overrides[path]
    if (cache.has(path)) return cache.get(path)
    const module = { exports: {} }
    const js = ts.transpileModule(readFileSync(path, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText
    const require = (id: string) => id in overrides ? overrides[id] : load(resolve(dirname(path), `${id}.ts`))
    new Function('require', 'module', 'exports', ...Object.keys(globals), js)(require, module, module.exports, ...Object.values(globals))
    cache.set(path, module.exports)
    return module.exports
  }
  return load
}

function replayRig() {
  const host = hookHost()
  const requests: { atMs: number; resolve: (state: any) => void }[] = []
  const timers = new Map<number, () => void>()
  const streams: any[] = []
  let nextTimer = 0
  const clock = {
    setTimeout(fn: () => void) { const id = ++nextTimer; timers.set(id, fn); return id },
    clearTimeout(id: number) { timers.delete(id) },
  }
  const api = {
    getTimeline: async () => ({ session_id: 'race', start_ms: 1000, end_ms: 100_000, lights_out_ms: 1000, laps: [] }),
    getMarkers: async () => ({ markers: [] }),
    getState: (_sid: string, atMs: number) => new Promise((resolve) => requests.push({ atMs, resolve })),
    getInsights: async () => ({ insights: [] }),
    getFeed: async () => ({ items: [] }),
    getBattles: async () => ({ battles: [] }),
    getCommentary: async () => ({ items: [] }),
  }
  const load = moduleLoader({
    react: host.react,
    [resolve(root, 'api/client.ts')]: api,
    [resolve(root, 'api/url.ts')]: { apiUrl: (path: string) => path },
    [resolve(root, 'api/dataSource.ts')]: { buildStreamUrl: () => '/stream' },
  }, {
    window: clock, ...clock,
    performance: { now: () => 1000 },
    fetch: async () => ({ ok: true, json: async () => ({ session_id: 'race', start_ms: 0, tick_ms: 1000, drivers: {} }) }),
    EventSource: class {
      onmessage: any
      constructor() { streams.push(this) }
      close() {}
      addEventListener() {}
    },
  })
  const { useReplay } = load(resolve(root, 'features/replay/useReplay.ts'))
  let model: any
  const render = () => { model = host.render(() => useReplay({ kind: 'replay', sessionId: 'race' })) }
  render()
  return {
    requests,
    get model() { render(); return model },
    async settle() { for (let i = 0; i < 20; i++) { await Promise.resolve(); render() } },
    frame(atMs: number) { streams.at(-1).onmessage({ data: JSON.stringify({ at_ms: atMs, session_status: 'started' }) }); render() },
    runTimers() { const pending = [...timers.values()]; timers.clear(); pending.forEach((fn) => fn()); render() },
  }
}

test('Play owns the frame even when the initial snapshot completes later', async () => {
  const rig = replayRig()
  await rig.settle()
  rig.model.play()
  rig.frame(5000)
  rig.requests[0].resolve({ at_ms: 1000, session_status: 'started' })
  await rig.settle()
  assert.equal(rig.model.atMs, 5000)
  assert.equal(rig.model.loading, false)
})

test('a new seek invalidates the old snapshot before its debounce expires', async () => {
  const rig = replayRig()
  await rig.settle()
  rig.model.scrub(9000)
  rig.requests[0].resolve({ at_ms: 1000, session_status: 'started' })
  await rig.settle()
  assert.equal(rig.model.atMs, 9000)
  rig.runTimers()
  rig.requests[1].resolve({ at_ms: 9000, session_status: 'started' })
  await rig.settle()
  assert.equal(rig.model.state.at_ms, 9000)
  assert.equal(rig.model.loading, false)
})

test('Play cancels a debounced scrub instead of loading a snapshot over the stream', async () => {
  const rig = replayRig()
  await rig.settle()
  rig.requests[0].resolve({ at_ms: 1000, session_status: 'started' })
  await rig.settle()
  rig.model.scrub(9000)
  rig.model.play()
  rig.frame(12_000)
  rig.runTimers()
  assert.equal(rig.requests.length, 1)
  assert.equal(rig.model.atMs, 12_000)
})

test('an obsolete initial load cannot clear the newer seek loading state', async () => {
  const rig = replayRig()
  await rig.settle()
  rig.model.scrub(9000)
  rig.runTimers()
  rig.requests[0].resolve({ at_ms: 1000, session_status: 'started' })
  await rig.settle()
  assert.equal(rig.model.loading, true)
  rig.requests[1].resolve({ at_ms: 9000, session_status: 'started' })
  await rig.settle()
  assert.equal(rig.model.loading, false)
})

test('both stream transports preserve selected language and detail', () => {
  const load = moduleLoader({ [resolve(root, 'api/url.ts')]: { apiUrl: (path: string) => path } })
  const { buildStreamUrl } = load(resolve(root, 'api/dataSource.ts'))
  for (const kind of ['replay', 'live']) {
    const url = new URL(buildStreamUrl({ kind, sessionId: 'race' }, 'ru', 'beginner', 5, 1000), 'https://example.test')
    assert.equal(url.searchParams.get('lang'), 'ru', kind)
    assert.equal(url.searchParams.get('level'), 'beginner', kind)
  }
})
