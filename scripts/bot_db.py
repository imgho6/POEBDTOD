"""Optional PostgreSQL persistence for the Telegram bot.

When NEON_DATABASE_URL is absent or the database is temporarily unavailable,
callers continue using the existing gh-pages JSON state.
"""
import json
import os
from contextlib import contextmanager


@contextmanager
def connection():
    url = os.environ.get("NEON_DATABASE_URL", "").strip()
    if not url:
        yield None
        return
    try:
        import psycopg
    except ImportError:
        print("База даних: psycopg не встановлено, використовую JSON-стан.")
        yield None
        return
    try:
        conn = psycopg.connect(url, connect_timeout=5)
    except Exception as error:
        print(f"База даних тимчасово недоступна: {error}")
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()


def load_news_state():
    with connection() as conn:
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT state_value FROM bot_news_state WHERE state_key = %s",
                ("editorial",),
            ).fetchone()
            return row[0] if row else None
        except Exception as error:
            print(f"Не вдалося прочитати стан новин із БД: {error}")
            return None


def save_news_state(value):
    with connection() as conn:
        if conn is None:
            return False
        try:
            from psycopg.types.json import Jsonb
            conn.execute(
                """
                INSERT INTO bot_news_state (state_key, state_value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (state_key)
                DO UPDATE SET state_value = EXCLUDED.state_value, updated_at = NOW()
                """,
                ("editorial", Jsonb(value)),
            )
            return True
        except Exception as error:
            print(f"Не вдалося зберегти стан новин у БД: {error}")
            return False


def record_publication(kind, chat_id, message_id, *, source_handle=None,
                       source_post=None, content_hash=None, title=None, body=None,
                       importance_score=None, relevance_score=None,
                       source_priority=None, metadata=None):
    if not message_id:
        return False
    with connection() as conn:
        if conn is None:
            return False
        try:
            from psycopg.types.json import Jsonb
            conn.execute(
                """
                INSERT INTO bot_publications
                  (kind, chat_id, message_id, source_handle, source_post,
                   content_hash, title, body, importance_score, relevance_score,
                   source_priority, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chat_id, message_id)
                DO UPDATE SET
                  kind = EXCLUDED.kind,
                  source_handle = COALESCE(EXCLUDED.source_handle, bot_publications.source_handle),
                  source_post = COALESCE(EXCLUDED.source_post, bot_publications.source_post),
                  content_hash = COALESCE(EXCLUDED.content_hash, bot_publications.content_hash),
                  title = COALESCE(EXCLUDED.title, bot_publications.title),
                  body = COALESCE(EXCLUDED.body, bot_publications.body),
                  importance_score = COALESCE(EXCLUDED.importance_score, bot_publications.importance_score),
                  relevance_score = COALESCE(EXCLUDED.relevance_score, bot_publications.relevance_score),
                  source_priority = COALESCE(EXCLUDED.source_priority, bot_publications.source_priority),
                  metadata = bot_publications.metadata || EXCLUDED.metadata
                """,
                (kind, str(chat_id), int(message_id), source_handle, source_post,
                 content_hash, title, body, importance_score, relevance_score,
                 source_priority, Jsonb(metadata or {})),
            )
            return True
        except Exception as error:
            print(f"Не вдалося записати Telegram-публікацію в БД: {error}")
            return False


def mark_deleted(chat_id, message_id, reason=None):
    with connection() as conn:
        if conn is None:
            return False
        try:
            from psycopg.types.json import Jsonb
            conn.execute(
                """
                UPDATE bot_publications
                SET deleted_at = NOW(),
                    metadata = metadata || %s
                WHERE chat_id = %s AND message_id = %s
                """,
                (Jsonb({"deletion_reason": reason} if reason else {}), str(chat_id), int(message_id)),
            )
            return True
        except Exception as error:
            print(f"Не вдалося записати видалення в БД: {error}")
            return False


def record_error(component, message, context=None, severity="error"):
    with connection() as conn:
        if conn is None:
            return False
        try:
            from psycopg.types.json import Jsonb
            conn.execute(
                """
                INSERT INTO bot_errors (component, severity, message, context)
                VALUES (%s, %s, %s, %s)
                """,
                (component, severity, str(message)[:5000], Jsonb(context or {})),
            )
            return True
        except Exception as error:
            print(f"Не вдалося записати помилку в БД: {error}")
            return False


def record_monitor_check(component, check_name, status, duration_ms=None, details=None):
    with connection() as conn:
        if conn is None:
            return False
        try:
            from psycopg.types.json import Jsonb
            conn.execute(
                """
                INSERT INTO bot_monitor_checks
                  (component, check_name, status, duration_ms, details)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (component, check_name, status, duration_ms, Jsonb(details or {})),
            )
            return True
        except Exception as error:
            print(f"Не вдалося записати перевірку моніторингу в БД: {error}")
            return False
