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
            CREATE TABLE IF NOT EXISTS unauthorized_command_offenses (
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
            """
            CREATE TABLE IF NOT EXISTS referral_profiles (
                inviter_id BIGINT PRIMARY KEY,
                invite_link TEXT NOT NULL UNIQUE,
                chat_id BIGINT,
                confirmed_count INTEGER NOT NULL DEFAULT 0,
                rewarded_at TIMESTAMPTZ
            )
            """,
            """
            ALTER TABLE referral_profiles ADD COLUMN IF NOT EXISTS chat_id BIGINT
            """,
            """
            CREATE TABLE IF NOT EXISTS referral_link_refresh_queue (
                inviter_id BIGINT PRIMARY KEY,
                next_attempt_at TIMESTAMPTZ NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS referral_pending (
                candidate_user_id BIGINT PRIMARY KEY,
                inviter_id BIGINT NOT NULL,
                invite_link TEXT NOT NULL,
                requested_at TIMESTAMPTZ NOT NULL,
                joined_at TIMESTAMPTZ
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS referral_conversions (
                candidate_user_id BIGINT PRIMARY KEY,
                inviter_id BIGINT NOT NULL,
                confirmed_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS session_messages (
                session_key TEXT NOT NULL,
                message_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(session_key, message_id)
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

    def register_unauthorized_command_offense(self, user_id: int) -> int:
        with self._transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO unauthorized_command_offenses(user_id, offense_count, last_offense_at)
                VALUES (%s, 1, %s)
                ON CONFLICT(user_id) DO UPDATE SET
                    offense_count = unauthorized_command_offenses.offense_count + 1,
                    last_offense_at = EXCLUDED.last_offense_at
                RETURNING offense_count
                """,
                (user_id, datetime.now(timezone.utc)),
            ).fetchone()
        if not row:
            raise RuntimeError("Impossible d’enregistrer l’abus de commande PostgreSQL.")
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

    def get_referral_profile(self, inviter_id: int) -> tuple[str, int, bool, int | None] | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT invite_link, confirmed_count, rewarded_at, chat_id "
                "FROM referral_profiles WHERE inviter_id = %s",
                (inviter_id,),
            ).fetchone()
        if not row:
            return None
        return str(row[0]), int(row[1]), row[2] is not None, int(row[3]) if row[3] is not None else None

    def save_referral_link(self, inviter_id: int, invite_link: str, chat_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM referral_pending WHERE inviter_id = %s AND invite_link <> %s",
                (inviter_id, invite_link),
            )
            connection.execute(
                "INSERT INTO referral_profiles(inviter_id, invite_link, chat_id, confirmed_count) "
                "VALUES (%s, %s, %s, 0) ON CONFLICT(inviter_id) DO UPDATE SET "
                "invite_link = EXCLUDED.invite_link, chat_id = EXCLUDED.chat_id",
                (inviter_id, invite_link, chat_id),
            )
            connection.execute(
                "DELETE FROM referral_link_refresh_queue WHERE inviter_id = %s",
                (inviter_id,),
            )

    def enqueue_all_referral_link_refreshes(self) -> int:
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT inviter_id FROM referral_profiles ORDER BY inviter_id"
            ).fetchall()
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO referral_link_refresh_queue(inviter_id, next_attempt_at, attempts) "
                    "VALUES (%s, %s, 0) ON CONFLICT(inviter_id) DO UPDATE SET "
                    "next_attempt_at = EXCLUDED.next_attempt_at, attempts = 0",
                    [(int(row[0]), now) for row in rows],
                )
        return len(rows)

    def enqueue_stale_referral_link_refreshes(self, chat_id: int) -> int:
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT inviter_id FROM referral_profiles "
                "WHERE chat_id IS NULL OR chat_id <> %s ORDER BY inviter_id",
                (chat_id,),
            ).fetchall()
            inviter_ids = [int(row[0]) for row in rows]
            if inviter_ids:
                connection.execute(
                    "DELETE FROM referral_pending WHERE inviter_id = ANY(%s)",
                    (inviter_ids,),
                )
                with connection.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO referral_link_refresh_queue(inviter_id, next_attempt_at, attempts) "
                        "VALUES (%s, %s, 0) ON CONFLICT(inviter_id) DO UPDATE SET "
                        "next_attempt_at = EXCLUDED.next_attempt_at, attempts = 0",
                        [(inviter_id, now) for inviter_id in inviter_ids],
                    )
        return len(inviter_ids)

    def due_referral_link_refreshes(self, limit: int = 10) -> list[int]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT inviter_id FROM referral_link_refresh_queue "
                "WHERE next_attempt_at <= %s ORDER BY next_attempt_at, inviter_id LIMIT %s",
                (datetime.now(timezone.utc), limit),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def defer_referral_link_refresh(self, inviter_id: int, delay_seconds: int = 300) -> None:
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE referral_link_refresh_queue SET attempts = attempts + 1, next_attempt_at = %s "
                "WHERE inviter_id = %s",
                (next_attempt, inviter_id),
            )

    def pending_referral_link_refresh_count(self) -> int:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM referral_link_refresh_queue"
            ).fetchone()
        return int(row[0]) if row else 0

    def record_referral_request(
        self,
        candidate_user_id: int,
        invite_link: str,
        requested_at: datetime,
        chat_id: int,
    ) -> bool:
        with self._transaction() as connection:
            profile = connection.execute(
                "SELECT inviter_id FROM referral_profiles WHERE invite_link = %s AND chat_id = %s",
                (invite_link, chat_id),
            ).fetchone()
            if not profile:
                return False
            inviter_id = int(profile[0])
            if inviter_id == candidate_user_id:
                return False
            converted = connection.execute(
                "SELECT 1 FROM referral_conversions WHERE candidate_user_id = %s",
                (candidate_user_id,),
            ).fetchone()
            if converted:
                return False
            connection.execute(
                "INSERT INTO referral_pending"
                "(candidate_user_id, inviter_id, invite_link, requested_at, joined_at) "
                "VALUES (%s, %s, %s, %s, NULL) ON CONFLICT(candidate_user_id) DO UPDATE SET "
                "inviter_id = EXCLUDED.inviter_id, invite_link = EXCLUDED.invite_link, "
                "requested_at = EXCLUDED.requested_at, joined_at = NULL",
                (candidate_user_id, inviter_id, invite_link, requested_at.astimezone(timezone.utc)),
            )
        return True

    def mark_referral_joined(self, candidate_user_id: int, joined_at: datetime) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE referral_pending SET joined_at = COALESCE(joined_at, %s) "
                "WHERE candidate_user_id = %s",
                (joined_at.astimezone(timezone.utc), candidate_user_id),
            )
        return cursor.rowcount == 1

    def cancel_referral_pending(self, candidate_user_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM referral_pending WHERE candidate_user_id = %s",
                (candidate_user_id,),
            )

    def due_referrals(self, cutoff: datetime, limit: int = 50) -> list[tuple[int, int]]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT candidate_user_id, inviter_id FROM referral_pending "
                "WHERE joined_at IS NOT NULL AND joined_at <= %s ORDER BY joined_at LIMIT %s",
                (cutoff.astimezone(timezone.utc), limit),
            ).fetchall()
        return [(int(row[0]), int(row[1])) for row in rows]

    def confirm_referral(
        self,
        candidate_user_id: int,
        cutoff: datetime,
    ) -> tuple[int, int] | None:
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            pending = connection.execute(
                "SELECT inviter_id FROM referral_pending WHERE candidate_user_id = %s "
                "AND joined_at IS NOT NULL AND joined_at <= %s FOR UPDATE",
                (candidate_user_id, cutoff.astimezone(timezone.utc)),
            ).fetchone()
            if not pending:
                return None
            inviter_id = int(pending[0])
            inserted = connection.execute(
                "INSERT INTO referral_conversions(candidate_user_id, inviter_id, confirmed_at) "
                "VALUES (%s, %s, %s) ON CONFLICT(candidate_user_id) DO NOTHING",
                (candidate_user_id, inviter_id, now),
            )
            connection.execute(
                "DELETE FROM referral_pending WHERE candidate_user_id = %s",
                (candidate_user_id,),
            )
            if inserted.rowcount != 1:
                return None
            profile = connection.execute(
                "UPDATE referral_profiles SET confirmed_count = confirmed_count + 1 "
                "WHERE inviter_id = %s RETURNING confirmed_count",
                (inviter_id,),
            ).fetchone()
        if not profile:
            raise RuntimeError("Profil de parrainage PostgreSQL introuvable.")
        return inviter_id, int(profile[0])

    def due_referral_rewards(self, required_count: int, limit: int = 50) -> list[tuple[int, int]]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT inviter_id, confirmed_count FROM referral_profiles "
                "WHERE confirmed_count >= %s AND rewarded_at IS NULL "
                "ORDER BY confirmed_count DESC LIMIT %s",
                (required_count, limit),
            ).fetchall()
        return [(int(row[0]), int(row[1])) for row in rows]

    def mark_referral_rewarded(self, inviter_id: int) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE referral_profiles SET rewarded_at = %s "
                "WHERE inviter_id = %s AND rewarded_at IS NULL",
                (datetime.now(timezone.utc), inviter_id),
            )
        return cursor.rowcount == 1

    def record_session_message(self, session_key: str, message_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO session_messages(session_key, message_id, created_at) "
                "VALUES (%s, %s, %s) ON CONFLICT(session_key, message_id) DO NOTHING",
                (session_key, message_id, datetime.now(timezone.utc)),
            )

    def list_session_keys(self) -> list[str]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT session_key FROM session_messages "
                "GROUP BY session_key ORDER BY MIN(created_at)"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def list_session_message_ids(self, session_key: str) -> list[int]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT message_id FROM session_messages "
                "WHERE session_key = %s ORDER BY message_id",
                (session_key,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def forget_session_message_ids(self, session_key: str, message_ids: list[int]) -> None:
        unique_ids = sorted(set(message_ids))
        if not unique_ids:
            return
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM session_messages WHERE session_key = %s "
                "AND message_id = ANY(%s)",
                (session_key, unique_ids),
            )

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
            connection.execute("DELETE FROM referral_pending WHERE requested_at < %s", (cutoff,))
            connection.execute("DELETE FROM session_messages WHERE created_at < %s", (cutoff,))
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
