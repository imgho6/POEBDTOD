import test from 'node:test'
import assert from 'node:assert/strict'
import {
  convertSchedulePayload,
  convertTodayPayload,
  expandHour,
  validateTomorrowPayload,
} from './update-schedules.mjs'

test('msecond carries a confirmed outage from the previous hour', () => {
  assert.deepEqual(expandHour('msecond', 'no'), ['off', 'switching'])
  assert.deepEqual(expandHour('msecond', 'second'), ['off', 'switching'])
})

test('msecond keeps the first half on when the previous hour ends with power', () => {
  assert.deepEqual(expandHour('msecond', 'yes'), ['on', 'switching'])
  assert.deepEqual(expandHour('msecond', 'first'), ['on', 'switching'])
  assert.deepEqual(expandHour('msecond'), ['on', 'switching'])
})

test('converts all 12 queues into 48 half-hour slots', () => {
  const timestamp = Math.floor(Date.UTC(2026, 10, 11, 12) / 1000)
  const sourceQueues = Object.fromEntries(
    Array.from({ length: 12 }, (_, index) => {
      const queue = `GPV${Math.floor(index / 2) + 1}.${index % 2 + 1}`
      return [queue, Object.fromEntries(
        Array.from({ length: 24 }, (_, hour) => [String(hour + 1), hour === 1 ? 'msecond' : 'no']),
      )]
    }),
  )

  const result = convertTodayPayload({
    fact: { today: timestamp, data: { [String(timestamp)]: sourceQueues } },
    lastUpdated: '2026-11-11T14:00:00+02:00',
  }, '2026-11-11')

  assert.equal(Object.keys(result.queues).length, 12)
  assert.equal(result.queues['1.1'].length, 48)
  assert.deepEqual(result.queues['1.1'].slice(2, 4), ['off', 'switching'])
})

test('converts tomorrow from the second date entry in the same upstream payload', () => {
  const todayTimestamp = Math.floor(Date.UTC(2026, 10, 11, 22) / 1000)
  const tomorrowTimestamp = todayTimestamp + 86400
  const makeQueues = (status) => Object.fromEntries(
    Array.from({ length: 12 }, (_, index) => {
      const queue = `GPV${Math.floor(index / 2) + 1}.${index % 2 + 1}`
      return [queue, Object.fromEntries(
        Array.from({ length: 24 }, (_, hour) => [String(hour + 1), status]),
      )]
    }),
  )

  const payload = {
    fact: {
      today: todayTimestamp,
      data: {
        [String(todayTimestamp)]: makeQueues('no'),
        [String(tomorrowTimestamp)]: makeQueues('yes'),
      },
    },
    lastUpdated: '2026-11-11T22:10:00.000Z',
  }

  const result = convertSchedulePayload(payload, '2026-11-13')
  assert.equal(result.date, '2026-11-13')
  assert.equal(result.queues['1.1'].length, 48)
  assert.deepEqual(result.queues['1.1'], Array(48).fill('on'))
})

test('rejects a tomorrow schedule with a missing or invalid queue', () => {
  const queues = Object.fromEntries(
    Array.from({ length: 12 }, (_, index) => [
      `${Math.floor(index / 2) + 1}.${index % 2 + 1}`,
      Array(48).fill('on'),
    ]),
  )
  assert.throws(() => validateTomorrowPayload({ date: '2026-11-12', queues }, '2026-11-13'))
  delete queues['6.2']
  assert.throws(() => validateTomorrowPayload({ date: '2026-11-13', queues }, '2026-11-13'))
})

test('rejects unknown source statuses instead of silently guessing', () => {
  assert.throws(() => expandHour('unexpected'), /Невідомий стан/)
})
