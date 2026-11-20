#!/usr/bin/env python3
"""Detect missed scheduled runs of the Telegram publisher and alert the admin."""
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

ADMIN_CHAT_ID = "1234960363"
REPOSITORY = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GH_TOKEN = os.environ["GH_TOKEN"]
DATABASE_URL = os.environ["NEON_DATABASE_URL"]
STALE_AFTER = timedelta(minutes=20)
ALERT_INTERVAL = timedelta(minutes=30)


def utcnow():
    return datetime.now(timezone.utc)


def latest_scheduled_run():
    url = (
        f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/"
        "telegram-publisher-scheduler.yml/runs?event=schedule&per_page=1"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GH_TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PoltavaLight-WorkflowWatchdog/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))
    runs = data.get("workflow_runs", [])
    if not runs:
        return None
    return runs[0]


def send_admin(text):
    payload = json.dumps({
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def main():
    now = utcnow()
    run = latest_scheduled_run()
    last_run_at = None
    if run:
        last_run_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    age = now - last_run_at if last_run_at else None
    stale = age is None or age > STALE_AFTER
    status = "stale" if stale else "healthy"

    with psycopg.connect(DATABASE_URL, connect_timeout=8) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_watchdog_state (
                watchdog_key TEXT PRIMARY KEY,
                last_status TEXT NOT NULL,
                last_alert_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        row = conn.execute(
            "SELECT last_status, last_alert_at FROM bot_watchdog_state WHERE watchdog_key = %s",
            ("telegram-publisher-schedule",),
        ).fetchone()
        previous_status, last_alert_at = row if row else (None, None)

        should_alert = False
        if stale:
            should_alert = (
                previous_status != "stale"
                or last_alert_at is None
                or now - last_alert_at >= ALERT_INTERVAL
            )
            if should_alert:
                age_text = f"{int(age.total_seconds() // 60)} хв" if age else "невідомо"
                run_text = (
                    f'<a href="{run["html_url"]}">Останній запуск</a>'
                    if run else "Запусків із подією schedule ще немає"
                )
                send_admin(
                    "🚨 <b>Моніторинг автоматичних запусків</b>\n\n"
                    "Основний workflow графіків не запускався за розкладом понад 20 хвилин.\n"
                    f"Час від останнього запуску: <b>{age_text}</b>.\n"
                    f"{run_text}\n\n"
                    "Перевірте GitHub Actions."
                )
        elif previous_status == "stale":
            send_admin(
                "🟢 <b>Автоматичні запуски відновилися</b>\n\n"
                f"Остання перевірка графіків: {last_run_at:%d.%m.%Y %H:%M UTC}."
            )

        conn.execute("""
            INSERT INTO bot_watchdog_state (watchdog_key, last_status, last_alert_at, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (watchdog_key) DO UPDATE SET
                last_status = EXCLUDED.last_status,
                last_alert_at = COALESCE(EXCLUDED.last_alert_at, bot_watchdog_state.last_alert_at),
                updated_at = NOW()
        """, (
            "telegram-publisher-schedule",
            status,
            now if stale and should_alert else None,
        ))

    if stale:
        print("WATCHDOG: publisher schedule is stale")
    else:
        print(f"WATCHDOG: latest scheduled publisher run was {int(age.total_seconds() // 60)} minutes ago")


if __name__ == "__main__":
    main()
