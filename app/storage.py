from __future__ import annotations

import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_SETTINGS = {
    "auto_open": "1",
    "links_forbidden": "1",
    "forwards_forbidden": "1",
    "popular_justice": "1",
    "popular_threshold": "5",
    "open_time": "23:00",
    "close_time": "02:00",
    "rules_text": "",
    "invitation_ad_text": "",
    "invitation_ad_photo_id": "",
    "referral_reward_link": "",
    "last_countdown_message_id": "",
}


def normalize_word(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().strip().split())


class SQLiteStorage:
    backend_name = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")

    def initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS banned_words (
                    word TEXT NOT NULL,
                    normalized TEXT PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS offenses (
                    user_id INTEGER PRIMARY KEY,
                    offense_count INTEGER NOT NULL,
                    last_offense_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sent_events (
                    event_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trusted_offenses (
                    user_id INTEGER PRIMARY KEY,
                    offense_count INTEGER NOT NULL,
                    last_offense_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS unauthorized_command_offenses (
                    user_id INTEGER PRIMARY KEY,
                    offense_count INTEGER NOT NULL,
                    last_offense_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_group_messages (
                    media_group_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    author_user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(media_group_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS popular_justice_votes (
                    target_key TEXT NOT NULL,
                    voter_id INTEGER NOT NULL,
                    report_message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(target_key, voter_id)
                );

                CREATE TABLE IF NOT EXISTS popular_justice_cases (
                    target_key TEXT PRIMARY KEY,
                    target_user_id INTEGER NOT NULL,
                    resolved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS referral_profiles (
                    inviter_id INTEGER PRIMARY KEY,
                    invite_link TEXT NOT NULL UNIQUE,
                    chat_id INTEGER,
                    confirmed_count INTEGER NOT NULL DEFAULT 0,
                    rewarded_at TEXT
                );

                CREATE TABLE IF NOT EXISTS referral_link_refresh_queue (
                    inviter_id INTEGER PRIMARY KEY,
                    next_attempt_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS referral_pending (
                    candidate_user_id INTEGER PRIMARY KEY,
                    inviter_id INTEGER NOT NULL,
                    invite_link TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    joined_at TEXT
                );

                CREATE TABLE IF NOT EXISTS referral_conversions (
                    candidate_user_id INTEGER PRIMARY KEY,
                    inviter_id INTEGER NOT NULL,
                    confirmed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_messages (
                    session_key TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_key, message_id)
                );
                """
            )
            profile_columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(referral_profiles)").fetchall()
            }
            if "chat_id" not in profile_columns:
                self._connection.execute("ALTER TABLE referral_profiles ADD COLUMN chat_id INTEGER")
            self._connection.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                DEFAULT_SETTINGS.items(),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def get_all_settings(self) -> dict[str, str]:
        with self._lock:
            rows = self._connection.execute("SELECT key, value FROM settings").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_bool(self, key: str) -> bool:
        return self.get(key) == "1"

    def toggle(self, key: str) -> bool:
        with self._lock, self._connection:
            row = self._connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            new_value = "0" if row and row["value"] == "1" else "1"
            self._connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, new_value),
            )
        return new_value == "1"

    def list_words(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT word FROM banned_words ORDER BY normalized COLLATE NOCASE"
            ).fetchall()
        return [str(row["word"]) for row in rows]

    def add_word(self, word: str) -> bool:
        cleaned = " ".join(word.strip().split())
        normalized = normalize_word(cleaned)
        if not normalized:
            raise ValueError("Le mot ne peut pas être vide.")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO banned_words(word, normalized) VALUES (?, ?)",
                (cleaned, normalized),
            )
        return cursor.rowcount == 1

    def remove_word(self, word: str) -> bool:
        normalized = normalize_word(word)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM banned_words WHERE normalized = ?",
                (normalized,),
            )
        return cursor.rowcount == 1

    def register_offense(self, user_id: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT offense_count FROM offenses WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            count = int(row["offense_count"]) + 1 if row else 1
            self._connection.execute(
                "INSERT INTO offenses(user_id, offense_count, last_offense_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "offense_count = excluded.offense_count, last_offense_at = excluded.last_offense_at",
                (user_id, count, now),
            )
        return count

    def register_trusted_offense(self, user_id: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT offense_count FROM trusted_offenses WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            count = int(row["offense_count"]) + 1 if row else 1
            self._connection.execute(
                "INSERT INTO trusted_offenses(user_id, offense_count, last_offense_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "offense_count = excluded.offense_count, last_offense_at = excluded.last_offense_at",
                (user_id, count, now),
            )
        return count

    def register_unauthorized_command_offense(self, user_id: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT offense_count FROM unauthorized_command_offenses WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            count = int(row["offense_count"]) + 1 if row else 1
            self._connection.execute(
                "INSERT INTO unauthorized_command_offenses"
                "(user_id, offense_count, last_offense_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "offense_count = excluded.offense_count, last_offense_at = excluded.last_offense_at",
                (user_id, count, now),
            )
        return count

    def record_media_group_message(self, media_group_id: str, message_id: int, author_user_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO media_group_messages"
                "(media_group_id, message_id, author_user_id, created_at) VALUES (?, ?, ?, ?)",
                (media_group_id, message_id, author_user_id, datetime.now(timezone.utc).isoformat()),
            )

    def list_media_group_message_ids(self, media_group_id: str) -> list[int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT message_id FROM media_group_messages WHERE media_group_id = ? ORDER BY message_id",
                (media_group_id,),
            ).fetchall()
        return [int(row["message_id"]) for row in rows]

    def register_popular_vote(
        self,
        target_key: str,
        voter_id: int,
        report_message_id: int,
    ) -> tuple[int, bool, bool]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            resolved = self._connection.execute(
                "SELECT 1 FROM popular_justice_cases WHERE target_key = ?",
                (target_key,),
            ).fetchone()
            if resolved:
                return 0, False, True
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO popular_justice_votes"
                "(target_key, voter_id, report_message_id, created_at) VALUES (?, ?, ?, ?)",
                (target_key, voter_id, report_message_id, now),
            )
            row = self._connection.execute(
                "SELECT COUNT(*) AS vote_count FROM popular_justice_votes WHERE target_key = ?",
                (target_key,),
            ).fetchone()
        return int(row["vote_count"]), cursor.rowcount == 1, False

    def popular_vote_message_ids(self, target_key: str) -> list[int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT report_message_id FROM popular_justice_votes "
                "WHERE target_key = ? ORDER BY created_at, report_message_id",
                (target_key,),
            ).fetchall()
        return [int(row["report_message_id"]) for row in rows]

    def all_popular_vote_message_ids(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT report_message_id FROM popular_justice_votes ORDER BY created_at, report_message_id"
            ).fetchall()
        return [int(row["report_message_id"]) for row in rows]

    def claim_popular_case(self, target_key: str, target_user_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO popular_justice_cases(target_key, target_user_id, resolved_at) "
                "VALUES (?, ?, ?)",
                (target_key, target_user_id, datetime.now(timezone.utc).isoformat()),
            )
        return cursor.rowcount == 1

    def release_popular_case(self, target_key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM popular_justice_cases WHERE target_key = ?",
                (target_key,),
            )

    def clear_popular_votes(self, target_key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM popular_justice_votes WHERE target_key = ?",
                (target_key,),
            )

    def clear_all_popular_votes(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM popular_justice_votes")

    def get_referral_profile(self, inviter_id: int) -> tuple[str, int, bool, int | None] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT invite_link, confirmed_count, rewarded_at, chat_id "
                "FROM referral_profiles WHERE inviter_id = ?",
                (inviter_id,),
            ).fetchone()
        if not row:
            return None
        profile_chat_id = int(row["chat_id"]) if row["chat_id"] is not None else None
        return (
            str(row["invite_link"]),
            int(row["confirmed_count"]),
            row["rewarded_at"] is not None,
            profile_chat_id,
        )

    def save_referral_link(self, inviter_id: int, invite_link: str, chat_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM referral_pending WHERE inviter_id = ? AND invite_link <> ?",
                (inviter_id, invite_link),
            )
            self._connection.execute(
                "INSERT INTO referral_profiles(inviter_id, invite_link, chat_id, confirmed_count) "
                "VALUES (?, ?, ?, 0) ON CONFLICT(inviter_id) DO UPDATE SET "
                "invite_link = excluded.invite_link, chat_id = excluded.chat_id",
                (inviter_id, invite_link, chat_id),
            )
            self._connection.execute(
                "DELETE FROM referral_link_refresh_queue WHERE inviter_id = ?",
                (inviter_id,),
            )

    def enqueue_all_referral_link_refreshes(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT inviter_id FROM referral_profiles ORDER BY inviter_id"
            ).fetchall()
            self._connection.executemany(
                "INSERT INTO referral_link_refresh_queue(inviter_id, next_attempt_at, attempts) "
                "VALUES (?, ?, 0) ON CONFLICT(inviter_id) DO UPDATE SET "
                "next_attempt_at = excluded.next_attempt_at, attempts = 0",
                [(int(row["inviter_id"]), now) for row in rows],
            )
        return len(rows)

    def enqueue_stale_referral_link_refreshes(self, chat_id: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT inviter_id FROM referral_profiles "
                "WHERE chat_id IS NULL OR chat_id <> ? ORDER BY inviter_id",
                (chat_id,),
            ).fetchall()
            inviter_ids = [int(row["inviter_id"]) for row in rows]
            if inviter_ids:
                placeholders = ",".join("?" for _ in inviter_ids)
                self._connection.execute(
                    f"DELETE FROM referral_pending WHERE inviter_id IN ({placeholders})",
                    inviter_ids,
                )
                self._connection.executemany(
                    "INSERT INTO referral_link_refresh_queue(inviter_id, next_attempt_at, attempts) "
                    "VALUES (?, ?, 0) ON CONFLICT(inviter_id) DO UPDATE SET "
                    "next_attempt_at = excluded.next_attempt_at, attempts = 0",
                    [(inviter_id, now) for inviter_id in inviter_ids],
                )
        return len(inviter_ids)

    def due_referral_link_refreshes(self, limit: int = 10) -> list[int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT inviter_id FROM referral_link_refresh_queue "
                "WHERE next_attempt_at <= ? ORDER BY next_attempt_at, inviter_id LIMIT ?",
                (datetime.now(timezone.utc).isoformat(), limit),
            ).fetchall()
        return [int(row["inviter_id"]) for row in rows]

    def defer_referral_link_refresh(self, inviter_id: int, delay_seconds: int = 300) -> None:
        next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE referral_link_refresh_queue SET attempts = attempts + 1, next_attempt_at = ? "
                "WHERE inviter_id = ?",
                (next_attempt, inviter_id),
            )

    def pending_referral_link_refresh_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS pending_count FROM referral_link_refresh_queue"
            ).fetchone()
        return int(row["pending_count"])

    def record_referral_request(
        self,
        candidate_user_id: int,
        invite_link: str,
        requested_at: datetime,
        chat_id: int,
    ) -> bool:
        with self._lock, self._connection:
            profile = self._connection.execute(
                "SELECT inviter_id FROM referral_profiles WHERE invite_link = ? AND chat_id = ?",
                (invite_link, chat_id),
            ).fetchone()
            if not profile:
                return False
            inviter_id = int(profile["inviter_id"])
            if inviter_id == candidate_user_id:
                return False
            converted = self._connection.execute(
                "SELECT 1 FROM referral_conversions WHERE candidate_user_id = ?",
                (candidate_user_id,),
            ).fetchone()
            if converted:
                return False
            self._connection.execute(
                "INSERT INTO referral_pending"
                "(candidate_user_id, inviter_id, invite_link, requested_at, joined_at) "
                "VALUES (?, ?, ?, ?, NULL) ON CONFLICT(candidate_user_id) DO UPDATE SET "
                "inviter_id = excluded.inviter_id, invite_link = excluded.invite_link, "
                "requested_at = excluded.requested_at, joined_at = NULL",
                (candidate_user_id, inviter_id, invite_link, requested_at.astimezone(timezone.utc).isoformat()),
            )
        return True

    def mark_referral_joined(self, candidate_user_id: int, joined_at: datetime) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE referral_pending SET joined_at = COALESCE(joined_at, ?) "
                "WHERE candidate_user_id = ?",
                (joined_at.astimezone(timezone.utc).isoformat(), candidate_user_id),
            )
        return cursor.rowcount == 1

    def cancel_referral_pending(self, candidate_user_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM referral_pending WHERE candidate_user_id = ?",
                (candidate_user_id,),
            )

    def due_referrals(self, cutoff: datetime, limit: int = 50) -> list[tuple[int, int]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT candidate_user_id, inviter_id FROM referral_pending "
                "WHERE joined_at IS NOT NULL AND joined_at <= ? ORDER BY joined_at LIMIT ?",
                (cutoff.astimezone(timezone.utc).isoformat(), limit),
            ).fetchall()
        return [(int(row["candidate_user_id"]), int(row["inviter_id"])) for row in rows]

    def confirm_referral(
        self,
        candidate_user_id: int,
        cutoff: datetime,
    ) -> tuple[int, int] | None:
        cutoff_value = cutoff.astimezone(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            pending = self._connection.execute(
                "SELECT inviter_id FROM referral_pending WHERE candidate_user_id = ? "
                "AND joined_at IS NOT NULL AND joined_at <= ?",
                (candidate_user_id, cutoff_value),
            ).fetchone()
            if not pending:
                return None
            inviter_id = int(pending["inviter_id"])
            inserted = self._connection.execute(
                "INSERT OR IGNORE INTO referral_conversions(candidate_user_id, inviter_id, confirmed_at) "
                "VALUES (?, ?, ?)",
                (candidate_user_id, inviter_id, now),
            )
            self._connection.execute(
                "DELETE FROM referral_pending WHERE candidate_user_id = ?",
                (candidate_user_id,),
            )
            if inserted.rowcount != 1:
                return None
            self._connection.execute(
                "UPDATE referral_profiles SET confirmed_count = confirmed_count + 1 WHERE inviter_id = ?",
                (inviter_id,),
            )
            profile = self._connection.execute(
                "SELECT confirmed_count FROM referral_profiles WHERE inviter_id = ?",
                (inviter_id,),
            ).fetchone()
        if not profile:
            raise RuntimeError("Profil de parrainage SQLite introuvable.")
        return inviter_id, int(profile["confirmed_count"])

    def due_referral_rewards(self, required_count: int, limit: int = 50) -> list[tuple[int, int]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT inviter_id, confirmed_count FROM referral_profiles "
                "WHERE confirmed_count >= ? AND rewarded_at IS NULL "
                "ORDER BY confirmed_count DESC LIMIT ?",
                (required_count, limit),
            ).fetchall()
        return [(int(row["inviter_id"]), int(row["confirmed_count"])) for row in rows]

    def mark_referral_rewarded(self, inviter_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE referral_profiles SET rewarded_at = ? "
                "WHERE inviter_id = ? AND rewarded_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), inviter_id),
            )
        return cursor.rowcount == 1

    def record_session_message(self, session_key: str, message_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO session_messages(session_key, message_id, created_at) "
                "VALUES (?, ?, ?)",
                (session_key, message_id, datetime.now(timezone.utc).isoformat()),
            )

    def list_session_keys(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT session_key FROM session_messages "
                "GROUP BY session_key ORDER BY MIN(created_at)"
            ).fetchall()
        return [str(row["session_key"]) for row in rows]

    def list_session_message_ids(self, session_key: str) -> list[int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT message_id FROM session_messages "
                "WHERE session_key = ? ORDER BY message_id",
                (session_key,),
            ).fetchall()
        return [int(row["message_id"]) for row in rows]

    def forget_session_message_ids(self, session_key: str, message_ids: list[int]) -> None:
        unique_ids = sorted(set(message_ids))
        if not unique_ids:
            return
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock, self._connection:
            self._connection.execute(
                f"DELETE FROM session_messages WHERE session_key = ? "
                f"AND message_id IN ({placeholders})",
                (session_key, *unique_ids),
            )

    def claim_event(self, event_key: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO sent_events(event_key, created_at) VALUES (?, ?)",
                (event_key, datetime.now(timezone.utc).isoformat()),
            )
        return cursor.rowcount == 1

    def release_event(self, event_key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM sent_events WHERE event_key = ?", (event_key,))

    def prune_events(self, keep_days: int = 14) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM sent_events WHERE created_at < ?", (cutoff,))
            self._connection.execute("DELETE FROM media_group_messages WHERE created_at < ?", (cutoff,))
            self._connection.execute("DELETE FROM popular_justice_votes WHERE created_at < ?", (cutoff,))
            self._connection.execute("DELETE FROM referral_pending WHERE requested_at < ?", (cutoff,))
            self._connection.execute("DELETE FROM session_messages WHERE created_at < ?", (cutoff,))
            case_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            self._connection.execute(
                "DELETE FROM popular_justice_cases WHERE resolved_at < ?",
                (case_cutoff,),
            )


class Storage:
    """Select a backend and cache hot moderation settings in memory."""

    def __init__(self, path: Path, *, database_url: str = "") -> None:
        if database_url:
            # Lazy import keeps local unit tests dependency-free while Railway's
            # Docker build installs Psycopg for production.
            from app.postgres_storage import PostgresStorage

            self._backend = PostgresStorage(database_url)
        else:
            self._backend = SQLiteStorage(path)
        self._cache_lock = threading.RLock()
        self._settings_cache: dict[str, str] = {}
        self._words_cache: list[str] = []

    def initialize(self) -> None:
        with self._cache_lock:
            self._backend.initialize()
            stored_settings = self._backend.get_all_settings()
            self._settings_cache = {
                key: stored_settings.get(key, default)
                for key, default in DEFAULT_SETTINGS.items()
            }
            self._words_cache = self._backend.list_words()

    def close(self) -> None:
        self._backend.close()

    @property
    def backend_name(self) -> str:
        return str(self._backend.backend_name)

    def get(self, key: str, default: str = "") -> str:
        with self._cache_lock:
            if key in self._settings_cache:
                return self._settings_cache[key]
            value = self._backend.get(key, default)
            self._settings_cache[key] = value
            return value

    def set(self, key: str, value: str) -> None:
        with self._cache_lock:
            self._backend.set(key, value)
            self._settings_cache[key] = value

    def get_bool(self, key: str) -> bool:
        return self.get(key) == "1"

    def toggle(self, key: str) -> bool:
        with self._cache_lock:
            enabled = self._backend.toggle(key)
            self._settings_cache[key] = "1" if enabled else "0"
            return enabled

    def list_words(self) -> list[str]:
        with self._cache_lock:
            return list(self._words_cache)

    def add_word(self, word: str) -> bool:
        with self._cache_lock:
            added = self._backend.add_word(word)
            if added:
                self._words_cache = self._backend.list_words()
            return added

    def remove_word(self, word: str) -> bool:
        with self._cache_lock:
            removed = self._backend.remove_word(word)
            if removed:
                self._words_cache = self._backend.list_words()
            return removed

    def __getattr__(self, name: str):
        return getattr(self._backend, name)
