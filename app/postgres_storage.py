from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from app.storage import DEFAULT_SETTINGS, normalize_word


class PostgresStorage:
    """Thread-safe PostgreSQL storage using one lazy persistent connection."""

    backend_name = "postgresql"

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Le paquet psycopg est requis lorsque DATABASE_URL est configuré."
            ) from exc

        self._psycopg = psycopg
        self._database_url = database_url
        self._lock = threading.RLock()
        self._connection: Any | None = None

    def _ensure_connection(self) -> Any:
        if self._connection is None or self._connection.closed:
            self._connection = self._psycopg.connect(
                self._database_url,
                connect_timeout=10,
                application_name="telegram-group-manager",
            )
        return self._connection

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with self._lock:
            connection = self._ensure_connection()
            try:
                with connection.transaction():
                    yield connection
            except Exception as exc:
                connection_error = isinstance(
                    exc,
                    (self._psycopg.OperationalError, self._psycopg.InterfaceError),
                )
                if connection_error or connection.closed:
                    try:
                        connection.close()
                    finally:
                        self._connection = None
                raise

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
            """
            CREATE TABLE IF NOT EXISTS trusted_offenses (
                user_id BIGINT PRIMARY KEY,
                offense_count INTEGER NOT NULL,
                last_offense_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS media_group_messages (
                media_group_id TEXT NOT NULL,
                message_id BIGINT NOT NULL,
                author_user_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(media_group_id, message_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS popular_justice_votes (
                target_key TEXT NOT NULL,
                voter_id BIGINT NOT NULL,
                report_message_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(target_key, voter_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS popular_justice_cases (
                target_key TEXT PRIMARY KEY,
                target_user_id BIGINT NOT NULL,
                resolved_at TIMESTAMPTZ NOT NULL
            )
            """,
        )
        with self._transaction() as connection:
            for statement in statements:
                connection.execute(statement)
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT(key) DO NOTHING",
                    list(DEFAULT_SETTINGS.items()),
                )

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def get(self, key: str, default: str = "") -> str:
        with self._transaction() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
        return str(row[0]) if row else default

    def get_all_settings(self) -> dict[str, str]:
        with self._transaction() as connection:
            rows = connection.execute("SELECT key, value FROM settings").fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def set(self, key: str, value: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )

    def get_bool(self, key: str) -> bool:
        return self.get(key) == "1"

    def toggle(self, key: str) -> bool:
        with self._transaction() as connection:
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
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT word FROM banned_words ORDER BY normalized"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def add_word(self, word: str) -> bool:
        cleaned, normalized = _clean_word(word)
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO banned_words(word, normalized) VALUES (%s, %s) "
                "ON CONFLICT(normalized) DO NOTHING",
                (cleaned, normalized),
            )
            inserted = cursor.rowcount == 1
        return inserted

    def remove_word(self, word: str) -> bool:
        normalized = normalize_word(word)
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM banned_words WHERE normalized = %s",
                (normalized,),
            )
            removed = cursor.rowcount == 1
        return removed

    def register_offense(self, user_id: int) -> int:
        with self._transaction() as connection:
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

    def register_trusted_offense(self, user_id: int) -> int:
        with self._transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO trusted_offenses(user_id, offense_count, last_offense_at)
                VALUES (%s, 1, %s)
                ON CONFLICT(user_id) DO UPDATE SET
                    offense_count = trusted_offenses.offense_count + 1,
                    last_offense_at = EXCLUDED.last_offense_at
                RETURNING offense_count
                """,
                (user_id, datetime.now(timezone.utc)),
            ).fetchone()
        if not row:
            raise RuntimeError("Impossible d’enregistrer la sanction trusted PostgreSQL.")
        return int(row[0])

    def record_media_group_message(self, media_group_id: str, message_id: int, author_user_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO media_group_messages"
                "(media_group_id, message_id, author_user_id, created_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT(media_group_id, message_id) DO NOTHING",
                (media_group_id, message_id, author_user_id, datetime.now(timezone.utc)),
            )

    def list_media_group_message_ids(self, media_group_id: str) -> list[int]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT message_id FROM media_group_messages WHERE media_group_id = %s ORDER BY message_id",
                (media_group_id,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def register_popular_vote(
        self,
        target_key: str,
        voter_id: int,
        report_message_id: int,
    ) -> tuple[int, bool, bool]:
        with self._transaction() as connection:
            resolved = connection.execute(
                "SELECT 1 FROM popular_justice_cases WHERE target_key = %s",
                (target_key,),
            ).fetchone()
            if resolved:
                return 0, False, True
            cursor = connection.execute(
                "INSERT INTO popular_justice_votes"
                "(target_key, voter_id, report_message_id, created_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT(target_key, voter_id) DO NOTHING",
                (target_key, voter_id, report_message_id, datetime.now(timezone.utc)),
            )
            row = connection.execute(
                "SELECT COUNT(*) FROM popular_justice_votes WHERE target_key = %s",
                (target_key,),
            ).fetchone()
        return int(row[0]), cursor.rowcount == 1, False

    def popular_vote_message_ids(self, target_key: str) -> list[int]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT report_message_id FROM popular_justice_votes "
                "WHERE target_key = %s ORDER BY created_at, report_message_id",
                (target_key,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def all_popular_vote_message_ids(self) -> list[int]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT report_message_id FROM popular_justice_votes ORDER BY created_at, report_message_id"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def claim_popular_case(self, target_key: str, target_user_id: int) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO popular_justice_cases(target_key, target_user_id, resolved_at) "
                "VALUES (%s, %s, %s) ON CONFLICT(target_key) DO NOTHING",
                (target_key, target_user_id, datetime.now(timezone.utc)),
            )
        return cursor.rowcount == 1

    def release_popular_case(self, target_key: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM popular_justice_cases WHERE target_key = %s",
                (target_key,),
            )

    def clear_popular_votes(self, target_key: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM popular_justice_votes WHERE target_key = %s",
                (target_key,),
            )

    def clear_all_popular_votes(self) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM popular_justice_votes")

    def claim_event(self, event_key: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO sent_events(event_key, created_at) VALUES (%s, %s) "
                "ON CONFLICT(event_key) DO NOTHING",
                (event_key, datetime.now(timezone.utc)),
            )
            claimed = cursor.rowcount == 1
        return claimed

    def release_event(self, event_key: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM sent_events WHERE event_key = %s", (event_key,))

    def prune_events(self, keep_days: int = 14) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        with self._transaction() as connection:
            connection.execute("DELETE FROM sent_events WHERE created_at < %s", (cutoff,))
            connection.execute("DELETE FROM media_group_messages WHERE created_at < %s", (cutoff,))
            connection.execute("DELETE FROM popular_justice_votes WHERE created_at < %s", (cutoff,))
            case_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            connection.execute(
                "DELETE FROM popular_justice_cases WHERE resolved_at < %s",
                (case_cutoff,),
            )


def _clean_word(word: str) -> tuple[str, str]:
    cleaned = " ".join(word.strip().split())
    normalized = normalize_word(cleaned)
    if not normalized:
        raise ValueError("Le mot ne peut pas être vide.")
    return cleaned, normalized
