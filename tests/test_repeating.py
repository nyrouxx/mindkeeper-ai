import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from support import Hass, Store, component, entry, event, install_ha_fakes
from test_model import NOW

install_ha_fakes()
c = component("coordinator")
m = component("model")


class RepeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Store.database.clear()
        self.hass = Hass()
        self.entry = entry()
        self.s = c.Secretary(self.hass, self.entry)
        self.s.now = lambda: NOW
        self.s.kick = lambda: None

    async def first(self):
        await self.s.change(
            [
                {
                    "action": "create",
                    "title": "Врач",
                    "due_at": (NOW + timedelta(hours=4)).isoformat(),
                    "category": "Здоровье",
                    "priority": "Важная",
                    "repeat_until_ack": True,
                }
            ],
            "new",
        )
        when = m.timestamp(self.s.state["tasks"]["1"]["next_reminder_at"])
        self.s.now = lambda: when
        await self.s.tick()
        self.start_time = when
        return self.hass.services.calls[-1], self.s.state["outbox"][
            next(iter(self.s.state["outbox"]))
        ]["message_ids"][0]

    async def test_mode_is_opt_in_not_implied_by_importance(self):
        state, _ = m.apply_operations(
            m.empty_state(),
            [{"action": "create", "title": "Важно", "priority": "Важная"}],
            NOW,
            "new",
        )
        self.assertFalse(state["tasks"]["1"]["repeat_until_ack"])

    async def test_repeats_at_interval_and_continues_after_event(self):
        await self.first()
        self.s.now = lambda: self.start_time + timedelta(minutes=9)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 1)
        self.s.now = lambda: self.start_time + timedelta(minutes=10)
        await self.s.tick()
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 2)
        self.s.now = lambda: NOW + timedelta(days=1)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 3)

    async def test_reply_completes_task_and_stops_repeats_without_model(self):
        _, mid = await self.first()
        self.s.parser.parse = AsyncMock(side_effect=AssertionError("Model must not be called"))
        await self.s.receive(event(id=2, text="Вижу!", reply_to_message_id=mid))
        message = self.s.state["messages"][-1]
        await self.s.process(message)
        self.assertIsNotNone(self.s.state["tasks"]["1"]["reminder_ack_at"])
        self.assertEqual(self.s.state["tasks"]["1"]["status"], "done")
        self.s.now = lambda: NOW + timedelta(days=2)
        await self.s.tick()
        self.assertEqual(
            len([v for v in self.hass.services.calls if v[2]["message"].startswith("Напомню ещё")]),
            0,
        )
        self.s.parser.parse.assert_not_awaited()

    async def test_done_response_also_marks_task_completed(self):
        _, mid = await self.first()
        self.s.parser.parse = AsyncMock(
            return_value={"reply": "Готово.", "operations": [{"action": "done", "task_id": "1"}]}
        )
        await self.s.receive(event(id=2, text="Сделал", reply_to_message_id=mid))
        await self.s.process(self.s.state["messages"][-1])
        self.assertEqual(self.s.state["tasks"]["1"]["status"], "done")

    async def test_unthreaded_ack_only_when_unambiguous(self):
        await self.first()
        await self.s.receive(event(id=2, text="Ок"))
        self.assertIsNotNone(self.s.state["tasks"]["1"]["reminder_ack_at"])
        self.assertEqual(self.s.state["tasks"]["1"]["status"], "done")

    async def test_other_chat_member_or_picture_cannot_acknowledge(self):
        _, mid = await self.first()
        await self.s.receive(event(id=2, user_id=43, text="Вижу", reply_to_message_id=mid))
        await self.s.receive(
            event(
                "telegram_attachment",
                id=3,
                text="Вижу",
                file_mime_type="image/jpeg",
                reply_to_message_id=mid,
            )
        )
        self.assertIsNone(self.s.state["tasks"]["1"]["reminder_ack_at"])
        self.assertEqual(self.s.state["messages"], [])

    async def test_reply_to_unrelated_message_does_not_acknowledge(self):
        await self.first()
        await self.s.receive(event(id=2, text="Ок", reply_to_message_id=987654))
        self.assertIsNone(self.s.state["tasks"]["1"]["reminder_ack_at"])

    async def test_tag_can_be_disabled_and_queued_repeat_cancelled(self):
        await self.first()
        self.s.enqueue("repeat-pending", "repeat", "1", True, kind="repeat")
        await self.s.change(
            [{"action": "update", "task_id": "1", "repeat_until_ack": False}], "disable"
        )
        await self.s.deliver()
        self.assertEqual(self.s.state["outbox"]["repeat-pending"]["status"], "cancelled")
        self.s.now = lambda: NOW + timedelta(days=2)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 1)

    async def test_moving_event_rearms_a_previously_acknowledged_reminder(self):
        _, mid = await self.first()
        await self.s.receive(event(id=2, text="Вижу", reply_to_message_id=mid))
        await self.s.change(
            [
                {"action": "reopen", "task_id": "1"},
                {
                    "action": "update",
                    "task_id": "1",
                    "due_at": (NOW + timedelta(days=2)).isoformat(),
                },
            ],
            "move",
        )
        task = self.s.state["tasks"]["1"]
        self.assertIsNone(task["reminder_ack_at"])
        self.assertIsNone(task["last_notification_at"])
        self.assertIsNotNone(task["next_reminder_at"])

    async def test_restart_keeps_repeat_and_ack_state(self):
        _, mid = await self.first()
        await self.s.receive(event(id=2, text="Вижу", reply_to_message_id=mid))
        await self.s.start()
        self.assertIsNotNone(self.s.state["tasks"]["1"]["reminder_ack_at"])
        self.s.now = lambda: NOW + timedelta(days=2)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 1)

    async def test_repeat_interval_can_be_changed(self):
        await self.first()
        self.entry.options["repeat_interval_minutes"] = 2
        self.s.now = lambda: self.start_time + timedelta(minutes=2)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 2)

    async def test_pictures_and_captions_are_ignored_without_model_calls(self):
        self.s.parser.parse = AsyncMock()
        await self.s.receive(
            event("telegram_attachment", text="Создай встречу завтра", file_mime_type="image/jpeg")
        )
        self.assertEqual(self.s.state["messages"], [])
        self.assertEqual(self.s.state["outbox"], {})
        self.s.parser.parse.assert_not_called()

    async def test_legacy_attachment_does_not_generate_an_answer(self):
        await self.s.submit("__attachment__", "telegram")
        await self.s.start()
        await self.s.deliver()
        self.assertEqual(self.s.state["messages"][0]["status"], "processed")
        self.assertEqual(self.hass.services.calls, [])

    async def test_acknowledge_completes_task(self):
        await self.first()
        await self.s.change([{"action": "acknowledge", "task_id": "1"}], "ack")
        task = self.s.state["tasks"]["1"]
        self.assertEqual(task["status"], "done")
        self.assertIsNone(m.repeat_due(task, 10))

    async def test_enabling_repeat_after_cancelled_stale_initial_reminder_sends(self):
        await self.first()
        task = self.s.state["tasks"]["1"]
        task["repeat_until_ack"] = False
        task["last_notification_at"] = None
        task["reminder_sent_at"] = None
        for item in self.s.state["outbox"].values():
            item["status"] = "cancelled"
        self.s.now = lambda: NOW + timedelta(days=1)
        await self.s.change(
            [{"action": "update", "task_id": "1", "repeat_until_ack": True}], "enable"
        )
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 2)

    async def test_multiple_waiting_tasks_require_specific_ack(self):
        await self.first()
        await self.s.change(
            [
                {
                    "action": "create",
                    "title": "Заказ",
                    "due_at": (NOW + timedelta(hours=5)).isoformat(),
                    "repeat_until_ack": True,
                }
            ],
            "other",
        )
        self.s.state["tasks"]["2"]["last_notification_at"] = self.s.now().isoformat()
        self.s.state["tasks"]["2"]["next_reminder_at"] = None
        await self.s.receive(event(id=2, text="Вижу"))
        self.assertTrue(
            all(t.get("reminder_ack_at") is None for t in self.s.state["tasks"].values())
        )

    async def test_repeat_interval_validation(self):
        for value in (0, 181, True, 1.5, "10", None):
            with self.assertRaises(m.InvalidPlan):
                m.validate_repeat_interval(value)
        self.assertEqual(m.validate_repeat_interval(15.0), 15)

    async def test_day_only_first_notice_after_hour_then_ten_minutes_until_reply(self):
        await self.s.change(
            [{"action": "create", "title": "Магазин", "repeat_until_ack": True}], "all-day"
        )
        self.s.now = lambda: NOW + timedelta(minutes=59)
        await self.s.tick()
        self.assertEqual(self.hass.services.calls, [])
        self.s.now = lambda: NOW + timedelta(hours=1)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 1)
        self.assertIn("в течение дня", self.hass.services.calls[0][2]["message"])
        self.s.now = lambda: NOW + timedelta(hours=1, minutes=9)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 1)
        self.s.now = lambda: NOW + timedelta(hours=1, minutes=10)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 2)
        await self.s.receive(event(id=2, text="Вижу"))
        self.s.now = lambda: NOW + timedelta(hours=2)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 2)

    async def test_timed_repeat_does_not_start_before_one_hour_lead(self):
        await self.s.change(
            [
                {
                    "action": "create",
                    "title": "Встреча",
                    "due_at": (NOW + timedelta(hours=4)).isoformat(),
                    "repeat_until_ack": True,
                }
            ],
            "timed",
        )
        self.s.now = lambda: NOW + timedelta(hours=2, minutes=59)
        await self.s.tick()
        self.assertEqual(self.hass.services.calls, [])
        self.s.now = lambda: NOW + timedelta(hours=3)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 1)
        self.s.now = lambda: NOW + timedelta(hours=3, minutes=10)
        await self.s.tick()
        self.assertEqual(len(self.hass.services.calls), 2)
