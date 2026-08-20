from __future__ import annotations

from typing import Any

from app.domain import DailySchedule, format_hhmm


def _button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def _state_label(enabled: bool) -> str:
    return "ON ✅" if enabled else "OFF ❌"


def panel_text(
    *,
    is_open: bool,
    auto_open: bool,
    links_forbidden: bool,
    forwards_forbidden: bool,
    schedule: DailySchedule,
    timezone_name: str,
) -> str:
    state = "OUVERT 🟢" if is_open else "FERMÉ 🔒"
    return (
        "Panneau d’administration\n\n"
        f"État du groupe : {state}\n"
        f"Ouverture automatique : {_state_label(auto_open)}\n"
        f"Liens interdits : {_state_label(links_forbidden)}\n"
        f"Transferts interdits : {_state_label(forwards_forbidden)}\n"
        f"Horaire : {format_hhmm(schedule.opens_at)} → {format_hhmm(schedule.closes_at)}\n"
        f"Fuseau : {timezone_name}\n\n"
        "Seuls les ADMIN_IDS définis dans Railway peuvent utiliser ces boutons."
    )


def panel_keyboard(*, auto_open: bool, links_forbidden: bool, forwards_forbidden: bool) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [_button(f"Automatique {_state_label(auto_open)}", "toggle:auto")],
            [
                _button(f"Liens {_state_label(links_forbidden)}", "toggle:links"),
                _button(f"Forwards {_state_label(forwards_forbidden)}", "toggle:forwards"),
            ],
            [_button("🚫 Mots interdits", "words:menu"), _button("📜 Règles", "rules:menu")],
            [_button("🕒 Horaires", "schedule:menu"), _button("🔄 Resynchroniser", "sync")],
            [_button("Actualiser", "panel")],
        ]
    }


def words_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [_button("Voir", "words:view")],
            [_button("Ajouter", "words:add"), _button("Supprimer", "words:remove")],
            [_button("⬅️ Retour", "panel")],
        ]
    }


def rules_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [_button("Voir / modifier", "rules:set")],
            [_button("Publier maintenant", "rules:publish")],
            [_button("⬅️ Retour", "panel")],
        ]
    }


def schedule_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [_button("22 h → 00 h", "schedule:2200-0000")],
            [_button("23 h → 01 h", "schedule:2300-0100")],
            [_button("23 h → 02 h", "schedule:2300-0200")],
            [_button("00 h → 03 h", "schedule:0000-0300")],
            [_button("⬅️ Retour", "panel")],
        ]
    }


def cancel_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [[_button("Annuler", "cancel")]]}

