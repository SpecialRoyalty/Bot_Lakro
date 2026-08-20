from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.storage import DEFAULT_SETTINGS, normalize_word


class PostgresStorage:
    """Thread-safe PostgreSQL storage backed by a small Psycopg pool."""

    backend_name = "postgresql"

    def __init__(self, database_url: str) -> None:
        try:
            from psycopg_pool import ConnectionPool
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Le paquet psycopg[pool] est requis lorsque DATABASE_URL est configuré."
            ) from exc

        self._pool: Any = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=4,
            open=True,
            timeout=30,
            max_idle=300,
            kwargs={"connect_timeout": 10},
            check=ConnectionPool.check_connection,
            name="telegram-group-manager",
        )

    def initialize(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS banned_words (
                word TEXT NOT NULL,
                normalized TEXT PRIMARY KEY
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS offenses (
                user_id BIGINT PRIMARY KEY,
                offense_count INTEGER NOT NULL,
                last_offense_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sent_events (
                event_key TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL
            )
            """,
        )
        with self._pool.connection() as connection:
            for statement in statements:
                connection.execute(statement)
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT(key) DO NOTHING",
                    list(DEFAULT_SETTINGS.items()),
                )

    def close(self) -> None:
        self._pool.close()

    def get(self, key: str, default: str = "") -> str:
        with self._pool.connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
        return str(row[0]) if row else default

    def set(self, key: str, value: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )

    def get_bool(self, key: str) -> bool:
        return self.get(key) == "1"

    def toggle(self, key: str) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = %s FOR UPDATE",
                (key,),
            ).fetchone()
            new_value = "0" if row and row[0] == "1" else "1"
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                (key, new_value),
            )
        return new_value == "1"

    def list_words(self) -> list[str]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                "SELECT word FROM banned_words ORDER BY normalized"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def add_word(self, word: str) -> bool:
        cleaned, normalized = _clean_word(word)
        with self._pool.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO banned_words(word, normalized) VALUES (%s, %s) "
                "ON CONFLICT(normalized) DO NOTHING",
                (cleaned, normalized),
            )
            inserted = cursor.rowcount == 1
        return inserted

    def remove_word(self, word: str) -> bool:
        normalized = normalize_word(word)
        with self._pool.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM banned_words WHERE normalized = %s",
                (normalized,),
            )
            removed = cursor.rowcount == 1
        return removed

    def register_offense(self, user_id: int) -> int:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO offenses(user_id, offense_count, last_offense_at)
                VALUES (%s, 1, %s)
                ON CONFLICT(user_id) DO UPDATE SET
                    offense_count = offenses.offense_count + 1,
                    last_offense_at = EXCLUDED.last_offense_at
                RETURNING offense_count
                """,
                (user_id, datetime.now(timezone.utc)),
            ).fetchone()
        if not row:
            raise RuntimeError("Impossible d’enregistrer la récidive PostgreSQL.")
        return int(row[0])

    def claim_event(self, event_key: str) -> bool:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO sent_events(event_key, created_at) VALUES (%s, %s) "
                "ON CONFLICT(event_key) DO NOTHING",
                (event_key, datetime.now(timezone.utc)),
            )
            claimed = cursor.rowcount == 1
        return claimed

    def release_event(self, event_key: str) -> None:
        with self._pool.connection() as connection:
            connection.execute("DELETE FROM sent_events WHERE event_key = %s", (event_key,))

    def prune_events(self, keep_days: int = 14) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        with self._pool.connection() as connection:
            connection.execute("DELETE FROM sent_events WHERE created_at < %s", (cutoff,))


def _clean_word(word: str) -> tuple[str, str]:
    cleaned = " ".join(word.strip().split())
    normalized = normalize_word(cleaned)
    if not normalized:
        raise ValueError("Le mot ne peut pas être vide.")
    return cleaned, normalized

