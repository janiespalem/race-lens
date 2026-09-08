import React, { useEffect, useMemo, useRef, useState } from 'react'
import { getOvertake } from '../../api/client'
import type { CommentaryItem, Insight } from '../../api/types'
import { focusDriverIds } from '../../lib/insightFocus'
import { evidenceData } from '../../lib/insightPresentation'
import { NEUTRAL_STATUSES, writePersisted } from './replayTypes'

type Lang = 'en' | 'ru'

type Props = {
  lang?: Lang
  insights: Insight[]
  commentary: CommentaryItem[]
  selectedIds?: string[]
  sessionStatus?: string
  /** Session ID for overtake % endpoint (replay only). */
  sessionId?: string | null
  atMs?: number
  onFocusDrivers?: (ids: string[]) => void
}

/** Cache overtake probabilities per replay moment and driver pair. */
const overtakeCache = new Map<string, number>()

const MIN_VISIBLE_MS = 6000
const ABSENT_THRESHOLD = 2

function stableKey(ins: Insight): string {
  const stripped = ins.insight_id.replace(/:\d+$/, '')
  return stripped || `${ins.type}:${ins.driver_ids.join(',')}`
}

const TYPE_LABELS: Array<[string, string]> = [
  ['TRAFFIC_RISK', 'TRAFFIC'],
  ['DRS_TRAIN', 'DRS TRAIN'],
  ['PIT_WINDOW', 'PIT WINDOW'],
  ['UNDERCUT_RISK', 'UNDERCUT'],
  ['DEGRADATION_TREND', 'TYRE DEG'],
  ['CLEAN_AIR_PACE', 'CLEAN AIR'],
  ['BATTLE_DETECTED', 'BATTLE'],
]

const LABELS_RU: Record<string, string> = {
  TRAFFIC: 'ТРАФИК', 'DRS TRAIN': 'DRS-ПОЕЗД', 'PIT WINDOW': 'ПИТ-ОКНО',
  UNDERCUT: 'АНДЕРКАТ', 'TYRE DEG': 'ИЗНОС ШИН', 'CLEAN AIR': 'ЧИСТЫЙ ВОЗДУХ',
  BATTLE: 'БОРЬБА', 'SC PIT WINDOW': 'ПИТ-ОКНО ПРИ SC',
}

function localizedLabel(label: string, lang: Lang): string {
  return lang === 'ru' ? (LABELS_RU[label] ?? 'АНАЛИТИКА') : label
}

function baseLabel(type: string): string {
  const hit = TYPE_LABELS.find(([prefix]) => type.startsWith(prefix))
  return hit ? hit[1] : type.replace(/_/g, ' ')
}

const WTW_HIDDEN_TYPES_KEY = 'racelens_wtw_hidden_types'

function readHiddenTypes(): Set<string> {
  try {
    const raw = localStorage.getItem(WTW_HIDDEN_TYPES_KEY)
    if (!raw) return new Set()
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? new Set(parsed.filter((v) => typeof v === 'string')) : new Set()
  } catch { return new Set() }
}

/** True if the insight's type matches a prefix currently hidden by the user. */
function isHiddenType(type: string, hidden: Set<string>): boolean {
  if (hidden.size === 0) return false
  return TYPE_LABELS.some(([prefix]) => type.startsWith(prefix) && hidden.has(prefix))
}

function insightTitle(insight: Insight, lang: Lang): string {
  return insight.driver_ids.join(' ← ') || localizedLabel(baseLabel(insight.type), lang)
}

function insightSubtitle(insight: Insight, lang: Lang): string {
  const label = localizedLabel(baseLabel(insight.type), lang)
  return insight.severity === 'high' ? `${label} · ${lang === 'ru' ? 'ВАЖНО' : 'HIGH'}` : label
}

const SEVERITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 }

// Importance of each insight type — real action (battles, strategic threats)
// outranks passive analytics (traffic/clean-air/deg), which are the repetitive
// "noise". Ranked BEFORE severity because the noisy types are often flagged
// "high" too, so severity alone can't separate them.
const TYPE_PRIORITY: Array<[string, number]> = [
  ['BATTLE_DETECTED', 0],
  ['UNDERCUT_RISK', 1],
  ['DRS_TRAIN', 2],
  ['PIT_WINDOW', 3],
  ['DEGRADATION_TREND', 5],
  ['CLEAN_AIR_PACE', 6],
  ['TRAFFIC_RISK', 7],
]
function typeRank(type: string): number {
  const hit = TYPE_PRIORITY.find(([prefix]) => type.startsWith(prefix))
  return hit ? hit[1] : 4
}

function severityClass(severity: string): string {
  if (severity === 'high') return 'ins high'
  if (severity === 'medium') return 'ins'
  return 'ins pace'
}

