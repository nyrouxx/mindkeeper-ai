"""Calendar sync through the Google Calendar integration's authenticated client.

OAuth and refresh tokens belong exclusively to the native Google integration.
Its calendar entity cannot update events, so this adapter uses its existing
gcal-sync service for updates and verifies every remote mutation by reading it.
"""

import asyncio
import logging
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from .calendar_recovery import calendar_key, recover_links, recovery_sources
from .model import InvalidPlan, apply_operations, task_bounds, timestamp

_LOGGER = logging.getLogger(__name__)
DATE_FIELDS = ("due_at", "end_at", "due_date", "end_date")
GROUPS = (("title",), ("notes",), DATE_FIELDS, ("status",))
MARKER = re.compile(r"\n\nmindKeeper AI · (активно|выполнено)$")


def calendar_choices(hass):
    from homeassistant.helpers import entity_registry as er

    return [
        {"value": e.entity_id, "label": e.name or e.original_name or e.entity_id}
        for e in er.async_get(hass).entities.values()
        if e.domain == "calendar" and e.platform == "google" and not e.disabled_by
    ]


def validate_selection(hass, calendar, mode):
    if mode not in ("bidirectional", "export"):
        raise InvalidPlan("Выберите направление синхронизации")
    if calendar and calendar not in {e["value"] for e in calendar_choices(hass)}:
        raise InvalidPlan("Выберите календарь из штатной интеграции Google Calendar")
    return calendar, mode


def task_data(task):
    return {
        "title": task["title"],
        "notes": task.get("notes", ""),
        "due_at": timestamp(task["due_at"]).astimezone(UTC).isoformat()
        if task.get("due_at")
        else None,
        "end_at": timestamp(task["end_at"]).astimezone(UTC).isoformat()
        if task.get("end_at")
        else None,
        "due_date": task.get("due_date"),
        "end_date": task.get("end_date"),
        "status": task["status"],
    }


def remote_data(event):
    """Convert native gcal-sync models, including true date-only events."""
    start, end = event.start.value, event.end.value
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise InvalidPlan("Google вернул событие с разными форматами дат")
    title, notes = event.summary, event.description or ""
    marker = MARKER.search(notes)
    done = bool(marker and marker[1] == "выполнено" and title.startswith("✅ "))
    if marker:
        notes = notes[: marker.start()]
        if done and title.startswith("✅ "):
            title = title[2:]
    timed = isinstance(start, datetime)
    if timed and (start.tzinfo is None or end.tzinfo is None):
        raise InvalidPlan("Google вернул дату без часового пояса")
    return {
        "title": title or "Без названия",
        "notes": notes,
        "due_at": start.astimezone(UTC).isoformat() if timed else None,
        "end_at": end.astimezone(UTC).isoformat() if timed else None,
        "due_date": None if timed else start.isoformat(),
        "end_date": None if timed else end.isoformat(),
        "status": "done" if done else "active",
    }


def event_body(data):
    if data["due_date"]:
        start, end = {"date": data["due_date"]}, {"date": data["end_date"]}
    else:
        start, end = {"dateTime": data["due_at"]}, {"dateTime": data["end_at"]}
    done = data["status"] == "done"
    return {
        "summary": ("✅ " if done else "") + data["title"],
        "description": data["notes"] + f"\n\nmindKeeper AI · {'выполнено' if done else 'активно'}",
        "start": start,
        "end": end,
    }


def merge_data(base, local, remote):
    """Three-way merge; Google wins a simultaneous edit of the same field group."""
    result, conflicts = deepcopy(local), 0
    for group in GROUPS:
        remote_changed = any(remote[k] != base[k] for k in group)
        local_changed = any(local[k] != base[k] for k in group)
        if remote_changed:
            if local_changed and any(local[k] != remote[k] for k in group):
                conflicts += 1
            for key in group:
                result[key] = remote[key]
    return result, conflicts


def response_status(error):
    seen = set()
    while error and id(error) not in seen:
        seen.add(id(error))
        if isinstance(getattr(error, "status", None), int):
            return error.status
        error = error.__cause__ or error.__context__
    return None


