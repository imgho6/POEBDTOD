#!/usr/bin/env python3
"""Private Telegram admin panel and scheduled public-channel posts."""
import argparse
import json
import html
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ADMIN_ID = 1234960363
ADMIN_CHANNEL = "-1003570758649"
PUBLIC_CHANNEL = "@PoltavaLight"
KYIV = ZoneInfo("Europe/Kyiv")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

MODULES = {
    "schedules": "Оновлення графіків",
    "news": "Новини та редакційні дописи",
    "monitor": "Моніторинг",
}


def telegram(method, payload):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API {method}: HTTP {error.code}: {detail[:500]}") from error
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {result}")
    return result.get("result")


def db_connection():
    from bot_db import connection
    return connection()


def ensure_admin_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            setting_key TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_admin_sessions (
            admin_id BIGINT PRIMARY KEY,
            state TEXT NOT NULL,
            draft TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_scheduled_posts (
            id BIGSERIAL PRIMARY KEY,
            body TEXT NOT NULL,
            scheduled_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_by BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sent_at TIMESTAMPTZ,
            telegram_message_id BIGINT,
            last_error TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS bot_scheduled_posts_due_idx ON bot_scheduled_posts (scheduled_at) WHERE status = 'scheduled'")
    for key in MODULES:
        conn.execute(
            "INSERT INTO bot_settings (setting_key, enabled) VALUES (%s, TRUE) ON CONFLICT (setting_key) DO NOTHING",
            (key,),
        )


def menu_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "⚙️ Модулі", "callback_data": "modules"}, {"text": "📝 Створити допис", "callback_data": "compose"}],
            [{"text": "🗓 Заплановані дописи", "callback_data": "posts"}, {"text": "🔄 Оновити панель", "callback_data": "menu"}],
        ]
    }


def modules_keyboard(conn):
    rows = conn.execute("SELECT setting_key, enabled FROM bot_settings ORDER BY setting_key").fetchall()
    states = {key: enabled for key, enabled in rows}
    buttons = []
    for key, label in MODULES.items():
        enabled = states.get(key, True)
        buttons.append([{"text": ("🟢 " if enabled else "🔴 ") + label, "callback_data": "toggle:" + key}])
    buttons.append([{"text": "⬅️ Назад", "callback_data": "menu"}])
    return {"inline_keyboard": buttons}


def panel_text(conn):
    rows = conn.execute("SELECT setting_key, enabled FROM bot_settings ORDER BY setting_key").fetchall()
    states = {key: enabled for key, enabled in rows}
    lines = ["<b>Панель керування Світло.ua</b>", "", "Стан модулів:"]
    for key, label in MODULES.items():
        lines.append(("🟢" if states.get(key, True) else "🔴") + " " + label)
    lines += ["", "Керування доступне лише власнику бота."]
    return "\n".join(lines)


def send_panel(chat_id=ADMIN_CHANNEL, message_id=None):
    with db_connection() as conn:
        if conn is None:
            raise RuntimeError("Neon database unavailable")
        ensure_admin_schema(conn)
        text = panel_text(conn)
    if message_id:
        try:
            telegram("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": text,
                "parse_mode": "HTML", "reply_markup": menu_keyboard(),
            })
            return
        except Exception:
            pass
    telegram("sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "reply_markup": menu_keyboard(), "disable_web_page_preview": True,
    })


def set_session(conn, state, draft=None):
    conn.execute("""
        INSERT INTO bot_admin_sessions (admin_id, state, draft, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (admin_id) DO UPDATE
        SET state = EXCLUDED.state, draft = EXCLUDED.draft, updated_at = NOW()
    """, (ADMIN_ID, state, draft))


def clear_session(conn):
    conn.execute("DELETE FROM bot_admin_sessions WHERE admin_id = %s", (ADMIN_ID,))


def get_session(conn):
    return conn.execute(
        "SELECT state, draft FROM bot_admin_sessions WHERE admin_id = %s",
        (ADMIN_ID,),
    ).fetchone()


