#!/usr/bin/env node
import { createHash } from 'node:crypto'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname } from 'node:path'
import { chromium } from 'playwright'

const SITE_URL = 'https://poe-energy.pages.dev/'
const SITE_DATA_PATH = '/customs/newgpv-info.php'
const TOKEN = process.env.TELEGRAM_BOT_TOKEN
const CHAT_ID = process.env.TELEGRAM_CHAT_ID || '@PoltavaLight'
const FORCE_SEND = process.env.TELEGRAM_FORCE_SEND === '1'
const STATE_PATH = process.env.TELEGRAM_STATE_PATH || 'public/data/schedules/telegram-state.json'

if (!TOKEN) throw new Error('TELEGRAM_BOT_TOKEN не задано')

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

    page.on('console', (message) => {
      if (message.type() === 'error') console.log('BROWSER_CONSOLE_ERROR:', message.text())
    })
    page.on('pageerror', (error) => console.log('BROWSER_PAGE_ERROR:', error.message))
    page.on('requestfailed', (request) => {
      console.log('BROWSER_REQUEST_FAILED:', request.url(), request.failure()?.errorText ?? 'unknown')
    })

    // Чекаємо саме на запит, який виконує сам poe-energy.pages.dev.
    // Жодних окремих запитів до обленерго або підміни відповіді тут немає.
    const siteDataResponsePromise = page.waitForResponse((response) => {
      const request = response.request()
      return response.url().includes(SITE_DATA_PATH) && request.method() === 'POST'
    }, { timeout: 90000 })

    const navigationResponse = await page.goto(SITE_URL, {
      waitUntil: 'domcontentloaded',
      timeout: 60000,
    })
    if (!navigationResponse?.ok()) {
      throw new Error('Сайт poe-energy.pages.dev повернув HTTP ' + (navigationResponse?.status() ?? 'невідомо'))
    }

    await page.locator('body').waitFor({ state: 'visible', timeout: 15000 })

    let siteDataResponse
    try {
      siteDataResponse = await siteDataResponsePromise
    } catch {
      throw new Error('Сайт відкрився, але його власний запит графіка не отримав відповіді за 90 секунд.')
    }

    const responseHtml = await siteDataResponse.text()
    console.log('Відповідь запиту самого сайту:', siteDataResponse.status(), 'HTTP,', responseHtml.length, 'символів.')
    if (!siteDataResponse.ok() || !responseHtml.includes('gpvinfodetail')) {
      throw new Error('Сайт не отримав таблицю графіка: відповідь його власного запиту некоректна.')
    }

    // Vue на сайті автоматично вибирає сьогоднішню дату за часовим поясом браузера.
    // Чекаємо, поки його ж код Vue/D3 побудує всі 12 черг.
    await page.waitForFunction(() => document.querySelectorAll('#app svg').length >= 12, {
      timeout: 60000,
    }).catch(() => {})

    const diagnostics = await page.evaluate(() => ({
      text: (document.body?.innerText ?? '').trim(),
      selectedDate: document.querySelector('input[type="date"]')?.value ?? '',
      svgCount: document.querySelectorAll('#app svg').length,
      tableCount: document.querySelectorAll('#app table').length,
    }))
    console.log('Діагностика сторінки:', JSON.stringify(diagnostics))

    if (diagnostics.svgCount < 12 || diagnostics.text.length < 200) {
      throw new Error('poe-energy.pages.dev отримав дані, але не побудував повний графік. Скриншот не надсилатиму.')
    }
    console.log('Графік сайту готовий:', diagnostics.svgCount, 'SVG; дата:', diagnostics.selectedDate)
    await page.waitForTimeout(1500)

    // Надсилаємо саме скриншот готової сторінки poe-energy.pages.dev.
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
      console.log('Скриншот poe-energy.pages.dev не змінився; повторне повідомлення не потрібне.')
      return
    }

    const form = new FormData()
    form.append('chat_id', CHAT_ID)
    form.append('caption', FORCE_SEND ? '🧪 ТЕСТОВЕ ОНОВЛЕННЯ ГРАФІКА · скриншот poe-energy.pages.dev' : '⚡ Оновлений графік погодинних відключень')
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
