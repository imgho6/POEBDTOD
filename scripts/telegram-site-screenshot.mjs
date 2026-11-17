#!/usr/bin/env node
import { createHash } from 'node:crypto'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname } from 'node:path'
import { chromium } from 'playwright'

const SITE_URL = 'https://poe-energy.pages.dev/'
const TOKEN = process.env.TELEGRAM_BOT_TOKEN
const CHAT_ID = process.env.TELEGRAM_CHAT_ID || '@PoltavaLight'
const FORCE_SEND = process.env.TELEGRAM_FORCE_SEND === '1'
const STATE_PATH = process.env.TELEGRAM_STATE_PATH || 'public/data/schedules/telegram-state.json'
const POE_API_URL = 'https://www.poe.pl.ua/customs/newgpv-info.php'

if (!TOKEN) throw new Error('TELEGRAM_BOT_TOKEN не задано')

function kyivDateParts() {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/Kyiv',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  return Object.fromEntries(parts.map((part) => [part.type, part.value]))
}

function kyivDateInputValue() {
  const { year, month, day } = kyivDateParts()
  return `${year}-${month}-${day}`
}

async function fetchOfficialScheduleHtml() {
  const { year, month, day } = kyivDateParts()
  const form = new FormData()
  form.append('seldate', JSON.stringify({ date_in: `${day}-${month}-${year}` }))

  const response = await fetch(POE_API_URL, {
    method: 'POST',
    headers: {
      Origin: 'https://poe-energy.pages.dev',
      Referer: SITE_URL,
    },
    body: form,
    signal: AbortSignal.timeout(30000),
  })
  const html = await response.text()
  console.log('Офіційний API графіків:', response.status, 'HTTP,', html.length, 'символів.')
  if (!response.ok || !html.includes('gpvinfodetail')) {
    throw new Error('Офіційний API Полтаваобленерго не повернув таблицю графіка.')
  }
  return html
}

async function loadState() {
  try {
    return JSON.parse(await readFile(STATE_PATH, 'utf8'))
  } catch {
    return {}
  }
}

