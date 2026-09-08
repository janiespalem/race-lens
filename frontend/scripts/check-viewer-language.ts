import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../src')
const nodeRequire = createRequire(import.meta.url)
const cache = new Map<string, Record<string, unknown>>()

// Render production components with real React. Transpile the same TS/TSX as
// Vite without adding a browser or test-renderer dependency; effects stay idle.
function load(path: string): Record<string, unknown> {
  if (cache.has(path)) return cache.get(path)!
  const module = { exports: {} }
  cache.set(path, module.exports)
  const source = ts.transpileModule(readFileSync(path, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText
  const require = (id: string) => {
    if (id === 'react-grid-layout') return {
      ...nodeRequire(id),
      __esModule: true,
      useContainerWidth: () => ({ width: 1600, containerRef: { current: null }, mounted: true }),
    }
    if (!id.startsWith('.')) return nodeRequire(id)
    // API calls are not executed during server rendering.
    if (id.endsWith('/api/client')) return {}
    const base = resolve(dirname(path), id)
    return load(existsSync(`${base}.tsx`) ? `${base}.tsx` : `${base}.ts`)
  }
  new Function('require', 'module', 'exports', source)(require, module, module.exports)
  return module.exports
}

function render(name: string, props: Record<string, unknown>): string {
  const Component = load(resolve(root, `features/replay/${name}.tsx`))[name] as Parameters<typeof createElement>[0]
  return renderToStaticMarkup(createElement(Component, props))
}

const deck = {
  timeline: { start_ms: 0, end_ms: 100_000, lights_out_ms: 0, lap_marks: { 1: 20_000, 2: 40_000 } },
  atMs: 30_000, playing: true, speed: 5,
  onScrub() {}, onPlay() {}, onPause() {}, onSpeed() {},
}

test('EN → RU → EN renders shell, controls and selection without changing playback props', () => {
  const baseline = JSON.stringify(deck)
  const outputs = ['en', 'ru', 'en'].map((lang) => {
    const top = render('TopBar', {
      lang, mode: 'replay', sessionId: 'monza_2025_race', lap: 2, totalLaps: 53,
      level: 'pro', desk: 'classic', liveAvailable: true, liveNowAvailable: false,
      projection: false, voice: false, customEditing: false, onCatalogOpen() {},
    })
    const transport = render('ReplayDeck', { ...deck, lang })
    const timing = render('TimingTower', { lang, rows: [{ id: 'NOR', position: 2 }], battles: [], selectedIds: ['NOR'] })
    assert.match(transport, /value="30000"/)
    assert.match(transport, /deck-transport is-playing/)
    assert.match(timing, /aria-pressed="true"/)
    assert.match(top, lang === 'ru' ? /СЛОИ/ : /LAYERS/)
    assert.match(top, lang === 'ru' ? /Гонка/ : /Race/)
    assert.match(transport, lang === 'ru' ? /Приостановить повтор/ : /Pause replay/)
    assert.match(timing, lang === 'ru' ? /ХРОНОМЕТРАЖ/ : /TIMING/)
    return top + transport + timing
  })
  assert.equal(outputs[0], outputs[2])
  assert.equal(JSON.stringify(deck), baseline)
})

test('Live and unknown/loading copy follow lang while official source and transcript stay verbatim', () => {
  const official = 'RACE WILL RESUME AT 15:30'
  const transcript = 'Box now, box now'
  for (const lang of ['en', 'ru']) {
    const lobby = render('LiveLobby', { lang, signalrAvailable: true })
    assert.match(lobby, lang === 'ru' ? /Подключение к хронометражу F1/ : /Connect to F1 live timing/)
    assert.match(lobby, /value="Race"/)
    const liveTop = render('TopBar', { lang, mode: 'live', sessionName: 'SILVERSTONE · RACE', lap: 2, level: 'pro', desk: 'classic' })
    assert.match(liveTop, lang === 'ru' ? /SILVERSTONE · Гонка/ : /SILVERSTONE · RACE/)
    const unknown = render('TimingTower', { lang, rows: [{ id: 'NOR', position: null }], battles: [], selectedIds: [] })
    assert.match(unknown, lang === 'ru' ? /позиция неизвестна/ : /position unknown/)
    const { viewerError } = load(resolve(root, 'lib/viewerError.ts')) as { viewerError: (message: string, lang: string) => string }
    assert.equal(viewerError('Stream disconnected · reconnecting', lang), lang === 'ru' ? 'Соединение прервано · переподключение' : 'Stream disconnected · reconnecting')
    assert.match(render('ReplayDeck', { ...deck, lang, canScrub: false }), lang === 'ru' ? /ПОДКЛЮЧЕНИЕ/ : /CONNECTING/)
    assert.match(render('RaceFeed', { lang, items: [], loading: true }), lang === 'ru' ? /Загрузка/ : /Loading/)
    const feed = render('RaceFeed', { lang, items: [{ id: 'source', kind: 'status', tag: 'FLAG', text: official, at_ms: 30000, lap: 2, transcript }] })
    assert.ok(feed.includes(official))
    assert.ok(feed.includes(transcript))
    assert.match(feed, lang === 'ru' ? /ВОЗМОЖНЫ ОШИБКИ/ : /MAY BE INACCURATE/)
    const status = render('StatusStrip', { lang, status: 'red_flag', restartAnnouncement: official })
    assert.ok(status.includes(official))
    assert.match(status, lang === 'ru' ? /КРАСНЫЙ ФЛАГ/ : /RED FLAG/)
  }
})

test('all App viewer call sites pass the existing lang selection', () => {
  const app = ts.createSourceFile('main.tsx', readFileSync(resolve(root, 'main.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const viewers = new Set(['TimingTower', 'BattleIntelligence', 'TrackMap', 'FocusPanel', 'InsightPanel', 'RaceFeed', 'StintTimeline', 'ForecastStrip', 'SessionCatalog', 'CompanionLink', 'LiveLobby', 'StatusStrip', 'WorkspaceGrid', 'ReplayDeck'])
  let checked = 0
  const visit = (node: ts.Node) => {
    if (ts.isJsxSelfClosingElement(node) && viewers.has(node.tagName.getText(app))) {
      assert.ok(node.attributes.properties.some((attr) => ts.isJsxAttribute(attr) && attr.name.getText(app) === 'lang'), node.tagName.getText(app))
      checked++
    }
    ts.forEachChild(node, visit)
  }
  visit(app)
  assert.ok(checked > 20)
})

test('custom workspace hide controls and settings headings render localized text', () => {
  const { defaultWorkspace } = load(resolve(root, 'features/replay/workspace.ts')) as { defaultWorkspace: (mode: string) => unknown }
  for (const lang of ['en', 'ru']) {
    const workspace = render('WorkspaceGrid', {
      lang, mode: 'replay', workspace: defaultWorkspace('replay'), editing: true,
      widgets: { timing: 'NOR' }, onChange() {}, onDone() {}, onCancel() {}, onReset() {},
    })
    assert.match(workspace, lang === 'ru' ? /aria-label="Скрыть Хронометраж"/ : /aria-label="Hide Timing"/)
    assert.match(workspace, lang === 'ru' ? /title="Скрыть Хронометраж"/ : /title="Hide Timing"/)
    const settings = render('SettingsDrawer', { lang, open: true, mode: 'replay', sessionId: 'race', onSeek() {} })
    assert.match(settings, lang === 'ru' ? /МОМЕНТЫ И DOTD/ : /HIGHLIGHTS &amp; DOTD/)
    assert.ok(!settings.includes('HIGHLIGHTS &amp;amp; DOTD'))
  }
})
