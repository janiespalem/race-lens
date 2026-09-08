import assert from 'node:assert/strict'

import { hasFinishedRace, lastKnownFrame, progressPathPosition, resolveTrackPosition, sectorBoundaryMarkers } from '../src/lib/trackInterpolation.ts'
import { isRetryableLiveTrackError, isRetryablePositionsError } from '../src/lib/liveTrack.ts'

assert.equal(isRetryableLiveTrackError(new Error('404 Track data is not ready')), true)
assert.equal(isRetryableLiveTrackError(new Error('503 Storage unavailable')), true)
assert.equal(isRetryableLiveTrackError(new TypeError('Failed to fetch')), true)
assert.equal(isRetryableLiveTrackError(new Error('500 Backend unavailable')), false)
assert.equal(isRetryableLiveTrackError('404 Track data is not ready'), false)
assert.equal(isRetryablePositionsError(new TypeError('Failed to fetch')), true)
assert.equal(isRetryablePositionsError(new Error('503 Positions unavailable')), true)
assert.equal(isRetryablePositionsError(new SyntaxError('Unexpected token')), false)
assert.equal(isRetryablePositionsError(new Error('404 Positions unavailable')), false)

assert.equal(hasFinishedRace(70, 70), true)
assert.equal(hasFinishedRace(69, 70), false)
assert.equal(hasFinishedRace(70, null), false)

const square: [number, number][] = [[0, 0], [10, 0], [10, 10], [0, 10]]

assert.deepEqual(sectorBoundaryMarkers(square, [
  { sector: 1, progress: 0.25 }, { sector: 2, progress: 0.625 },
]), [
  { sector: 1, position: [10, 0] },
  { sector: 2, position: [5, 10] },
  { sector: 3, position: [0, 0] },
])
assert.deepEqual(sectorBoundaryMarkers(square, undefined), [])
assert.deepEqual(sectorBoundaryMarkers(undefined, [{ sector: 1, progress: 0.2 }]), [])
assert.deepEqual(sectorBoundaryMarkers(square, [
  { sector: 1, progress: 0.7 }, { sector: 2, progress: 0.2 },
]), [])
assert.deepEqual(sectorBoundaryMarkers(square, [
  { sector: 1, progress: Number.NaN }, { sector: 2, progress: 0.7 },
]), [])

const midpoint = progressPathPosition([0.1, 0.2], square, 0.5)
assert(midpoint)
assert(Math.abs(midpoint[0] - 6) < 1e-9)
assert.equal(midpoint[1], 0)
assert.deepEqual(progressPathPosition([0.95, 1.05], square, 0.5), [0, 0])
assert.equal(progressPathPosition([null], square, 0), null)
assert.equal(progressPathPosition([0.1, 0.5], square, 0.5), null)
assert.equal(progressPathPosition([0.5, 0.4], square, 0.5), null)
assert.equal(lastKnownFrame([0.1, null, 0.3, null]), 2)
assert.equal(lastKnownFrame([null, null]), null)

const line = Array.from({ length: 100 }, (_, index) => [index, 0] as [number, number])
const uneven = [0, 0.01, 0.04, 0.05, 0.06]
const samples = Array.from(
  { length: 61 },
  (_, index) => progressPathPosition(uneven, line, 1 + index / 30)?.[0],
)
assert(samples.every((sample) => sample !== undefined))
assert(samples.every((sample, index) => index === 0 || sample! >= samples[index - 1]!))
const leftVelocity = samples[30]! - samples[29]!
const rightVelocity = samples[31]! - samples[30]!
assert(Math.abs(leftVelocity - rightVelocity) < 0.002)

assert.deepEqual(resolveTrackPosition(
  null,
  [3, 3],
), [3, 3], 'XY telemetry keeps the car visible when derived progress ends early')

console.log('track interpolation check passed')
