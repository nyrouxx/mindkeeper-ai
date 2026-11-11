import unittest
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

from support import Hass, Store, component, entry, event, install_ha_fakes
from test_model import NOW

install_ha_fakes()
c = component("coordinator")


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Store.database.clear()
        self.hass = Hass()
        self.entry = entry()
        self.s = c.Secretary(self.hass, self.entry)
        self.s.now = lambda: NOW
        self.s.kick = lambda: None

    async def create(self, **fields):
        await self.s.change(
            [
                {
                    "action": "create",
                    "title": "Встреча",
                    "category": "Работа",
                    "priority": "Обычная",
                    **fields,
                }
            ],
            "test-create",
        )

    async def test_no_commands_or_callbacks_or_other_people(self):
        for ev in (
            event(bot={"config_entry_id": "other"}),
            event(chat_id=-42),
            event(user_id=43),
            event("telegram_command", command="/plans", args=[]),
            event(text="/today"),
            event("telegram_callback", data="ps:d:1"),
        ):
            await self.s.receive(ev)
        self.assertEqual(self.s.state["messages"], [])

    async def test_duplicate_inbox_is_persisted_before_ai(self):
        await self.s.receive(event())
        await self.s.receive(event())
        self.assertEqual(len(self.s.state["messages"]), 1)
        self.assertEqual(Store.database[self.s.store.key]["messages"][0]["status"], "pending")

    async def test_model_reply_is_human_not_template_or_buttons(self):
        self.s.parser.parse = AsyncMock(
            return_value={
                "reply": "Хорошо, запомнил про встречу.",
                "operations": [
                    {
                        "action": "create",
                        "title": "Встреча",
                        "category": "Работа",
                        "priority": "Важная",
                    }
                ],
            }
        )
        await self.s.receive(event())
        await self.s.process(self.s.state["messages"][0])
        await self.s.deliver()
        saved = Store.database[self.s.store.key]
        self.assertEqual(saved["messages"][0]["status"], "processed")
        self.assertEqual(saved["tasks"]["1"]["category"], "Работа")
        self.assertEqual(self.hass.services.calls[0][2]["message"], "Хорошо, запомнил про встречу.")
        self.assertNotIn("inline_keyboard", self.hass.services.calls[0][2])

    async def test_one_reminder_no_digest_or_followup(self):
        await self.create(due_at=(NOW + timedelta(hours=4)).isoformat())
        task = self.s.state["tasks"]["1"]
        scheduled = c.timestamp(task["next_reminder_at"])
        self.s.now = lambda: scheduled
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 1)
        self.assertIsNone(self.s.state["tasks"]["1"]["next_reminder_at"])
        self.s.now = lambda: NOW + timedelta(days=3)
        await self.s.tick()
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 1)
        self.assertIsNotNone(self.s.state["tasks"]["1"]["reminder_sent_at"])

    async def test_no_daily_messages_without_events(self):
        await self.create(clear_dates=True)
        for hour in (9, 12, 20, 23):
            self.s.now = lambda h=hour: NOW.replace(hour=h)
            await self.s.tick()
        self.assertEqual(self.hass.services.calls, [])

    async def test_outage_does_not_send_past_event(self):
        await self.create(due_at=(NOW + timedelta(hours=4)).isoformat())
        self.s.now = lambda: NOW + timedelta(hours=5)
        await self.s.tick()
        self.assertEqual(self.hass.services.calls, [])

    async def test_metadata_change_keeps_queued_reminder(self):
        await self.create(due_at=(NOW + timedelta(hours=4)).isoformat())
        self.s.enqueue("r", "Test", "1", True)
        await self.s.change([{"action": "update", "task_id": "1", "category": "Здоровье"}], "tags")
        await self.s.deliver()
        self.assertEqual(len(self.hass.services.calls), 1)

    async def test_changed_or_done_event_cancels_old_reminder(self):
        await self.create(due_at=(NOW + timedelta(hours=4)).isoformat())
        self.s.enqueue("r", "Old", "1", True)
        await self.s.change(
            [{"action": "update", "task_id": "1", "due_at": (NOW + timedelta(days=2)).isoformat()}],
            "move",
        )
        await self.s.deliver()
        self.assertEqual(self.hass.services.calls, [])
        self.assertEqual(self.s.state["outbox"]["r"]["status"], "cancelled")

    async def test_failure_keeps_message_without_commands(self):
        self.s.parser.parse = AsyncMock(side_effect=c.CodexError("timeout"))
        await self.s.receive(event())
        await self.s.process(self.s.state["messages"][0])
        self.assertEqual(self.s.state["messages"][0]["status"], "error")
        self.assertNotIn(
            "/", self.s.state["outbox"]["error:" + self.s.state["messages"][0]["key"]]["message"]
        )

    async def test_reply_binds_reminder_to_its_task(self):
        await self.create()
        self.s.enqueue("r", "Test", "1")
        await self.s.deliver()
        mid = self.s.state["outbox"]["r"]["message_ids"][0]
        self.assertEqual(self.s.reply_task({"reply_to": mid}), "1")

    async def test_restart_does_not_resample_random_reminder(self):
        await self.create(due_at=(NOW + timedelta(days=3)).isoformat())
        scheduled = self.s.state["tasks"]["1"]["next_reminder_at"]
        await self.s.start()
        self.assertEqual(self.s.state["tasks"]["1"]["next_reminder_at"], scheduled)
        self.assertEqual(self.hass.services.calls, [])

    async def test_panel_edit_queues_classification_without_telegram_response(self):
        await self.s.change(
            [{"action": "create", "title": "Купить краску", "category": "Стройка"}], "panel-edit"
        )
        message = self.s.state["messages"][0]
        self.assertEqual(message["origin"], "classification")
        self.s.parser.parse = AsyncMock(
            return_value={
                "reply": "",
                "operations": [
                    {
                        "action": "update",
                        "task_id": "1",
                        "category": "Стройка",
                        "priority": "Обычная",
                    }
                ],
            }
        )
        await self.s.process(message)
        self.assertFalse(self.s.state["tasks"]["1"]["classification_pending"])
        self.assertEqual(self.s.state["outbox"], {})

    async def test_classifier_cannot_change_date_or_manual_tag(self):
        await self.s.change(
            [{"action": "create", "title": "A", "category": "Стройка"}], "panel-edit"
        )
        for op in (
            {"action": "update", "task_id": "1", "category": "Здоровье", "priority": "Важная"},
            {"action": "done", "task_id": "1", "category": "Стройка", "priority": "Обычная"},
        ):
            self.s.parser.parse = AsyncMock(return_value={"reply": "", "operations": [op]})
            await self.s.process(self.s.state["messages"][0])
            self.assertEqual(self.s.state["tasks"]["1"]["category"], "Стройка")
            self.assertEqual(self.s.state["tasks"]["1"]["status"], "active")

    async def test_long_messages_not_truncated(self):
        body = "\n".join(f"Do {i} " + "x" * 200 for i in range(100))
        self.s.enqueue("long", body)
        self.assertEqual("\n".join(x["message"] for x in self.s.state["outbox"].values()), body)

    async def test_stale_plan_rejected(self):
        await self.create()

        async def parse(_):
            await self.s.change([{"action": "done", "task_id": "1"}], "edit")
            return {
                "reply": "ok",
                "operations": [{"action": "update", "task_id": "1", "title": "Stale"}],
            }

        self.s.parser.parse = parse
        await self.s.receive(event())
        await self.s.process(self.s.state["messages"][0])
        self.assertEqual(self.s.state["tasks"]["1"]["status"], "done")
        self.assertNotEqual(self.s.state["tasks"]["1"]["title"], "Stale")

    async def test_native_change_idempotent(self):
        await self.create()
        op = [{"action": "done", "task_id": "1"}]
        await self.s.change(op, "once")
        before = deepcopy(self.s.state)
        await self.s.change(op, "once")
        self.assertEqual(self.s.state, before)
