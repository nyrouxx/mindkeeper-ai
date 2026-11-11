"""Expose the same plans to Home Assistant's calendar."""

from datetime import timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .model import active_tasks, day_value, task_bounds, timestamp


def task_event(task):
    if task.get("due_date"):
        start = day_value(task["due_date"])
        end = day_value(task["end_date"]) if task.get("end_date") else start + timedelta(days=1)
    else:
        start = timestamp(task["due_at"])
        end = (timestamp(task.get("end_at")) or start + timedelta(minutes=30)).astimezone(
            start.tzinfo
        )
    return CalendarEvent(
        start=start,
        end=end,
        summary=task["title"],
        uid=task["id"],
        description=" · ".join(filter(None, [task.get("priority"), task.get("category")]))
        + ("\n" + task["notes"] if task["notes"] else ""),
    )


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([SecretaryCalendar(entry)])


class SecretaryCalendar(CalendarEntity):
    _attr_has_entity_name = True
    _attr_name = "Дела"
    _attr_icon = "mdi:calendar-check-outline"
    _attr_should_poll = False

    def __init__(self, entry):
        self.secretary = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_calendar"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def event(self):
        for task in active_tasks(self.secretary.state):
            if (
                task_bounds(task, dt_util.DEFAULT_TIME_ZONE)[1]
                and task_bounds(task, dt_util.DEFAULT_TIME_ZONE)[1] > self.secretary.now()
            ):
                return task_event(task)
        return None

    async def async_get_events(self, hass, start_date, end_date):
        return [
            task_event(t)
            for t in active_tasks(self.secretary.state)
            if task_bounds(t, dt_util.DEFAULT_TIME_ZONE)[0]
            and task_bounds(t, dt_util.DEFAULT_TIME_ZONE)[1] > start_date
            and task_bounds(t, dt_util.DEFAULT_TIME_ZONE)[0] < end_date
        ]

    async def async_added_to_hass(self):
        self.async_on_remove(self.secretary.listen(self.changed))

    def changed(self):
        self.async_write_ha_state()
        self.async_update_event_listeners()
