#!/usr/bin/env python3
"""Independent health checks for the Poltava Light website and Telegram bot."""
import json
import os
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path("public/data/schedules")
STATE_PATH = Path(".monitoring-state.json")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "@PoltavaLight")
KYIV = ZoneInfo("Europe/Kyiv")
MAX_SCHEDULE_AGE = timedelta(minutes=90)
MAX_STATUS_AGE = timedelta(minutes=70)
SITE_URL = "https://svitlo-ua.vercel.app/poltava"
VALID_STATES = {"on", "off", "switching"}
EXPECTED_QUEUES = {f"{queue}.{sub}" for queue in range(1, 7) for sub in (1, 2)}


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KYIV)
        return parsed.astimezone(KYIV)
    except (TypeError, ValueError):
        return None


def load_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def check_schedule(now):
    date_key = now.date().isoformat()
    path = ROOT / f"{date_key}.json"
    if not path.exists():
        return [f"Немає файлу графіка на сьогодні ({date_key})."]
    data = load_json(path)
    queues = data.get("queues")
    if not isinstance(queues, dict):
        return ["Файл сьогоднішнього графіка має некоректну структуру."]

    problems = []
    missing = sorted(EXPECTED_QUEUES - set(queues))
    extra = sorted(set(queues) - EXPECTED_QUEUES)
    if missing:
        problems.append("У графіку відсутні підчерги: " + ", ".join(missing))
    if extra:
        problems.append("У графіку є невідомі підчерги: " + ", ".join(extra))
    for queue in sorted(EXPECTED_QUEUES & set(queues)):
        values = queues[queue]
        if not isinstance(values, list) or len(values) != 48:
            problems.append(f"Підчерга {queue} не містить 48 півгодинних інтервалів.")
        elif any(value not in VALID_STATES for value in values):
            problems.append(f"Підчерга {queue} містить невідомий стан.")

    fetched_at = parse_datetime(data.get("fetchedAt"))
    if not fetched_at:
        problems.append("У графіку відсутній коректний час отримання fetchedAt.")
    elif now - fetched_at > MAX_SCHEDULE_AGE:
        age_minutes = int((now - fetched_at).total_seconds() // 60)
        problems.append(f"Дані графіка не оновлювалися {age_minutes} хвилин.")
    return problems


def check_bot_state(now):
    state = load_json(ROOT / "telegram-state.json")
    sent_at = parse_datetime(state.get("statusMessages", {}).get("sentAt"))
    if not sent_at:
        return ["У стані бота немає часу останнього півгодинного прогнозу."]
    age = now - sent_at
    if age > MAX_STATUS_AGE:
        return [f"Останній півгодинний прогноз був {int(age.total_seconds() // 60)} хв тому."]
    return []


def check_website():
    request = urllib.request.Request(SITE_URL, headers={"User-Agent": "PoltavaLight-Monitor/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                return [f"Сайт відповів HTTP {response.status}."]
            body = response.read(200_000).decode("utf-8", errors="replace")
            if "Світло.ua" not in body:
                return ["Сторінка сайту не містить очікуваного заголовка."]
            return []
    except Exception as error:
        return [f"Сайт недоступний: {error}"]


def telegram_send(message):
    if not TOKEN:
        raise RuntimeError("Не задано TELEGRAM_BOT_TOKEN для моніторингу.")
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API: {result}")


def main():
    now = datetime.now(KYIV)
    issues = check_schedule(now) + check_bot_state(now) + check_website()
    workflow_name = os.environ.get("MONITORED_WORKFLOW_NAME", "")
    conclusion = os.environ.get("MONITORED_WORKFLOW_CONCLUSION", "")
    workflow_url = os.environ.get("MONITORED_WORKFLOW_URL", "")
    if workflow_name and workflow_name != "Unified monitoring" and conclusion not in ("", "success", "neutral", "skipped"):
        issues.append(f"Workflow «{workflow_name}» завершився зі статусом {conclusion}. {workflow_url}".strip())

    state = load_json(STATE_PATH)
    previous = set(state.get("activeIssues", []))
    current = set(issues)
    new_issues = sorted(current - previous)
    resolved = sorted(previous - current)
    if new_issues:
        telegram_send("⚠️ Моніторинг Світло.ua / PoltavaLight\n\n" + "\n".join("• " + item for item in new_issues))
    elif resolved:
        telegram_send("✅ Моніторинг: роботу відновлено.\n\n" + "\n".join("• " + item for item in resolved))
    else:
        print(f"Моніторинг: проблем немає; активних проблем: {len(current)}")

    state["activeIssues"] = sorted(current)
    state["checkedAt"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
