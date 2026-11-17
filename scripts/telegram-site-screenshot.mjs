#!/usr/bin/env node
import { createHash } from 'node:crypto'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname } from 'node:path'
import { chromium } from 'playwright'

const SITE_URL = 'https://poe-energy.pages.dev/'
const TOKEN = process.env.TELEGRAM_BOT_TOKEN
const CHAT_ID = process.env.TELEGRAM_CHAT_ID || '@PoltavaLight'
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

    const response = await page.goto(SITE_URL, {
      waitUntil: 'domcontentloaded',
      timeout: 60000,
    })
    if (!response?.ok()) {
      throw new Error('Сайт графіків повернув HTTP ' + (response?.status() ?? 'невідомо'))
    }

    await page.getByText('Точно як у Полтаваобленерго', { exact: true }).first().waitFor({
      state: 'visible',
      timeout: 45000,
    })

    // На сайті є перемикачі «Завтра» та «Сьогодні». Для оперативного
    // оновлення каналу беремо саме поточну добу.
    const today = page.getByText('Сьогодні', { exact: true }).first()
    await today.click({ timeout: 5000 }).catch(() => {})
    await page.waitForTimeout(1800)

    // Це скриншот самого сайту, а не перемальована з JSON копія графіка.
    const screenshot = await page.screenshot({
      type: 'jpeg',
      quality: 88,
      fullPage: true,
      animations: 'disabled',
    })

    const digest = createHash('sha256').update(screenshot).digest('hex')
    const state = await loadState()
    if (state.siteScreenshotHash === digest) {
      console.log('Скріншот poe-energy.pages.dev не змінився; повторне повідомлення не потрібне.')
      return
    }

    const form = new FormData()
    form.append('chat_id', CHAT_ID)
    form.append('caption', '⚡ Оновлений графік погодинних відключень')
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
    console.log('Telegram: актуальний скриншот poe-energy.pages.dev успішно надіслано.')
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error('Помилка публікації скриншота сайту:', error)
  process.exitCode = 1
})