class NativeGoogleCalendar:
    def __init__(self, hass, entity_id):
        from homeassistant.components.calendar.const import DATA_COMPONENT
        from homeassistant.helpers import entity_registry as er

        registry_entry = er.async_get(hass).async_get(entity_id)
        component = hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(entity_id) if component else None
        entry = (
            hass.config_entries.async_get_entry(registry_entry.config_entry_id)
            if registry_entry
            else None
        )
        if (
            not entity
            or not entry
            or entry.domain != "google"
            or not getattr(entry, "runtime_data", None)
        ):
            raise InvalidPlan("Google Calendar пока недоступен")
        if getattr(entity.entity_description, "read_only", True):
            raise InvalidPlan("Для синхронизации нужен календарь Google с правом изменения")
        self.service = entry.runtime_data.service
        self.calendar_id = entity.calendar_id
        self.entity = entity
        self.key = calendar_key(self.calendar_id)
        self.legacy_key = sha256(f"{entry.entry_id}:{self.calendar_id}".encode()).hexdigest()

    async def events(self, start, end):
        from gcal_sync.api import ListEventsRequest

        response = await self.service.async_list_events(
            ListEventsRequest(calendar_id=self.calendar_id, start_time=start, end_time=end)
        )
        result = {}
        async for page in response:
            for event in page.items:
                if event.id and str(event.status) not in ("cancelled", "EventStatusEnum.CANCELLED"):
                    if str(event.event_type) not in ("default", "EventTypeEnum.DEFAULT"):
                        continue
                    if any(
                        a.is_self
                        and str(a.response_status) in ("declined", "ResponseStatus.DECLINED")
                        for a in event.attendees
                    ):
                        continue
                    result[event.id] = event
                if len(result) > 2000:
                    raise InvalidPlan(
                        "В календаре слишком много событий. Выберите отдельный календарь для дел"
                    )
        return result

    async def get(self, uid):
        try:
            event = await self.service.async_get_event(self.calendar_id, uid)
        except Exception as error:
            if response_status(error) in (404, 410):
                return None
            raise
        return None if str(event.status) in ("cancelled", "EventStatusEnum.CANCELLED") else event

    async def write(self, uid, body, create=False):
        if create:
            from gcal_sync.model import Event

            try:
                await self.service.async_create_event(
                    self.calendar_id, Event.model_validate({"id": uid, **body})
                )
            except Exception as error:
                if response_status(error) != 409:
                    raise
        else:
            # Patch recursively merges objects: explicitly clear the other date type.
            body = deepcopy(body)
            for key in ("start", "end"):
                body[key].update(
                    {"dateTime": None, "timeZone": None} if "date" in body[key] else {"date": None}
                )
            await self.service.async_patch_event(self.calendar_id, uid, body)
        actual = await self.get(uid)
        if actual is None:
            raise InvalidPlan("Google не подтвердил сохранение события")
        return remote_data(actual)

    async def delete(self, uid):
        await self.service.async_delete_event(self.calendar_id, uid)
        if await self.get(uid) is not None:
            raise InvalidPlan("Google не подтвердил удаление события")

    async def refresh(self):
        await self.entity.coordinator.async_request_refresh()


