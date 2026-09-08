import type { Lang } from '../features/replay/replayTypes'

// Translate client failures at render time so changing language does not restart
// requests or leave an error captured in the previous language. Never use this
// for Race Control messages or radio transcripts.
const CLIENT_ERRORS: Record<string, string> = {
  'Could not load sessions': 'Не удалось загрузить сессии',
  'Failed to load sessions': 'Не удалось загрузить сессии',
  'Could not load race catalog': 'Не удалось загрузить архив гонок',
  'Could not queue this session': 'Не удалось поставить сессию в очередь',
  'Could not load replay state': 'Не удалось загрузить состояние повтора',
  'Could not load replay timeline': 'Не удалось загрузить шкалу повтора',
  'Feed unavailable': 'Лента недоступна',
  'Stream sent invalid data': 'Поток прислал некорректные данные',
  'Stream disconnected · reconnecting': 'Соединение прервано · переподключение',
  'Failed to fetch': 'Не удалось подключиться к серверу',
  'NetworkError when attempting to fetch resource.': 'Не удалось подключиться к серверу',
  'Failed to start live session': 'Не удалось запустить прямой эфир',
  'Failed to stop live session': 'Не удалось остановить прямой эфир',
  'Enter a country / event name': 'Введите страну или название этапа',
  'Enter the Grand Prix name, e.g. Silverstone': 'Введите название Гран-при, например Silverstone',
  'This public demo cannot build new archives yet. Run the recorder locally or enable worker storage.': 'Подготовка новых архивов в этой демоверсии недоступна.',
}

export function viewerError(message: string, lang: Lang): string {
  if (lang === 'en') return message
  if (CLIENT_ERRORS[message]) return CLIENT_ERRORS[message]
  const status = /^(\d{3})\b/.exec(message)?.[1]
  if (status) return `Запрос не выполнен · HTTP ${status}`
  return 'Не удалось получить данные'
}
