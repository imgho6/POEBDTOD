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
        CREATE TABLE IF NOT EXISTS bot_processed_updates (
            update_id BIGINT PRIMARY KEY,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
            [{"text": "📊 Статистика", "callback_data": "stats"}, {"text": "🩺 Стан системи", "callback_data": "health"}],
            [{"text": "⚙️ Модулі", "callback_data": "modules"}, {"text": "🗓 Заплановані", "callback_data": "posts"}],
            [{"text": "📰 Історія публікацій", "callback_data": "history"}, {"text": "🚨 Помилки", "callback_data": "errors"}],
            [{"text": "📝 Створити допис", "callback_data": "compose"}, {"text": "🔧 Налаштування", "callback_data": "config"}],
            [{"text": "🔄 Оновити", "callback_data": "menu"}],
        ]
    }


def back_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ До головної панелі", "callback_data": "menu"}]]}


POST_TEMPLATES = {
    "schedule": ("📊 Оновлення графіка", "📊 <b>Оновлення графіка відключень</b>\n\nОпубліковано актуальний графік для Полтавщини.\n\n🔗 Деталі: "),
    "alert": ("🚨 Термінове повідомлення", "🚨 <b>Увага!</b>\n\n[Текст повідомлення]\n\nСтежте за оновленнями."),
    "news": ("📰 Новина", "📰 <b>[Заголовок]</b>\n\n[Текст новини]\n\n🔗 Детальніше: "),
    "notice": ("📢 Оголошення", "📢 <b>Оголошення</b>\n\n[Текст оголошення]"),
}


def post_creation_keyboard():
    return {"inline_keyboard": [
        [{"text": "✍️ Написати з нуля", "callback_data": "post:blank"}],
        [{"text": "📊 Графік", "callback_data": "post:template:schedule"}, {"text": "🚨 Терміново", "callback_data": "post:template:alert"}],
        [{"text": "📰 Новина", "callback_data": "post:template:news"}, {"text": "📢 Оголошення", "callback_data": "post:template:notice"}],
        [{"text": "♻️ На основі останнього допису", "callback_data": "post:copy"}],
        [{"text": "⬅️ До панелі", "callback_data": "menu"}],
    ]}


def draft_keyboard():
    return {"inline_keyboard": [
        [{"text": "🚀 Опублікувати зараз", "callback_data": "draft:now"}],
        [{"text": "🗓 Запланувати", "callback_data": "draft:schedule"}],
        [{"text": "✏️ Редагувати текст", "callback_data": "draft:edit"}, {"text": "❌ Скасувати", "callback_data": "draft:cancel"}],
    ]}


def quick_schedule_keyboard():
    return {"inline_keyboard": [
        [{"text": "Через 1 годину", "callback_data": "schedule:1h"}],
        [{"text": "Сьогодні о 20:00", "callback_data": "schedule:today20"}],
        [{"text": "Завтра о 09:00", "callback_data": "schedule:tomorrow09"}],
        [{"text": "Ввести дату й час", "callback_data": "schedule:custom"}],
        [{"text": "⬅️ Назад до чернетки", "callback_data": "draft:back"}],
    ]}


def format_local(value):
    if not value:
        return "невідомо"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KYIV).strftime("%d.%m.%Y %H:%M")


def health_text(conn):
    rows = conn.execute("""
        SELECT DISTINCT ON (check_name) check_name, status, checked_at, details
        FROM bot_monitor_checks ORDER BY check_name, checked_at DESC
    """).fetchall()
    if not rows:
        return "<b>🩺 Стан системи</b>\n\nДаних моніторингу поки немає."
    icons = {"ok": "🟢", "success": "🟢", "warning": "🟡", "not-configured": "⚪", "error": "🔴", "failure": "🔴"}
    lines = ["<b>🩺 Стан системи</b>", ""]
    for name, status, checked_at, details in rows:
        lines.append(f"{icons.get(str(status).lower(), '⚪')} <b>{html.escape(str(name))}</b> – {html.escape(str(status))}")
        lines.append(f"   <i>{format_local(checked_at)}</i>")
        if isinstance(details, dict) and details.get("reason"):
            lines.append("   " + html.escape(str(details["reason"])[:180]))
    lines += ["", "Це останні збережені результати перевірок."]
    return "\n".join(lines)


