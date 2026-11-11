"""HA native to-do list backed by the same tasks as Telegram."""

from datetime import datetime
from uuid import uuid4

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .model import InvalidPlan, day_value, timestamp


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([SecretaryTodo(entry)])


class SecretaryTodo(TodoListEntity):
    _attr_has_entity_name = True
    _attr_name = "Планы"
    _attr_icon = "mdi:clipboard-check-outline"
    _attr_should_poll = False
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(self, entry):
        self.secretary = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_plans"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": NAME,
            "manufacturer": NAME,
            "model": "Telegram + Codex",
        }

    @property
    def todo_items(self):
        return [
            TodoItem(
                uid=t["id"],
                summary=t["title"],
                status=TodoItemStatus.NEEDS_ACTION
                if t["status"] == "active"
                else TodoItemStatus.COMPLETED,
                due=day_value(t["due_date"]) if t.get("due_date") else timestamp(t["due_at"]),
                description=t["notes"] or None,
            )
            for t in self.secretary.state["tasks"].values()
            if t["status"] != "cancelled"
        ]

    async def async_added_to_hass(self):
        self.async_on_remove(self.secretary.listen(self.async_write_ha_state))

    def due(self, value):
        if value is None:
            return {}
        if not isinstance(value, datetime):
            return {"due_date": value.isoformat()}
        return {"due_at": dt_util.as_local(value).isoformat()}

    async def apply(self, op):
        try:
            await self.secretary.change([op], "ha:" + uuid4().hex)
        except InvalidPlan as error:
            raise HomeAssistantError(str(error)) from error

    async def async_create_todo_item(self, item):
        await self.apply(
            {
                "action": "create",
                "title": item.summary,
                "notes": item.description or "",
                **self.due(item.due),
            }
        )

    async def async_update_todo_item(self, item):
        current = self.secretary.state["tasks"].get(item.uid)
        if not current:
            raise HomeAssistantError("Задача не найдена")
        if item.status == TodoItemStatus.COMPLETED:
            await self.apply({"action": "done", "task_id": item.uid})
        elif current["status"] != "active":
            await self.apply({"action": "reopen", "task_id": item.uid})
        else:
            await self.apply(
                {
                    "action": "update",
                    "task_id": item.uid,
                    "title": item.summary,
                    "notes": item.description or "",
                    **self.due(item.due),
                    "clear_dates": item.due is None,
                }
            )

    async def async_delete_todo_items(self, uids):
        try:
            await self.secretary.change(
                [{"action": "cancel", "task_id": uid} for uid in uids], "ha:" + uuid4().hex
            )
        except InvalidPlan as error:
            raise HomeAssistantError(str(error)) from error
