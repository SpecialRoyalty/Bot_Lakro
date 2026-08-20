from __future__ import annotations

import logging
import math
import threading
import time as time_module
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Config
from app.domain import (
    DailySchedule,
    closing_warning_threshold,
    countdown_slot,
    current_rules_slot,
    format_duration,
    format_hhmm,
    is_effectively_open,
    parse_hhmm,
)
from app.health import HealthServer
from app.keyboards import (
    cancel_keyboard,
    invitation_ad_keyboard,
    invitation_publication_keyboard,
    panel_keyboard,
    panel_text,
    rules_keyboard,
    schedule_keyboard,
    words_keyboard,
)
from app.moderation import (
    contains_link,
    extract_text,
    find_banned_word,
    is_allowed_open_content,
    is_forward,
    is_membership_event,
    is_popular_justice_signal,
    is_story,
    sender_user_id,
)
from app.policy import MessagePolicyContext, ModerationAction, decide_message_action
from app.storage import Storage
from app.telegram_api import TelegramAPI, TelegramAPIError, closed_permissions, open_permissions


LOGGER = logging.getLogger(__name__)
SCHEDULE_PRESETS = {
    "2200-0000": ("22:00", "00:00"),
    "2300-0100": ("23:00", "01:00"),
    "2300-0200": ("23:00", "02:00"),
    "0000-0300": ("00:00", "03:00"),
}
TRUSTED_COMMANDS = {"/supprime", "/pasfr", "/ban"}
POPULAR_THRESHOLD_MIN = 2
POPULAR_THRESHOLD_MAX = 50
REFERRAL_REQUIRED_COUNT = 10
REFERRAL_STAY_SECONDS = 600
REFERRAL_CHECK_INTERVAL = 30
INVITATION_AD_TEXT_MAX = 1000


class StartupError(RuntimeError):
    """Configuration error that should be shown without a noisy traceback."""


@dataclass(slots=True)
class PendingInput:
    action: str
    panel_chat_id: int
    panel_message_id: int


