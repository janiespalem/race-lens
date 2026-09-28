import assert from 'node:assert/strict'

import { formatLocalFlags, finishSummary, localYellowSummary } from '../src/lib/raceControl.ts'

const sectors = { '20': 'yellow', '15': 'double_yellow', '14': 'yellow' } as const
assert.equal(formatLocalFlags(sectors), 'S14 · DOUBLE S15 · S20')
assert.equal(localYellowSummary(sectors), 'LOCAL YELLOW · S14 · DOUBLE S15 · S20')
assert.equal(formatLocalFlags(sectors, 'ru'), 'S14 · ДВОЙНОЙ S15 · S20')
assert.equal(localYellowSummary(sectors, 'ru'), 'ЛОКАЛЬНЫЙ ЖЁЛТЫЙ · S14 · ДВОЙНОЙ S15 · S20')
assert.equal(finishSummary(null), 'CHEQUERED FLAG')
assert.equal(finishSummary(null, 'ru'), 'КЛЕТЧАТЫЙ ФЛАГ')
assert.equal(finishSummary({ at_ms: 100, control_mode: 'green', sector_flags: sectors }),
  'CHEQUERED FLAG · FINISHED UNDER LOCAL YELLOW · S14 · DOUBLE S15 · S20')
assert.equal(finishSummary({ at_ms: 100, control_mode: 'safety_car', sector_flags: {} }),
  'CHEQUERED FLAG · FINISHED UNDER SAFETY CAR')
assert.equal(finishSummary({ at_ms: 100, control_mode: 'vsc', sector_flags: {} }),
  'CHEQUERED FLAG · FINISHED UNDER VSC')
assert.equal(finishSummary({ at_ms: 100, control_mode: 'red_flag', sector_flags: {} }),
  'CHEQUERED FLAG · FINISHED UNDER RED FLAG')
assert.equal(finishSummary({ at_ms: 100, control_mode: 'green', sector_flags: sectors }, 'ru'),
  'КЛЕТЧАТЫЙ ФЛАГ · ФИНИШ ПОД ЛОКАЛЬНЫМ ЖЁЛТЫМ · S14 · ДВОЙНОЙ S15 · S20')
assert.equal(finishSummary({ at_ms: 100, control_mode: 'safety_car', sector_flags: {} }, 'ru'),
  'КЛЕТЧАТЫЙ ФЛАГ · ФИНИШ ПОД МАШИНОЙ БЕЗОПАСНОСТИ')
assert.equal(finishSummary({ at_ms: 100, control_mode: 'red_flag', sector_flags: {} }, 'ru'),
  'КЛЕТЧАТЫЙ ФЛАГ · ФИНИШ ПОД КРАСНЫМ ФЛАГОМ')
assert.equal(localYellowSummary({}), null)

console.log('race-control presentation checks passed')
