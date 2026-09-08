import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

// Same lightweight hook-host convention as check-replay-requests: run the
// production component/effects, controlling only React scheduling and timers.
function mapRig() {
  const slots: any[] = []
  let cursor = 0
  let dirty = true
  let mounted = true
  let effects: (() => void)[] = []
  const same = (a: unknown[] | undefined, b: unknown[]) => a?.length === b.length && a.every((v, i) => Object.is(v, b[i]))
  const memo = (fn: () => any, deps: unknown[]) => {
    const i = cursor++
    if (!same(slots[i]?.deps, deps)) slots[i] = { deps, value: fn() }
    return slots[i].value
  }
  const react = {
    memo: (component: any) => component,
    createElement: (type: any, props: any, ...children: any[]) => ({ type, props, children }),
    useState(initial: any) {
      const i = cursor++
      if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial
      return [slots[i], (value: any) => {
        assert.ok(mounted, 'obsolete timer must not set state after unmount')
        const next = typeof value === 'function' ? value(slots[i]) : value
        if (!Object.is(slots[i], next)) { slots[i] = next; dirty = true }
      }]
    },
    useRef: (current: any) => memo(() => ({ current }), []),
    useEffect(fn: () => any, deps: unknown[]) {
      const i = cursor++
      if (same(slots[i]?.deps, deps)) return
      const previous = slots[i]
      slots[i] = { deps }
      effects.push(() => { previous?.cleanup?.(); slots[i].cleanup = fn() })
    },
  }
  const timers = new Map<number, { fn: () => void; ms: number }>()
  let timerId = 0
  const module = { exports: {} as any }
  const js = ts.transpileModule(readFileSync(new URL('../src/features/replay/TrackMap.tsx', import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText
  const imports: Record<string, any> = {
    react: { ...react, default: react },
    '../../api/client': { getTrack: () => new Promise(() => {}), getLiveTrack: () => new Promise(() => {}) },
    '../../lib/battles': { battlePair: () => [] },
    '../../lib/trackGeometry': {},
    '../../lib/trackInterpolation': { hasFinishedRace: () => false },
    '../../lib/liveTrack': {},
    './teamColors': { teamColor: () => '#fff' },
    './useTrackAnimation': { useTrackAnimation: () => ({ pathRef: {}, registerCar: () => null }) },
  }
  new Function('require', 'module', 'exports', 'setTimeout', 'clearTimeout', js)(
    (id: string) => { assert.ok(id in imports, `unexpected import ${id}`); return imports[id] },
    module, module.exports,
    (fn: () => void, ms: number) => {
      const id = ++timerId
      timers.set(id, { fn: () => { timers.delete(id); fn() }, ms })
      return id
    },
    (id: number) => timers.delete(id),
  )
  let props = { sessionId: 'race', atMs: 0, live: false, playing: true, playbackSpeed: 1,
    drivers: { VER: {} }, classification: ['VER'], positionsData: null, recentPasses: [] as any[] }
  const rings = (node: any): number => !node || typeof node !== 'object' ? 0
    : Array.isArray(node) ? node.reduce((n, child) => n + rings(child), 0)
    : (node.props?.className === 'overtake-ring' ? 1 : 0) + rings(node.children)
  const frame = (next: Partial<typeof props> = {}) => {
    props = { ...props, ...next }
    let tree
    do {
      cursor = 0
      dirty = false
      tree = module.exports.TrackMap(props)
      const pending = effects
      effects = []
      pending.forEach((fn) => fn())
    } while (dirty)
    return rings(tree)
  }
  return { frame, timers, unmount() { slots.forEach((slot) => slot?.cleanup?.()); mounted = false } }
}

const passes = [{ ahead: 'VER', behind: 'HAM', at_ms: 10_000 }]
const rig = mapRig()
assert.equal(rig.frame({ atMs: 10_000, recentPasses: passes }), 1, 'pass A flashes')
const [firstId, firstTimer] = [...rig.timers][0]
assert.equal(firstTimer.ms, 3000)
assert.equal(rig.frame({ atMs: 11_000 }), 1)
assert.deepEqual([...rig.timers.keys()], [firstId], 'forward frames must not restart A')
assert.equal(rig.frame({ atMs: 5000 }), 0, 'rewind clears A with the exact same passes reference')
assert.equal(rig.timers.size, 0, 'rewind cancels old timers and ignores future passes')
assert.equal(rig.frame({ atMs: 10_000 }), 1, 'A flashes again on the new playback epoch')
const [secondId, secondTimer] = [...rig.timers][0]
assert.notEqual(secondId, firstId)
firstTimer.fn()
assert.equal(rig.frame(), 1, 'obsolete callback cannot clear the new A flash')
assert.deepEqual([...rig.timers.keys()], [secondId])
secondTimer.fn()
assert.equal(rig.frame({ atMs: 11_000 }), 0, 'flash still expires after three seconds')
assert.equal(rig.timers.size, 0)
assert.equal(rig.frame({ atMs: 12_000, recentPasses: [...passes] }), 0, 'new forward arrays also deduplicate A')
assert.equal(rig.frame({ sessionId: 'other', atMs: 10_000 }), 1, 'session change resets pass ownership')
const pendingUnmount = [...rig.timers.values()][0]
rig.unmount()
assert.equal(rig.timers.size, 0)
pendingUnmount.fn()

const live = mapRig()
assert.equal(live.frame({ live: true, atMs: 10_000, recentPasses: passes }), 1)
const [liveId, liveTimer] = [...live.timers][0]
assert.equal(live.frame({ atMs: 9900 }), 1, 'Live correction is not a Replay rewind')
assert.deepEqual([...live.timers.keys()], [liveId])
liveTimer.fn()
assert.equal(live.frame({ atMs: 10_000, recentPasses: [...passes] }), 0, 'Live corrections never replay seen A')
live.unmount()
console.log('production TrackMap rewind/flash ownership checks passed')
