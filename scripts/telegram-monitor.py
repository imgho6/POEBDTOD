#!/usr/bin/env python3
"""Post-run health checks for schedule publishing and editorial Telegram tasks."""
import json
import os
import urllib.error
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
            "https://prosvitlo.pp.ua/poltava",
            headers={"User-Agent": "PoltavaLightMonitor/1.0"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(300_000)
            content_type = response.headers.get("Content-Type", "").lower()
            details = {
                "httpStatus": response.status,
                "url": response.geturl(),
                "contentType": content_type,
            }
            if not 200 <= response.status < 300:
                return "error", {**details, "reason": f"HTTP статус {response.status}"}
            if "text/html" not in content_type:
                return "error", {**details, "reason": "Сайт повернув відповідь не у форматі HTML"}
            if not body.strip():
                return "error", {**details, "reason": "Сайт повернув порожню HTML-сторінку"}
            return "ok", details
    except Exception as error:
        return "error", {"reason": str(error)[:500]}


def check_api():
    today = now().date().isoformat()
    try:
        request = urllib.request.Request(
            f"https://prosvitlo.pp.ua/api/schedule?date={today}",
            headers={"User-Agent": "PoltavaLightMonitor/1.0"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
            queues = data.get("queues")
            expected = [f"{group}.{sub}" for group in range(1, 7) for sub in (1, 2)]
            if response.status != 200:
                return "error", {"reason": f"API HTTP {response.status}"}
            if data.get("date") != today or not isinstance(queues, dict):
                return "error", {"reason": "API повернув графік з неправильною датою або без черг"}
            for queue in expected:
                values = queues.get(queue)
                if not isinstance(values, list) or len(values) != 48 or any(
                    value not in ("on", "off", "switching") for value in values
                ):
                    return "error", {"reason": f"API повернув неповну підчергу {queue}"}
            return "ok", {"httpStatus": response.status, "date": today, "queues": len(expected)}
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "warning", {"reason": "API відповідає, але графік на сьогодні ще не опубліковано", "httpStatus": 404}
        return "error", {"reason": f"API HTTP {error.code}"}
    except Exception as error:
        return "error", {"reason": f"API недоступний: {type(error).__name__}"}


def check_telegram_api():
    if not TOKEN:
        return "not-configured", {"reason": "TELEGRAM_BOT_TOKEN не налаштовано"}
    try:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/getMe",
            headers={"User-Agent": "PoltavaLightMonitor/1.0"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
            if response.status != 200 or not result.get("ok"):
                return "error", {"reason": "Telegram Bot API не підтвердив доступність бота"}
            user = result.get("result", {})
            return "ok", {"botUsername": user.get("username"), "botId": user.get("id")}
    except urllib.error.HTTPError as error:
        return "error", {"reason": f"Telegram Bot API HTTP {error.code}"}
    except Exception as error:
        return "error", {"reason": f"Telegram Bot API недоступний: {type(error).__name__}"}


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
    event_name = os.environ.get("WORKFLOW_EVENT_NAME", "unknown")
    refresh_status = os.environ.get("REFRESH_STATUS", "unknown")
    publish_status = os.environ.get("SCHEDULE_PUBLISH_STATUS", "unknown")
    publish_result = os.environ.get("SCHEDULE_PUBLISH_RESULT", "")
    editorial_status = os.environ.get("EDITORIAL_STATUS", "not-run")
    if event_name == "push" and editorial_status == "skipped":
        editorial_status = "not-run"
    if publish_status == "success" and publish_result == "no-change":
        publish_status = "no-change"
    elif publish_status == "success" and publish_result == "published":
        publish_status = "published"
    elif publish_status == "skipped" and event_name == "push":
        publish_status = "not-run"
    statuses = {
        "оновлення джерела графіків": refresh_status,
        "публікація змін графіка": publish_status,
        "редакційний модуль": editorial_status,
    }
    issues = []
    for label, status in statuses.items():
        is_problem = status not in ("success", "published", "no-change", "not-run")
        record_monitor_check("telegram_bot", label, "error" if is_problem else status, details={"workflowOutcome": status})
        if is_problem and label != "редакційний модуль":
            issues.append(f"• {label}: {status}")
        elif status == "no-change":
            print("Графіки: перевірка успішна, нових змін немає")
        elif status == "published":
            print("Графіки: зміну успішно опубліковано в Telegram")
        elif is_problem:
            print("Редакційний модуль повідомляє про власні помилки окремим повідомленням")
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

    api_status, api_details = check_api()
    record_monitor_check("api", "API графіків", api_status, details=api_details)
    if api_status == "error":
        reason = str(api_details.get("reason", "API недоступний"))
        issues.append("• API: " + reason)
        record_error("api", reason, api_details)
    elif api_status == "warning":
        reason = str(api_details.get("reason", "Графік на сьогодні ще не опубліковано"))
        record_error("api", reason, api_details, "warning")

    telegram_api_status, telegram_api_details = check_telegram_api()
    record_monitor_check("telegram_bot", "доступність Telegram Bot API", telegram_api_status, details=telegram_api_details)
    if telegram_api_status == "error":
        reason = str(telegram_api_details.get("reason", "Telegram Bot API недоступний"))
        issues.append("• Telegram: " + reason)
        record_error("telegram_bot", reason, telegram_api_details)

    database_status, database_details = check_database()
    record_monitor_check("database", "підключення до PostgreSQL", database_status, details=database_details)
    if database_status == "error":
        reason = str(database_details.get("reason", "невідома помилка"))
        issues.append("• База даних: " + reason)
        record_error("database", reason, database_details)

    # Публічний знімок стану для сторінки /status. Секрети та URL із токенами сюди не потрапляють.
    state = load_state()
    checked_at = now().isoformat()
    source_status = "ok" if refresh_status == "success" and schedule_status == "ok" else (
        "warning" if refresh_status == "success" and schedule_status == "warning" else "error"
    )
    source_message = (
        "Графік успішно оновлено та перевірено."
        if source_status == "ok"
        else str(schedule_details.get("reason") or f"Оновлення джерела: {refresh_status}")
    )
    website_message = "Сайт відповідає коректним HTML." if site_status == "ok" else str(site_details.get("reason", "Сайт недоступний"))
    api_message = "API повертає повний графік на поточну дату." if api_status == "ok" else str(api_details.get("reason", "API недоступний"))
    publication_problem = publish_status not in ("success", "published", "no-change", "not-run")
    if telegram_api_status == "not-configured":
        public_telegram_status = "not-configured"
        telegram_message = "Перевірка бота не налаштована: відсутній токен."
    elif telegram_api_status != "ok" or publication_problem:
        public_telegram_status = "error"
        telegram_message = str(telegram_api_details.get("reason") or f"Результат публікації: {publish_status}")
    else:
        public_telegram_status = "ok"
        if publish_status == "published":
            telegram_message = "Бот доступний, повідомлення успішно опубліковано."
        elif publish_status == "no-change":
            telegram_message = "Бот доступний, нових змін для публікації не було."
        elif publish_status == "not-run":
            telegram_message = "Bot API доступний; публікація не запускалася в цьому циклі."
        else:
            telegram_message = "Bot API доступний, останній процес публікації завершився успішно."

    previous_services = state.get("services", {}) if isinstance(state.get("services"), dict) else {}
    services = {
        "scheduleSource": {"status": source_status, "message": source_message, "checkedAt": checked_at,
                           "lastSuccessAt": checked_at if source_status == "ok" else previous_services.get("scheduleSource", {}).get("lastSuccessAt")},
        "website": {"status": site_status, "message": website_message, "checkedAt": checked_at,
                    "lastSuccessAt": checked_at if site_status == "ok" else previous_services.get("website", {}).get("lastSuccessAt")},
        "api": {"status": api_status, "message": api_message, "checkedAt": checked_at,
                "lastSuccessAt": checked_at if api_status == "ok" else previous_services.get("api", {}).get("lastSuccessAt")},
        "telegram": {"status": public_telegram_status, "message": telegram_message, "checkedAt": checked_at,
                     "lastSuccessAt": checked_at if public_telegram_status == "ok" else previous_services.get("telegram", {}).get("lastSuccessAt")},
    }
    last_errors = state.get("lastErrors", [])
    if not isinstance(last_errors, list):
        last_errors = []
    current_errors = [
        {"service": "Джерело графіків", "message": source_message, "at": checked_at} for _ in [0] if source_status != "ok"
    ] + [
        {"service": "Вебсайт", "message": website_message, "at": checked_at} for _ in [0] if site_status != "ok"
    ] + [
        {"service": "API", "message": api_message, "at": checked_at} for _ in [0] if api_status != "ok"
    ] + [
        {"service": "Telegram", "message": telegram_message, "at": checked_at} for _ in [0] if public_telegram_status == "error"
    ]
    for item in current_errors:
        duplicate = any(
            previous.get("service") == item["service"] and previous.get("message") == item["message"]
            for previous in last_errors[-5:] if isinstance(previous, dict)
        )
        if not duplicate:
            last_errors.append(item)
    state.update({
        "checkedAt": checked_at,
        "services": services,
        "lastErrors": last_errors[-20:],
        "lastSuccessfulScheduleCheckAt": checked_at if source_status == "ok" else state.get("lastSuccessfulScheduleCheckAt"),
        "lastSuccessfulScheduleDataAt": schedule_details.get("fetchedAt") if source_status == "ok" else state.get("lastSuccessfulScheduleDataAt"),
        "lastSuccessfulApiAt": checked_at if api_status == "ok" else state.get("lastSuccessfulApiAt"),
        "lastSuccessfulTelegramAt": checked_at if public_telegram_status == "ok" else state.get("lastSuccessfulTelegramAt"),
    })
    if all(item["status"] == "ok" for item in services.values()):
        state["lastSuccessfulCheckAt"] = checked_at
    save_state(state)

    if not issues:
        if database_status == "not-configured":
            print("Моніторинг: активні перевірки успішні; PostgreSQL очікує секрет NEON_DATABASE_URL")
        else:
            print("Моніторинг: усі перевірки успішні")
        return

    message = "Виявлено проблеми:\n" + "\n".join(issues)
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
