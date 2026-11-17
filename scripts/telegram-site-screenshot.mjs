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
    // Чекаємо саме на змонтований графік, а не на оболонку з двома кнопками.
    await page.waitForFunction(() => {
      const body = document.body?.innerText?.trim() ?? ''
      const html = document.body?.innerHTML ?? ''
      const hasTemplatePlaceholders = /\\{\\{\\s*[^}]+\\}\\}/.test(html)
      const hasChartContent = body.length > 250 || document.querySelectorAll('canvas, svg, table').length > 0
      return !hasTemplatePlaceholders && hasChartContent
    }, { timeout: 60000 }).catch(() => {})

    const diagnostics = await page.evaluate(() => ({
      text: (document.body?.innerText ?? '').trim(),
      htmlLength: document.documentElement?.outerHTML.length ?? 0,
      scriptSources: [...document.scripts].map((script) => script.src || '[inline]'),
      failedTemplate: /\\{\\{\\s*[^}]+\\}\\}/.test(document.body?.innerHTML ?? ''),
      canvasCount: document.querySelectorAll('canvas').length,
      svgCount: document.querySelectorAll('svg').length,
      tableCount: document.querySelectorAll('table').length,
      bodyHtml: (document.body?.innerHTML ?? '').slice(0, 2500),
    }))
    const visibleText = diagnostics.text
    console.log('Діагностика сторінки:', JSON.stringify(diagnostics))
    if (visibleText.length < 200 && diagnostics.canvasCount + diagnostics.svgCount + diagnostics.tableCount === 0) {
      throw new Error('Графік не завантажився: сторінка містить лише оболонку без даних. Скриншот не надсилатиму.')
    }

    // На сайті є перемикачі «Завтра» та «Сьогодні». Для оперативного
    // оновлення каналу беремо саме поточну добу.
    const today = page.getByText('Сьогодні', { exact: true }).first()
    if (await today.count()) await today.click({ timeout: 5000 }).catch(() => {})
    await page.waitForTimeout(3000)

    const finalText = (await page.locator('body').innerText()).trim()
    if (finalText.length < 200) {
      throw new Error('Після перемикання на «Сьогодні» графік не відобразився. Скриншот не надсилатиму.')
    }
    console.log('Готовий графік:', finalText.length, 'символів тексту.')

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
