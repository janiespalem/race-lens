import assert from 'node:assert/strict'

import { formatWeather, formatWeatherAge, weatherAgeMs } from '../src/lib/weather.ts'

assert.equal(formatWeather(null), null)
assert.equal(formatWeather({ rainfall: true, track_temp_c: 32.9, air_temp_c: 18.7 }), 'RAIN · TRACK 32.9° · AIR 18.7°')
assert.equal(formatWeather({ rainfall: false, air_temp_c: 18.7 }), 'DRY · AIR 18.7°')

const weather = { air_temp_c: 18.7, rainfall: false, track_temp_c: 32.9 }
// Staleness is the WORST displayed field: fresh air must not hide stale
// track/rain that the source stopped re-reporting 20 minutes ago.
const mixed = { air_temp_c: 110_000, rainfall: 10_000, track_temp_c: 10_000 }
assert.equal(weatherAgeMs(weather, mixed, 120_000), 110_000)
assert.equal(formatWeatherAge(weather, mixed, 120_000), '1m')
assert.equal(formatWeatherAge(weather, mixed, 10_000 + 300_000 + 60_000, 'ru'), 'УСТАРЕЛО · 6м')
// Future-stamped values clamp to zero, never negative.
assert.equal(weatherAgeMs(weather, mixed, 5_000), 0)
// All-fresh source: worst age is small.
assert.equal(formatWeatherAge(weather, { air_temp_c: 115_000, rainfall: 115_000, track_temp_c: 110_000 }, 120_000), '0m')
// Unknown observation times (old archives) or no weather → no badge.
assert.equal(weatherAgeMs(weather, undefined, 120_000), null)
assert.equal(weatherAgeMs(null, mixed, 120_000), null)
assert.equal(weatherAgeMs(weather, {}, 120_000), null)
// Invisible fields (humidity etc.) never drive the displayed freshness.
assert.equal(
  weatherAgeMs(
    { air_temp_c: 18.7, humidity_percent: 56.2 },
    { air_temp_c: 110_000, humidity_percent: 10_000 },
    120_000,
  ),
  10_000,
)

console.log('weather presentation checks passed')
