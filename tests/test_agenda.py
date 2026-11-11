import asyncio
import unittest
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

from support import Hass, Store, component, entry, install_ha_fakes
from test_model import NOW

install_ha_fakes()
a = component("agenda")
c = component("coordinator")


class AgendaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Store.database.clear()
        self.hass = Hass()
        self.entry = entry()
        self.entry.options["agenda_enabled"] = True
        self.s = c.Secretary(self.hass, self.entry)
        self.s.now = lambda: NOW
        self.s.kick = lambda: None

    async def task(self, **values):
        await self.s.change(
            [
                {
                    "action": "create",
                    "title": "Врач <важно> & вовремя",
                    "due_at": (NOW + timedelta(hours=2)).isoformat(),
                    "category": "Здоровье",
                    "priority": "Важная",
                    **values,
                }
            ],
            "new",
        )

    async def test_create_once_then_edit_same_message_after_task_change(self):
        await self.s.agenda.sync()
        key = self.s.agenda.key()
        mid = self.s.state["agendas"][key]["message_id"]
        await self.task()
        await self.s.agenda.sync()
        await self.s.agenda.sync()
        calls = self.hass.services.calls
        self.assertEqual([v[1] for v in calls], ["send_message", "edit_message"])
        self.assertEqual(calls[-1][2]["message_id"], mid)
        self.assertIn("<b>14:00</b>", calls[-1][2]["message"])
        self.assertIn("&lt;важно&gt;", calls[-1][2]["message"])
        self.assertEqual(calls[-1][2]["parse_mode"], "html")

    async def test_new_day_updates_existing_message(self):
        await self.task()
        await self.s.agenda.sync()
        mid = self.s.state["agendas"][self.s.agenda.key()]["message_id"]
        self.s.now = lambda: NOW + timedelta(days=1)
        await self.s.agenda.sync()
        call = self.hass.services.calls[-1]
        self.assertEqual(call[1], "edit_message")
        self.assertEqual(call[2]["message_id"], mid)
        self.assertIn("11 ноября", call[2]["message"])
        self.assertNotIn("14:00", call[2]["message"])

    async def test_completed_task_is_crossed_out(self):
        await self.task()
        await self.s.agenda.sync()
        await self.s.change([{"action": "done", "task_id": "1"}], "done")
        await self.s.agenda.sync()
        self.assertIn("<s>", self.hass.services.calls[-1][2]["message"])
        self.assertIn("Всё сделано", self.hass.services.calls[-1][2]["message"])

    async def test_today_message_contains_tasks_without_exact_time_and_excludes_other_days(self):
        await self.s.change(
            [
                {"action": "create", "title": "Магазин"},
                {"action": "create", "title": "Завтрашнее дело", "due_date": "2026-11-11"},
            ],
            "all-day",
        )
        body = a.today_message(self.s.state, NOW, NOW.tzinfo)
        self.assertIn("В течение дня", body)
        self.assertIn("Магазин", body)
        self.assertNotIn("Завтрашнее дело", body)
        self.assertNotIn("00:00", body)

    async def test_restart_keeps_pinned_message_reference(self):
        await self.s.agenda.sync()
        mid = self.s.state["agendas"][self.s.agenda.key()]["message_id"]
        await self.s.start()
        await self.s.agenda.sync()
        self.assertEqual(len(self.hass.services.calls), 1)
        self.assertEqual(self.s.state["agendas"][self.s.agenda.key()]["message_id"], mid)

    async def test_missing_message_is_replaced_once(self):
        await self.s.agenda.sync()
        await self.task()
        original = self.hass.services.async_call
        self.hass.services.async_call = AsyncMock(
            side_effect=ValueError("Bad Request: message to edit not found")
        )
        await self.s.agenda.sync()
        self.hass.services.async_call = original
        await self.s.agenda.sync()
        await self.s.agenda.sync()
        self.assertEqual([v[1] for v in self.hass.services.calls], ["send_message", "send_message"])

    async def test_not_modified_response_is_success_not_new_message(self):
        await self.s.agenda.sync()
        await self.task()
        self.hass.services.async_call = AsyncMock(
            side_effect=ValueError("Bad Request: message is not modified")
        )
        await self.s.agenda.sync()
        await self.s.agenda.sync()
        self.assertEqual(self.s.agenda.status(), "ready")
        self.hass.services.async_call.assert_awaited_once()

    async def test_uncertain_create_requires_explicit_retry(self):
        self.hass.services.fail = True
        await self.s.agenda.sync()
        self.assertEqual(self.s.agenda.status(), "uncertain")
        self.hass.services.fail = False
        await self.s.agenda.sync()
        self.assertEqual(self.hass.services.calls, [])
        await self.s.agenda.sync(force=True, recreate=True)
        await self.s.agenda.sync(force=True, recreate=True)
        self.assertEqual([v[1] for v in self.hass.services.calls], ["send_message", "edit_message"])

    async def test_concurrent_refresh_creates_one_message(self):
        await asyncio.gather(self.s.agenda.sync(), self.s.agenda.sync())
        self.assertEqual(len(self.hass.services.calls), 1)

    async def test_model_state_copy_during_send_does_not_lose_receipt(self):
        original = self.hass.services.async_call

        async def call(*args, **kwargs):
            await self.task()
            return await original(*args, **kwargs)

        self.hass.services.async_call = call
        await self.s.agenda.sync()
        self.assertIsInstance(self.s.state["agendas"][self.s.agenda.key()]["message_id"], int)
        self.assertEqual(self.s.agenda.status(), "ready")

    async def test_disabled_board_does_not_send_or_delete(self):
        self.entry.options["agenda_enabled"] = False
        await self.s.agenda.sync()
        self.assertEqual(self.hass.services.calls, [])

    async def test_long_agenda_stays_within_telegram_limit(self):
        await self.task()
        task = self.s.state["tasks"]["1"]
        for uid in range(2, 100):
            self.s.state["tasks"][str(uid)] = {
                **deepcopy(task),
                "id": str(uid),
                "title": "🙂<&>" * 50,
            }
        body = a.today_message(self.s.state, NOW, NOW.tzinfo)
        self.assertLess(a.units(body), 4096)
        self.assertIn("Ещё ", body)
        self.assertEqual(body.count("<b>"), body.count("</b>"))
        self.assertNotIn("<&>", body)

    async def test_ha_local_day_and_multiday_overlap(self):
        await self.task(due_at="2026-11-09T22:00:00+00:00", end_at="2026-11-10T12:00:00+03:00")
        self.assertIn("01:00", a.today_message(self.s.state, NOW, NOW.tzinfo))
        self.s.state["tasks"]["1"]["due_at"] = "2026-11-09T10:00:00+03:00"
        self.assertIn("В течение дня", a.today_message(self.s.state, NOW, NOW.tzinfo))

    async def test_telegram_not_ready_does_not_create_uncertain_board(self):
        self.hass.services.has_service = lambda *args: False
        await self.s.agenda.sync()
        self.assertEqual(self.s.state["agendas"], {})
        self.hass.services.has_service = lambda *args: True
        await self.s.agenda.sync()
        self.assertEqual(self.s.agenda.status(), "ready")

    async def test_rejected_create_is_retried_after_backoff(self):
        class RetryAfter(Exception):
            pass

        original = self.hass.services.async_call
        self.hass.services.async_call = AsyncMock(side_effect=RetryAfter("retry"))
        await self.s.agenda.sync()
        self.assertEqual(self.s.agenda.status(), "error")
        self.hass.services.async_call = original
        await self.s.agenda.sync()
        self.assertEqual(self.hass.services.calls, [])
        self.s.now = lambda: NOW + timedelta(minutes=3)
        await self.s.agenda.sync()
        self.assertEqual(len(self.hass.services.calls), 1)

    async def test_switching_back_to_a_chat_reuses_its_message(self):
        from test_telegram import ConfigEntries

        self.hass.config_entries = ConfigEntries()
        await self.s.agenda.sync()
        key = self.s.agenda.key()
        mid = self.s.state["agendas"][key]["message_id"]
        await self.s.configure_telegram({"bot_entry_id": "other", "chat_id": 84, "user_id": 84})
        await self.s.agenda.sync()
        await self.s.configure_telegram({"bot_entry_id": "bot", "chat_id": 42, "user_id": 42})
        await self.task()
        await self.s.agenda.sync()
        self.assertEqual(self.hass.services.calls[-1][1], "edit_message")
        self.assertEqual(self.hass.services.calls[-1][2]["message_id"], mid)