def stats_text(conn):
    totals = conn.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE published_at >= NOW() - INTERVAL '7 days'),
               COUNT(*) FILTER (WHERE published_at >= NOW() - INTERVAL '30 days')
        FROM bot_publications WHERE chat_id = %s AND deleted_at IS NULL
    """, (PUBLIC_CHANNEL,)).fetchone()
    kinds = conn.execute("""
        SELECT kind, COUNT(*) FROM bot_publications
        WHERE chat_id = %s AND deleted_at IS NULL
          AND published_at >= NOW() - INTERVAL '30 days'
        GROUP BY kind ORDER BY COUNT(*) DESC
    """, (PUBLIC_CHANNEL,)).fetchall()
    scheduled = conn.execute("""
        SELECT COUNT(*) FILTER (WHERE status = 'scheduled'),
               COUNT(*) FILTER (WHERE status = 'sent'),
               COUNT(*) FILTER (WHERE status = 'failed')
        FROM bot_scheduled_posts
    """).fetchone()
    lines = [
        "<b>📊 Статистика каналу</b>", "",
        f"Усього записаних публікацій: <b>{totals[0]}</b>",
        f"За 7 днів: <b>{totals[1]}</b>",
        f"За 30 днів: <b>{totals[2]}</b>", "",
        "<b>Типи публікацій за 30 днів:</b>",
    ]
    lines.extend([f"• {html.escape(str(kind))}: {count}" for kind, count in kinds] or ["• Немає даних"])
    lines += ["", "<b>Відкладені дописи:</b>", f"• Очікують: {scheduled[0]}", f"• Опубліковано: {scheduled[1]}", f"• Помилки: {scheduled[2]}"]
    return "\n".join(lines)


def errors_text(conn):
    rows = conn.execute("SELECT component, severity, message, created_at FROM bot_errors ORDER BY created_at DESC LIMIT 7").fetchall()
    if not rows:
        return "<b>🚨 Журнал помилок</b>\n\nПомилок у журналі немає."
    lines = ["<b>🚨 Останні помилки</b>", ""]
    for component, severity, message, created_at in rows:
        lines.append(f"<b>{html.escape(str(component))}</b> · {html.escape(str(severity))} · {format_local(created_at)}")
        lines.append(html.escape(str(message)[:350]))
        lines.append("")
    return "\n".join(lines)


def history_text(conn):
    rows = conn.execute("""
        SELECT kind, title, body, published_at, message_id
        FROM bot_publications WHERE chat_id = %s AND deleted_at IS NULL
        ORDER BY published_at DESC LIMIT 10
    """, (PUBLIC_CHANNEL,)).fetchall()
    if not rows:
        return "<b>📰 Історія публікацій</b>\n\nПублікацій у базі поки немає."
    lines = ["<b>📰 Останні публікації</b>", ""]
    for kind, title, body, published_at, message_id in rows:
        preview = str(title or body or "Без текстового опису").replace("\n", " ")
        if len(preview) > 180:
            preview = preview[:177] + "…"
        lines.extend([f"<b>{html.escape(str(kind))}</b> · {format_local(published_at)}", html.escape(preview), f"<i>ID повідомлення: {message_id}</i>", ""])
    return "\n".join(lines)


def config_text(conn):
    rows = conn.execute("SELECT setting_key, enabled FROM bot_settings ORDER BY setting_key").fetchall()
    states = {key: enabled for key, enabled in rows}
    lines = [
        "<b>🔧 Налаштування та довідка</b>", "",
        f"Публічний канал: {html.escape(PUBLIC_CHANNEL)}",
        f"Адмін-чат: <code>{html.escape(ADMIN_CHANNEL)}</code>",
        "Часовий пояс: Europe/Kyiv", "Сховище стану: Neon PostgreSQL", "", "<b>Модулі:</b>",
    ]
    for key, label in MODULES.items():
        lines.append(("🟢 " if states.get(key, True) else "🔴 ") + html.escape(label))
    lines += ["", "<b>Команди:</b>", "/panel – головна панель", "/status – стан модулів", "/health – перевірки", "/stats – статистика", "/errors – журнал помилок", "/history – публікації", "/post – створити допис"]
    return "\n".join(lines)


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
    pending = conn.execute("SELECT COUNT(*) FROM bot_scheduled_posts WHERE status = 'scheduled'").fetchone()[0]
    recent_errors = conn.execute("SELECT COUNT(*) FROM bot_errors WHERE created_at >= NOW() - INTERVAL '24 hours'").fetchone()[0]
    lines += ["", f"🗓 Очікують публікації: <b>{pending}</b>", f"🚨 Помилок за 24 години: <b>{recent_errors}</b>", f"🕒 Оновлено: {datetime.now(KYIV):%d.%m.%Y %H:%M}", "", "Керування доступне лише власнику бота."]
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
        return
    telegram("answerCallbackQuery", {"callback_query_id": query.get("id")})
    data = query.get("data", "")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id not in (int(ADMIN_CHANNEL), ADMIN_ID):
        return
    message_id = message.get("message_id")

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
        elif data == "stats":
            telegram("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": stats_text(conn), "parse_mode": "HTML", "reply_markup": back_keyboard()})
        elif data == "health":
            telegram("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": health_text(conn), "parse_mode": "HTML", "reply_markup": back_keyboard()})
        elif data == "errors":
            telegram("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": errors_text(conn), "parse_mode": "HTML", "reply_markup": back_keyboard()})
        elif data == "history":
            telegram("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": history_text(conn), "parse_mode": "HTML", "reply_markup": back_keyboard()})
        elif data == "config":
            telegram("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": config_text(conn), "parse_mode": "HTML", "reply_markup": back_keyboard()})
        elif data == "compose":
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "<b>Створення допису</b>\n\nОбери спосіб підготовки публікації:", "parse_mode": "HTML", "reply_markup": post_creation_keyboard()})
        elif data == "post:blank":
            set_session(conn, "awaiting_text")
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Надішли текст допису. Після цього зможеш відредагувати його, опублікувати або запланувати."})
        elif data.startswith("post:template:"):
            key = data.split(":")[-1]
            template = POST_TEMPLATES.get(key)
            if not template:
                return
            set_session(conn, "confirm_draft", template[1])
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": f"<b>{html.escape(template[0])}</b>\n\n{template[1]}\n\nЗаміни текст у шаблоні кнопкою редагування.", "parse_mode": "HTML", "reply_markup": draft_keyboard()})
        elif data == "post:copy":
            previous = conn.execute("""
                SELECT body FROM bot_publications
                WHERE chat_id = %s AND deleted_at IS NULL AND body IS NOT NULL AND body <> ''
                ORDER BY published_at DESC LIMIT 1
            """, (PUBLIC_CHANNEL,)).fetchone()
            if not previous:
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "У базі ще немає допису, який можна взяти за основу."})
            else:
                set_session(conn, "confirm_draft", previous[0])
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Чернетка на основі останньої публікації. Відредагуй її перед використанням:\n\n" + previous[0], "reply_markup": draft_keyboard()})
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
                    lines.append(f"\n<b>#{post_id}</b> · {when.astimezone(KYIV):%d.%m.%Y %H:%M}\n{html.escape(preview)}")
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
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": f"✅ Допис опубліковано в @{PUBLIC_CHANNEL.lstrip('@')}.", "reply_markup": {"inline_keyboard": [[{"text": "📌 Закріпити допис", "callback_data": f"pin:{result.get('message_id')}"}], [{"text": "➕ Створити ще один", "callback_data": "compose"}, {"text": "🏠 Панель", "callback_data": "menu"}]]}})
        elif data.startswith("pin:"):
            post_id = int(data.split(":", 1)[1])
            telegram("pinChatMessage", {"chat_id": PUBLIC_CHANNEL, "message_id": post_id, "disable_notification": True})
            telegram("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "Допис закріплено"})
        elif data == "draft:edit":
            session = get_session(conn)
            if session and session[1]:
                set_session(conn, "awaiting_text", session[1])
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Надішли повний оновлений текст чернетки одним повідомленням."})
        elif data == "draft:back":
            session = get_session(conn)
            if session and session[1]:
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Поточна чернетка:\n\n" + session[1], "reply_markup": draft_keyboard()})
        elif data == "draft:cancel":
            clear_session(conn)
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Чернетку скасовано."})
        elif data == "draft:schedule":
            session = get_session(conn)
            if session and session[1]:
                set_session(conn, "awaiting_schedule", session[1])
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Обери швидкий час або введи власну дату:", "reply_markup": quick_schedule_keyboard()})
        elif data.startswith("schedule:"):
            session = get_session(conn)
            if not session or session[0] != "awaiting_schedule" or not session[1]:
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Чернетку не знайдено. Почни створення допису заново."})
                return
            now = datetime.now(KYIV)
            choice = data.split(":", 1)[1]
            if choice == "1h":
                when = now.replace(second=0, microsecond=0) + __import__("datetime").timedelta(hours=1)
            elif choice == "today20":
                when = now.replace(hour=20, minute=0, second=0, microsecond=0)
                if when <= now:
                    when += __import__("datetime").timedelta(days=1)
            elif choice == "tomorrow09":
                when = (now + __import__("datetime").timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            elif choice == "custom":
                telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Формат: /at YYYY-MM-DD HH:MM (час Києва)."})
                return
            else:
                return
            conn.execute("INSERT INTO bot_scheduled_posts (body, scheduled_at, created_by) VALUES (%s, %s, %s)", (session[1], when.astimezone(timezone.utc), ADMIN_ID))
            clear_session(conn)
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": f"🗓 Заплановано на {when:%d.%m.%Y о %H:%M} за Києвом.", "reply_markup": {"inline_keyboard": [[{"text": "🗓 Переглянути розклад", "callback_data": "posts"}], [{"text": "➕ Новий допис", "callback_data": "compose"}]]}})


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
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "<b>Створення допису</b>\n\nОбери шаблон або почни з чистого аркуша:", "parse_mode": "HTML", "reply_markup": post_creation_keyboard()})
        elif text in ("/status", "/health", "/stats", "/errors", "/history"):
            renderers = {"/status": lambda: panel_text(conn), "/health": lambda: health_text(conn), "/stats": lambda: stats_text(conn), "/errors": lambda: errors_text(conn), "/history": lambda: history_text(conn)}
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": renderers[text](), "parse_mode": "HTML", "reply_markup": menu_keyboard()})
        elif text == "/help":
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": config_text(conn), "parse_mode": "HTML", "reply_markup": menu_keyboard()})
        elif text in ("/cancel", "/cancel_draft"):
            clear_session(conn)
            telegram("sendMessage", {"chat_id": ADMIN_ID, "text": "Поточну дію скасовано."})
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
                    "text": "👀 Попередній перегляд допису:\n\n" + text + "\n\nПеревір текст. Можеш відредагувати, опублікувати або запланувати.",
                    "reply_markup": draft_keyboard(),
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
            LIMIT 10
            FOR UPDATE SKIP LOCKED
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
    parser.add_argument("--init-panel", action="store_true")
    parser.add_argument("--is-enabled", choices=tuple(MODULES))
    args = parser.parse_args()
    if args.dispatch_due:
        dispatch_due()
        return
    if args.init_panel:
        send_panel()
        return
    if args.is_enabled:
        sys.exit(0 if is_enabled(args.is_enabled) else 1)
    raw = os.environ.get("TELEGRAM_UPDATE_JSON", "")
    if not raw:
        raise SystemExit("TELEGRAM_UPDATE_JSON is required")
    update = json.loads(raw)
    update_id = update.get("update_id")
    if update_id is not None:
        with db_connection() as conn:
            if conn is None:
                raise RuntimeError("Neon database unavailable")
            ensure_admin_schema(conn)
            claimed = conn.execute(
                "INSERT INTO bot_processed_updates (update_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING update_id",
                (int(update_id),),
            ).fetchone()
            if not claimed:
                print(f"Duplicate Telegram update {update_id}; ignored")
                return
    if "callback_query" in update:
        process_callback(update)
    elif "message" in update:
        process_message(update)


if __name__ == "__main__":
    main()
