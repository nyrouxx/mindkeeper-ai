"""Configured Telegram bots, recipients and sender validation; no token copying."""

import re

from .model import InvalidPlan

ROUTE_FIELDS = ("bot_entry_id", "chat_id", "user_id")


def bot_choices(hass):
    result = {}
    for entry in hass.config_entries.async_entries("telegram_bot"):
        if getattr(entry, "disabled_by", None) is not None or entry.data.get("platform") not in (
            "polling",
            "webhooks",
        ):
            continue
        label = entry.title
        runtime = getattr(entry, "runtime_data", None)
        try:
            username = getattr(getattr(runtime, "bot", None), "username", None)
        except RuntimeError:
            username = None
        if username:
            label += f" · @{username}"
        result[entry.entry_id] = {"value": entry.entry_id, "label": label}
    return result


def chat_choices(hass, bot_entry_id=None):
    bots = bot_choices(hass)
    result = {}
    for entry in hass.config_entries.async_entries("telegram_bot"):
        if entry.entry_id not in bots or (bot_entry_id and entry.entry_id != bot_entry_id):
            continue
        for subentry in entry.subentries.values():
            chat_id = int(subentry.data.get("chat_id", 0))
            if not chat_id:
                continue
            result[f"{entry.entry_id}:{chat_id}"] = {
                "bot_entry_id": entry.entry_id,
                "chat_id": chat_id,
                "label": f"{subentry.title} · {'Личный чат' if chat_id > 0 else 'Группа'} ({chat_id})",
                "user_id": chat_id if chat_id > 0 else None,
            }
    return result


def positive_user_id(value):
    if not re.fullmatch(r"[0-9]{1,16}", str(value).strip()) or int(value) <= 0:
        raise InvalidPlan("Укажите положительный Telegram ID пользователя")
    return int(value)


def validate_route(hass, selection):
    bot_id = selection.get("bot_entry_id")
    if not isinstance(bot_id, str) or bot_id not in bot_choices(hass):
        raise InvalidPlan("Выберите настроенного бота с приёмом сообщений")
    chat_id = selection.get("chat_id")
    if not re.fullmatch(r"-?[0-9]{1,16}", str(chat_id).strip()):
        raise InvalidPlan("Выберите разрешённый чат этого бота")
    chat = chat_choices(hass, bot_id).get(f"{bot_id}:{int(chat_id)}")
    if chat is None:
        raise InvalidPlan("Этот чат не добавлен в выбранного бота Telegram Bot")
    user_id = chat["chat_id"] if chat["chat_id"] > 0 else positive_user_id(selection.get("user_id"))
    return {"bot_entry_id": bot_id, "chat_id": chat["chat_id"], "user_id": user_id}


def same_chat(left, right):
    return left.get("bot_entry_id") == right.get("bot_entry_id") and left.get(
        "chat_id"
    ) == right.get("chat_id")


def public_settings(hass, route):
    bots = bot_choices(hass)
    chats = chat_choices(hass)
    selected = chats.get(f"{route['bot_entry_id']}:{route['chat_id']}")
    return {
        "route": dict(route),
        "bots": list(bots.values()),
        "chats": list(chats.values()),
        "bot_label": bots.get(route["bot_entry_id"], {}).get("label", "Бот недоступен"),
        "chat_label": selected["label"] if selected else "Чат недоступен",
        "available": selected is not None,
    }
