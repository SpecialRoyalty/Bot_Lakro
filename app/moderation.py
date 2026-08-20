from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any


LINK_ENTITY_TYPES = {"url", "text_link", "email"}
LINK_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.|tg://|t\.me/|telegram\.me/|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?)"
)


def extract_text(message: dict[str, Any]) -> str:
    return str(message.get("text") or message.get("caption") or "")


def contains_link(message: dict[str, Any]) -> bool:
    entities = list(message.get("entities") or []) + list(message.get("caption_entities") or [])
    if any(entity.get("type") in LINK_ENTITY_TYPES for entity in entities):
        return True
    return bool(LINK_PATTERN.search(extract_text(message)))


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def is_popular_justice_signal(text: str) -> bool:
    """Recognize only a standalone pedo/pdo report, accents and case ignored."""
    decomposed = unicodedata.normalize("NFKD", text).casefold()
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    compact = re.sub(r"[^a-z0-9]+", "", without_accents)
    return compact in {"pedo", "pdo"}


def find_banned_word(text: str, banned_words: Iterable[str]) -> str | None:
    normalized_text = _normalize(text)
    for word in banned_words:
        normalized_word = _normalize(word)
        if not normalized_word:
            continue
        pattern = rf"(?<!\w){re.escape(normalized_word).replace(r'\ ', r'\s+')}(?!\w)"
        if re.search(pattern, normalized_text, flags=re.UNICODE):
            return word
    return None


def is_story(message: dict[str, Any]) -> bool:
    return message.get("story") is not None


def is_membership_event(message: dict[str, Any]) -> bool:
    return bool(message.get("new_chat_members") or message.get("left_chat_member"))


def is_forward(message: dict[str, Any]) -> bool:
    return message.get("forward_origin") is not None or bool(message.get("is_automatic_forward"))


def is_allowed_open_content(message: dict[str, Any]) -> bool:
    return bool(message.get("text") is not None or message.get("photo") or message.get("video"))


def sender_user_id(message: dict[str, Any]) -> int | None:
    sender = message.get("from") or {}
    value = sender.get("id")
    return int(value) if value is not None else None
