import sys
import unittest
from dataclasses import dataclass
from datetime import timedelta
from types import ModuleType

from support import Hass, component, entry, install_ha_fakes
from test_model import NOW

install_ha_fakes()
calendar_api = ModuleType("homeassistant.components.calendar")
calendar_api.CalendarEntity = type("CalendarEntity", (), {})


@dataclass
class CalendarEvent:
    start: object
    end: object
    summary: str
    uid: str
    description: str


calendar_api.CalendarEvent = CalendarEvent
sys.modules["homeassistant.components.calendar"] = calendar_api
cal = component("calendar")
c = component("coordinator")


class CalendarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entry = entry()
        self.s = c.Secretary(Hass(), self.entry)
        self.entry.runtime_data = self.s
        self.s.now = lambda: NOW
        self.s.kick = lambda: None
        self.entity = cal.SecretaryCalendar(self.entry)
        await self.s.change(
            [
                {
                    "action": "create",
                    "title": "Приём",
                    "due_at": (NOW + timedelta(days=2)).isoformat(),
                    "category": "Здоровье",
                    "priority": "Важная",
                }
            ],
            "new",
        )

    async def test_range_is_exclusive_and_includes_spanning_events(self):
        start = NOW + timedelta(days=2)
        self.assertEqual(
            await self.entity.async_get_events(None, start - timedelta(hours=1), start), []
        )
        events = await self.entity.async_get_events(
            None, start + timedelta(minutes=1), start + timedelta(hours=2)
        )
        self.assertEqual(len(events), 1)
        self.assertIn("Здоровье", events[0].description)
        self.assertEqual(events[0].uid, "1")

    async def test_done_tasks_disappear_from_calendar(self):
        await self.s.change([{"action": "done", "task_id": "1"}], "done")
        self.assertIsNone(self.entity.event)
        self.assertEqual(await self.entity.async_get_events(None, NOW, NOW + timedelta(days=5)), [])

    async def test_day_only_events_use_dates_and_exclude_midnight_boundary(self):
        await self.s.change(
            [{"action": "create", "title": "Магазин", "due_date": "2026-11-11"}], "day"
        )
        events = await self.entity.async_get_events(
            None, NOW.replace(day=11, hour=0), NOW.replace(day=12, hour=0)
        )
        self.assertEqual(len(events), 1)
        from datetime import date

        self.assertEqual(events[0].start, date(2026, 11, 11))
        self.assertEqual(events[0].end, date(2026, 11, 12))
        events = await self.entity.async_get_events(
            None, NOW.replace(day=12, hour=0), NOW.replace(day=12, hour=1)
        )
        self.assertEqual(events, [])