class GroupManagerService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.storage = Storage(config.database_path, database_url=config.database_url)
        self.api = TelegramAPI(config.bot_token)
        self.health_server = HealthServer(config.port, self.health_payload)
        self.stop_event = threading.Event()
        self._scheduler_lock = threading.RLock()
        self._admin_lock = threading.RLock()
        self._group_admin_ids: set[int] = set(config.admin_ids)
        self._pending_inputs: dict[int, PendingInput] = {}
        self._scheduler_thread: threading.Thread | None = None
        self._last_permission_signature: tuple[bool, bool] | None = None
        self._last_permission_sync = 0.0
        self._last_admin_refresh = 0.0
        self._last_prune = 0.0
        self._last_referral_check = 0.0
        self._last_update_at: datetime | None = None
        self._last_scheduler_at: datetime | None = None
        self._started_at = datetime.now(timezone.utc)
        self._bot_id: int | None = None
        self._bot_username = ""
        self._invite_link_cache = config.group_invite_link

    def health_payload(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "bot": self._bot_username or "starting",
            "timezone": self.config.timezone_name,
            "database": self.storage.backend_name,
            "started_at": self._started_at.isoformat(),
            "last_update_at": self._last_update_at.isoformat() if self._last_update_at else None,
            "last_scheduler_at": self._last_scheduler_at.isoformat() if self._last_scheduler_at else None,
        }

    def run(self) -> None:
        try:
            self.storage.initialize()
            try:
                self.api.delete_webhook(drop_pending_updates=True)
                me = self.api.get_me()
            except TelegramAPIError as exc:
                raise StartupError(
                    "Impossible d’identifier le bot. Vérifiez que BOT_TOKEN est complet et valide. "
                    f"Détail Telegram : {exc.description}"
                ) from exc

            self._bot_id = int(me["id"])
            self._bot_username = str(me.get("username") or me.get("first_name") or self._bot_id)
            self._validate_target_chat()
            try:
                admins = self._refresh_group_admins(strict=True)
                self._validate_bot_rights(admins)
            except TelegramAPIError as exc:
                raise StartupError(
                    "Impossible de lire les administrateurs du groupe. Vérifiez que le bot est déjà "
                    f"administrateur de TARGET_CHAT_ID={self.config.target_chat_id}. "
                    f"Détail Telegram : {exc.description}"
                ) from exc

            self.health_server.start()
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="group-scheduler",
                daemon=True,
            )
            self._scheduler_thread.start()
            LOGGER.info("Bot @%s démarré pour le groupe %s", self._bot_username, self.config.target_chat_id)
            self._poll_updates()
        finally:
            self.stop_event.set()
            self.health_server.stop()
            self.storage.close()

    def stop(self) -> None:
        self.stop_event.set()

    def _validate_target_chat(self) -> None:
        try:
            chat = self.api.get_chat(self.config.target_chat_id)
        except TelegramAPIError as exc:
            if exc.error_code == 400 and "chat not found" in exc.description.lower():
                raise StartupError(
                    f"Telegram ne trouve pas TARGET_CHAT_ID={self.config.target_chat_id}. "
                    "Utilisez l’identifiant numérique du supergroupe (il commence généralement par -100), "
                    f"ajoutez @{self._bot_username or 'le bot'} à ce groupe comme administrateur et vérifiez "
                    "que BOT_TOKEN appartient bien à ce même bot."
                ) from exc
            raise StartupError(
                f"Impossible d’accéder à TARGET_CHAT_ID={self.config.target_chat_id}. "
                f"Détail Telegram : {exc.description}"
            ) from exc
        if chat.get("type") not in {"group", "supergroup"}:
            raise StartupError("TARGET_CHAT_ID doit désigner un groupe ou un supergroupe Telegram.")
        if not self._invite_link_cache:
            self._invite_link_cache = str(chat.get("invite_link") or "")

    def _validate_bot_rights(self, admins: list[dict[str, Any]] | None = None) -> None:
        if self._bot_id is None:
            raise StartupError("Identité du bot indisponible.")
        if admins is None:
            admins = self.api.get_chat_administrators(self.config.target_chat_id)
        entry = next((item for item in admins if int((item.get("user") or {}).get("id", 0)) == self._bot_id), None)
        if not entry:
            raise StartupError("Le bot doit être administrateur du groupe.")
        if entry.get("status") != "creator":
            missing = [
                label
                for field, label in (
                    ("can_delete_messages", "supprimer les messages"),
                    ("can_restrict_members", "bannir et restreindre les membres"),
                    ("can_invite_users", "créer des liens et recevoir les demandes d’adhésion"),
                )
                if not entry.get(field)
            ]
            if missing:
                raise StartupError("Droits administrateur manquants pour le bot : " + ", ".join(missing) + ".")

    def _poll_updates(self) -> None:
        offset: int | None = None
        backoff = 1
        while not self.stop_event.is_set():
            try:
                updates = self.api.get_updates(offset, timeout=45)
                backoff = 1
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self._last_update_at = datetime.now(timezone.utc)
                    try:
                        self._handle_update(update)
                    except Exception:
                        LOGGER.exception("Erreur pendant le traitement de l’update %s", update.get("update_id"))
            except TelegramAPIError as exc:
                LOGGER.warning("Long polling interrompu : %s", exc)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30)

    def _handle_update(self, update: dict[str, Any]) -> None:
        join_request = update.get("chat_join_request")
        if join_request:
            self._handle_chat_join_request(join_request)
            return

        chat_member = update.get("chat_member")
        if chat_member:
            self._handle_chat_member_update(chat_member)
            return

        callback_query = update.get("callback_query")
        if callback_query:
            self._handle_callback(callback_query)
            return

        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        if chat.get("type") == "private":
            self._handle_private_message(message)
        elif chat_id == self.config.target_chat_id:
            self._handle_group_message(message)

    def _handle_private_message(self, message: dict[str, Any]) -> None:
        user_id = sender_user_id(message)
        if user_id is None:
            return
        text = extract_text(message).strip()
        chat_id = int(message["chat"]["id"])
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", maxsplit=1)[0].lower() if text.startswith("/") else ""
        payload = parts[1].strip().lower() if len(parts) == 2 else ""

        if command == "/start" and payload == "invite":
            self._pending_inputs.pop(user_id, None)
            self._send_personal_referral_link(user_id, chat_id)
            return

        if user_id not in self.config.admin_ids:
            if command == "/start":
                self.api.send_message(chat_id, "Ce bot est réservé à la gestion de son groupe Telegram.")
            elif command == "/panel":
                self.api.send_message(chat_id, "Accès refusé.")
            return

        if command in {"/start", "/panel"}:
            self._pending_inputs.pop(user_id, None)
            self._show_panel(chat_id)
            return
        if command == "/cancel":
            self._pending_inputs.pop(user_id, None)
            self._show_panel(chat_id)
            return

        pending = self._pending_inputs.get(user_id)
        if pending:
            self._consume_pending_input(user_id, message, pending)

    def _send_personal_referral_link(self, user_id: int, chat_id: int) -> None:
        profile = self.storage.get_referral_profile(user_id)
        if profile:
            invite_link, confirmed_count, _ = profile
        else:
            try:
                result = self.api.create_chat_invite_link(
                    self.config.target_chat_id,
                    name=f"parrain-{user_id}",
                    creates_join_request=True,
                )
            except TelegramAPIError:
                LOGGER.exception("Impossible de créer le lien personnel de %s", user_id)
                self.api.send_message(
                    chat_id,
                    "Impossible de créer votre lien pour le moment. Réessayez un peu plus tard.",
                )
                return
            invite_link = str(result.get("invite_link") or "")
            if not invite_link:
                self.api.send_message(chat_id, "Telegram n’a pas retourné de lien d’invitation.")
                return
            self.storage.save_referral_link(user_id, invite_link)
            confirmed_count = 0

        self.api.send_message(
            chat_id,
            "Voici votre lien personnel d’invitation :\n"
            f"{invite_link}\n\n"
            f"Invitations validées : {confirmed_count}/{REFERRAL_REQUIRED_COUNT}",
        )

    def _handle_chat_join_request(self, request: dict[str, Any]) -> None:
        chat_id = int((request.get("chat") or {}).get("id", 0))
        if chat_id != self.config.target_chat_id:
            return
        candidate_user_id = sender_user_id({"from": request.get("from") or {}})
        invite_link = str((request.get("invite_link") or {}).get("invite_link") or "")
        if candidate_user_id is None or not invite_link:
            return
        requested_at = datetime.fromtimestamp(
            int(request.get("date") or datetime.now(timezone.utc).timestamp()),
            timezone.utc,
        )
        self.storage.record_referral_request(candidate_user_id, invite_link, requested_at)

    def _handle_chat_member_update(self, update: dict[str, Any]) -> None:
        chat_id = int((update.get("chat") or {}).get("id", 0))
        if chat_id != self.config.target_chat_id:
            return
        member = update.get("new_chat_member") or {}
        user_id = sender_user_id({"from": member.get("user") or {}})
        if user_id is None:
            return
        event_at = datetime.fromtimestamp(
            int(update.get("date") or datetime.now(timezone.utc).timestamp()),
            timezone.utc,
        )
        if self._chat_member_is_present(member):
            self.storage.mark_referral_joined(user_id, event_at)
            return

        result = self.storage.confirm_referral(
            user_id,
            event_at - timedelta(seconds=REFERRAL_STAY_SECONDS),
        )
        if result:
            self._notify_referral_confirmation(*result)
        self.storage.cancel_referral_pending(user_id)

    @staticmethod
    def _chat_member_is_present(member: dict[str, Any]) -> bool:
        status = str(member.get("status") or "")
        if status in {"creator", "administrator", "member"}:
            return True
        return status == "restricted" and bool(member.get("is_member"))

    def _handle_group_message(self, message: dict[str, Any]) -> None:
        user_id = sender_user_id(message)
        for new_member in message.get("new_chat_members") or []:
            new_user_id = sender_user_id({"from": new_member})
            if new_user_id is not None:
                self.storage.mark_referral_joined(new_user_id, datetime.now(timezone.utc))
        media_group_id = str(message.get("media_group_id") or "")
        if media_group_id and user_id is not None:
            self.storage.record_media_group_message(
                media_group_id,
                int(message["message_id"]),
                user_id,
            )

        command = self._group_command(message)
        if command in TRUSTED_COMMANDS:
            self._handle_trusted_command(message, command, user_id)
            return

        now = datetime.now(self.config.timezone)
        schedule = self._schedule()
        is_open_now = is_effectively_open(self.storage.get_bool("auto_open"), schedule, now)
        popular_justice_enabled = self.storage.get_bool("popular_justice")
        popular_signal = message.get("text") is not None and is_popular_justice_signal(
            extract_text(message)
        )
        if (
            is_open_now
            and popular_justice_enabled
            and self._handle_popular_justice_report(message, user_id)
        ):
            return

        matched_word = find_banned_word(extract_text(message), self.storage.list_words())
        if user_id in self.config.trusted_ids or (popular_signal and not popular_justice_enabled):
            matched_word = None
        membership_event = is_membership_event(message)
        action = decide_message_action(
            MessagePolicyContext(
                membership_event=membership_event,
                privileged=self._is_privileged(message, user_id),
                group_open=is_open_now,
                story=is_story(message),
                forwarded=is_forward(message),
                forwards_forbidden=self.storage.get_bool("forwards_forbidden"),
                contains_link=contains_link(message),
                links_forbidden=self.storage.get_bool("links_forbidden"),
                allowed_content=is_allowed_open_content(message),
                contains_banned_word=matched_word is not None,
            )
        )
        if action is ModerationAction.ALLOW:
            return

        # Entry and exit notices must disappear regardless of who triggered
        # them. All other moderation verifies the live Telegram admin status
        # one last time before deleting or sanctioning. If that verification
        # temporarily fails, delete the content but do not apply an irreversible
        # sanction.
        if membership_event:
            self._safe_delete(message)
            return

        member_status = self._moderation_member_status(user_id) if user_id is not None else "regular"
        if member_status == "admin":
            return
        self._safe_delete(message)
        if member_status == "unknown":
            return

        if action is ModerationAction.BAN:
            reason = "partage d’une story" if is_story(message) else "publication d’un lien"
            self._ban_sender(message, reason=reason)
        elif action is ModerationAction.SANCTION and matched_word and user_id is not None:
            self._sanction_forbidden_word(message, user_id, matched_word)

    def _group_command(self, message: dict[str, Any]) -> str:
        text = extract_text(message).strip()
        if not text.startswith("/"):
            return ""
        return text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()

    def _handle_trusted_command(
        self,
        message: dict[str, Any],
        command: str,
        operator_user_id: int | None,
    ) -> None:
        self._safe_delete(message)
        if operator_user_id not in self.config.admin_ids | self.config.trusted_ids:
            self._sanction_unauthorized_command(message, operator_user_id, command)
            return

        target = message.get("reply_to_message") or {}
        if not target or not target.get("message_id"):
            return

        if command == "/supprime":
            self._safe_delete_ids(self._target_message_ids(target))
            return

        target_user = target.get("from") or {}
        target_user_id = sender_user_id(target)
        if (
            target_user_id is None
            or target_user_id == operator_user_id
            or target_user_id == self._bot_id
            or target_user.get("is_bot")
        ):
            return

        member_status = self._moderation_member_status(target_user_id)
        if member_status != "regular":
            return

        display_name = str(target_user.get("first_name") or target_user_id)
        if command == "/ban":
            try:
                self.api.ban_chat_member(self.config.target_chat_id, target_user_id)
                self._safe_delete_ids(self._target_message_ids(target))
                self.api.send_message(
                    self.config.target_chat_id,
                    f"{display_name} a été banni par la modération.",
                )
                LOGGER.info("Bannissement par commande de %s par %s", target_user_id, operator_user_id)
            except TelegramAPIError:
                LOGGER.exception("Échec du bannissement par commande pour %s", target_user_id)
            return

        if command == "/pasfr":
            level = self.storage.register_trusted_offense(target_user_id)
            try:
                if level == 1:
                    duration = timedelta(hours=1)
                    label = "1 heure"
                elif level == 2:
                    duration = timedelta(days=1)
                    label = "1 jour"
                elif level == 3:
                    duration = timedelta(days=5)
                    label = "5 jours"
                else:
                    self.api.ban_chat_member(self.config.target_chat_id, target_user_id)
                    self.api.send_message(
                        self.config.target_chat_id,
                        f"{display_name} a été banni après plusieurs sanctions.",
                    )
                    LOGGER.info("Bannissement /pasfr de %s au niveau %s", target_user_id, level)
                    return

                until = int((datetime.now(timezone.utc) + duration).timestamp())
                self.api.restrict_chat_member(self.config.target_chat_id, target_user_id, until)
                self.api.send_message(
                    self.config.target_chat_id,
                    f"{display_name} ne peut plus écrire pendant {label}.",
                )
                LOGGER.info("Sanction /pasfr de %s au niveau %s", target_user_id, level)
            except TelegramAPIError:
                LOGGER.exception("Échec de la sanction /pasfr pour %s", target_user_id)

    def _sanction_unauthorized_command(
        self,
        message: dict[str, Any],
        user_id: int | None,
        command: str,
    ) -> None:
        if user_id is None or self._is_privileged(message, user_id):
            return
        if self._moderation_member_status(user_id) != "regular":
            return

        level = self.storage.register_unauthorized_command_offense(user_id)
        sender = message.get("from") or {}
        display_name = str(sender.get("first_name") or user_id)
        try:
            if level == 1:
                until = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
                self.api.restrict_chat_member(self.config.target_chat_id, user_id, until)
                notice = f"{display_name} est sanctionné pendant 1 jour pour utilisation non autorisée de {command}."
            elif level == 2:
                until = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp())
                self.api.restrict_chat_member(self.config.target_chat_id, user_id, until)
                notice = f"{display_name} est sanctionné pendant 3 jours pour récidive avec {command}."
            else:
                self.api.ban_chat_member(self.config.target_chat_id, user_id)
                notice = f"{display_name} a été banni après plusieurs utilisations de commandes réservées."
            self.api.send_message(self.config.target_chat_id, notice)
            LOGGER.info("Commande non autorisée %s par %s, niveau %s", command, user_id, level)
        except TelegramAPIError:
            LOGGER.exception("Échec de la sanction pour commande non autorisée de %s", user_id)

    def _handle_popular_justice_report(
        self,
        message: dict[str, Any],
        voter_user_id: int | None,
    ) -> bool:
        if message.get("text") is None or not is_popular_justice_signal(extract_text(message)):
            return False
        target = message.get("reply_to_message") or {}
        if not target or not target.get("message_id"):
            return False

        voter = message.get("from") or {}
        target_user = target.get("from") or {}
        target_user_id = sender_user_id(target)
        if (
            voter_user_id is None
            or voter.get("is_bot")
            or target_user_id is None
            or target_user.get("is_bot")
            or target_user_id == voter_user_id
            or target_user_id == self._bot_id
            or self._is_privileged(target, target_user_id)
        ):
            self._safe_delete(message)
            return True

        target_key = self._popular_target_key(target)
        vote_count, added, resolved = self.storage.register_popular_vote(
            target_key,
            voter_user_id,
            int(message["message_id"]),
        )
        if resolved:
            self._safe_delete(message)
            return True
        if not added:
            self._safe_delete(message)

        if vote_count < self._popular_threshold():
            return True

        member_status = self._moderation_member_status(target_user_id)
        if member_status == "unknown":
            return True

        report_message_ids = self.storage.popular_vote_message_ids(target_key)
        if not self.storage.claim_popular_case(target_key, target_user_id):
            self._safe_delete_ids(report_message_ids)
            return True

        if member_status == "admin":
            self._safe_delete_ids(report_message_ids)
            self.storage.clear_popular_votes(target_key)
            return True

        try:
            self.api.ban_chat_member(self.config.target_chat_id, target_user_id)
        except TelegramAPIError:
            self.storage.release_popular_case(target_key)
            LOGGER.exception("Échec du bannissement par justice populaire pour %s", target_user_id)
            return True

        self._safe_delete_ids(self._target_message_ids(target))
        self._safe_delete_ids(report_message_ids)
        self.storage.clear_popular_votes(target_key)
        self.api.send_message(
            self.config.target_chat_id,
            "Merci à tous d’avoir lutté et d’avoir signalé. "
            "Le contenu a été supprimé et son auteur a été banni.",
        )
        LOGGER.info(
            "Justice populaire appliquée à %s après %s votes distincts",
            target_user_id,
            vote_count,
        )
        return True

    def _popular_target_key(self, target: dict[str, Any]) -> str:
        media_group_id = str(target.get("media_group_id") or "")
        if media_group_id:
            return f"media:{self.config.target_chat_id}:{media_group_id}"
        return f"message:{self.config.target_chat_id}:{int(target['message_id'])}"

    def _target_message_ids(self, target: dict[str, Any]) -> list[int]:
        target_id = int(target["message_id"])
        media_group_id = str(target.get("media_group_id") or "")
        if not media_group_id:
            return [target_id]
        stored_ids = self.storage.list_media_group_message_ids(media_group_id)
        return sorted(set(stored_ids) | {target_id})

    def _popular_threshold(self) -> int:
        try:
            threshold = int(self.storage.get("popular_threshold", "5"))
        except ValueError:
            return 5
        return min(POPULAR_THRESHOLD_MAX, max(POPULAR_THRESHOLD_MIN, threshold))

    def _is_privileged(self, message: dict[str, Any], user_id: int | None) -> bool:
        if user_id in self.config.admin_ids:
            return True
        sender_chat = message.get("sender_chat") or {}
        if int(sender_chat.get("id", 0)) == self.config.target_chat_id:
            return True
        with self._admin_lock:
            return user_id is not None and user_id in self._group_admin_ids

    def _moderation_member_status(self, user_id: int) -> str:
        if user_id in self.config.admin_ids:
            return "admin"
        try:
            member = self.api.get_chat_member(self.config.target_chat_id, user_id)
        except TelegramAPIError:
            LOGGER.exception("Impossible de confirmer le statut du membre %s", user_id)
            return "unknown"
        if member.get("status") in {"creator", "administrator"}:
            with self._admin_lock:
                self._group_admin_ids.add(user_id)
            return "admin"
        return "regular"

    def _ban_sender(self, message: dict[str, Any], *, reason: str) -> None:
        user_id = sender_user_id(message)
        try:
            if user_id is not None:
                self.api.ban_chat_member(self.config.target_chat_id, user_id)
                LOGGER.info("Membre %s banni : %s", user_id, reason)
                return
            sender_chat = message.get("sender_chat") or {}
            sender_chat_id = sender_chat.get("id")
            if sender_chat_id and int(sender_chat_id) != self.config.target_chat_id:
                self.api.ban_chat_sender_chat(self.config.target_chat_id, int(sender_chat_id))
                LOGGER.info("Chat expéditeur %s banni : %s", sender_chat_id, reason)
        except TelegramAPIError:
            LOGGER.exception("Échec du bannissement pour %s", reason)

    def _sanction_forbidden_word(self, message: dict[str, Any], user_id: int, word: str) -> None:
        count = self.storage.register_offense(user_id)
        sender = message.get("from") or {}
        display_name = str(sender.get("first_name") or user_id)
        try:
            if count == 1:
                until = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
                self.api.restrict_chat_member(self.config.target_chat_id, user_id, until)
                notice = f"{display_name} est sanctionné pendant 1 jour pour l’utilisation d’un mot interdit."
            elif count == 2:
                until = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp())
                self.api.restrict_chat_member(self.config.target_chat_id, user_id, until)
                notice = f"{display_name} est sanctionné pendant 3 jours pour récidive."
            else:
                self.api.ban_chat_member(self.config.target_chat_id, user_id)
                notice = f"{display_name} a été banni après plusieurs récidives."
            self.api.send_message(self.config.target_chat_id, notice)
            LOGGER.info("Sanction mot interdit pour %s (niveau %s, mot %r)", user_id, count, word)
        except TelegramAPIError:
            LOGGER.exception("Impossible d’appliquer la sanction au membre %s", user_id)

    def _safe_delete(self, message: dict[str, Any]) -> None:
        try:
            self.api.delete_message(int(message["chat"]["id"]), int(message["message_id"]))
        except TelegramAPIError as exc:
            if exc.error_code != 400:
                LOGGER.warning("Impossible de supprimer le message %s : %s", message.get("message_id"), exc)

    def _safe_delete_ids(self, message_ids: list[int]) -> None:
        unique_ids = sorted(set(message_ids))
        for index in range(0, len(unique_ids), 100):
            chunk = unique_ids[index : index + 100]
            if not chunk:
                continue
            try:
                self.api.delete_messages(self.config.target_chat_id, chunk)
            except TelegramAPIError as exc:
                if exc.error_code != 400:
                    LOGGER.warning("Impossible de supprimer les messages %s : %s", chunk, exc)

    def _handle_callback(self, query: dict[str, Any]) -> None:
        query_id = str(query["id"])
        user_id = int((query.get("from") or {}).get("id", 0))
        if user_id not in self.config.admin_ids:
            self.api.answer_callback_query(query_id, "Accès refusé.", alert=True)
            return

        message = query.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        message_id = int(message.get("message_id", 0))
        data = str(query.get("data") or "")
        self.api.answer_callback_query(query_id)

        if data == "panel":
            self._pending_inputs.pop(user_id, None)
            self._show_panel(chat_id, message_id)
        elif data == "toggle:auto":
            enabled = self.storage.toggle("auto_open")
            if not enabled:
                self._clear_countdown()
                self._apply_permissions(False, force=True)
                self.api.send_message(self.config.target_chat_id, self._no_opening_message())
            else:
                self._scheduler_tick(force=True, force_countdown=True)
            self._show_panel(chat_id, message_id)
        elif data == "toggle:links":
            self.storage.toggle("links_forbidden")
            self._scheduler_tick(force=True)
            self._show_panel(chat_id, message_id)
        elif data == "toggle:forwards":
            self.storage.toggle("forwards_forbidden")
            self._show_panel(chat_id, message_id)
        elif data == "toggle:justice":
            enabled = self.storage.toggle("popular_justice")
            if not enabled:
                report_ids = self.storage.all_popular_vote_message_ids()
                self._safe_delete_ids(report_ids)
                self.storage.clear_all_popular_votes()
            self._show_panel(chat_id, message_id)
        elif data == "justice:threshold":
            self._pending_inputs[user_id] = PendingInput("set_popular_threshold", chat_id, message_id)
            self._edit_message(
                chat_id,
                message_id,
                f"Envoyez le nouveau seuil entre {POPULAR_THRESHOLD_MIN} et {POPULAR_THRESHOLD_MAX}.\n"
                "Seuls les votes de comptes distincts sont comptés.\n\n"
                "Utilisez /cancel pour annuler.",
                cancel_keyboard(),
            )
        elif data == "invite_ad:menu":
            self._pending_inputs.pop(user_id, None)
            self._show_invitation_ad_menu(chat_id, message_id)
        elif data == "invite_ad:text":
            self._pending_inputs[user_id] = PendingInput("set_invitation_ad_text", chat_id, message_id)
            self._edit_message(
                chat_id,
                message_id,
                f"Envoyez le texte de la publicité (maximum : {INVITATION_AD_TEXT_MAX} caractères).\n\n"
                "Utilisez /cancel pour annuler.",
                cancel_keyboard(),
            )
        elif data == "invite_ad:photo":
            self._pending_inputs[user_id] = PendingInput("set_invitation_ad_photo", chat_id, message_id)
            self._edit_message(
                chat_id,
                message_id,
                "Envoyez la nouvelle photo de la publicité.\n\nUtilisez /cancel pour annuler.",
                cancel_keyboard(),
            )
        elif data == "invite_ad:reward":
            self._pending_inputs[user_id] = PendingInput("set_referral_reward_link", chat_id, message_id)
            self._edit_message(
                chat_id,
                message_id,
                "Envoyez le lien Telegram du groupe de récompense.\n\nUtilisez /cancel pour annuler.",
                cancel_keyboard(),
            )
        elif data == "invite_ad:preview":
            error = self._invitation_ad_configuration_error(require_reward=False)
            if error:
                self.api.send_message(chat_id, error)
            else:
                self.api.send_photo(
                    chat_id,
                    self.storage.get("invitation_ad_photo_id"),
                    caption=self.storage.get("invitation_ad_text"),
                    reply_markup=invitation_publication_keyboard(self._bot_username),
                )
            self._show_invitation_ad_menu(chat_id, message_id)
        elif data == "invite_ad:publish":
            error = self._invitation_ad_configuration_error()
            if error:
                self.api.send_message(chat_id, error)
            else:
                self.api.send_photo(
                    self.config.target_chat_id,
                    self.storage.get("invitation_ad_photo_id"),
                    caption=self.storage.get("invitation_ad_text"),
                    reply_markup=invitation_publication_keyboard(self._bot_username),
                )
                self.api.send_message(chat_id, "La publicité d’invitation a été publiée dans le groupe.")
            self._show_invitation_ad_menu(chat_id, message_id)
        elif data == "words:menu":
            self._edit_message(chat_id, message_id, "Gestion des mots interdits", words_keyboard())
        elif data == "words:view":
            words = self.storage.list_words()
            body = "\n".join(f"• {word}" for word in words) if words else "Aucun mot interdit enregistré."
            text = f"Mots interdits ({len(words)})\n\n{body}"
            self._edit_message(chat_id, message_id, text[:3900], words_keyboard())
        elif data in {"words:add", "words:remove"}:
            action = "add_word" if data.endswith("add") else "remove_word"
            self._pending_inputs[user_id] = PendingInput(action, chat_id, message_id)
            prompt = "Envoyez le mot ou l’expression à ajouter." if action == "add_word" else "Envoyez exactement le mot ou l’expression à supprimer."
            self._edit_message(chat_id, message_id, prompt + "\n\nUtilisez /cancel pour annuler.", cancel_keyboard())
        elif data == "rules:menu":
            rules = self.storage.get("rules_text")
            status = "configurées" if rules else "non configurées"
            self._edit_message(chat_id, message_id, f"Règles du groupe : {status}", rules_keyboard())
        elif data == "rules:set":
            self._pending_inputs[user_id] = PendingInput("set_rules", chat_id, message_id)
            current = self.storage.get("rules_text")
            preview = f"\n\nRègles actuelles :\n{current[:2500]}" if current else ""
            self._edit_message(
                chat_id,
                message_id,
                "Envoyez le nouveau texte complet des règles.\nUtilisez /cancel pour annuler." + preview,
                cancel_keyboard(),
            )
        elif data == "rules:publish":
            rules = self.storage.get("rules_text").strip()
            if rules:
                self.api.send_message(self.config.target_chat_id, rules)
                self._edit_message(chat_id, message_id, "Les règles ont été publiées dans le groupe.", rules_keyboard())
            else:
                self._edit_message(chat_id, message_id, "Aucune règle n’est encore configurée.", rules_keyboard())
        elif data == "schedule:menu":
            self._edit_message(chat_id, message_id, "Choisissez les horaires automatiques.", schedule_keyboard())
        elif data.startswith("schedule:"):
            preset = data.split(":", maxsplit=1)[1]
            if preset in SCHEDULE_PRESETS:
                opens_at, closes_at = SCHEDULE_PRESETS[preset]
                self.storage.set("open_time", opens_at)
                self.storage.set("close_time", closes_at)
                self._scheduler_tick(force=True, force_countdown=True)
            self._show_panel(chat_id, message_id)
        elif data == "sync":
            self._refresh_group_admins(strict=False)
            self._scheduler_tick(force=True, force_countdown=True)
            self._show_panel(chat_id, message_id)
        elif data == "cancel":
            self._pending_inputs.pop(user_id, None)
            self._show_panel(chat_id, message_id)

    def _consume_pending_input(
        self,
        user_id: int,
        message: dict[str, Any],
        pending: PendingInput,
    ) -> None:
        text = extract_text(message).strip()
        if pending.action == "set_invitation_ad_photo":
            photos = list(message.get("photo") or [])
            if not photos or not photos[-1].get("file_id"):
                self.api.send_message(pending.panel_chat_id, "Envoyez une photo, pas un fichier ni du texte.")
                return
            self.storage.set("invitation_ad_photo_id", str(photos[-1]["file_id"]))
            result = "Photo de la publicité enregistrée."
        elif not text:
            self.api.send_message(pending.panel_chat_id, "Le texte ne peut pas être vide.")
            return
        elif pending.action == "add_word":
            added = self.storage.add_word(text)
            result = "Mot ajouté." if added else "Ce mot existe déjà."
        elif pending.action == "remove_word":
            removed = self.storage.remove_word(text)
            result = "Mot supprimé." if removed else "Ce mot n’a pas été trouvé."
        elif pending.action == "set_rules":
            if len(text) > 3900:
                self.api.send_message(pending.panel_chat_id, "Le texte est trop long (maximum : 3 900 caractères).")
                return
            self.storage.set("rules_text", text)
            result = "Règles enregistrées. Elles seront publiées trois fois par séance."
        elif pending.action == "set_popular_threshold":
            try:
                threshold = int(text)
            except ValueError:
                self.api.send_message(pending.panel_chat_id, "Le seuil doit être un nombre entier.")
                return
            if not POPULAR_THRESHOLD_MIN <= threshold <= POPULAR_THRESHOLD_MAX:
                self.api.send_message(
                    pending.panel_chat_id,
                    f"Le seuil doit être compris entre {POPULAR_THRESHOLD_MIN} et {POPULAR_THRESHOLD_MAX}.",
                )
                return
            self.storage.set("popular_threshold", str(threshold))
            result = f"Seuil de justice populaire enregistré : {threshold} votes distincts."
        elif pending.action == "set_invitation_ad_text":
            if len(text) > INVITATION_AD_TEXT_MAX:
                self.api.send_message(
                    pending.panel_chat_id,
                    f"Le texte est trop long (maximum : {INVITATION_AD_TEXT_MAX} caractères).",
                )
                return
            self.storage.set("invitation_ad_text", text)
            result = "Texte de la publicité enregistré."
        elif pending.action == "set_referral_reward_link":
            if not text.startswith(("https://t.me/", "https://telegram.me/")) or any(char.isspace() for char in text):
                self.api.send_message(
                    pending.panel_chat_id,
                    "Envoyez un lien Telegram valide commençant par https://t.me/.",
                )
                return
            self.storage.set("referral_reward_link", text)
            result = "Lien du groupe de récompense enregistré."
        else:
            result = "Action inconnue."
        self._pending_inputs.pop(user_id, None)
        self.api.send_message(pending.panel_chat_id, result)
        if pending.action.startswith("set_invitation_") or pending.action == "set_referral_reward_link":
            self._show_invitation_ad_menu(pending.panel_chat_id, pending.panel_message_id)
        else:
            self._show_panel(pending.panel_chat_id, pending.panel_message_id)

    def _show_invitation_ad_menu(self, chat_id: int, message_id: int) -> None:
        ad_text = self.storage.get("invitation_ad_text").strip()
        photo_id = self.storage.get("invitation_ad_photo_id").strip()
        reward_link = self.storage.get("referral_reward_link").strip()
        text = (
            "Publicité d’invitation\n\n"
            f"Texte : {'configuré' if ad_text else 'manquant'}\n"
            f"Photo : {'configurée' if photo_id else 'manquante'}\n"
            f"Lien de récompense : {'configuré' if reward_link else 'manquant'}\n\n"
            "Une seule publicité est conservée : toute modification remplace la précédente."
        )
        self._edit_message(chat_id, message_id, text, invitation_ad_keyboard())

    def _invitation_ad_configuration_error(self, *, require_reward: bool = True) -> str:
        missing: list[str] = []
        if not self.storage.get("invitation_ad_text").strip():
            missing.append("le texte")
        if not self.storage.get("invitation_ad_photo_id").strip():
            missing.append("la photo")
        if require_reward and not self.storage.get("referral_reward_link").strip():
            missing.append("le lien de récompense")
        if not self._bot_username:
            missing.append("le nom d’utilisateur du bot")
        if not missing:
            return ""
        return "Publication impossible : configurez " + ", ".join(missing) + "."

    def _show_panel(self, chat_id: int, message_id: int | None = None) -> None:
        now = datetime.now(self.config.timezone)
        schedule = self._schedule()
        auto_open = self.storage.get_bool("auto_open")
        links_forbidden = self.storage.get_bool("links_forbidden")
        forwards_forbidden = self.storage.get_bool("forwards_forbidden")
        popular_justice = self.storage.get_bool("popular_justice")
        popular_threshold = self._popular_threshold()
        text = panel_text(
            is_open=is_effectively_open(auto_open, schedule, now),
            auto_open=auto_open,
            links_forbidden=links_forbidden,
            forwards_forbidden=forwards_forbidden,
            popular_justice=popular_justice,
            popular_threshold=popular_threshold,
            schedule=schedule,
            timezone_name=self.config.timezone_name,
        )
        keyboard = panel_keyboard(
            auto_open=auto_open,
            links_forbidden=links_forbidden,
            forwards_forbidden=forwards_forbidden,
            popular_justice=popular_justice,
            popular_threshold=popular_threshold,
        )
        if message_id:
            self._edit_message(chat_id, message_id, text, keyboard)
        else:
            self.api.send_message(chat_id, text, reply_markup=keyboard)

    def _edit_message(self, chat_id: int, message_id: int, text: str, keyboard: dict[str, Any]) -> None:
        try:
            self.api.edit_message_text(chat_id, message_id, text, reply_markup=keyboard)
        except TelegramAPIError as exc:
            if "message is not modified" not in exc.description.lower():
                raise

    def _schedule(self) -> DailySchedule:
        return DailySchedule(
            opens_at=parse_hhmm(self.storage.get("open_time", "23:00")),
            closes_at=parse_hhmm(self.storage.get("close_time", "02:00")),
        )

    def _scheduler_loop(self) -> None:
        first_tick = True
        while not self.stop_event.is_set():
            try:
                self._scheduler_tick(force=first_tick, force_countdown=first_tick)
                first_tick = False
            except Exception:
                LOGGER.exception("Erreur dans le planificateur")
            self.stop_event.wait(10)

    def _scheduler_tick(self, *, force: bool = False, force_countdown: bool = False) -> None:
        with self._scheduler_lock:
            now = datetime.now(self.config.timezone)
            self._last_scheduler_at = datetime.now(timezone.utc)
            schedule = self._schedule()
            auto_open = self.storage.get_bool("auto_open")
            links_forbidden = self.storage.get_bool("links_forbidden")
            is_open_now = is_effectively_open(auto_open, schedule, now)

            if force or time_module.monotonic() - self._last_admin_refresh >= 300:
                self._refresh_group_admins(strict=False)
            self._apply_permissions(is_open_now, links_forbidden=links_forbidden, force=force)

            if time_module.monotonic() - self._last_prune >= 86400:
                self.storage.prune_events()
                self._last_prune = time_module.monotonic()

            if force or time_module.monotonic() - self._last_referral_check >= REFERRAL_CHECK_INTERVAL:
                self._process_due_referrals()
                self._process_referral_rewards()
                self._last_referral_check = time_module.monotonic()

            if not auto_open:
                self._clear_countdown()
                return

            if is_open_now:
                self._clear_countdown()
                self._handle_open_session(now, schedule)
                return

            next_open = schedule.next_open(now)
            slot = countdown_slot(now, next_open, force=force_countdown)
            if slot:
                if force_countdown:
                    slot = f"forced:{now.strftime('%Y%m%d%H%M')}"
                event_key = f"countdown:{next_open.isoformat()}:{slot}"
                text = f"⏳ Ouverture du groupe dans {format_duration(next_open - now)}."
                self._replace_countdown_event(event_key, text)

    def _process_due_referrals(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=REFERRAL_STAY_SECONDS)
        for candidate_user_id, _ in self.storage.due_referrals(cutoff):
            try:
                member = self.api.get_chat_member(self.config.target_chat_id, candidate_user_id)
            except TelegramAPIError:
                LOGGER.exception("Impossible de vérifier l’adhésion de %s", candidate_user_id)
                continue
            if not self._chat_member_is_present(member):
                self.storage.cancel_referral_pending(candidate_user_id)
                continue
            result = self.storage.confirm_referral(candidate_user_id, cutoff)
            if result:
                self._notify_referral_confirmation(*result)

    def _notify_referral_confirmation(self, inviter_id: int, confirmed_count: int) -> None:
        try:
            self.api.send_message(
                inviter_id,
                "✅ Une nouvelle invitation a été validée.\n"
                f"Votre compteur : {confirmed_count}/{REFERRAL_REQUIRED_COUNT}",
            )
        except TelegramAPIError:
            LOGGER.exception("Impossible de notifier le parrain %s", inviter_id)

    def _process_referral_rewards(self) -> None:
        reward_link = self.storage.get("referral_reward_link").strip()
        if not reward_link:
            return
        for inviter_id, _ in self.storage.due_referral_rewards(REFERRAL_REQUIRED_COUNT):
            try:
                self.api.send_message(
                    inviter_id,
                    "🎉 Objectif atteint ! Voici le lien de votre groupe de récompense :\n"
                    f"{reward_link}",
                )
            except TelegramAPIError:
                LOGGER.exception("Impossible d’envoyer la récompense au parrain %s", inviter_id)
                continue
            self.storage.mark_referral_rewarded(inviter_id)

    def _handle_open_session(self, now: datetime, schedule: DailySchedule) -> None:
        session_key = schedule.session_key(now)
        end = schedule.session_end(now)
        if not session_key or not end:
            return

        self._send_event(
            f"open:{session_key}",
            f"🟢 Le groupe est ouvert jusqu’à {format_hhmm(schedule.closes_at)}. "
            "Seuls les messages, les photos et les vidéos sont autorisés.",
        )

        rules = self.storage.get("rules_text").strip()
        rules_slot = current_rules_slot(now, schedule)
        if rules and rules_slot:
            self._send_event(f"rules:{session_key}:{rules_slot}", rules)

        threshold = closing_warning_threshold(now, end)
        if threshold:
            actual_minutes = max(1, math.ceil((end - now).total_seconds() / 60))
            self._send_event(
                f"closing-warning:{session_key}:{threshold}",
                f"⚠️ Le groupe fermera dans {actual_minutes} minute{'s' if actual_minutes > 1 else ''}.",
            )

    def _send_event(self, event_key: str, text: str) -> None:
        if not self.storage.claim_event(event_key):
            return
        try:
            self.api.send_message(self.config.target_chat_id, text)
        except Exception:
            self.storage.release_event(event_key)
            raise

    def _replace_countdown_event(self, event_key: str, text: str) -> None:
        if not self.storage.claim_event(event_key):
            return
        try:
            new_message = self.api.send_message(self.config.target_chat_id, text)
        except Exception:
            self.storage.release_event(event_key)
            raise

        old_id = self.storage.get("last_countdown_message_id")
        self.storage.set("last_countdown_message_id", str(new_message["message_id"]))
        if old_id and old_id != str(new_message["message_id"]):
            try:
                self.api.delete_message(self.config.target_chat_id, int(old_id))
            except TelegramAPIError as exc:
                if exc.error_code != 400:
                    LOGGER.warning("Ancien compte à rebours impossible à supprimer : %s", exc)

    def _clear_countdown(self) -> None:
        old_id = self.storage.get("last_countdown_message_id")
        if not old_id:
            return
        self.storage.set("last_countdown_message_id", "")
        try:
            self.api.delete_message(self.config.target_chat_id, int(old_id))
        except TelegramAPIError as exc:
            if exc.error_code != 400:
                LOGGER.warning("Compte à rebours impossible à supprimer : %s", exc)

    def _apply_permissions(
        self,
        is_open_now: bool,
        *,
        links_forbidden: bool | None = None,
        force: bool = False,
    ) -> None:
        if links_forbidden is None:
            links_forbidden = self.storage.get_bool("links_forbidden")
        signature = (is_open_now, links_forbidden)
        due = time_module.monotonic() - self._last_permission_sync >= 60
        if not force and signature == self._last_permission_signature and not due:
            return
        permissions = open_permissions(links_forbidden=links_forbidden) if is_open_now else closed_permissions()
        self.api.set_chat_permissions(self.config.target_chat_id, permissions)
        self._last_permission_signature = signature
        self._last_permission_sync = time_module.monotonic()
        LOGGER.info("Permissions du groupe resynchronisées : %s", "ouvert" if is_open_now else "fermé")

    def _refresh_group_admins(self, *, strict: bool) -> list[dict[str, Any]]:
        try:
            admins = self.api.get_chat_administrators(self.config.target_chat_id)
            ids = {
                int((member.get("user") or {}).get("id"))
                for member in admins
                if (member.get("user") or {}).get("id") is not None
            }
            with self._admin_lock:
                self._group_admin_ids = ids | set(self.config.admin_ids)
            self._last_admin_refresh = time_module.monotonic()
            return admins
        except TelegramAPIError:
            if strict:
                raise
            LOGGER.exception("Impossible d’actualiser la liste des administrateurs")
            return []

    def _no_opening_message(self) -> str:
        if self._invite_link_cache:
            return (
                "Aucune ouverture n’est prévue aujourd’hui. Revenez demain et partagez le groupe :\n"
                f"{self._invite_link_cache}"
            )
        return (
            "Aucune ouverture n’est prévue aujourd’hui. Revenez demain et partagez le groupe.\n"
            "Le lien principal doit être configuré dans GROUP_INVITE_LINK."
        )