/** Canonical pair key: sorted driver IDs joined. */
function pairKey(ins: Insight): string {
  return [...ins.driver_ids].sort().join(':')
}

/** Group insights by driver pair. Return the best per pair + 'also' list. */
function groupByPair(insights: Insight[]): {
  primary: Insight
  also: string[]
}[] {
  const byPair = new Map<string, Insight[]>()
  for (const ins of insights) {
    const key = pairKey(ins)
    const arr = byPair.get(key) ?? []
    arr.push(ins)
    byPair.set(key, arr)
  }
  const result: { primary: Insight; also: string[] }[] = []
  for (const [, group] of byPair) {
    const [primary, ...rest] = group
    result.push({
      primary,
      also: rest.map((r) => baseLabel(r.type)),
    })
  }
  return result
}

/** Grouped SC_PIT_WINDOW card — shown first under neutralization */
const ScPitWindowCard = React.memo(function ScPitWindowCard({
  drivers,
  lang,
}: {
  drivers: string[]
  lang: Lang
}) {
  return (
    <div className="ins ins-sc-pit">
      <h4>
        {lang === 'ru' ? 'В БОКСЫ СЕЙЧАС' : 'PIT NOW'}
        <small>{lang === 'ru' ? 'ПИТ-ОКНО ПРИ SC' : 'SC PIT WINDOW'}</small>
      </h4>
      <p>
        {drivers.join(', ')} — {lang === 'ru' ? 'пит-стоп с меньшей потерей времени при SC' : 'cheap stop under SC'}
      </p>
    </div>
  )
})

const InsightCard = React.memo(function InsightCard({
  lang,
  ins,
  also,
  text,
  leaving,
  focused,
  overtakePct,
  onFocusDrivers,
}: {
  lang: Lang
  ins: Insight
  also: string[]
  text: string
  leaving: boolean
  focused: boolean
  overtakePct?: number | null
  onFocusDrivers?: (ids: string[]) => void
}) {
  const data = evidenceData(ins, lang)
  const focusIds = focusDriverIds(ins.driver_ids)
  const className = [
    severityClass(ins.severity),
    leaving ? 'ins-leaving' : 'ins-entering',
    focused ? 'ins-focused' : '',
  ].filter(Boolean).join(' ')
  const content = (
    <>
      <h4>
        {insightTitle(ins, lang)}
        <small>{insightSubtitle(ins, lang)}</small>
      </h4>
      {onFocusDrivers && focusIds.length > 0 && (
        <span className="ins-action-label">{lang === 'ru' ? 'ВЫБРАТЬ' : 'FOCUS'} {focusIds.join(' / ')} →</span>
      )}
      {also.length > 0 && (
        <div className="ins-also">{lang === 'ru' ? 'также:' : 'also:'} {also.map((label) => localizedLabel(label, lang)).join(' · ')}</div>
      )}
      {text && <p>{text}</p>}
      {overtakePct !== null && overtakePct !== undefined && (
        <div className="ins-overtake">{lang === 'ru' ? 'ОЦЕНКА АТАКИ:' : 'ATTACK SCORE:'} {Math.round(overtakePct * 100)}</div>
      )}
      {data.length > 0 && (
        <div className="data">
          {data.map((d) => (
            <span key={d.label}>
              {d.label} <b>{d.value}</b>
            </span>
          ))}
        </div>
      )}
    </>
  )
  if (onFocusDrivers && focusIds.length > 0) {
    return (
      <button
        type="button"
        className={`${className} ins-action`}
        onClick={() => onFocusDrivers(focusIds)}
      >
        {content}
      </button>
    )
  }
  return <div className={className}>{content}</div>
})

type CardState = {
  ins: Insight
  also: string[]
  text: string
  appearedAt: number
  absentCount: number
  leaving: boolean
  overtakePct: number | null
}

function isBattleType(type: string): boolean {
  return type.startsWith('BATTLE') || type.startsWith('TRAFFIC')
}

