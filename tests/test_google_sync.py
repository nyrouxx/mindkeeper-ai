"""Exercise the durable sync engine without a Google account or real events."""

import sys
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from support import Hass, Store, component, entry, install_ha_fakes
from test_model import NOW

install_ha_fakes()
c = component("coordinator")
g = component("google_sync")
m = component("model")


def event(uid, title="Встреча", start=None, end=None, notes=""):
    start = start or NOW + timedelta(days=1)
    end = end or start + timedelta(hours=1)
    return SimpleNamespace(
        id=uid,
        status="confirmed",
        event_type="default",
        attendees=[],
        summary=title,
        description=notes,
        start=SimpleNamespace(value=start),
        end=SimpleNamespace(value=end),
    )


class Remote:
    key = "native-calendar"

    def __init__(self):
        self.data = {}
        self.writes = []
        self.deletes = []
        self.fail = False
        self.lose_receipt = False
        self.exclude = set()
        self.during_write = None

    async def events(self, start, end):
        if self.fail:
            raise ConnectionError("synthetic failure")
        return deepcopy({k: v for k, v in self.data.items() if k not in self.exclude})

    async def get(self, uid):
        return deepcopy(self.data.get(uid))

    async def write(self, uid, body, create=False):
        self.writes.append((uid, deepcopy(body), create))
        if create and uid in self.data:
            raise AssertionError("Duplicate creation")

        def value(d):
            return (
                date.fromisoformat(d["date"])
                if d.get("date")
                else datetime.fromisoformat(d["dateTime"])
            )

        self.data[uid] = event(
            uid, body["summary"], value(body["start"]), value(body["end"]), body["description"]
        )
        if self.lose_receipt:
            self.lose_receipt = False
            raise ConnectionError("Created remotely; response was lost")
        if self.during_write:
            callback, self.during_write = self.during_write, None
            await callback()
        return g.remote_data(self.data[uid])

    async def delete(self, uid):
        self.deletes.append(uid)
        self.data.pop(uid, None)

    async def refresh(self):
        pass


class GoogleSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Store.database.clear()
        self.e = entry()
        self.e.options.update(google_calendar="calendar.google", google_sync_mode="bidirectional")
        self.s = c.Secretary(Hass(), self.e)
        self.s.now = lambda: NOW
        self.s.kick = lambda: None
        self.remote = Remote()
        self.sync = g.GoogleSync(self.s, adapter=lambda *args: self.remote)

    async def create(self, **fields):
        await self.s.change([{"action": "create", "title": "Магазин", **fields}], "new")

    async def run_sync(self):
        await self.sync.sync(force=True)
        self.assertIsNone(self.sync.error)

    def task(self):
        return self.s.state["tasks"]["1"]

    async def test_exports_and_imports_all_day_without_duplicates_after_restart(self):
        await self.create()
        self.remote.data["remote"] = event(
            "remote", start=date(2026, 11, 12), end=date(2026, 11, 14)
        )
        await self.run_sync()
        self.assertEqual(len(self.s.state["tasks"]), 2)
        self.assertEqual(len(self.remote.data), 2)
        imported = self.s.state["tasks"]["2"]
        self.assertEqual(imported["due_date"], "2026-11-12")
        self.assertEqual(imported["end_date"], "2026-11-14")
        self.assertIsNone(imported["due_at"])
        self.s.state = deepcopy(Store.database[self.s.store.key])
        self.sync = g.GoogleSync(self.s, adapter=lambda *args: self.remote)
        await self.run_sync()
        self.assertEqual(len(self.remote.writes), 1)
        self.assertEqual(len(self.s.state["tasks"]), 2)

    async def test_legacy_reconnect_relinks_existing_remote_events_without_import_or_export(self):
        await self.create()
        await self.run_sync()
        self.s.state["google"][self.remote.key].pop("identity_version", None)
        self.remote.key = "stable-calendar"
        self.remote.legacy_key = "new-native-entry"
        self.sync = g.GoogleSync(self.s, adapter=lambda *args: self.remote)
        await self.run_sync()
        self.assertEqual(len(self.s.state["tasks"]), 1)
        self.assertEqual(len(self.remote.writes), 1)
        self.assertEqual(len(self.s.state["google"]), 1)

    async def seed_reconnect_duplicates(self):
        await self.create(category="Работа", priority="Важная")
        await self.run_sync()
        original_uid = next(iter(self.remote.data))
        old = self.s.state["google"][self.remote.key]
        old.pop("identity_version", None)
        self.s.state["google"]["new-native-entry"] = {"records": {}}
        await self.sync.import_event(
            "new-native-entry", original_uid, self.remote.data[original_uid]
        )
        copy_uid = "mk" + "b" * 32
        await self.remote.write(copy_uid, g.event_body(g.task_data(self.task())), create=True)
        self.s.state["google"]["new-native-entry"]["records"][copy_uid] = {
            "task_id": "1",
            "base": g.task_data(self.task()),
        }
        self.remote.key = "stable-calendar"
        self.remote.legacy_key = "new-native-entry"
        self.sync = g.GoogleSync(self.s, adapter=lambda *args: self.remote)
        await self.s.save()
        return original_uid, copy_uid

    async def test_repairs_proven_reimport_duplicates_and_removes_only_redundant_cloud_copy(self):
        original_uid, copy_uid = await self.seed_reconnect_duplicates()
        await self.run_sync()
        self.assertEqual(self.task()["status"], "active")
        self.assertEqual(self.task()["category"], "Работа")
        self.assertEqual(self.task()["priority"], "Важная")
        duplicate = self.s.state["tasks"]["2"]
        self.assertEqual(duplicate["status"], "cancelled")
        self.assertEqual(duplicate["merged_into"], "1")
        self.assertEqual(list(self.remote.data), [original_uid])
        self.assertEqual(self.remote.deletes, [copy_uid])
        await self.run_sync()
        self.assertEqual(self.remote.deletes, [copy_uid])
        self.assertEqual(len(self.s.state["tasks"]), 2)

    async def test_automatic_completion_before_legacy_repair_does_not_block_proven_merge(self):
        original_uid, copy_uid = await self.seed_reconnect_duplicates()
        self.s.now = lambda: NOW + timedelta(days=2)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "done")
        await self.run_sync()
        self.assertEqual(self.s.state["tasks"]["2"]["merged_into"], "1")
        self.assertEqual(list(self.remote.data), [original_uid])
        self.assertTrue(self.remote.data[original_uid].summary.startswith("✅ "))
        self.assertEqual(self.remote.deletes, [copy_uid])

    async def test_repair_refuses_to_merge_a_duplicate_independently_edited_by_user(self):
        await self.seed_reconnect_duplicates()
        await self.s.change(
            [{"action": "update", "task_id": "2", "priority": "Важная"}], "panel:edit"
        )
        await self.sync.sync(force=True)
        self.assertIsNotNone(self.sync.error)
        self.assertEqual(self.s.state["tasks"]["2"]["status"], "active")
        self.assertEqual(self.remote.deletes, [])

    async def test_repair_refuses_to_delete_cloud_copies_with_different_edits(self):
        _, copy_uid = await self.seed_reconnect_duplicates()
        self.remote.data[copy_uid].description = "Независимая правка Google"
        await self.sync.sync(force=True)
        self.assertIsNotNone(self.sync.error)
        self.assertEqual(self.s.state["tasks"]["2"]["status"], "active")
        self.assertEqual(self.remote.deletes, [])

    async def test_repair_delete_failure_is_durable_and_does_not_reimport_retired_event(self):
        original_uid, copy_uid = await self.seed_reconnect_duplicates()
        delete = self.remote.delete
        self.remote.delete = AsyncMock(side_effect=ConnectionError("offline"))
        await self.sync.sync(force=True)
        self.assertIsNotNone(self.sync.error)
        self.s.state = deepcopy(Store.database[self.s.store.key])
        self.sync = g.GoogleSync(self.s, adapter=lambda *args: self.remote)
        self.remote.delete = delete
        await self.run_sync()
        self.assertEqual(list(self.remote.data), [original_uid])
        self.assertEqual(self.s.state["tasks"]["2"]["status"], "cancelled")
        self.assertEqual(len(self.s.state["tasks"]), 2)
        self.assertEqual(self.remote.deletes, [copy_uid])

    async def test_lost_delete_receipt_recovery_keeps_backup_and_does_not_delete_original(self):
        original_uid, copy_uid = await self.seed_reconnect_duplicates()
        delete = self.remote.delete

        async def lost_receipt(uid):
            await delete(uid)
            raise ConnectionError("Deleted remotely; response was lost")

        self.remote.delete = lost_receipt
        await self.sync.sync(force=True)
        self.assertIsNotNone(self.sync.error)
        self.s.state = deepcopy(Store.database[self.s.store.key])
        self.sync = g.GoogleSync(self.s, adapter=lambda *args: self.remote)
        self.remote.delete = delete
        await self.run_sync()
        self.assertEqual(list(self.remote.data), [original_uid])
        self.assertEqual(self.remote.deletes, [copy_uid])
        archive = self.s.state["google"][self.remote.key]["removed_duplicates"][copy_uid]
        self.assertTrue(archive["remote_backup"])
        self.assertEqual(self.s.state["tasks"]["2"]["merged_into"], "1")

    async def test_custom_data_on_cloud_duplicate_is_preserved_for_manual_review(self):
        _, copy_uid = await self.seed_reconnect_duplicates()
        self.remote.data[copy_uid].location = "Место, добавленное вручную"
        await self.sync.sync(force=True)
        self.assertIsNotNone(self.sync.error)
        self.assertEqual(self.remote.deletes, [])
        self.assertIn(copy_uid, self.remote.data)

    async def test_different_real_calendars_keep_separate_identity_even_with_shared_event_ids(self):
        await self.create()
        await self.run_sync()
        self.s.state["google"][self.remote.key]["identity_version"] = 2
        self.remote.key = "another-real-calendar"
        self.remote.legacy_key = "another-native-entry"
        self.sync = g.GoogleSync(self.s, adapter=lambda *args: self.remote)
        await self.run_sync()
        self.assertEqual(len(self.s.state["google"]), 2)
        self.assertEqual(len(self.s.state["tasks"]), 2)

    async def test_google_edits_move_and_rename_the_same_task_preserving_manual_tags(self):
        self.remote.data["remote"] = event("remote")
        await self.run_sync()
        await self.s.change(
            [{"action": "update", "task_id": "1", "category": "Работа", "priority": "Важная"}],
            "manual",
        )
        self.remote.data["remote"].summary = "Другая встреча"
        self.remote.data["remote"].start.value = NOW + timedelta(days=4)
        self.remote.data["remote"].end.value = NOW + timedelta(days=4, hours=2)
        await self.run_sync()
        self.assertEqual(self.task()["title"], "Другая встреча")
        self.assertEqual(m.timestamp(self.task()["due_at"]), NOW + timedelta(days=4))
        self.assertEqual(self.task()["category"], "Работа")
        self.assertEqual(self.task()["priority"], "Важная")
        self.assertEqual(
            m.timestamp(self.task()["next_reminder_at"]), NOW + timedelta(days=4, hours=-1)
        )

    async def test_local_edit_updates_google_in_place_and_keeps_remote_id(self):
        await self.create()
        await self.run_sync()
        uid = next(iter(self.remote.data))
        await self.s.change(
            [
                {
                    "action": "update",
                    "task_id": "1",
                    "title": "Купить краску",
                    "due_at": (NOW + timedelta(hours=5)).isoformat(),
                }
            ],
            "edit",
        )
        await self.run_sync()
        self.assertEqual(list(self.remote.data), [uid])
        self.assertEqual(self.remote.data[uid].summary, "Купить краску")
        self.assertIsInstance(self.remote.data[uid].start.value, datetime)

    async def test_independent_edits_merge_and_same_field_conflict_uses_google(self):
        await self.create()
        await self.run_sync()
        uid = next(iter(self.remote.data))
        self.remote.data[uid].summary = "Название из Google"
        await self.s.change(
            [{"action": "update", "task_id": "1", "notes": "Местная заметка"}], "notes"
        )
        await self.run_sync()
        self.assertEqual(self.task()["title"], "Название из Google")
        self.assertEqual(self.task()["notes"], "Местная заметка")
        self.assertIn("Местная заметка", self.remote.data[uid].description)
        self.remote.data[uid].summary = "Ещё правка Google"
        await self.s.change([{"action": "update", "task_id": "1", "title": "Правка HA"}], "title")
        await self.run_sync()
        self.assertEqual(self.task()["title"], "Ещё правка Google")
        self.assertEqual(self.s.state["google"][self.remote.key]["conflicts"], 1)

    async def test_remote_deletion_cancels_but_retains_history_and_does_not_recreate(self):
        await self.create()
        await self.run_sync()
        self.remote.data.clear()
        await self.run_sync()
        self.assertEqual(self.task()["status"], "cancelled")
        await self.run_sync()
        self.assertEqual(self.remote.data, {})
        self.assertEqual(len(self.remote.writes), 1)

    async def test_local_cancel_deletes_only_its_linked_event_and_reopen_uses_fresh_id(self):
        self.e.options["google_sync_mode"] = "export"
        self.remote.data["unrelated"] = event("unrelated")
        await self.create()
        await self.run_sync()
        uid = next(uid for uid in self.remote.data if uid != "unrelated")
        await self.s.change([{"action": "cancel", "task_id": "1"}], "cancel")
        await self.run_sync()
        self.assertEqual(self.remote.deletes, [uid])
        self.assertEqual(list(self.remote.data), ["unrelated"])
        await self.s.change([{"action": "reopen", "task_id": "1"}], "reopen")
        await self.run_sync()
        self.assertEqual(len(self.remote.data), 2)
        self.assertNotIn(uid, self.remote.data)

    async def test_completion_is_visible_in_google_and_does_not_reopen_on_next_poll(self):
        await self.create()
        await self.run_sync()
        uid = next(iter(self.remote.data))
        await self.s.change([{"action": "done", "task_id": "1"}], "done")
        await self.run_sync()
        self.assertTrue(self.remote.data[uid].summary.startswith("✅ "))
        await self.run_sync()
        self.assertEqual(self.task()["status"], "done")

    async def test_automatic_completion_syncs_and_google_move_into_future_reopens_same_event(self):
        await self.create(due_at=(NOW - timedelta(hours=2)).isoformat())
        await self.run_sync()
        uid = next(iter(self.remote.data))
        await self.s.tick()
        self.assertEqual(self.task()["completion_reason"], "elapsed")
        await self.run_sync()
        self.assertTrue(self.remote.data[uid].summary.startswith("✅ "))
        self.remote.data[uid].start.value = NOW + timedelta(days=1)
        self.remote.data[uid].end.value = NOW + timedelta(days=1, hours=1)
        await self.run_sync()
        self.assertEqual(self.task()["status"], "active")
        self.assertIsNone(self.task()["completion_reason"])
        self.assertFalse(self.remote.data[uid].summary.startswith("✅ "))
        await self.run_sync()
        self.assertEqual(self.task()["status"], "active")
        self.assertEqual(list(self.remote.data), [uid])

    async def test_reply_completion_is_exported_without_waiting_for_event_end(self):
        await self.create(due_at=(NOW + timedelta(days=1)).isoformat(), repeat_until_ack=True)
        await self.run_sync()
        uid = next(iter(self.remote.data))
        await self.s.change([{"action": "acknowledge", "task_id": "1"}], "reply")
        self.assertEqual(self.task()["completion_reason"], "reply")
        await self.run_sync()
        self.assertTrue(self.remote.data[uid].summary.startswith("✅ "))
        self.assertEqual(list(self.remote.data), [uid])

    async def test_removing_completion_checkmark_in_google_reopens_task(self):
        await self.create()
        await self.run_sync()
        uid = next(iter(self.remote.data))
        await self.s.change([{"action": "done", "task_id": "1"}], "done")
        await self.run_sync()
        self.remote.data[uid].summary = self.remote.data[uid].summary.removeprefix("✅ ")
        await self.run_sync()
        self.assertEqual(self.task()["status"], "active")

    async def test_remote_read_failure_is_not_treated_as_deletion(self):
        await self.create()
        await self.run_sync()
        uid = next(iter(self.remote.data))
        self.remote.exclude.add(uid)
        self.remote.get = AsyncMock(side_effect=ConnectionError("offline"))
        await self.sync.sync(force=True)
        self.assertIsNotNone(self.sync.error)
        self.assertEqual(self.task()["status"], "active")

    async def test_poll_interval_is_five_minutes_but_local_edit_syncs_immediately(self):
        await self.create()
        await self.run_sync()
        self.remote.events = AsyncMock(wraps=self.remote.events)
        self.s.now = lambda: NOW + timedelta(minutes=4)
        await self.sync.sync()
        self.remote.events.assert_not_called()
        await self.s.change([{"action": "update", "task_id": "1", "title": "Правка"}], "edit")
        await self.sync.sync()
        self.remote.events.assert_awaited_once()
        self.remote.events.reset_mock()
        self.s.now = lambda: NOW + timedelta(minutes=9)
        await self.sync.sync()
        self.remote.events.assert_awaited_once()

    async def test_one_way_mode_ignores_external_events_and_overwrites_remote_edit(self):
        self.e.options["google_sync_mode"] = "export"
        await self.create()
        self.remote.data["unrelated"] = event("unrelated")
        await self.run_sync()
        uid = next(uid for uid in self.remote.data if uid != "unrelated")
        self.remote.data[uid].summary = "Случайная правка"
        await self.run_sync()
        self.assertEqual(self.remote.data[uid].summary, "Магазин")
        self.assertEqual(len(self.s.state["tasks"]), 1)

    async def test_calendar_outage_never_cancels_tasks_or_loses_mappings(self):
        await self.create()
        await self.run_sync()
        before = deepcopy(self.s.state["google"])
        self.remote.fail = True
        await self.sync.sync(force=True)
        self.assertIsNotNone(self.sync.error)
        self.assertEqual(self.task()["status"], "active")
        self.assertEqual(self.s.state["google"], before)

    async def test_moved_event_outside_scan_window_is_looked_up_by_id(self):
        await self.create()
        await self.run_sync()
        uid = next(iter(self.remote.data))
        self.remote.exclude.add(uid)
        self.remote.data[uid].start.value = date(2029, 1, 1)
        self.remote.data[uid].end.value = date(2029, 1, 2)
        await self.run_sync()
        self.assertEqual(self.task()["due_date"], "2029-01-01")
        self.assertEqual(self.task()["status"], "active")

    async def test_lost_create_response_reuses_reserved_id_without_duplicate(self):
        await self.create()
        self.remote.lose_receipt = True
        await self.sync.sync(force=True)
        self.assertIsNotNone(self.sync.error)
        self.assertEqual(len(self.remote.data), 1)
        self.s.state = deepcopy(Store.database[self.s.store.key])
        await self.run_sync()
        self.assertEqual(len(self.remote.writes), 1)
        self.assertEqual(len(self.remote.data), 1)

    async def test_edit_during_remote_write_is_preserved_and_exported_next_pass(self):
        await self.create()

        async def edit():
            await self.s.change(
                [{"action": "update", "task_id": "1", "title": "Изменено во время запроса"}],
                "concurrent",
            )

        self.remote.during_write = edit
        await self.run_sync()
        self.assertEqual(self.task()["title"], "Изменено во время запроса")
        await self.run_sync()
        self.assertEqual(next(iter(self.remote.data.values())).summary, "Изменено во время запроса")

    async def test_clearing_deadline_removes_event_but_does_not_cancel_task(self):
        await self.create()
        await self.run_sync()
        await self.s.change([{"action": "update", "task_id": "1", "clear_dates": True}], "clear")
        await self.run_sync()
        await self.run_sync()
        self.assertEqual(self.task()["status"], "active")
        self.assertEqual(self.remote.data, {})

    async def test_disabled_sync_has_no_remote_side_effects(self):
        self.e.options["google_calendar"] = ""
        await self.create()
        await self.run_sync()
        self.assertEqual(self.remote.data, {})
        self.assertEqual(self.s.state["google"], {})

    async def test_switching_calendar_during_read_does_not_migrate_or_modify_old_calendar(self):
        await self.create()

        async def switched(start, end):
            self.e.options["google_calendar"] = "calendar.other"
            return {}

        self.remote.events = switched
        await self.sync.sync(force=True)
        self.assertEqual(self.s.state["google"], {})
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(self.remote.deletes, [])

    def test_schedule_merge_keeps_all_date_fields_together(self):
        base = {
            "title": "A",
            "notes": "",
            "due_at": NOW.isoformat(),
            "end_at": (NOW + timedelta(hours=1)).isoformat(),
            "due_date": None,
            "end_date": None,
            "status": "active",
        }
        local = {
            **base,
            "due_date": "2026-11-11",
            "end_date": "2026-11-12",
            "due_at": None,
            "end_at": None,
        }
        remote = {
            **base,
            "due_at": (NOW + timedelta(hours=1)).isoformat(),
            "end_at": (NOW + timedelta(hours=2)).isoformat(),
        }
        merged, conflicts = g.merge_data(base, local, remote)
        self.assertEqual(merged, remote)
        self.assertEqual(conflicts, 1)


class NativeAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_native_calendar_identity_survives_google_entry_and_entity_rename(self):
        constants = ModuleType("homeassistant.components.calendar.const")
        constants.DATA_COMPONENT = "calendar"
        registry_module = ModuleType("homeassistant.helpers.entity_registry")
        registry = SimpleNamespace(async_get=lambda _: SimpleNamespace(config_entry_id="native"))
        registry_module.async_get = lambda _: registry
        native_entry = SimpleNamespace(
            entry_id="old-entry", domain="google", runtime_data=SimpleNamespace(service=object())
        )
        native_entity = SimpleNamespace(
            calendar_id="stable-google-calendar",
            entity_description=SimpleNamespace(read_only=False),
        )
        hass = SimpleNamespace(
            data={"calendar": SimpleNamespace(get_entity=lambda _: native_entity)},
            config_entries=SimpleNamespace(async_get_entry=lambda _: native_entry),
        )
        with patch.dict(
            sys.modules, {constants.__name__: constants, registry_module.__name__: registry_module}
        ):
            first = g.NativeGoogleCalendar(hass, "calendar.before")
            native_entry.entry_id = "replacement-entry"
            second = g.NativeGoogleCalendar(hass, "calendar.after")
            self.assertEqual(first.key, second.key)
            self.assertNotEqual(first.legacy_key, second.legacy_key)
            native_entity.calendar_id = "a-different-google-calendar"
            self.assertNotEqual(second.key, g.NativeGoogleCalendar(hass, "calendar.other").key)

    def adapter(self):
        native = g.NativeGoogleCalendar.__new__(g.NativeGoogleCalendar)
        native.calendar_id = "native-id"
        native.service = SimpleNamespace(
            async_get_event=AsyncMock(return_value=event("event")),
            async_patch_event=AsyncMock(),
            async_delete_event=AsyncMock(),
        )
        return native

    async def test_patch_that_did_not_apply_is_reported_not_committed(self):
        native = self.adapter()
        data = g.remote_data(event("event"))
        data["title"] = "Правка"
        actual = await native.write("event", g.event_body(data))
        self.assertNotEqual(actual, data)  # Engine refuses to commit this unconfirmed mutation.
        native.service.async_patch_event.assert_awaited_once()

    async def test_date_type_patch_explicitly_clears_old_datetime_or_date(self):
        native = self.adapter()
        data = g.remote_data(event("event", start=date(2026, 11, 12), end=date(2026, 11, 13)))
        await native.write("event", g.event_body(data))
        body = native.service.async_patch_event.call_args.args[2]
        self.assertIsNone(body["start"]["dateTime"])
        self.assertIsNone(body["start"]["timeZone"])
        self.assertEqual(body["start"]["date"], "2026-11-12")
        data = g.remote_data(event("event"))
        await native.write("event", g.event_body(data))
        self.assertIsNone(native.service.async_patch_event.call_args.args[2]["start"]["date"])

    async def test_delete_is_verified_instead_of_trusting_unchecked_native_response(self):
        native = self.adapter()
        with self.assertRaises(m.InvalidPlan):
            await native.delete("event")
        native.service.async_delete_event.assert_awaited_once_with("native-id", "event")

    async def test_only_explicit_404_or_410_is_remote_deletion(self):
        native = self.adapter()
        for status in (401, 403, 404, 410, 500):
            error = RuntimeError("wrapped Google error")
            cause = RuntimeError("HTTP response")
            cause.status = status
            error.__cause__ = cause
            native.service.async_get_event = AsyncMock(side_effect=error)
            if status in (404, 410):
                self.assertIsNone(await native.get("event"))
            else:
                with self.assertRaises(RuntimeError):
                    await native.get("event")
