import assert from 'node:assert/strict'

import { evidenceData } from '../src/lib/insightPresentation.ts'

assert.deepEqual(
  evidenceData({
    evidence: {
      interval_s: 0.84,
      pace_delta_ms: 240,
      attacker_tyre_age_laps: 18,
    },
  }),
  [
    { label: 'INT', value: '0.84s' },
    { label: 'Δ PACE', value: '+0.2/lap' },
    { label: 'TYRES', value: '18 LAPS' },
  ],
)

assert.deepEqual(
  evidenceData({ evidence: { margin_s: 1.26, tyre_age_laps: 22 } }),
  [
    { label: 'MARGIN', value: '+1.3s' },
    { label: 'TYRES', value: '22 LAPS' },
  ],
)

console.log('insight presentation check passed')
