"""A single editable Telegram message with today's plans; no model calls."""

import asyncio
from datetime import timedelta
from hashlib import sha256
from html import escape

from homeassistant.util import dt as dt_util

from .model import task_bounds, timestamp

WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def units(text):
    return len(text.encode("utf-16-le")) // 2


def today_message(state, now, timezone, ordinary="Обычная"):
    today = now.astimezone(timezone).date()
    tasks = []
    for task in state["tasks"].values():
        if task["status"] == "cancelled":
            continue
        start, end = task_bounds(task, timezone)
        if not start:
            continue
        start = start.astimezone(timezone)
        if start.date() == today or (
            start.date() < today
            and (
                end.astimezone(timezone).date() > today
                or (
                    end.astimezone(timezone).date() == today
                    and end.astimezone(timezone).time().isoformat() != "00:00:00"
                )
            )
        ):
            tasks.append(task)
    tasks.sort(key=lambda t: task_bounds(t, timezone)[0])
    body = f"<b>📋 Дела на сегодня:</b>\n<i>{WEEKDAYS[today.weekday()]}, {today.day} {MONTHS[today.month - 1]}</i>\n"
    if not tasks:
        return body + "\nНа сегодня дел пока нет. Пусть день будет спокойным ☀️"
    rows = []
    for task in tasks:
        start = task_bounds(task, timezone)[0].astimezone(timezone)
        when = (
            start.strftime("%H:%M")
            if start.date() == today and not task.get("due_date")
            else "В течение дня"
        )
        title = escape(task["title"][:180] + ("…" if len(task["title"]) > 180 else ""))
        if task["status"] == "done":
            row = f"✅ <s>{when} — {title}</s>"
        else:
            icon = "⭐" if task.get("priority") and task["priority"] != ordinary else "▫️"
            row = f"{icon} <b>{when}</b> — {title}"
            if task.get("repeat_until_ack") and not task.get("reminder_ack_at"):
                row += " 🔁"
        rows.append(row)
    count = 0
    for row in rows:
        if units(body + "\n" + row) > 3400:
            break
        body += "\n" + row
        count += 1
    if count < len(rows):
        body += f"\n\n<i>Ещё {len(rows) - count} дел — в панели mindKeeper AI.</i>"
    remaining = sum(t["status"] == "active" for t in tasks)
    return (
        body
        + f"\n\n<i>{'Всё сделано ✨' if not remaining else f'Осталось {remaining} из {len(tasks)}'}</i>"
    )


def error_kind(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        text = str(error).lower()
        if "message is not modified" in text:
            return "unchanged"
        if (
            type(error).__name__ in ("RetryAfter", "Forbidden", "BadRequest", "ServiceNotFound")
            or "too many requests" in text
        ):
            # Check missing-message phrases below before classifying other rejected requests.
            rejected = True
        else:
            rejected = False
        if any(
            x in text
            for x in ("message to edit not found", "message can't be edited", "message_id_invalid")
        ):
            return "missing"
        if rejected:
            return "retry"
        error = error.__cause__ or error.__context__
    return "temporary"


class DailyAgenda:
    def __init__(self, secretary):
        self.secretary = secretary
        self.lock = asyncio.Lock()

    def key(self):
        route = self.secretary.telegram_route
        return f"{route['bot_entry_id']}:{route['chat_id']}"

    def status(self):
        return self.secretary.state.get("agendas", {}).get(self.key(), {}).get("status", "new")

    async def sync(self, force=False, recreate=False):
        sec = self.secretary
        if not sec.options["enabled"] or not sec.options["agenda_enabled"] or sec.stopping:
            return
        async with self.lock:
            if not sec.options["enabled"] or not sec.options["agenda_enabled"] or sec.stopping:
                return
            route = sec.telegram_route
            getter = getattr(getattr(sec.hass, "config_entries", None), "async_get_entry", None)
            if getter:
                bot_entry = getter(route["bot_entry_id"])
                if bot_entry is None or getattr(bot_entry, "runtime_data", None) is None:
                    return
            has_service = getattr(sec.hass.services, "has_service", None)
            if has_service and not has_service("telegram_bot", "send_message"):
                return
            key = self.key()
            now = sec.now()
            body = today_message(
                sec.state, now, dt_util.DEFAULT_TIME_ZONE, sec.options["priorities"][0]
            )
            digest = sha256(body.encode()).hexdigest()
            async with sec.lock:
                board = sec.state.setdefault("agendas", {}).setdefault(
                    key, {"status": "new", "message_id": None, "hash": None}
                )
                if board["status"] == "uncertain":
                    if not recreate:
                        return
                    board["status"] = "new"
                    board["message_id"] = None
                if not force and (
                    board.get("hash") == digest
                    or (board.get("retry_at") and timestamp(board["retry_at"]) > now)
                ):
                    return
                message_id = board.get("message_id")
                board["status"] = "editing" if message_id else "creating"
                await sec.save()
            payload = {
                "config_entry_id": route["bot_entry_id"],
                "chat_id": route["chat_id"],
                "message": body,
                "parse_mode": "html",
                "disable_web_page_preview": True,
            }
            if message_id:
                payload["message_id"] = message_id
            else:
                payload["disable_notification"] = True
            try:
                result = await sec.hass.services.async_call(
                    "telegram_bot",
                    "edit_message" if message_id else "send_message",
                    payload,
                    blocking=True,
                    return_response=not bool(message_id),
                )
                if not message_id:
                    for chat in (result or {}).get("chats", []):
                        if chat.get("chat_id") == route["chat_id"] and isinstance(
                            chat.get("message_id"), int
                        ):
                            message_id = chat["message_id"]
                    if not message_id:
                        raise ValueError("Missing agenda receipt")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                kind = error_kind(error)
                async with sec.lock:
                    board = sec.state["agendas"][key]
                    if kind == "unchanged" and message_id:
                        board.update(status="ready", hash=digest)
                        board.pop("retry_at", None)
                    elif kind == "missing" and message_id:
                        board.update(status="new", message_id=None, hash=None)
                        board.pop("retry_at", None)
                    else:
                        board["status"] = "error" if message_id or kind == "retry" else "uncertain"
                        board["retry_at"] = (now + timedelta(minutes=2)).isoformat()
                    await sec.save()
            else:
                async with sec.lock:
                    sec.state["agendas"][key].update(
                        status="ready",
                        message_id=message_id,
                        hash=digest,
                        updated_at=now.isoformat(),
                    )
                    sec.state["agendas"][key].pop("retry_at", None)
                    await sec.save()