def process_callback(update):
    query = update["callback_query"]
    user = query.get("from", {})
    if user.get("id") != ADMIN_ID:
        telegram("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Немає доступу", "show_alert": True})
        return
    data = query.get("data", "")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id", ADMIN_CHANNEL)
    message_id = message.get("message_id")
    telegram("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Відкрий приватний чат із ботом, натисни Start і надішли /post.", "show_alert": True} if data == "compose" else {"callback_query_id": query["id"]})

    with db_connection() as conn:
        if conn is None:
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Помилка: немає з'єднання з Neon."})
            return
        ensure_admin_schema(conn)

        if data == "menu":
            send_panel(chat_id, message_id)
        elif data == "modules":
            telegram("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": "<b>Керування модулями</b>\nНатисни модуль, щоб увімкнути або вимкнути його.",
                "parse_mode": "HTML", "reply_markup": modules_keyboard(conn),
            })
        elif data.startswith("toggle:"):
            key = data.split(":", 1)[1]
            if key not in MODULES:
                return
            conn.execute("""
                INSERT INTO bot_settings (setting_key, enabled) VALUES (%s, FALSE)
                ON CONFLICT (setting_key) DO UPDATE
                SET enabled = NOT bot_settings.enabled, updated_at = NOW()
            """, (key,))
            telegram("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": "<b>Керування модулями</b>\nНатисни модуль, щоб змінити його стан.",
                "parse_mode": "HTML", "reply_markup": modules_keyboard(conn),
            })
        elif data == "compose":
            pass
        elif data == "posts":
            rows = conn.execute("""
                SELECT id, body, scheduled_at FROM bot_scheduled_posts
                WHERE status = 'scheduled' ORDER BY scheduled_at LIMIT 10
            """).fetchall()
            if not rows:
                body = "Запланованих дописів поки немає."
                keyboard = {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "menu"}]]}
            else:
                lines = ["<b>Заплановані дописи</b>"]
                buttons = []
                for post_id, text, when in rows:
                    preview = text[:100] + ("…" if len(text) > 100 else "")
                    lines.append(f"\\n<b>#{post_id}</b> · {when.astimezone(KYIV):%d.%m.%Y %H:%M}\\n{html.escape(preview)}")
                    buttons.append([{"text": f"Скасувати #{post_id}", "callback_data": f"cancel:{post_id}"}])
                buttons.append([{"text": "⬅️ Назад", "callback_data": "menu"}])
                body = "\n".join(lines)
                keyboard = {"inline_keyboard": buttons}
            telegram("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": body, "parse_mode": "HTML", "reply_markup": keyboard})
        elif data.startswith("cancel:"):
            post_id = int(data.split(":", 1)[1])
            conn.execute("UPDATE bot_scheduled_posts SET status = 'cancelled' WHERE id = %s AND status = 'scheduled'", (post_id,))
            send_panel(chat_id, message_id)
        elif data == "draft:now":
            session = get_session(conn)
            if not session or session[0] != "confirm_draft" or not session[1]:
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Чернетку не знайдено. Почни з /post."})
                return
            result = telegram("sendMessage", {"chat_id": PUBLIC_CHANNEL, "text": session[1], "disable_web_page_preview": True})
            clear_session(conn)
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": f"Допис опубліковано в @{PUBLIC_CHANNEL.lstrip('@')}. ID повідомлення: {result.get('message_id')}."})
        elif data == "draft:cancel":
            clear_session(conn)
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Чернетку скасовано."})
        elif data == "draft:schedule":
            set_session(conn, "awaiting_schedule", get_session(conn)[1] if get_session(conn) else None)
            telegram("sendMessage", {
                "chat_id": ADMIN_ID,
                "text": "Надішли дату й час у форматі:\n<code>/at 2026-11-20 09:30</code>\nЧас указуй за Києвом.",
                "parse_mode": "HTML",
            })


