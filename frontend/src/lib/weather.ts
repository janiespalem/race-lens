import type { Lang } from '../features/replay/replayTypes'
import type { WeatherState } from '../api/types'

export function formatWeather(weather?: WeatherState | null, lang: Lang = 'en'): string | null {
  if (!weather) return null
  const parts: string[] = []
  if (weather.rainfall != null) parts.push(lang === 'ru' ? (weather.rainfall ? 'ДОЖДЬ' : 'СУХО') : (weather.rainfall ? 'RAIN' : 'DRY'))
  if (weather.track_temp_c != null) parts.push(`${lang === 'ru' ? 'ТРАССА' : 'TRACK'} ${weather.track_temp_c.toFixed(1)}°`)
  if (weather.air_temp_c != null) parts.push(`${lang === 'ru' ? 'ВОЗДУХ' : 'AIR'} ${weather.air_temp_c.toFixed(1)}°`)
  return parts.length ? parts.join(' · ') : null
}