async function main() {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] })
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1800 },
      deviceScaleFactor: 1,
      locale: 'uk-UA',
      timezoneId: 'Europe/Kyiv',
    })

    // The website's own cross-origin POST may stall on CI egress. Fetch the exact
    // official HTML server-side, then let the website's Vue/D3 code render it.
    const officialHtml = await fetchOfficialScheduleHtml()
    await page.route('**/customs/newgpv-info.php', async (route) => {
      await route.fulfill({
        status: 200,
        headers: {
          'content-type': 'text/html; charset=utf-8',
          'access-control-allow-origin': 'https://poe-energy.pages.dev',
          'access-control-allow-methods': 'POST, OPTIONS',
          'access-control-allow-headers': '*',
        },
        body: officialHtml,
      })
    })

    page.on('console', (message) => {
      if (message.type() === 'error') console.log('BROWSER_CONSOLE_ERROR:', message.text())
    })
    page.on('pageerror', (error) => console.log('BROWSER_PAGE_ERROR:', error.message))
    page.on('requestfailed', (request) => {
      console.log('BROWSER_REQUEST_FAILED:', request.url(), request.failure()?.errorText ?? 'unknown')
    })
    page.on('response', (result) => {
      if (result.status() >= 400) console.log('BROWSER_HTTP_ERROR:', result.status(), result.url())
    })

    const response = await page.goto(SITE_URL, {
      waitUntil: 'domcontentloaded',
      timeout: 60000,
    })
    if (!response?.ok()) {
      throw new Error('Сайт графіків повернув HTTP ' + (response?.status() ?? 'невідомо'))
    }

    await page.locator('body').waitFor({ state: 'visible', timeout: 15000 })
    // Сторінка відкривається з порожнім полем дати, тому графік ще не побудований.
    // Явно вибираємо поточну дату за київським часом.
    const todayValue = kyivDateInputValue()
    const dateInput = page.locator('input[type="date"]').first()
    if (!(await dateInput.count())) {
      throw new Error('На сторінці не знайдено поле вибору дати.')
    }
    page.on('request', (request) => {
      if (['xhr', 'fetch'].includes(request.resourceType())) {
        console.log('BROWSER_DATA_REQUEST:', request.method(), request.url())
      }
    })
    page.on('response', async (result) => {
      if (['xhr', 'fetch'].includes(result.request().resourceType())) {
        const body = await result.text().catch(() => '')
        console.log('BROWSER_DATA_RESPONSE:', result.status(), result.url(), body.slice(0, 1000))
      }
    })

    await dateInput.fill(todayValue)
    await dateInput.dispatchEvent('input')
    await dateInput.dispatchEvent('change')
    await dateInput.press('Enter').catch(() => {})
    console.log('Вибрано дату графіка:', todayValue)

    // Чекаємо на SVG-графік або розгорнутий текстовий звіт, а не на прихований canvas для копіювання.
    await page.waitForFunction(() => {
      const body = document.body?.innerText?.trim() ?? ''
      const html = document.body?.innerHTML ?? ''
      const hasTemplatePlaceholders = /\\{\\{\\s*[^}]+\\}\\}/.test(html)
      const hasChartContent = document.querySelectorAll('#app svg').length > 0 ||
        (body.length > 250 && /Текстовий звіт|Графік/.test(body))
      return !hasTemplatePlaceholders && hasChartContent
    }, { timeout: 60000 }).catch(() => {})

    const diagnostics = await page.evaluate(() => ({
      text: (document.body?.innerText ?? '').trim(),
      htmlLength: document.documentElement?.outerHTML.length ?? 0,
      selectedDate: document.querySelector('input[type="date"]')?.value ?? '',
      scriptSources: [...document.scripts].map((script) => script.src || '[inline]'),
      failedTemplate: /\\{\\{\\s*[^}]+\\}\\}/.test(document.body?.innerHTML ?? ''),
      canvasCount: document.querySelectorAll('canvas').length,
      svgCount: document.querySelectorAll('#app svg').length,
      tableCount: document.querySelectorAll('table').length,
      dateInputHtml: document.querySelector('input[type="date"]')?.outerHTML ?? '',
      inlineScripts: [...document.scripts].filter((script) => !script.src).map((script, index) => ({
        index, type: script.type, length: script.textContent.length, text: script.textContent.slice(0, 12000),
      })),
    }))
    const visibleText = diagnostics.text
    console.log('Діагностика графіка:', JSON.stringify(diagnostics))
    if (diagnostics.svgCount === 0 && visibleText.length < 200) {
      throw new Error('Сайт не побудував графік для вибраної дати. Скриншот не надсилатиму.')
    }
    console.log('Графік завантажено:', visibleText.length, 'символів тексту, SVG:', diagnostics.svgCount)
    await page.waitForTimeout(2000)

    // Це скриншот самого сайту, а не перемальована з JSON копія графіка.
    const screenshot = await page.screenshot({
      type: 'jpeg',
      quality: 88,
      fullPage: true,
      animations: 'disabled',
    })

    console.log('Скриншот сформовано:', Math.round(screenshot.length / 1024), 'КБ.')
    const digest = createHash('sha256').update(screenshot).digest('hex')
    const state = await loadState()
    if (!FORCE_SEND && state.siteScreenshotHash === digest) {
      console.log('Скріншот poe-energy.pages.dev не змінився; повторне повідомлення не потрібне.')
      return
    }

    const form = new FormData()
    form.append('chat_id', CHAT_ID)
    form.append('caption', FORCE_SEND ? '🧪 ТЕСТОВЕ ОНОВЛЕННЯ ГРАФІКА · перевірка скриншота сайту' : '⚡ Оновлений графік погодинних відключень')
    form.append('photo', new Blob([screenshot], { type: 'image/jpeg' }), 'poe-energy-schedule.jpg')

    const telegramResponse = await fetch(`https://api.telegram.org/bot${TOKEN}/sendPhoto`, {
      method: 'POST',
      body: form,
      signal: AbortSignal.timeout(60000),
    })
    const result = await telegramResponse.json()
    if (!telegramResponse.ok || !result.ok) {
      throw new Error('Telegram не прийняв скриншот: ' + JSON.stringify(result))
    }

    state.siteScreenshotHash = digest
    state.siteScreenshotSentAt = new Date().toISOString()
    await mkdir(dirname(STATE_PATH), { recursive: true })
    await writeFile(STATE_PATH, JSON.stringify(state, null, 2) + '\n', 'utf8')
    console.log(FORCE_SEND ? 'Telegram: тестовий скриншот poe-energy.pages.dev успішно надіслано.' : 'Telegram: актуальний скриншот poe-energy.pages.dev успішно надіслано.')
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error('Помилка публікації скриншота сайту:', error)
  process.exitCode = 1
})
