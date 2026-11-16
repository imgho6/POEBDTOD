import { mkdir, rm, writeFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

const OUTPUT_DIR = 'public/data/schedules'
const UPSTREAM_SOURCE = 'https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Poltavaoblenergo.json'

export function kyivDate(offset = 0) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Kyiv',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]))
  const date = new Date(Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day) + offset))
  return date.toISOString().slice(0, 10)
}

export function expandHour(value, previousValue) {
  switch (value) {
    case 'yes': return ['on', 'on']
    case 'no': return ['off', 'off']
    case 'first': return ['off', 'on']
    case 'second': return ['on', 'off']
    case 'mfirst': return ['switching', 'on']
    case 'msecond': {
      const previousSecond = previousValue ? expandHour(previousValue)[1] : 'on'
      return [previousSecond === 'off' ? 'off' : 'on', 'switching']
    }
    default: throw new Error('Невідомий стан у джерелі графіків: ' + value)
  }
}

export function convertSchedulePayload(payload, date) {
  const sourceData = payload?.fact?.data
  const timestamp = sourceData && Object.keys(sourceData).find((value) => {
    const sourceDate = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Kyiv' })
      .format(new Date(Number(value) * 1000))
    return sourceDate === date
  })

  if (!timestamp) {
    throw new Error('Джерело Полтавської області не містить графіка на ' + date)
  }

  const sourceQueues = sourceData[timestamp]
  if (!sourceQueues || typeof sourceQueues !== 'object') {
    throw new Error('У джерелі немає даних графіка на ' + date)
  }

  const queues = {}
  for (let group = 1; group <= 6; group += 1) {
    for (let sub = 1; sub <= 2; sub += 1) {
      const name = group + '.' + sub
      const source = sourceQueues['GPV' + name]
      if (!source || typeof source !== 'object') {
        throw new Error('У джерелі відсутня підчерга ' + name + ' на ' + date)
      }
      const slots = []
      for (let hour = 1; hour <= 24; hour += 1) {
        slots.push(...expandHour(source[String(hour)], hour > 1 ? source[String(hour - 1)] : undefined))
      }
      if (slots.length !== 48) throw new Error('Неповна підчерга ' + name + ' на ' + date)
      queues[name] = slots
    }
  }

  return {
    date,
    fetchedAt: payload.lastUpdated || new Date().toISOString(),
    queues,
    source: 'OE_OUTAGE_DATA',
  }
}

// Залишаємо стару назву експорту для сумісності з наявними тестами та імпортами.
export function convertTodayPayload(payload, date) {
  return convertSchedulePayload(payload, date)
}

export function validateTomorrowPayload(payload, date) {
  if (payload?.date !== date || !payload?.queues) {
    throw new Error('API Світло.ua не повернуло графік на ' + date)
  }
  const expected = Array.from({ length: 6 }, (_, index) => [String(index + 1) + '.1', String(index + 1) + '.2']).flat()
  for (const name of expected) {
    const slots = payload.queues[name]
    if (!Array.isArray(slots) || slots.length !== 48 || slots.some(value => !['on', 'off', 'switching'].includes(value))) {
      throw new Error('Неповна або некоректна підчерга ' + name + ' для ' + date)
    }
  }
  return { date, fetchedAt: payload.fetchedAt || new Date().toISOString(), queues: payload.queues, source: 'svitlo-ua.vercel.app' }
}

export async function fetchJson(url, label) {
  // Bypass stale raw.githubusercontent.com CDN responses after an upstream schedule update.
  const freshUrl = new URL(url)
  freshUrl.searchParams.set('_cacheBust', Date.now().toString())
  const response = await fetch(freshUrl, {
    headers: {
      'User-Agent': 'Mozilla/5.0 svitlo-ua Telegram schedule monitor',
      'Cache-Control': 'no-cache, no-store, max-age=0',
      Pragma: 'no-cache',
    },
    signal: AbortSignal.timeout(20000),
    cache: 'no-store',
  })
  if (!response.ok) throw new Error(label + ': HTTP ' + response.status)
  return response.json()
}

export async function saveSchedule(data) {
  await mkdir(OUTPUT_DIR, { recursive: true })
  await writeFile(OUTPUT_DIR + '/' + data.date + '.json', JSON.stringify(data, null, 2))
  console.log('Графік ' + data.date + ' отримано з ' + data.source)
}

async function removeStaleSchedule(date) {
  await rm(OUTPUT_DIR + '/' + date + '.json', { force: true })
  console.log('Видалено застарілий графік ' + date + ', оскільки джерело не підтвердило його актуальність')
}

export async function main() {
  const dates = [kyivDate(0), kyivDate(1)]
  let payload

  try {
    // One upstream payload contains both today's and tomorrow's schedules.
    // Do not query our own /api/schedule endpoint here: it depends on a
    // separate source and can return 502 even when OE_OUTAGE_DATA is current.
    payload = await fetchJson(UPSTREAM_SOURCE, 'Джерело графіків Полтавщини')
  } catch (error) {
    console.error('Не вдалося отримати джерело графіків Полтавщини:', error)
    process.exitCode = 1
    return
  }

  let updatedCount = 0
  for (const date of dates) {
    try {
      await saveSchedule(convertSchedulePayload(payload, date))
      updatedCount += 1
    } catch (error) {
      console.log('Графік на ' + date + ' поки не опублікований або недоступний: ' + error.message)
      await removeStaleSchedule(date)
    }
  }

  if (updatedCount === 0) process.exitCode = 1
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main()
}
