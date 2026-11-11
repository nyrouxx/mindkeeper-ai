"""Human conversation, durable plans and a single reminder before each event."""

import asyncio
import logging
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .agenda import DailyAgenda
from .codex import CodexError, CodexParser
from .const import DEFAULTS, DOMAIN
from .google_sync import GoogleSync
from .model import (
    InvalidPlan,
    active_tasks,
    apply_operations,
    awaiting_ack,
    complete_ready_tasks,
    empty_state,
    migrate_state,
    reminder_expired,
    reminder_text,
    repeat_due,
    timestamp,
)
from .telegram import ROUTE_FIELDS, same_chat, validate_route

_LOGGER = logging.getLogger(__name__)
ACK_WORDS = {
    "да",
    "принял",
    "приняла",
    "ок",
    "окей",
    "ага",
    "вижу",
    "увидел",
    "увидела",
    "понял",
    "поняла",
    "помню",
    "хорошо",
    "спасибо",
}
DONE_WORDS = {"сделал", "сделала", "сделано", "готово", "выполнил", "выполнила"}


class Secretary:
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", private=True, atomic_writes=True)
        self.state = empty_state()
        self.lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.listeners = set()
        self.worker = None
        self.stopping = False
        self.last_error = None
        self.authenticating = False
        self.parser = CodexParser(hass.config.path(".personal_secretary"))
        self.agenda = DailyAgenda(self)
        self.google_sync = GoogleSync(self)

    @property
    def options(self):
        return {key: self.entry.options.get(key, value) for key, value in DEFAULTS.items()}

    @property
    def telegram_route(self):
        return {key: self.entry.data[key] for key in ROUTE_FIELDS}

    async def configure_telegram(self, selection):
        route = validate_route(self.hass, selection)
        async with self.lock:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, **route}
            )
            await self.save()
        self.kick()

    def now(self):
        return dt_util.now()

    async def save(self):
        completed = [m for m in self.state["messages"] if m["status"] == "processed"][-200:]
        retained = {m["key"] for m in completed}
        self.state["messages"] = [
            m for m in self.state["messages"] if m["status"] != "processed" or m["key"] in retained
        ]
        recent = (self.now() - timedelta(days=30)).isoformat()
        self.state["outbox"] = {
            k: v
            for k, v in self.state["outbox"].items()
            if v["status"] not in ("sent", "cancelled") or v["created_at"] >= recent
        }
        await self.store.async_save(self.state)
        for listener in tuple(self.listeners):
            listener()

    async def start(self):
        loaded = await self.store.async_load()
        self.state = migrate_state(loaded, self.now(), self.options) if loaded else empty_state()
        if self.options["enabled"]:
            self.state = complete_ready_tasks(
                self.state, self.now(), dt_util.DEFAULT_TIME_ZONE, self.options
            )
        for message in self.state["messages"]:
            if message.get("origin", "telegram") == "telegram":
                message.setdefault("telegram_route", self.telegram_route)
        for message in self.state["messages"]:
            if message["text"] == "__attachment__":
                message.update(status="processed", reply="")
                for prefix in ("reply:", "error:"):
                    previous = self.state["outbox"].get(prefix + message["key"])
                    if previous and previous["status"] != "sent":
                        previous["status"] = "cancelled"
        for board in self.state["agendas"].values():
            if board["status"] == "creating":
                board["status"] = "uncertain"
            elif board["status"] == "editing":
                board["status"] = "ready"
        for item in self.state["outbox"].values():
            item.setdefault("telegram_route", self.telegram_route)
            if item["status"] == "sending":
                item["status"] = "uncertain"
                task = self.state["tasks"].get(item.get("task_id"))
                if (
                    item.get("proactive")
                    and task
                    and task.get("repeat_until_ack")
                    and not task.get("last_notification_at")
                ):
                    task["last_notification_at"] = item["created_at"]
        self.queue_classification()
        await self.save()
        for event in ("telegram_text",):
            self.entry.async_on_unload(self.hass.bus.async_listen(event, self.receive))
        self.entry.async_on_unload(
            async_track_time_interval(self.hass, self.tick, timedelta(seconds=30))
        )
        self.kick()

    async def stop(self):
        self.stopping = True
        await self.google_sync.stop()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.parser.stop()

    @callback
    def listen(self, listener):
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    def enqueue(self, key, message, task_id=None, proactive=False, route=None, kind="reply"):
        if len(message) > 3800:
            index = 0
            while message:
                stop = message.rfind("\n", 0, 3800) if len(message) > 3800 else len(message)
                stop = stop if stop > 0 else min(3800, len(message))
                self.enqueue(
                    key if index == 0 else f"{key}:part:{index}",
                    message[:stop],
                    task_id,
                    proactive,
                    route,
                    kind,
                )
                message = message[stop:].lstrip("\n")
                index += 1
            return
        if key not in self.state["outbox"]:
            task = self.state["tasks"].get(task_id, {})
            self.state["outbox"][key] = {
                "message": message,
                "kind": kind,
                "task_id": task_id,
                "status": "pending",
                "attempts": 0,
                "proactive": proactive,
                "created_at": self.now().isoformat(),
                "message_ids": [],
                "reminder_generation": task.get("reminder_generation"),
                "repeat_generation": task.get("repeat_generation", 0),
                "telegram_route": dict(route or self.telegram_route),
            }

    async def submit(self, text, origin="panel", key=None, reply_to=None, route=None):
        async with self.lock:
            key = key or f"{origin}:{uuid4().hex}"
            if any(m["key"] == key for m in self.state["messages"]):
                return
            message = {
                "key": key,
                "text": text[:8000],
                "origin": origin,
                "status": "pending",
                "attempts": 0,
                "received_at": self.now().isoformat(),
                "reply_to": reply_to,
                "telegram_route": dict(route or self.telegram_route),
            }
            if origin == "telegram":
                word = text.strip().casefold().rstrip(".!… ")
                uid = self.reply_task(message)
                if uid is None and reply_to is None and word in ACK_WORDS | DONE_WORDS:
                    waiting = [t["id"] for t in active_tasks(self.state) if awaiting_ack(t)]
                    uid = waiting[0] if len(waiting) == 1 else None
                task = self.state["tasks"].get(uid)
                if task and awaiting_ack(task):
                    self.state, _ = apply_operations(
                        self.state,
                        [{"action": "acknowledge", "task_id": uid}],
                        self.now(),
                        key + ":receipt",
                        self.options,
                    )
                    message["acknowledged_task"] = uid
                    message["ack_only"] = word in ACK_WORDS | DONE_WORDS
            self.state["messages"].append(message)
            await self.save()
        self.kick()

    async def receive(self, event):
        data = event.data
        if (
            not self.options["enabled"]
            or data.get("bot", {}).get("config_entry_id") != self.entry.data["bot_entry_id"]
            or data.get("chat_id") != self.entry.data["chat_id"]
            or data.get("user_id") != self.entry.data["user_id"]
        ):
            return
        if event.event_type != "telegram_text":
            return
        content = data.get("text")
        if not isinstance(content, str) or not content.strip() or content.startswith("/"):
            return
        route = self.telegram_route
        legacy = f"message:{data['id']}"
        if any(
            m["key"] == legacy and same_chat(m.get("telegram_route", route), route)
            for m in self.state["messages"]
        ):
            return
        await self.submit(
            content,
            "telegram",
            f"message:{route['bot_entry_id']}:{route['chat_id']}:{data['id']}",
            data.get("reply_to_message_id"),
            route,
        )

    def kick(self):
        self.google_sync.kick()
        if not self.stopping and (self.worker is None or self.worker.done()):
            self.worker = self.hass.async_create_background_task(
                self.run(), "personal_secretary_inbox"
            )

    async def run(self):
        try:
            await self.deliver()
            await self.agenda.sync()
            while not self.stopping and self.options["enabled"] and not self.authenticating:
                message = next(
                    (m for m in self.state["messages"] if m["status"] == "pending"), None
                )
                if message is None:
                    return
                await self.process(message)
                await self.deliver()
                await self.agenda.sync()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = "internal_error"
            _LOGGER.error("Secretary processing paused (%s); inbox retained", type(error).__name__)

    def queue_classification(self):
        queued = {
            uid
            for m in self.state["messages"]
            if m.get("origin") == "classification" and m["status"] in ("pending", "error")
            for uid in m.get("task_ids", [])
        }
        ids = [
            t["id"]
            for t in active_tasks(self.state)
            if t.get("classification_pending", True) and t["id"] not in queued
        ]
        for offset in range(0, len(ids), 20):
            self.state["messages"].append(
                {
                    "key": "classification:" + uuid4().hex,
                    "text": "Определи категории и важность этих дел.",
                    "origin": "classification",
                    "task_ids": ids[offset : offset + 20],
                    "status": "pending",
                    "attempts": 0,
                    "received_at": self.now().isoformat(),
                    "reply_to": None,
                }
            )

    async def change(self, operations, source, expected=None):
        async with self.lock:
            previous = [
                {"action": h["action"], "task": deepcopy(t)}
                for t in self.state["tasks"].values()
                for h in t["history"]
                if h["source"] == source
            ]
            if previous:
                return previous
            if expected is not None and expected != {
                k: t["revision"] for k, t in self.state["tasks"].items()
            }:
                raise InvalidPlan("Это дело уже изменилось. Открой его ещё раз.")
            self.state, changes = apply_operations(
                self.state, operations, self.now(), source, self.options
            )
            self.queue_classification()
            await self.save()
        self.kick()
        return changes

    async def retry_failed(self):
        async with self.lock:
            for item in self.state["messages"]:
                if item["status"] == "error":
                    item["status"] = "pending"
                    item["attempts"] = 0
            for item in self.state["outbox"].values():
                if item["status"] == "error":
                    item["status"] = "pending"
                    item["attempts"] = 0
                    item.pop("retry_at", None)
            await self.save()
        self.kick()

    def reply_task(self, message):
        if message.get("acknowledged_task"):
            return message["acknowledged_task"]
        if message.get("reply_to") is not None:
            for item in self.state["outbox"].values():
                if message["reply_to"] in item.get("message_ids", []) and same_chat(
                    item.get("sent_route", item.get("telegram_route", self.telegram_route)),
                    message.get("telegram_route", self.telegram_route),
                ):
                    return item.get("task_id")
        return None

    def context(self, message):
        selected = (
            active_tasks(self.state)[:100]
            + [t for t in self.state["tasks"].values() if t["status"] != "active"][-20:]
        )
        if message.get("origin") == "classification":
            selected = [
                self.state["tasks"][uid]
                for uid in message["task_ids"]
                if uid in self.state["tasks"] and self.state["tasks"][uid]["status"] == "active"
            ]
        return {
            "now": self.now().isoformat(),
            "timezone": self.hass.config.time_zone,
            "categories": self.options["categories"],
            "priorities": self.options["priorities"],
            "tasks": [
                {
                    k: t.get(k)
                    for k in (
                        "id",
                        "title",
                        "notes",
                        "status",
                        "due_at",
                        "end_at",
                        "due_date",
                        "end_date",
                        "category",
                        "priority",
                        "classification_fields",
                        "repeat_until_ack",
                        "reminder_ack_at",
                        "last_notification_at",
                        "completion_reason",
                    )
                }
                for t in selected
            ],
            "history": [
                {"text": m["text"][:1500], "reply": m.get("reply", "")[:1500]}
                for m in self.state["messages"]
                if m["status"] == "processed"
                and m.get("origin") != "classification"
                and m["text"] != "__attachment__"
            ][-12:],
            "message": message["text"],
            "reply_task_id": self.reply_task(message),
            "classification_only": message.get("origin") == "classification",
            "repeat_interval_minutes": self.options["repeat_interval_minutes"],
            "all_day_reminder_mode": self.options["all_day_reminder_mode"],
            "all_day_reminder_time": self.options["all_day_reminder_time"],
        }

    async def process(self, message):
        key = message["key"]
        origin = message.get("origin", "telegram")
        try:
            if message["text"] == "__attachment__":
                async with self.lock:
                    current = next(m for m in self.state["messages"] if m["key"] == key)
                    current.update(status="processed", reply="")
                    await self.save()
                return
            if message.get("ack_only"):
                async with self.lock:
                    self.finish_message(
                        key,
                        "Хорошо, отметил дело завершённым.",
                        message["acknowledged_task"],
                    )
                    await self.save()
                return
            expected = {k: t["revision"] for k, t in self.state["tasks"].items()}
            self.parser.model = self.options["model"]
            context = self.context(message)
            plan = await self.parser.parse(context)
            if origin == "classification":
                allowed = {t["id"] for t in context["tasks"]}
                seen = set()
                for op in plan["operations"]:
                    if (
                        op.get("action") != "update"
                        or op.get("task_id") not in allowed
                        or any(
                            op.get(k) is not None
                            for k in (
                                "title",
                                "notes",
                                "due_at",
                                "end_at",
                                "due_date",
                                "end_date",
                                "repeat_until_ack",
                            )
                        )
                        or op.get("clear_dates")
                    ):
                        raise InvalidPlan("Классификация не должна менять само дело")
                    if op.get("category") is None or op.get("priority") is None:
                        raise InvalidPlan("Не определены метки дела")
                    original = self.state["tasks"][op["task_id"]]
                    for field in ("category", "priority"):
                        if field not in original.get(
                            "classification_fields", ["category", "priority"]
                        ) and op.get(field) != original.get(field):
                            raise InvalidPlan("Не меняй выбранную человеком метку")
                    seen.add(op["task_id"])
                if seen != allowed:
                    raise InvalidPlan("Не все дела получили метки")
            async with self.lock:
                if expected != {k: t["revision"] for k, t in self.state["tasks"].items()}:
                    raise InvalidPlan("Список дел изменился во время ответа")
                self.state, changes = apply_operations(
                    self.state,
                    self.reply_operations(message, plan["operations"]),
                    self.now(),
                    key,
                    self.options,
                )
                self.finish_message(
                    key,
                    plan["reply"] or "Хорошо, запомнил.",
                    changes[0]["task"]["id"] if len(changes) == 1 else None,
                )
                self.queue_classification()
                self.last_error = None
                await self.save()
        except (CodexError, InvalidPlan) as error:
            async with self.lock:
                current = next(m for m in self.state["messages"] if m["key"] == key)
                current["status"] = "error"
                current["attempts"] += 1
                current["retry_at"] = (
                    self.now() + timedelta(minutes=5 * current["attempts"])
                ).isoformat()
                self.last_error = str(error) if isinstance(error, CodexError) else "invalid_plan"
                if current["attempts"] == 1 and origin == "telegram":
                    self.enqueue(
                        "error:" + key,
                        "Я сохранил твоё сообщение, но сейчас не получается его разобрать. Попробую ещё раз чуть позже.",
                        route=current.get("telegram_route"),
                    )
                await self.save()

    def finish_message(self, key, reply, task_id):
        current = next(m for m in self.state["messages"] if m["key"] == key)
        current["status"] = "processed"
        current["reply"] = reply if current.get("origin") != "classification" else ""
        if current.get("origin", "telegram") == "telegram":
            self.enqueue("reply:" + key, reply, task_id, route=current.get("telegram_route"))

    def reply_operations(self, message, operations):
        """A reply closes its task immediately; explicit edits can still move it."""
        if not isinstance(operations, list) or any(not isinstance(op, dict) for op in operations):
            return operations
        uid = message.get("acknowledged_task")
        task = self.state["tasks"].get(uid)
        if not task or task["status"] != "done" or task.get("completion_reason") != "reply":
            return operations
        if not any(h["source"] == message["key"] + ":receipt" for h in task["history"]):
            return operations
        result, closed = [], True
        for operation in operations:
            if operation.get("task_id") == uid:
                action = operation.get("action")
                if action == "update" and closed:
                    result.append({"action": "reopen", "task_id": uid})
                    result.append(operation)
                    moved = operation.get("clear_dates") or any(
                        operation.get(k) is not None
                        for k in ("due_at", "end_at", "due_date", "end_date")
                    )
                    if not moved:
                        result.append(
                            {"action": "done", "task_id": uid, "completion_reason": "reply"}
                        )
                    closed = not moved
                    continue
                if action == "reopen":
                    closed = False
                elif action in ("done", "cancel", "acknowledge"):
                    closed = True
            result.append(operation)
        return result

    async def tick(self, _now=None):
        if not self.options["enabled"] or self.stopping:
            return
        now = self.now()
        async with self.lock:
            self.state = complete_ready_tasks(
                self.state, now, dt_util.DEFAULT_TIME_ZONE, self.options
            )
            for message in self.state["messages"]:
                if (
                    message["status"] == "error"
                    and message["attempts"] < 3
                    and timestamp(message["retry_at"]) <= now
                ):
                    message["status"] = "pending"
            for task in active_tasks(self.state):
                scheduled = timestamp(task["next_reminder_at"])
                initial_pending = any(
                    i.get("task_id") == task["id"]
                    and i.get("proactive")
                    and i["status"] in ("pending", "sending")
                    for i in self.state["outbox"].values()
                )
                if scheduled and scheduled <= now:
                    if (
                        (not reminder_expired(task, now) or task.get("repeat_until_ack"))
                        and not task.get("reminder_ack_at")
                        and not initial_pending
                    ):
                        key = f"reminder:{task['id']}:{task['reminder_generation']}:{task.get('repeat_generation', 0)}"
                        self.enqueue(
                            key,
                            reminder_text(task, now, dt_util.DEFAULT_TIME_ZONE),
                            task["id"],
                            True,
                            kind="reminder",
                        )
                    task["next_reminder_at"] = None
                repeat_at = repeat_due(task, self.options["repeat_interval_minutes"])
                pending = any(
                    i.get("task_id") == task["id"]
                    and i.get("proactive")
                    and i["status"] in ("pending", "sending")
                    for i in self.state["outbox"].values()
                )
                if repeat_at and repeat_at <= now and not pending:
                    task["repeat_sequence"] = task.get("repeat_sequence", 0) + 1
                    key = f"repeat:{task['id']}:{task.get('repeat_generation', 0)}:{task['repeat_sequence']}"
                    self.enqueue(
                        key,
                        "Напомню ещё раз. "
                        + reminder_text(task, now, dt_util.DEFAULT_TIME_ZONE)
                        + " Дай знать, что увидел.",
                        task["id"],
                        True,
                        kind="repeat",
                    )
            await self.save()
        await self.deliver()
        await self.agenda.sync()
        self.kick()

    async def deliver(self):
        if not self.options["enabled"]:
            return
        async with self.send_lock:
            for key in list(self.state["outbox"]):
                async with self.lock:
                    item = self.state["outbox"][key]
                    if item["status"] != "pending" or (
                        item.get("retry_at") and timestamp(item["retry_at"]) > self.now()
                    ):
                        continue
                    task = self.state["tasks"].get(item["task_id"])
                    if item["proactive"] and (
                        not task
                        or task["status"] != "active"
                        or item.get("reminder_generation") != task.get("reminder_generation")
                        or task.get("reminder_ack_at")
                        or (reminder_expired(task, self.now()) and not task.get("repeat_until_ack"))
                        or (
                            item.get("kind") == "repeat"
                            and (
                                not task.get("repeat_until_ack")
                                or item.get("repeat_generation", 0)
                                != task.get("repeat_generation", 0)
                            )
                        )
                    ):
                        item["status"] = "cancelled"
                        await self.save()
                        continue
                    item["status"] = "sending"
                    await self.save()
                    route = (
                        self.telegram_route
                        if item["proactive"]
                        else item.get("telegram_route", self.telegram_route)
                    )
                    item["sent_route"] = dict(route)
                    payload = {
                        "config_entry_id": route["bot_entry_id"],
                        "chat_id": route["chat_id"],
                        "message": item["message"],
                        "parse_mode": "plain_text",
                    }
                try:
                    response = await self.hass.services.async_call(
                        "telegram_bot", "send_message", payload, blocking=True, return_response=True
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    async with self.lock:
                        item = self.state["outbox"][key]
                        item["attempts"] += 1
                        item["status"] = "pending" if item["attempts"] < 5 else "error"
                        item["retry_at"] = (
                            self.now() + timedelta(minutes=item["attempts"] * 2)
                        ).isoformat()
                        self.last_error = "telegram_unavailable"
                        await self.save()
                    _LOGGER.warning("Secretary message delayed (%s)", type(error).__name__)
                else:
                    async with self.lock:
                        item = self.state["outbox"][key]
                        item["status"] = "sent"
                        task = self.state["tasks"].get(item.get("task_id"))
                        if (
                            item["proactive"]
                            and task
                            and item.get("reminder_generation") == task.get("reminder_generation")
                        ):
                            task["reminder_sent_at"] = (
                                task.get("reminder_sent_at") or self.now().isoformat()
                            )
                            task["last_notification_at"] = self.now().isoformat()
                        for chat in (response or {}).get("chats", []):
                            ids = chat.get("message_id")
                            item["message_ids"].extend(ids if isinstance(ids, list) else [ids])
                        await self.save()
