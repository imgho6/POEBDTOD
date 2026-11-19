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
    """Перевіряє актуальну дату та повноту офіційного набору графіків Полтавщини."""
    url = "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Poltavaoblenergo.json"
    request = urllib.request.Request(url, headers={"User-Agent": "PoltavaLight-Monitor/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return [f"Джерело графіків Полтавщини недоступне: {error}"]

    timestamp = payload.get("fact", {}).get("today")
    if not timestamp:
        return ["У джерелі графіків немає дати поточного розкладу."]
    try:
        schedule_date = datetime.fromtimestamp(int(timestamp), KYIV).date()
    except (TypeError, ValueError, OSError):
        return ["Джерело графіків повернуло некоректну дату."]
    if schedule_date != now.date():
        return [f"Джерело графіків показує {schedule_date.isoformat()}, очікується {now.date().isoformat()}."]

    day = payload.get("fact", {}).get("data", {}).get(str(timestamp))
    if not isinstance(day, dict):
        return ["У джерелі відсутні дані графіка на поточну дату."]

    expected = {f"GPV{queue}.{sub}" for queue in range(1, 7) for sub in (1, 2)}
    missing = sorted(expected - set(day))
    if missing:
        return ["У джерелі відсутні підчерги: " + ", ".join(missing)]
    malformed = [
        name for name in sorted(expected)
        if not isinstance(day.get(name), dict) or len(day[name]) != 24
    ]
    if malformed:
        return ["Некоректні погодинні дані для підчерг: " + ", ".join(malformed)]
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
    issues = check_schedule(now) + check_website()
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
