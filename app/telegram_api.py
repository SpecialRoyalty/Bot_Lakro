from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, error_code: int | None, description: str) -> None:
        super().__init__(f"Telegram {method}: {error_code or '?'} {description}")
        self.method = method
        self.error_code = error_code
        self.description = description


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: int = 30) -> Any:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._base_url + method,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "telegram-group-manager/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                try:
                    data = json.loads(response.read().decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise TelegramAPIError(method, response.status, "Réponse Telegram invalide") from exc
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                data = {"error_code": exc.code, "description": "Erreur HTTP Telegram"}
            raise TelegramAPIError(method, data.get("error_code"), data.get("description", "Erreur inconnue")) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise TelegramAPIError(method, None, f"Erreur réseau : {exc}") from exc

        if not data.get("ok"):
            raise TelegramAPIError(method, data.get("error_code"), data.get("description", "Erreur inconnue"))
        return data.get("result")

    def delete_webhook(self, *, drop_pending_updates: bool = True) -> bool:
        return bool(self.call("deleteWebhook", {"drop_pending_updates": drop_pending_updates}))

    def get_me(self) -> dict[str, Any]:
        return dict(self.call("getMe"))

    def get_updates(self, offset: int | None, *, timeout: int = 45) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": [
                "message",
                "edited_message",
                "callback_query",
                "chat_join_request",
                "chat_member",
            ],
        }
        if offset is not None:
            payload["offset"] = offset
        return list(self.call("getUpdates", payload, timeout=timeout + 10))

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],
            "link_preview_options": {"is_disabled": disable_web_page_preview},
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return dict(self.call("sendMessage", payload))

    def send_photo(
        self,
        chat_id: int,
        photo: str,
        *,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
        if caption:
            payload["caption"] = caption[:1024]
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return dict(self.call("sendPhoto", payload))

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("editMessageText", payload)

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        return bool(self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))

    def delete_messages(self, chat_id: int, message_ids: list[int]) -> bool:
        if not 1 <= len(message_ids) <= 100:
            raise ValueError("deleteMessages accepte entre 1 et 100 identifiants.")
        return bool(self.call("deleteMessages", {"chat_id": chat_id, "message_ids": message_ids}))

    def answer_callback_query(self, callback_query_id: str, text: str = "", *, alert: bool = False) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": alert}
        if text:
            payload["text"] = text[:200]
        return bool(self.call("answerCallbackQuery", payload))

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return dict(self.call("getChat", {"chat_id": chat_id}))

    def get_chat_administrators(self, chat_id: int) -> list[dict[str, Any]]:
        return list(self.call("getChatAdministrators", {"chat_id": chat_id}))

    def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return dict(self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id}))

    def create_chat_invite_link(
        self,
        chat_id: int,
        *,
        name: str,
        creates_join_request: bool = True,
    ) -> dict[str, Any]:
        return dict(
            self.call(
                "createChatInviteLink",
                {
                    "chat_id": chat_id,
                    "name": name[:32],
                    "creates_join_request": creates_join_request,
                },
            )
        )

    def set_chat_permissions(self, chat_id: int, permissions: dict[str, bool]) -> bool:
        return bool(
            self.call(
                "setChatPermissions",
                {
                    "chat_id": chat_id,
                    "permissions": permissions,
                    "use_independent_chat_permissions": True,
                },
            )
        )

    def restrict_chat_member(self, chat_id: int, user_id: int, until_date: int) -> bool:
        return bool(
            self.call(
                "restrictChatMember",
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "permissions": closed_permissions(),
                    "use_independent_chat_permissions": True,
                    "until_date": until_date,
                },
            )
        )

    def ban_chat_member(self, chat_id: int, user_id: int) -> bool:
        return bool(
            self.call(
                "banChatMember",
                {"chat_id": chat_id, "user_id": user_id, "revoke_messages": True},
            )
        )

    def ban_chat_sender_chat(self, chat_id: int, sender_chat_id: int) -> bool:
        return bool(
            self.call(
                "banChatSenderChat",
                {"chat_id": chat_id, "sender_chat_id": sender_chat_id},
            )
        )


def closed_permissions() -> dict[str, bool]:
    return {
        "can_send_messages": False,
        "can_send_audios": False,
        "can_send_documents": False,
        "can_send_photos": False,
        "can_send_videos": False,
        "can_send_video_notes": False,
        "can_send_voice_notes": False,
        "can_send_polls": False,
        "can_send_other_messages": False,
        "can_add_web_page_previews": False,
        "can_react_to_messages": False,
        "can_edit_tag": False,
        "can_change_info": False,
        "can_invite_users": False,
        "can_pin_messages": False,
        "can_manage_topics": False,
    }


def open_permissions(*, links_forbidden: bool) -> dict[str, bool]:
    permissions = closed_permissions()
    permissions.update(
        {
            "can_send_messages": True,
            "can_send_photos": True,
            "can_send_videos": True,
            "can_add_web_page_previews": not links_forbidden,
        }
    )
    return permissions