def process_message(update):
    message = update["message"]
    user = message.get("from", {})
    if user.get("id") != ADMIN_ID or message.get("chat", {}).get("type") != "private":
        return
    text = (message.get("text") or "").strip()
    if not text:
        return
    with db_connection() as conn:
        if conn is None:
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Помилка: немає з'єднання з Neon."})
            return
        ensure_admin_schema(conn)
        if text in ("/start", "/panel"):
            clear_session(conn)
            send_panel(ADMIN_ID)
        elif text == "/post":
            set_session(conn, "awaiting_text")
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Надішли текст допису для каналу @PoltavaLight. Я покажу попередній перегляд перед публікацією."})
        elif text.startswith("/at "):
            session = get_session(conn)
            if not session or session[0] != "awaiting_schedule" or not session[1]:
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Спочатку створи чернетку командою /post."})
                return
            match = re.fullmatch(r"/at (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})", text)
            if not match:
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Формат: /at YYYY-MM-DD HH:MM"})
                return
            try:
                local = datetime.strptime(match.group(1) + " " + match.group(2), "%Y-%m-%d %H:%M").replace(tzinfo=KYIV)
                if local <= datetime.now(KYIV):
                    raise ValueError("time must be in the future")
            except ValueError:
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Некоректна дата або час. Час має бути в майбутньому."})
                return
            conn.execute("""
                INSERT INTO bot_scheduled_posts (body, scheduled_at, created_by)
                VALUES (%s, %s, %s)
            """, (session[1], local.astimezone(timezone.utc), ADMIN_ID))
            clear_session(conn)
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": f"Готово. Допис заплановано на {local:%d.%m.%Y о %H:%M} за Києвом."})
        else:
            session = get_session(conn)
            if session and session[0] == "awaiting_text":
                if len(text) > 4000:
                    telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Занадто довгий текст. Максимум 4000 символів."})
                    return
                set_session(conn, "confirm_draft", text)
                telegram("sendMessage", {
                    "chat_id": ADMIN_ID,
                    "text": "<b>Попередній перегляд допису:</b>\n\n" + text + "\n\nЩо робимо?",
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": [[
                        {"text": "🚀 Опублікувати зараз", "callback_data": "draft:now"},
                        {"text": "🗓 Запланувати", "callback_data": "draft:schedule"},
                    ], [{"text": "Скасувати", "callback_data": "draft:cancel"}]]},
                })
            elif session and session[0] == "awaiting_schedule":
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Використай формат /at YYYY-MM-DD HH:MM."})
            else:
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Команди: /start, /panel, /post"})


def dispatch_due():
    with db_connection() as conn:
        if conn is None:
            raise RuntimeError("Neon database unavailable")
        ensure_admin_schema(conn)
        rows = conn.execute("""
            SELECT id, body FROM bot_scheduled_posts
            WHERE status = 'scheduled' AND scheduled_at <= NOW()
            ORDER BY scheduled_at
            FOR UPDATE SKIP LOCKED
            LIMIT 10
        """).fetchall()
        for post_id, body in rows:
            conn.execute("UPDATE bot_scheduled_posts SET status = 'sending' WHERE id = %s", (post_id,))
            try:
                result = telegram("sendMessage", {"chat_id": PUBLIC_CHANNEL, "text": body, "disable_web_page_preview": True})
                conn.execute("""
                    UPDATE bot_scheduled_posts
                    SET status = 'sent', sent_at = NOW(), telegram_message_id = %s, last_error = NULL
                    WHERE id = %s
                """, (result.get("message_id"), post_id))
                print(f"Scheduled post {post_id} published")
            except Exception as error:
                conn.execute("""
                    UPDATE bot_scheduled_posts SET status = 'failed', last_error = %s WHERE id = %s
                """, (str(error)[:1000], post_id))
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": f"Не вдалося опублікувати запланований допис #{post_id}: {str(error)[:500]}"})
                print(f"Scheduled post {post_id} failed: {error}")


def is_enabled(key):
    if key not in MODULES:
        raise ValueError("unknown module")
    with db_connection() as conn:
        if conn is None:
            print("Neon unavailable; preserving default enabled state")
            return True
        ensure_admin_schema(conn)
        row = conn.execute("SELECT enabled FROM bot_settings WHERE setting_key = %s", (key,)).fetchone()
        return bool(row[0]) if row else True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispatch-due", action="store_true")
    parser.add_argument("--is-enabled", choices=tuple(MODULES))
    args = parser.parse_args()
    if args.dispatch_due:
        dispatch_due()
        return
    if args.is_enabled:
        sys.exit(0 if is_enabled(args.is_enabled) else 1)
    raw = os.environ.get("TELEGRAM_UPDATE_JSON", "")
    if not raw:
        raise SystemExit("TELEGRAM_UPDATE_JSON is required")
    update = json.loads(raw)
    if "callback_query" in update:
        process_callback(update)
    elif "message" in update:
        process_message(update)


if __name__ == "__main__":
    main()
