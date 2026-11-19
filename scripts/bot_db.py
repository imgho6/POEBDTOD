"""Optional PostgreSQL persistence for the Telegram bot.

When NEON_DATABASE_URL is absent or the database is temporarily unavailable,
callers continue using the existing gh-pages JSON state.
"""
import json
import os
from contextlib import contextmanager


_SCHEMA_READY = False


def ensure_schema(conn):
    """Створює мінімальну схему один раз на процес, без окремого ручного SQL-кроку."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS bot_news_state (
            state_key TEXT PRIMARY KEY,
            state_value JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bot_publications (
            id BIGSERIAL PRIMARY KEY,
            kind TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            message_id BIGINT NOT NULL,
            source_handle TEXT,
            source_post TEXT,
            content_hash TEXT,
            title TEXT,
            body TEXT,
            importance_score INTEGER,
            relevance_score INTEGER,
            source_priority INTEGER,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            UNIQUE (chat_id, message_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bot_errors (
            id BIGSERIAL PRIMARY KEY,
            component TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'error',
            message TEXT NOT NULL,
            context JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bot_monitor_checks (
            id BIGSERIAL PRIMARY KEY,
            component TEXT NOT NULL,
            check_name TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms INTEGER,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS bot_publications_published_at_idx ON bot_publications (published_at DESC)",
        "CREATE INDEX IF NOT EXISTS bot_publications_kind_idx ON bot_publications (kind, published_at DESC)",
        "CREATE INDEX IF NOT EXISTS bot_errors_created_at_idx ON bot_errors (created_at DESC)",
        "CREATE INDEX IF NOT EXISTS bot_monitor_checks_checked_at_idx ON bot_monitor_checks (checked_at DESC)",
    )
    for statement in statements:
        conn.execute(statement)


@contextmanager
def connection():
    global _SCHEMA_READY
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
        if not _SCHEMA_READY:
            try:
                with conn.transaction():
                    ensure_schema(conn)
                _SCHEMA_READY = True
            except Exception as error:
                print(f"Не вдалося підготувати схему PostgreSQL: {error}")
                conn.close()
                yield None
                return

        try:
            with conn:
                yield conn
        except Exception as error:
            print(f"Помилка транзакції PostgreSQL: {error}")
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
