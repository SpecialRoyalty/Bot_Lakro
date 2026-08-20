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
    "open_time": "23:00",
    "close_time": "02:00",
    "rules_text": "",
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
                """
            )
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


class Storage:
    """Use PostgreSQL when DATABASE_URL is configured, SQLite otherwise."""

    def __init__(self, path: Path, *, database_url: str = "") -> None:
        if database_url:
            # Lazy import keeps local unit tests dependency-free while Railway's
            # Docker build installs Psycopg for production.
            from app.postgres_storage import PostgresStorage

            self._backend = PostgresStorage(database_url)
        else:
            self._backend = SQLiteStorage(path)

    @property
    def backend_name(self) -> str:
        return str(self._backend.backend_name)

    def __getattr__(self, name: str):
        return getattr(self._backend, name)
