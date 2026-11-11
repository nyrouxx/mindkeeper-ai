"""Completion uses event ends for ordinary plans and receipts for persistent ones."""

import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from support import Hass, Store, component, entry, event, install_ha_fakes
from test_model import NOW

install_ha_fakes()
m = component("model")
c = component("coordinator")


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Store.database.clear()
        self.s = c.Secretary(Hass(), entry())
        self.s.now = lambda: NOW
        self.s.kick = lambda: None

    async def create(self, **fields):
        await self.s.change(
            [
                {
                    "action": "create",
                    "title": "Дело",
                    "category": "Работа",
                    "priority": "Обычная",
                    **fields,
                }
            ],
            "create",
        )

    def task(self):
        return self.s.state["tasks"]["1"]

    async def test_default_duration_closes_at_thirty_minutes_not_at_start(self):
        await self.create(due_at=NOW.isoformat())
        self.s.now = lambda: NOW + timedelta(minutes=29, seconds=59)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "active")
        self.s.now = lambda: NOW + timedelta(minutes=30)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.task()["completion_reason"], "elapsed")
        completed = deepcopy(self.task())
        await self.s.tick()
        self.assertEqual(self.task(), completed)

    async def test_explicit_end_controls_completion_and_preserves_labels(self):
        await self.create(due_at=NOW.isoformat(), end_at=(NOW + timedelta(hours=3)).isoformat())
        self.s.now = lambda: NOW + timedelta(hours=2)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "active")
        self.s.now = lambda: NOW + timedelta(hours=3)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.task()["category"], "Работа")
        self.assertEqual(self.task()["priority"], "Обычная")

    async def test_all_day_closes_at_exclusive_local_midnight(self):
        await self.create(due_date="2026-11-10")
        self.s.now = lambda: NOW.replace(hour=23, minute=59, second=59)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "active")
        self.s.now = lambda: NOW.replace(day=11, hour=0, minute=0, second=0)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "done")

    async def test_multiday_closes_only_at_last_day_end(self):
        await self.create(due_date="2026-11-10", end_date="2026-11-13")
        self.s.now = lambda: NOW.replace(day=12, hour=23)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "active")
        self.s.now = lambda: NOW.replace(day=13, hour=0)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "done")

    async def test_no_deadline_has_no_automatic_completion(self):
        await self.create(clear_dates=True)
        self.s.now = lambda: NOW + timedelta(days=50)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "active")

    async def test_legacy_acknowledged_task_is_completed_on_upgrade(self):
        await self.create(due_at=(NOW + timedelta(days=1)).isoformat(), repeat_until_ack=True)
        self.task()["reminder_ack_at"] = (NOW - timedelta(minutes=5)).isoformat()
        await self.s.save()
        await self.s.start()
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.task()["completion_reason"], "reply")

    async def test_persistent_task_stays_active_after_event_end_until_reply(self):
        await self.create(due_at=NOW.isoformat(), repeat_until_ack=True)
        self.s.now = lambda: NOW + timedelta(days=2)
        await self.s.tick()
        self.assertEqual(self.task()["status"], "active")
        await self.s.receive(event(id=2, text="Вижу"))
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.task()["completion_reason"], "reply")
        self.assertIsNone(self.task()["next_reminder_at"])

    async def test_restart_catches_up_without_model_or_past_notifications(self):
        await self.create(due_at=NOW.isoformat())
        self.s.now = lambda: NOW + timedelta(days=1)
        self.s.parser.parse = AsyncMock(side_effect=AssertionError("No model expected"))
        await self.s.start()
        self.assertEqual(self.task()["status"], "done")
        await self.s.deliver()
        self.assertEqual(self.s.hass.services.calls, [])
        self.s.parser.parse.assert_not_called()

    async def test_disabling_repeat_mode_after_event_end_enables_auto_completion(self):
        await self.create(due_at=NOW.isoformat(), repeat_until_ack=True)
        self.s.now = lambda: NOW + timedelta(days=1)
        await self.s.change(
            [{"action": "update", "task_id": "1", "repeat_until_ack": False}], "disable"
        )
        await self.s.tick()
        self.assertEqual(self.task()["status"], "done")

    async def test_direct_reply_can_move_task_after_its_immediate_completion(self):
        await self.create(due_at=(NOW + timedelta(hours=1)).isoformat(), repeat_until_ack=True)
        await self.s.tick()
        reminder = next(iter(self.s.state["outbox"].values()))["message_ids"][0]
        await self.s.receive(event(id=2, text="Перенеси на завтра", reply_to_message_id=reminder))
        self.assertEqual(self.task()["status"], "done")
        self.s.parser.parse = AsyncMock(
            return_value={
                "reply": "Перенёс на завтра.",
                "operations": [{"action": "update", "task_id": "1", "due_date": "2026-11-11"}],
            }
        )
        await self.s.process(self.s.state["messages"][-1])
        self.assertEqual(self.task()["status"], "active")
        self.assertEqual(self.task()["due_date"], "2026-11-11")
        self.assertIsNone(self.task()["completion_reason"])
        self.assertIsNone(self.task()["reminder_ack_at"])
        self.assertIsNotNone(self.task()["next_reminder_at"])

    async def test_reply_with_metadata_edit_keeps_task_completed(self):
        await self.create(due_at=(NOW + timedelta(hours=1)).isoformat(), repeat_until_ack=True)
        await self.s.tick()
        reminder = next(iter(self.s.state["outbox"].values()))["message_ids"][0]
        await self.s.receive(
            event(id=2, text="Хорошо, исправь название на Заказ", reply_to_message_id=reminder)
        )
        self.s.parser.parse = AsyncMock(
            return_value={
                "reply": "Хорошо.",
                "operations": [{"action": "update", "task_id": "1", "title": "Заказ"}],
            }
        )
        await self.s.process(self.s.state["messages"][-1])
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.task()["title"], "Заказ")
        self.assertEqual(self.task()["completion_reason"], "reply")
        self.assertIsNotNone(self.task()["reminder_ack_at"])

    async def test_unrelated_message_does_not_complete_waiting_task(self):
        await self.create(due_at=(NOW + timedelta(hours=1)).isoformat(), repeat_until_ack=True)
        await self.s.tick()
        await self.s.receive(event(id=2, text="Что у меня завтра?"))
        self.assertEqual(self.task()["status"], "active")

    async def test_paused_assistant_does_not_complete_tasks(self):
        await self.create(due_at=NOW.isoformat())
        self.s.entry.options["enabled"] = False
        self.s.now = lambda: NOW + timedelta(days=1)
        await self.s.tick()
        await self.s.start()
        self.assertEqual(self.task()["status"], "active")

    def test_completion_batches_more_than_twenty_overdue_tasks(self):
        state = m.empty_state()
        for i in range(25):
            state, _ = m.apply_operations(
                state,
                [{"action": "create", "title": f"Дело {i}", "due_at": NOW.isoformat()}],
                NOW,
                "create",
            )
        closed = m.complete_ready_tasks(state, NOW + timedelta(hours=1), NOW.tzinfo)
        self.assertEqual(sum(t["status"] == "done" for t in closed["tasks"].values()), 25)
        self.assertTrue(all(t["status"] == "active" for t in state["tasks"].values()))

    def test_day_only_completion_respects_dst_day_length(self):
        zone = ZoneInfo("America/New_York")
        now = datetime(2026, 3, 8, 12, tzinfo=zone)
        state, _ = m.apply_operations(
            m.empty_state(),
            [{"action": "create", "title": "День", "due_date": "2026-03-08"}],
            now,
            "create",
        )
        before = m.complete_ready_tasks(
            state, datetime(2026, 3, 9, 3, 59, tzinfo=ZoneInfo("UTC")), zone
        )
        after = m.complete_ready_tasks(state, datetime(2026, 3, 9, 4, tzinfo=ZoneInfo("UTC")), zone)
        self.assertEqual(before["tasks"]["1"]["status"], "active")
        self.assertEqual(after["tasks"]["1"]["status"], "done")
