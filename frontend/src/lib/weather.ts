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

/** Age of the newest weather value the source actually re-reported, in ms. */
export function weatherAgeMs(
  weather: WeatherState | null | undefined,
  observed: Record<string, number> | null | undefined,
  atMs: number,
): number | null {
  if (!weather || !observed) return null
  let latest: number | null = null
  for (const key of Object.keys(weather)) {
    const at = observed[key]
    if (typeof at === 'number' && (latest === null || at > latest)) latest = at
  }
  if (latest === null) return null
  return Math.max(0, atMs - latest)
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
