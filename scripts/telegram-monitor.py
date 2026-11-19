#!/usr/bin/env python3
"""Post-run health checks for schedule publishing and editorial Telegram tasks."""
import json
import os
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from bot_db import record_monitor_check, record_error, connection
except ImportError:
    connection = None
    def record_monitor_check(*args, **kwargs): return False
    def record_error(*args, **kwargs): return False

KYIV = ZoneInfo("Europe/Kyiv")
ROOT = Path("public/data/schedules")
STATE_PATH = ROOT / "monitor-state.json"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "@PoltavaLight")


def now():
    return datetime.now(KYIV)


def send_alert(message):
    if not TOKEN:
        print("Моніторинг: немає TELEGRAM_BOT_TOKEN, сповіщення не надіслано")
        return False
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": "⚠️ <b>Моніторинг Світло.ua</b>\n\n" + message,
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
        raise RuntimeError(f"Telegram повернув помилку: {result}")
    return True


def load_state():
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def check_schedule():
    today = now().date().isoformat()
    path = ROOT / f"{today}.json"
    if not path.exists():
        return "error", {"reason": "Немає файлу графіка на поточну дату", "date": today}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return "error", {"reason": f"Не вдалося прочитати JSON графіка: {error}", "date": today}

    queues = data.get("queues")
    expected = [f"{group}.{sub}" for group in range(1, 7) for sub in (1, 2)]
    if data.get("date") != today:
        return "error", {"reason": "Дата графіка не збігається з поточною", "date": today, "fileDate": data.get("date")}
    if not isinstance(queues, dict) or set(queues) != set(expected):
        return "error", {"reason": "У графіку немає всіх 12 підчерг", "date": today}
    for queue in expected:
        values = queues.get(queue)
        if not isinstance(values, list) or len(values) != 48:
            return "error", {"reason": f"Підчерга {queue} не має 48 інтервалів", "date": today}
        if any(value not in ("on", "off", "switching") for value in values):
            return "error", {"reason": f"Підчерга {queue} містить невідомі стани", "date": today}

    fetched = data.get("fetchedAt")
    if fetched:
        try:
            parsed = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=KYIV)
            age_hours = (now() - parsed.astimezone(KYIV)).total_seconds() / 3600
            if age_hours > 24:
                return "warning", {"reason": "Джерело не оновлювало дані понад 24 години", "ageHours": round(age_hours, 1), "date": today}
        except ValueError:
            return "warning", {"reason": "Не вдалося перевірити час оновлення джерела", "fetchedAt": str(fetched)}
    return "ok", {"date": today, "queues": len(expected), "intervalsPerQueue": 48, "fetchedAt": fetched}


def check_site():
    try:
        request = urllib.request.Request(
            "https://svitlo-ua.vercel.app/poltava",
            headers={"User-Agent": "PoltavaLightMonitor/1.0"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(300_000).decode("utf-8", errors="replace")
            if response.status != 200 or "Світло.ua" not in body:
                return "error", {"httpStatus": response.status, "titleFound": "Світло.ua" in body}
            return "ok", {"httpStatus": response.status}
    except Exception as error:
        return "error", {"reason": str(error)[:500]}


def check_database():
    if not os.environ.get("NEON_DATABASE_URL"):
        return "not-configured", {"reason": "NEON_DATABASE_URL ще не додано до GitHub Secrets"}
    if connection is None:
        return "error", {"reason": "Модуль підключення до БД недоступний"}
    try:
        with connection() as conn:
            if conn is None:
                return "error", {"reason": "Не вдалося відкрити з'єднання"}
            conn.execute("SELECT 1")
        return "ok", {"query": "SELECT 1"}
    except Exception as error:
        return "error", {"reason": str(error)[:500]}


def main():
    statuses = {
        "оновлення джерела графіків": os.environ.get("REFRESH_STATUS", "unknown"),
        "публікація змін графіка": os.environ.get("SCHEDULE_PUBLISH_STATUS", "unknown"),
        "редакційний модуль": os.environ.get("EDITORIAL_STATUS", "not-run"),
    }
    issues = []
    for label, status in statuses.items():
        is_problem = status not in ("success", "not-run") and not (
            label == "публікація змін графіка" and status == "skipped" and statuses["оновлення джерела графіків"] != "success"
        )
        record_monitor_check("telegram_bot", label, "error" if is_problem else status, details={"workflowOutcome": status})
        if is_problem:
            issues.append(f"• {label}: {status}")
            record_error("telegram_bot", f"{label}: {status}", {"workflowOutcome": status})

    schedule_status, schedule_details = check_schedule()
    record_monitor_check("schedules", "актуальність графіка Полтавщини", schedule_status, details=schedule_details)
    if schedule_status != "ok":
        reason = str(schedule_details.get("reason", "невідома помилка"))
        issues.append("• Перевірка графіка: " + reason)
        record_error("schedules", reason, schedule_details, "warning" if schedule_status == "warning" else "error")

    site_status, site_details = check_site()
    record_monitor_check("website", "доступність сайту", site_status, details=site_details)
    if site_status != "ok":
        reason = str(site_details.get("reason", "HTTP " + str(site_details.get("httpStatus", "невідомо"))))
        issues.append("• Сайт: " + reason)
        record_error("website", reason, site_details)

    database_status, database_details = check_database()
    record_monitor_check("database", "підключення до PostgreSQL", database_status, details=database_details)
    if database_status == "error":
        reason = str(database_details.get("reason", "невідома помилка"))
        issues.append("• База даних: " + reason)
        record_error("database", reason, database_details)

    if not issues:
        print("Моніторинг: усі перевірки успішні")
        return

    message = "Виявлено проблеми:\n" + "\n".join(issues)
    state = load_state()
    fingerprint = "|".join(issues)
    previous = state.get("lastAlertAt")
    previous_fp = state.get("lastAlertFingerprint")
    should_send = True
    if previous and previous_fp == fingerprint:
        try:
            previous_at = datetime.fromisoformat(previous)
            if previous_at.tzinfo is None:
                previous_at = previous_at.replace(tzinfo=KYIV)
            should_send = now() - previous_at.astimezone(KYIV) >= timedelta(hours=6)
        except ValueError:
            pass

    if should_send:
        try:
            if send_alert(message):
                state["lastAlertAt"] = now().isoformat()
                state["lastAlertFingerprint"] = fingerprint
                save_state(state)
        except Exception as error:
            record_error("monitor", error, {"issues": issues})
            print(f"Не вдалося надіслати сповіщення моніторингу: {error}")
            raise
    else:
        print("Моніторинг: таке саме сповіщення вже надсилалося протягом останніх 6 годин")


if __name__ == "__main__":
    main()
