from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when a required environment variable is missing or invalid."""


def _parse_admin_ids(raw: str) -> frozenset[int]:
    values = [part for part in re.split(r"[\s,;]+", raw.strip()) if part]
    if not values:
        raise ConfigError("ADMIN_IDS doit contenir au moins un identifiant Telegram.")
    try:
        return frozenset(int(value) for value in values)
    except ValueError as exc:
        raise ConfigError("ADMIN_IDS doit contenir uniquement des nombres séparés par des virgules.") from exc


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    target_chat_id: int
    admin_ids: frozenset[int]
    group_invite_link: str
    timezone: ZoneInfo
    timezone_name: str
    database_url: str
    database_path: Path
    port: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("BOT_TOKEN est obligatoire.")

        raw_chat_id = os.getenv("TARGET_CHAT_ID", "").strip()
        if not raw_chat_id:
            raise ConfigError("TARGET_CHAT_ID est obligatoire.")
        try:
            chat_id = int(raw_chat_id)
        except ValueError as exc:
            raise ConfigError("TARGET_CHAT_ID doit être un nombre, par exemple -1001234567890.") from exc

        timezone_name = os.getenv("TZ", "Europe/Paris").strip() or "Europe/Paris"
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"Fuseau horaire inconnu : {timezone_name}") from exc

        try:
            port = int(os.getenv("PORT", "8080"))
        except ValueError as exc:
            raise ConfigError("PORT doit être un nombre.") from exc

        database_url = os.getenv("DATABASE_URL", "").strip()
        if len(database_url) >= 2 and database_url[0] == database_url[-1] and database_url[0] in {'"', "'"}:
            database_url = database_url[1:-1].strip()
        if database_url and not database_url.startswith(("postgresql://", "postgres://")):
            raise ConfigError("DATABASE_URL doit être une URL PostgreSQL fournie par Railway.")

        return cls(
            bot_token=token,
            target_chat_id=chat_id,
            admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
            group_invite_link=os.getenv("GROUP_INVITE_LINK", "").strip(),
            timezone=timezone,
            timezone_name=timezone_name,
            database_url=database_url,
            database_path=Path(os.getenv("DATABASE_PATH", "/data/bot.sqlite3")),
            port=port,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
