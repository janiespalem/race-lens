import assert from 'node:assert/strict'

import { formatWeather, formatWeatherAge, weatherAgeMs } from '../src/lib/weather.ts'

assert.equal(formatWeather(null), null)
assert.equal(formatWeather({ rainfall: true, track_temp_c: 32.9, air_temp_c: 18.7 }), 'RAIN · TRACK 32.9° · AIR 18.7°')
assert.equal(formatWeather({ rainfall: false, air_temp_c: 18.7 }), 'DRY · AIR 18.7°')

const weather = { air_temp_c: 18.7, rainfall: false, track_temp_c: 32.9 }
const observed = { air_temp_c: 10_000, rainfall: 10_000, track_temp_c: 100_000 }
// Freshness follows the newest field the source re-reported.
assert.equal(weatherAgeMs(weather, observed, 120_000), 20_000)
// Future-stamped values clamp to zero, never negative.
assert.equal(weatherAgeMs(weather, observed, 50_000), 0)
// A partially-updated source keeps per-field ages; only observed keys count.
assert.equal(weatherAgeMs({ air_temp_c: 18.7, track_temp_c: 32.9 }, { track_temp_c: 0 }, 90_000), 90_000)
// Unknown observation times (old archives) or no weather → no badge.
assert.equal(weatherAgeMs(weather, undefined, 120_000), null)
assert.equal(weatherAgeMs(null, observed, 120_000), null)
assert.equal(weatherAgeMs(weather, {}, 120_000), null)

assert.equal(formatWeatherAge(weather, observed, 120_000), '0m')
assert.equal(formatWeatherAge(weather, observed, 130_000, 'ru'), '0м')
assert.equal(formatWeatherAge(weather, observed, 100_000 + 300_000 + 60_000), 'STALE · 6m')
assert.equal(formatWeatherAge(weather, undefined, 120_000), null)

console.log('weather presentation checks passed')
