"""Durable dated plans and reminders, including tasks without an exact time."""

from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta

from .const import DEFAULTS


class InvalidPlan(ValueError):
    """An invalid task operation; no partial changes are committed."""


def timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidPlan("Не удалось понять дату")
    try:
        date = datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidPlan("Не удалось понять дату") from error
    if date.tzinfo is None or date.utcoffset() is None:
        raise InvalidPlan("У даты должен быть часовой пояс")
    return date


def empty_state():
    return {
        "schema": 4,
        "next_id": 1,
        "tasks": {},
        "messages": [],
        "outbox": {},
        "agendas": {},
        "google": {},
    }


def day_value(value):
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as error:
        raise InvalidPlan("Укажите дату в формате ГГГГ-ММ-ДД") from error


def task_bounds(task, timezone):
    if task.get("due_date"):
        start = day_value(task["due_date"])
        end = day_value(task["end_date"]) if task.get("end_date") else start + timedelta(days=1)
        return datetime.combine(start, time(), timezone), datetime.combine(end, time(), timezone)
    start = timestamp(task.get("due_at"))
    return start, (
        timestamp(task.get("end_at")) or start + timedelta(minutes=30)
    ) if start else None


def reminder_expired(task, now):
    start, end = task_bounds(task, now.tzinfo)
    return not start or (end if task.get("due_date") else start) <= now


def complete_ready_tasks(state, now, timezone, options=None):
    """Close elapsed ordinary events and previously acknowledged persistent plans."""
    operations = []
    for task in active_tasks(state):
        if task.get("repeat_until_ack"):
            if task.get("reminder_ack_at"):
                operations.append(
                    {"action": "done", "task_id": task["id"], "completion_reason": "reply"}
                )
            continue
        _, end = task_bounds(task, timezone)
        if end is not None and end <= now:
            operations.append(
                {"action": "done", "task_id": task["id"], "completion_reason": "elapsed"}
            )
    result = state
    for offset in range(0, len(operations), 20):
        result, _ = apply_operations(
            result,
            operations[offset : offset + 20],
            now,
            "auto-complete:" + now.isoformat(),
            options,
        )
    return result


def mark_completed(task, now, reason):
    if task["status"] != "done":
        task["completed_at"] = now.isoformat()
        task["completion_reason"] = reason
    task["status"] = "done"
    task["next_reminder_at"] = None
    if reason == "reply":
        task["reminder_ack_at"] = task.get("reminder_ack_at") or now.isoformat()


def active_tasks(state):
    return sorted(
        (t for t in state["tasks"].values() if t["status"] == "active"),
        key=lambda t: (
            not (t.get("due_at") or t.get("due_date")),
            task_bounds(t, UTC)[0] or datetime.max.replace(tzinfo=UTC),
            int(t["id"]),
        ),
    )


def text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InvalidPlan("Пустой или слишком длинный текст")
    return value.strip()


