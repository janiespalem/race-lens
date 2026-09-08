import type { Lang } from '../features/replay/replayTypes'
import type { WeatherState } from '../api/types'

/** Weather older than this is presented as stale, not fresh. */
export const WEATHER_STALE_MS = 300_000

export function formatWeather(weather?: WeatherState | null, lang: Lang = 'en'): string | null {
  if (!weather) return null
  const parts: string[] = []
  if (weather.rainfall != null) parts.push(lang === 'ru' ? (weather.rainfall ? 'ДОЖДЬ' : 'СУХО') : (weather.rainfall ? 'RAIN' : 'DRY'))
  if (weather.track_temp_c != null) parts.push(`${lang === 'ru' ? 'ТРАССА' : 'TRACK'} ${weather.track_temp_c.toFixed(1)}°`)
  if (weather.air_temp_c != null) parts.push(`${lang === 'ru' ? 'ВОЗДУХ' : 'AIR'} ${weather.air_temp_c.toFixed(1)}°`)
  return parts.length ? parts.join(' · ') : null
}

/** Fields the UI actually renders; staleness is judged only for these. */
const DISPLAYED_WEATHER_KEYS = ['rainfall', 'track_temp_c', 'air_temp_c'] as const

/** Worst-case age of any displayed weather value the source re-reported, in ms.
 *
 * A fresh air reading never hides a stale track/rain value: the badge reflects
 * the oldest displayed field, not the newest. Unknown observation times (old
 * archives) or missing weather yield null.
 */
export function weatherAgeMs(
  weather: WeatherState | null | undefined,
  observed: Record<string, number> | null | undefined,
  atMs: number,
): number | null {
  if (!weather || !observed) return null
  let worst: number | null = null
  for (const key of DISPLAYED_WEATHER_KEYS) {
    if (weather[key] == null) continue
    const at = observed[key]
    if (typeof at !== 'number') continue
    const age = Math.max(0, atMs - at)
    if (worst === null || age > worst) worst = age
  }
  return worst
}

/** Compact age/staleness badge; null when the source age is unknown. */
export function formatWeatherAge(
  weather: WeatherState | null | undefined,
  observed: Record<string, number> | null | undefined,
  atMs: number,
  lang: Lang = 'en',
): string | null {
  const age = weatherAgeMs(weather, observed, atMs)
  if (age === null) return null
  const minutes = Math.floor(age / 60_000)
  if (age >= WEATHER_STALE_MS) return lang === 'ru' ? `УСТАРЕЛО · ${minutes}м` : `STALE · ${minutes}m`
  return `${minutes}${lang === 'ru' ? 'м' : 'm'}`
}