class GoogleSync:
    def __init__(self, secretary, adapter=NativeGoogleCalendar):
        self.sec = secretary
        self.adapter = adapter
        self.worker = None
        self.lock = asyncio.Lock()
        self.last_attempt = None
        self.last_signature = None
        self.last_key = None
        self.error = None

    def public(self):
        options = self.sec.options
        board = self.sec.state.get("google", {}).get(self.last_key, {})
        return {
            "calendar": options["google_calendar"],
            "mode": options["google_sync_mode"],
            "calendars": calendar_choices(self.sec.hass),
            "status": "disabled"
            if not options["google_calendar"]
            else "error"
            if self.error
            else "ready"
            if board.get("last_success")
            else "waiting",
            "last_success": board.get("last_success"),
            "error": self.error,
            "conflicts": board.get("conflicts", 0),
        }

    def kick(self):
        if not self.sec.options["google_calendar"] or self.sec.stopping:
            return
        if self.worker is None or self.worker.done():
            self.worker = self.sec.hass.async_create_background_task(
                self.sync(), "mindkeeper_google_sync"
            )

    async def stop(self):
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    def signature(self):
        return (
            self.sec.options["google_calendar"],
            self.sec.options["google_sync_mode"],
            tuple((t["id"], t["revision"]) for t in self.sec.state["tasks"].values()),
        )

    async def sync(self, force=False):
        async with self.lock:
            sec = self.sec
            if sec.stopping or not sec.options["enabled"] or not sec.options["google_calendar"]:
                return
            signature = self.signature()
            if not force and self.last_attempt:
                elapsed = (sec.now() - self.last_attempt).total_seconds()
                if elapsed < (60 if self.error else 300) and (
                    self.error or signature == self.last_signature
                ):
                    return
            self.last_attempt, self.last_signature = sec.now(), signature
            selection = (sec.options["google_calendar"], sec.options["google_sync_mode"])
            try:
                async with asyncio.timeout(120):
                    adapter = self.adapter(sec.hass, selection[0])
                    self.last_key = adapter.key
                    events = await adapter.events(
                        sec.now() - timedelta(days=30), sec.now() + timedelta(days=365)
                    )
                    if not await self.prepare_calendar(adapter, events, selection):
                        return
                    if not await self.clean_retired_events(adapter, events, selection):
                        return
                    board = sec.state["google"][adapter.key]
                    conflicts = 0
                    for uid in list(board["records"]):
                        if self.stale(selection):
                            return
                        event = events.pop(uid, None)
                        if event is None:
                            event = await adapter.get(uid)
                        conflicts += await self.reconcile(adapter, uid, event, selection[1])
                    if selection[1] == "bidirectional":
                        for uid, event in events.items():
                            if self.stale(selection):
                                return
                            await self.import_event(adapter.key, uid, event)
                    mapped = {
                        r["task_id"] for r in sec.state["google"][adapter.key]["records"].values()
                    }
                    for task in list(sec.state["tasks"].values()):
                        if self.stale(selection):
                            return
                        if (
                            task["id"] in mapped
                            or task["status"] != "active"
                            or not (task.get("due_at") or task.get("due_date"))
                        ):
                            continue
                        uid = "mk" + uuid4().hex  # Google IDs use base32hex; m/k and hex are valid.
                        async with sec.lock:
                            sec.state["google"][adapter.key]["records"][uid] = {
                                "task_id": task["id"],
                                "base": None,
                            }
                            await sec.save()  # Reserve a stable ID BEFORE any remote side effect.
                        await self.reconcile(adapter, uid, await adapter.get(uid), selection[1])
                    async with sec.lock:
                        board = sec.state["google"][adapter.key]
                        board.update(
                            last_success=sec.now().isoformat(),
                            conflicts=conflicts,
                            sync_runs=board.get("sync_runs", 0) + 1,
                        )
                        self.error = None
                        await sec.save()
                    await adapter.refresh()
                    # A concurrent local edit must remain dirty for the next pass.
                    self.last_signature = signature
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = (
                    str(error)
                    if isinstance(error, InvalidPlan)
                    else "Не удалось связаться с Google Calendar. Проверьте штатную интеграцию Google."
                )
                _LOGGER.warning("mindKeeper calendar sync delayed (%s)", type(error).__name__)
                async with sec.lock:
                    await sec.save()

    def stale(self, selection):
        return (
            self.sec.stopping
            or not self.sec.options["enabled"]
            or selection
            != (self.sec.options["google_calendar"], self.sec.options["google_sync_mode"])
        )

    async def prepare_calendar(self, adapter, events, selection):
        sec = self.sec
        if self.stale(selection):
            return False
        legacy_key = getattr(adapter, "legacy_key", adapter.key)
        sources = recovery_sources(sec.state, adapter.key, legacy_key, events)
        if (
            sources == [adapter.key]
            and sec.state["google"][adapter.key].get("identity_version") == 2
        ):
            return True
        observed_events = dict(events)
        for source in sources:
            for uid in sec.state["google"][source]["records"]:
                if uid not in observed_events:
                    observed_events[uid] = await adapter.get(uid)
                    if self.stale(selection):
                        return False
        observed = {
            uid: remote_data(event) if event else None for uid, event in observed_events.items()
        }
        async with sec.lock:
            if self.stale(selection):
                return False
            sec.state, report = recover_links(
                sec.state, adapter.key, sources, observed, sec.now(), sec.options, task_data
            )
            await sec.save()
        if report and report["merged_tasks"]:
            _LOGGER.info(
                "Recovered calendar links; archived %d reimported tasks",
                len(report["merged_tasks"]),
            )
        return True

    async def clean_retired_events(self, adapter, events, selection):
        """Verify and archive redundant cloud copies before deleting them."""
        sec = self.sec
        board = sec.state["google"][adapter.key]
        for uid in list(board.get("retired_events", {})):
            if self.stale(selection):
                return False
            retired = deepcopy(sec.state["google"][adapter.key]["retired_events"][uid])
            event = await adapter.get(uid)
            if event is not None:
                keeper = await adapter.get(retired["keep_uid"])
                if (
                    keeper is None
                    or remote_data(event) != retired["expected"]
                    or remote_data(keeper) != retired["expected"]
                ):
                    raise InvalidPlan(
                        "Событие-дубль изменилось в Google. Очистка остановлена, данные сохранены"
                    )
                if any(
                    getattr(event, field, None)
                    for field in (
                        "location",
                        "attendees",
                        "recurrence",
                        "recurring_event_id",
                        "color_id",
                        "event_label_id",
                    )
                ):
                    raise InvalidPlan(
                        "У события-дубля есть дополнительные данные Google. Автоматическая очистка остановлена"
                    )
                backup = (
                    event.model_dump(mode="json", by_alias=True, exclude_none=True)
                    if hasattr(event, "model_dump")
                    else remote_data(event)
                )
                async with sec.lock:
                    sec.state["google"][adapter.key]["retired_events"][uid]["remote_backup"] = (
                        backup
                    )
                    await sec.save()
                if self.stale(selection):
                    return False
                await adapter.delete(uid)
            events.pop(uid, None)
            async with sec.lock:
                board = sec.state["google"][adapter.key]
                archived = board["retired_events"].pop(uid)
                board.setdefault("removed_duplicates", {})[uid] = {
                    **archived,
                    "deleted_at": sec.now().isoformat(),
                }
                await sec.save()
        return True

    async def import_event(self, key, uid, event):
        data = remote_data(event)
        if data["status"] != "active":
            return
        async with self.sec.lock:
            # Mapping and task creation are one atomic store update.
            self.sec.state, changes = apply_operations(
                self.sec.state,
                [{"action": "create", **{k: data[k] for k in ("title", "notes", *DATE_FIELDS)}}],
                self.sec.now(),
                "google:" + uid,
                self.sec.options,
            )
            task_id = changes[0]["task"]["id"]
            self.sec.state["google"][key]["records"][uid] = {"task_id": task_id, "base": data}
            self.sec.queue_classification()
            await self.sec.save()
        self.sec.kick()

    async def local_change(self, task_id, data, revision):
        async with self.sec.lock:
            task = self.sec.state["tasks"].get(task_id)
            if task is None or task["revision"] != revision:
                return False
            operations = []
            if data["status"] != "cancelled":
                if task["status"] != "active":
                    operations.append({"action": "reopen", "task_id": task_id})
                operations.append(
                    {
                        "action": "update",
                        "task_id": task_id,
                        **{k: data[k] for k in ("title", "notes", *DATE_FIELDS)},
                        "clear_dates": not (data["due_at"] or data["due_date"]),
                    }
                )
            if data["status"] != "active":
                operations.append(
                    {"action": "done" if data["status"] == "done" else "cancel", "task_id": task_id}
                )
            self.sec.state, _ = apply_operations(
                self.sec.state,
                operations,
                self.sec.now(),
                "google:" + uuid4().hex,
                self.sec.options,
            )
            await self.sec.save()
        return True

    async def reconcile(self, adapter, uid, event, mode):
        sec = self.sec
        record = deepcopy(sec.state["google"][adapter.key]["records"][uid])
        task = deepcopy(sec.state["tasks"].get(record["task_id"]))
        if task is None:
            return 0
        local = task_data(task)
        base = record.get("base")
        expected_remote = bool(
            base and (base["due_at"] or base["due_date"]) and base["status"] != "cancelled"
        )
        remote = remote_data(event) if event else None
        conflicts = 0
        if (
            remote is None
            and expected_remote
            and mode == "bidirectional"
            and not (base["status"] == "done" and local["status"] == "active")
        ):
            # A remote deletion wins a simultaneous local edit; retain the cancelled task/history.
            conflicts += int(local != base and local["status"] != "cancelled")
            local = {**local, "status": "cancelled"}
            if task_data(task) != local and not await self.local_change(
                task["id"], local, task["revision"]
            ):
                return 0
        elif base and remote and mode == "bidirectional":
            merge_local, merge_remote = local, remote
            # Moving an automatically closed event into the future starts it again.
            if (
                task.get("completion_reason") == "elapsed"
                and local["status"] == "done"
                and any(remote[k] != base[k] for k in DATE_FIELDS)
                and task_bounds(remote, sec.now().tzinfo)[1] > sec.now()
            ):
                merge_local = {**local, "status": "active"}
                merge_remote = {**remote, "status": "active"}
            merged, conflicts = merge_data(base, merge_local, merge_remote)
            if merged != local:
                if not await self.local_change(task["id"], merged, task["revision"]):
                    return 0
                local = merged
        remove = local["status"] == "cancelled" or not (local["due_at"] or local["due_date"])
        if remove:
            if remote:
                await adapter.delete(uid)
        elif remote != local:
            # A second read avoids overwriting an edit made while processing other events.
            fresh = await adapter.get(uid)
            if (remote_data(fresh) if fresh else None) != remote:
                return conflicts
            if remote is None and base is not None:
                # Deleted Google IDs are tombstones and cannot safely be reused.
                replacement = "mk" + uuid4().hex
                async with sec.lock:
                    records = sec.state["google"][adapter.key]["records"]
                    records[replacement] = {"task_id": task["id"], "base": None}
                    del records[uid]
                    await sec.save()
                uid = replacement
            actual = await adapter.write(uid, event_body(local), create=remote is None)
            if actual != local:
                raise InvalidPlan(
                    "Google не подтвердил изменение события; повторю синхронизацию позже"
                )
        async with sec.lock:
            sec.state["google"][adapter.key]["records"][uid]["base"] = local
            await sec.save()
        return conflicts