def validate_labels(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 30:
        raise InvalidPlan("Нужно от одного до тридцати названий")
    labels = [text(v, 60) for v in values]
    if len({v.casefold() for v in labels}) != len(labels):
        raise InvalidPlan("Названия не должны повторяться")
    return labels


def choose_reminder(task, now, rng=None, options=None):
    options = {**DEFAULTS, **(options or {})}
    task["reminder_generation"] = task.get("reminder_generation", 0) + 1
    due, end = task_bounds(task, now.tzinfo)
    task["reminder_sent_at"] = None
    task["last_notification_at"] = None
    task["reminder_ack_at"] = None
    task["repeat_generation"] = task.get("repeat_generation", 0) + 1
    task["repeat_sequence"] = 0
    if (
        due is None
        or ((end if task.get("due_date") else due) <= now and not task.get("repeat_until_ack"))
        or task["status"] != "active"
    ):
        task["reminder_lead_seconds"] = None
        task["next_reminder_at"] = None
        return
    if task.get("due_date"):
        clock = time.fromisoformat(options["all_day_reminder_time"])
        scheduled = datetime.combine(due.date(), clock, now.tzinfo)
        if options["all_day_reminder_mode"] == "after_hour" and due.date() == now.date():
            scheduled = now + timedelta(hours=1)
        # A late-night task still gets its first reminder on its selected day.
        scheduled = min(scheduled, end - timedelta(seconds=1))
        task["reminder_lead_seconds"] = None
    else:
        scheduled = due - timedelta(hours=1)
        task["reminder_lead_seconds"] = 3600
    task["next_reminder_at"] = max(now, scheduled).isoformat()


def apply_operations(state, operations, now, source, options=None, rng=None):
    if not isinstance(operations, list) or len(operations) > 20:
        raise InvalidPlan("Слишком много изменений за один раз")
    options = {**DEFAULTS, **(options or {})}
    result = deepcopy(state)
    changes = []
    for op in operations:
        if not isinstance(op, dict):
            raise InvalidPlan("Не удалось понять изменение")
        kind = op.get("action")
        if kind not in ("create", "update", "done", "cancel", "reopen", "acknowledge"):
            raise InvalidPlan("Не удалось понять действие")
        if kind == "create":
            uid = str(result["next_id"])
            result["next_id"] += 1
            task = {
                "id": uid,
                "title": text(op.get("title"), 300),
                "notes": "",
                "due_at": None,
                "end_at": None,
                "due_date": None,
                "end_date": None,
                "next_reminder_at": None,
                "reminder_sent_at": None,
                "reminder_lead_seconds": None,
                "status": "active",
                "completed_at": None,
                "completion_reason": None,
                "repeat_until_ack": False,
                "reminder_ack_at": None,
                "last_notification_at": None,
                "repeat_generation": 0,
                "repeat_sequence": 0,
                "created_at": now.isoformat(),
                "category": None,
                "priority": options["priorities"][0],
                "classification_pending": True,
                "classification_fields": ["category", "priority"],
                "source": source,
                "history": [],
                "revision": 0,
            }
            result["tasks"][uid] = task
        else:
            uid = str(op.get("task_id"))
            if uid not in result["tasks"]:
                raise InvalidPlan("Не понял, о каком деле речь")
            task = result["tasks"][uid]
        old_due = task["due_at"]
        old_end = task.get("end_at")
        old_day = task.get("due_date")
        old_end_day = task.get("end_date")
        old_repeat = task.get("repeat_until_ack", False)
        if kind in ("create", "update", "reopen"):
            if kind == "update" and task["status"] != "active":
                raise InvalidPlan("Это дело уже завершено")
            if op.get("repeat_until_ack") is not None:
                if not isinstance(op["repeat_until_ack"], bool):
                    raise InvalidPlan("Режим напоминания должен быть включён или выключен")
                task["repeat_until_ack"] = op["repeat_until_ack"]
                if old_repeat != task["repeat_until_ack"]:
                    task["repeat_generation"] = task.get("repeat_generation", 0) + 1
                    if task["repeat_until_ack"]:
                        task["reminder_ack_at"] = None
            if kind == "reopen":
                task["status"] = "active"
                task["completed_at"] = None
                task["completion_reason"] = None
            if op.get("title") is not None:
                task["title"] = text(op["title"], 300)
            if op.get("notes") is not None:
                if not isinstance(op["notes"], str) or len(op["notes"]) > 3000:
                    raise InvalidPlan("Слишком длинное описание")
                task["notes"] = op["notes"]
            if op.get("clear_dates"):
                task["due_at"] = task["end_at"] = None
                task["due_date"] = task["end_date"] = None
            if op.get("due_date") and op.get("due_at"):
                raise InvalidPlan("Выберите день целиком или точное время")
            if op.get("due_date"):
                start_day = day_value(op["due_date"])
                duration = (
                    day_value(old_end_day) - day_value(old_day)
                    if old_day and old_end_day
                    else timedelta(days=1)
                )
                task["due_date"] = start_day.isoformat()
                task["end_date"] = (start_day + duration).isoformat()
                task["due_at"] = task["end_at"] = None
            if op.get("end_date"):
                task["end_date"] = day_value(op["end_date"]).isoformat()
            for key in ("due_at", "end_at"):
                if op.get(key) is not None:
                    task[key] = timestamp(op[key]).isoformat()
            if op.get("due_at"):
                task["due_date"] = task["end_date"] = None
            if (
                kind == "create"
                and not task["due_at"]
                and not task.get("due_date")
                and not op.get("clear_dates")
            ):
                task["due_date"] = now.date().isoformat()
                task["end_date"] = (now.date() + timedelta(days=1)).isoformat()
            if task.get("end_date") and (
                not task.get("due_date")
                or day_value(task["end_date"]) <= day_value(task["due_date"])
            ):
                raise InvalidPlan("Конец события должен быть позже начала")
            if task["due_at"] is None:
                task["end_at"] = None
            elif old_due != task["due_at"] and op.get("end_at") is None:
                duration = (
                    timestamp(old_end) - timestamp(old_due)
                    if old_end and old_due
                    else timedelta(minutes=30)
                )
                task["end_at"] = (timestamp(task["due_at"]) + duration).isoformat()
            if task["end_at"] and timestamp(task["end_at"]) <= timestamp(task["due_at"]):
                raise InvalidPlan("Конец события должен быть позже начала")
            classification_fields = set(task.get("classification_fields", []))
            for key, choices in (
                ("category", options["categories"]),
                ("priority", options["priorities"]),
            ):
                if op.get(key) is not None:
                    if op[key] not in choices:
                        raise InvalidPlan("Такой категории или важности больше нет")
                    task[key] = op[key]
                    classification_fields.discard(key)
            if "classify_fields" in op:
                if not isinstance(op["classify_fields"], list) or set(op["classify_fields"]) - {
                    "category",
                    "priority",
                }:
                    raise InvalidPlan("Не удалось определить метки")
                classification_fields.update(op["classify_fields"])
            task["classification_fields"] = sorted(classification_fields)
            task["classification_pending"] = bool(classification_fields)
            if (
                kind in ("create", "reopen")
                or old_due != task["due_at"]
                or old_day != task.get("due_date")
            ):
                choose_reminder(task, now, rng, options)
            elif (
                task.get("repeat_until_ack")
                and not old_repeat
                and task.get("last_notification_at") is None
                and task["due_at"]
                and timestamp(task["due_at"]) <= now
            ):
                task["next_reminder_at"] = now.isoformat()
        elif kind == "acknowledge":
            task["reminder_ack_at"] = now.isoformat()
            task["next_reminder_at"] = None
            task["repeat_generation"] = task.get("repeat_generation", 0) + 1
            if task["status"] == "active" and task.get("repeat_until_ack"):
                mark_completed(task, now, "reply")
        else:
            if kind == "done":
                reason = op.get("completion_reason", "manual")
                if reason not in ("manual", "elapsed", "reply"):
                    raise InvalidPlan("Не удалось определить причину завершения")
                mark_completed(task, now, reason)
            else:
                task["status"] = "cancelled"
                task["next_reminder_at"] = None
        task["revision"] += 1
        task["updated_at"] = now.isoformat()
        task["history"].append({"action": kind, "at": now.isoformat(), "source": source})
        task["history"] = task["history"][-50:]
        changes.append({"action": kind, "task": deepcopy(task)})
    return result, changes


def migrate_state(state, now, options=None):
    """Keep plans/history, drop all old recurring schedules and command notifications."""
    if state.get("schema", 1) >= 4:
        return state
    original_schema = state.get("schema", 1)
    state = deepcopy(state)
    if original_schema < 2:
        state["schema"] = 2
        for key in ("briefings", "daily", "welcomed"):
            state.pop(key, None)
        for item in state["outbox"].values():
            if item["status"] in ("pending", "sending", "error", "uncertain"):
                item["status"] = "cancelled"
        for message in state["messages"]:
            if message["text"].startswith(("/", "ps:")):
                message["status"] = "processed"
        for task in state["tasks"].values():
            task.pop("remind_at", None)
            task["category"] = None
            task["priority"] = DEFAULTS["priorities"][0]
            task["classification_pending"] = True
            task["classification_fields"] = ["category", "priority"]
            task["end_at"] = (
                (timestamp(task["due_at"]) + timedelta(minutes=30)).isoformat()
                if task["due_at"]
                else None
            )
            choose_reminder(task, now)
    state["schema"] = 3
    state.setdefault("agendas", {})
    for task in state["tasks"].values():
        task.setdefault("repeat_until_ack", False)
        task.setdefault("reminder_ack_at", None)
        task.setdefault("last_notification_at", task.get("reminder_sent_at"))
        task.setdefault("repeat_generation", 0)
        task.setdefault("repeat_sequence", 0)
        task.setdefault("due_date", None)
        task.setdefault("end_date", None)
        if task["status"] == "active" and not task.get("due_at"):
            task["due_date"] = now.date().isoformat()
            task["end_date"] = (now.date() + timedelta(days=1)).isoformat()
        if (
            task["status"] == "active"
            and not task.get("last_notification_at")
            and not task.get("reminder_ack_at")
        ):
            choose_reminder(task, now, options=options)
    state["schema"] = 4
    state.setdefault("google", {})
    return state


def reminder_text(task, now, timezone):
    when = task_bounds(task, timezone)[0].astimezone(timezone)
    days = (when.date() - now.astimezone(timezone).date()).days
    day = "Сегодня" if days == 0 else "Завтра" if days == 1 else when.strftime("%d.%m")
    moment = "в течение дня" if task.get("due_date") else f"в {when:%H:%M}"
    return f"{day} {moment} у тебя запланировано: {task['title']}."


def awaiting_ack(task):
    return (
        task["status"] == "active"
        and bool(task.get("repeat_until_ack"))
        and bool(task.get("last_notification_at"))
        and not task.get("reminder_ack_at")
    )


def repeat_due(task, interval):
    if not awaiting_ack(task) or task.get("next_reminder_at"):
        return None
    return timestamp(task["last_notification_at"]) + timedelta(minutes=interval)


def validate_repeat_interval(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or int(value) != value
        or not 1 <= value <= 180
    ):
        raise InvalidPlan("Интервал повторов — от 1 до 180 минут")
    return int(value)


def validate_day_settings(mode, clock):
    if mode not in ("after_hour", "day_time"):
        raise InvalidPlan("Выберите время напоминания о деле на день")
    try:
        if not isinstance(clock, str) or len(clock) != 5:
            raise ValueError
        time.fromisoformat(clock)
    except ValueError as error:
        raise InvalidPlan("Укажите время в формате ЧЧ:ММ") from error
    return mode, clock