export const InsightPanel = React.memo(function InsightPanel({ lang = 'en', insights, commentary, selectedIds = [], sessionStatus = '', sessionId, atMs = 0, onFocusDrivers }: Props) {
  const isNeutral = NEUTRAL_STATUSES.has(sessionStatus)

  // User-controlled type filters for the "WHAT TO WATCH" panel — read once on
  // mount (lazy initializer), persisted to localStorage on change.
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(() => readHiddenTypes())

  useEffect(() => {
    writePersisted(WTW_HIDDEN_TYPES_KEY, JSON.stringify([...hiddenTypes]))
  }, [hiddenTypes])

  const toggleTypeFilter = (prefix: string) => {
    setHiddenTypes((prev) => {
      const next = new Set(prev)
      if (next.has(prefix)) next.delete(prefix)
      else next.add(prefix)
      return next
    })
  }
  const commentaryMap: Record<string, string> = useMemo(() => {
    const m: Record<string, string> = {}
    for (const c of commentary) {
      if (c.insight_id) {
        const key = c.insight_id.replace(/:\d+$/, '')
        m[key] = c.text
      }
    }
    return m
  }, [commentary])

  // Follow-mode: 1 selected → insight must involve that driver as subject;
  // 2 selected → insight must involve BOTH (head-to-head pair only)
  const isFocused = (ins: Insight) => {
    if (selectedIds.length === 0) return false
    if (selectedIds.length === 1) {
      // "follow" mode: driver must be the primary subject (first id) or sole id
      return ins.driver_ids.includes(selectedIds[0])
    }
    // 2 selected: only highlight if insight involves BOTH selected drivers
    return selectedIds.every((id) => ins.driver_ids.includes(id))
  }

  // Collect all SC_PIT_WINDOW driver IDs (deduplicated) for grouped card
  const scPitDrivers = useMemo(() => {
    if (!isNeutral) return []
    const seen = new Set<string>()
    const drivers: string[] = []
    for (const ins of insights) {
      if (ins.type.startsWith('SC_PIT_WINDOW')) {
        for (const d of ins.driver_ids) {
          if (!seen.has(d)) { seen.add(d); drivers.push(d) }
        }
      }
    }
    return drivers
  }, [insights, isNeutral])

  // Sort, then group by pair, then rank groups (excluding SC_PIT_WINDOW when grouped)
  const incomingGroups = useMemo(() => {
    const scFiltered = isNeutral ? insights.filter((ins) => !ins.type.startsWith('SC_PIT_WINDOW')) : insights
    const filtered = scFiltered.filter((ins) => !isHiddenType(ins.type, hiddenTypes))
    const ranked = [...filtered].sort((a, b) => {
      const af = isFocused(a) ? 0 : 1
      const bf = isFocused(b) ? 0 : 1
      if (af !== bf) return af - bf
      const td = typeRank(a.type) - typeRank(b.type)
      if (td !== 0) return td
      const sd = (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9)
      if (sd !== 0) return sd
      return stableKey(a).localeCompare(stableKey(b))
    })
    const groups = groupByPair(ranked)
    // Re-sort groups by their primary insight's rank
    groups.sort((ga, gb) => {
      const af = isFocused(ga.primary) ? 0 : 1
      const bf = isFocused(gb.primary) ? 0 : 1
      if (af !== bf) return af - bf
      const td = typeRank(ga.primary.type) - typeRank(gb.primary.type)
      if (td !== 0) return td
      return (SEVERITY_ORDER[ga.primary.severity] ?? 9) - (SEVERITY_ORDER[gb.primary.severity] ?? 9)
    })
    return groups.slice(0, 3)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [insights, selectedIds, hiddenTypes])

  // Cards are real React state; the transition below is PURE (no timers, no
  // fetches inside the updater — StrictMode-safe). Side effects (leave timers,
  // overtake fetches) are driven FROM the state in the effect further down.
  const [cards, setCards] = useState<Map<string, CardState>>(new Map())

  useEffect(() => {
    const now = Date.now()
    const incomingKeys = new Set(incomingGroups.map((g) => stableKey(g.primary)))
    setCards((prev) => {
      const next = new Map<string, CardState>()

      // Carry existing cards forward (cloned — never mutate prev state).
      for (const [key, old] of prev) {
        const card = { ...old }
        if (!incomingKeys.has(key)) {
          card.absentCount += 1
          if (card.absentCount >= ABSENT_THRESHOLD && now - card.appearedAt >= MIN_VISIBLE_MS) {
            card.leaving = true
          }
        } else {
          card.absentCount = 0
          card.leaving = false
        }
        card.text = commentaryMap[key] ?? card.text
        next.set(key, card)
      }

      // Add new / refresh present cards.
      for (const group of incomingGroups) {
        const key = stableKey(group.primary)
        const existing = next.get(key)
        if (!existing) {
          const [ahead, behind] = group.primary.driver_ids
          const cached = sessionId && group.primary.driver_ids.length === 2
            ? overtakeCache.get(`${sessionId}:${atMs}:${ahead}:${behind}`) ?? null
            : null
          next.set(key, {
            ins: group.primary,
            also: group.also,
            text: commentaryMap[key] ?? '',
            appearedAt: now,
            absentCount: 0,
            leaving: false,
            overtakePct: cached,
          })
        } else {
          existing.ins = group.primary
          existing.also = group.also
          existing.text = commentaryMap[key] ?? existing.text
        }
      }

      // Current rank owns render and cap order; historical cards trail it.
      const ordered = new Map<string, CardState>()
      for (const group of incomingGroups) {
        const key = stableKey(group.primary)
        const card = next.get(key)
        if (card) ordered.set(key, card)
      }
      for (const [key, card] of next) {
        if (!ordered.has(key)) ordered.set(key, card)
      }

      // Cap visible cards: 1 focused + 2 non-focused. Evicted cards leave
      // (animated) once past their minimum visible time.
      const active = [...ordered.entries()].filter(([, c]) => !c.leaving)
      const keep = new Set([
        ...active.filter(([, c]) => isFocused(c.ins)).slice(0, 1),
        ...active.filter(([, c]) => !isFocused(c.ins)).slice(0, 2),
      ].map(([k]) => k))
      for (const [key, card] of active) {
        if (!keep.has(key) && now - card.appearedAt >= MIN_VISIBLE_MS) {
          card.leaving = true
        }
      }

      return new Map([
        ...[...ordered].filter(([, card]) => !card.leaving),
        ...[...ordered].filter(([, card]) => card.leaving),
      ])
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incomingGroups, commentaryMap])

  // Side effects derived from card state:
  //  - a leaving card gets a delete timer (cancelled if it comes back — the old
  //    ref-based version leaked that timer and blinked returning cards away);
  //  - a new 2-driver battle card without overtake % triggers one fetch.
  const leaveTimersRef = useRef<Map<string, number>>(new Map())
  const overtakeInFlightRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    const timers = leaveTimersRef.current
    for (const [key, card] of cards) {
      if (card.leaving && !timers.has(key)) {
        timers.set(key, window.setTimeout(() => {
          timers.delete(key)
          setCards((prev) => {
            const next = new Map(prev)
            next.delete(key)
            return next
          })
        }, 400))
      } else if (!card.leaving && timers.has(key)) {
        window.clearTimeout(timers.get(key)!)
        timers.delete(key)
      }

      if (
        sessionId && card.overtakePct === null && !card.leaving &&
        isBattleType(card.ins.type) && card.ins.driver_ids.length === 2
      ) {
        const [ahead, behind] = card.ins.driver_ids
        const cacheKey = `${sessionId}:${atMs}:${ahead}:${behind}`
        if (overtakeInFlightRef.current.has(cacheKey)) continue
        overtakeInFlightRef.current.add(cacheKey)
        getOvertake(sessionId, atMs, ahead, behind)
          .then((res) => {
            if (!res.error) {
              overtakeCache.set(cacheKey, res.probability)
              setCards((prev) => {
                const c = prev.get(key)
                if (!c) return prev
                const next = new Map(prev)
                next.set(key, { ...c, overtakePct: res.probability })
                return next
              })
            }
          })
          .catch(() => undefined)
          .finally(() => overtakeInFlightRef.current.delete(cacheKey))
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cards, sessionId])

  // Clear all pending timers on unmount.
  useEffect(() => () => {
    for (const t of leaveTimersRef.current.values()) window.clearTimeout(t)
    leaveTimersRef.current.clear()
  }, [])

  const displayItems = useMemo(() => {
    return [...cards.entries()].map(([key, card]) => ({
      key,
      ins: card.ins,
      also: card.also,
      text: card.text,
      leaving: card.leaving,
      focused: isFocused(card.ins),
      overtakePct: card.overtakePct,
    }))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cards, selectedIds])

  const hasVisible = displayItems.some((item) => !item.leaving)

  return (
    <div className="col col-insights">
      <div className="label">{lang === 'ru' ? 'ЗА ЧЕМ СЛЕДИТЬ' : 'WHAT TO WATCH'}</div>
      <div className="ins-filter">
        {TYPE_LABELS.map(([prefix, label]) => (
          <button
            key={prefix}
            type="button"
            className={`tog${hiddenTypes.has(prefix) ? '' : ' tog-on'}`}
            onClick={() => toggleTypeFilter(prefix)}
            title={lang === 'ru' ? `Показать/скрыть: ${localizedLabel(label, lang)}` : `Toggle ${label} insights`}
          >
            {localizedLabel(label, lang)}
          </button>
        ))}
      </div>
      {scPitDrivers.length > 0 && (
        <ScPitWindowCard lang={lang} drivers={scPitDrivers} />
      )}
      {displayItems.map(({ key, ins, also, text, leaving, focused, overtakePct }) => (
        <InsightCard
          key={key}
          lang={lang}
          ins={ins}
          also={also}
          text={text}
          leaving={leaving}
          focused={focused}
          overtakePct={overtakePct}
          onFocusDrivers={onFocusDrivers}
        />
      ))}
      {!hasVisible && displayItems.length === 0 && (
        <div className="ins pace">
          <h4>
            {lang === 'ru' ? 'Активной аналитики нет' : 'No active insights'}
          </h4>
          <p>{lang === 'ru' ? 'Ожидание данных гонки…' : 'Waiting for race data…'}</p>
        </div>
      )}
    </div>
  )
})
