"""Secretary status without exposing conversation content as state attributes."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .ai import ERRORS
from .const import DOMAIN
from .model import active_tasks, reminder_expired, timestamp


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [SecretarySensor(entry, key) for key in ("active", "overdue", "next", "pending", "status")]
    )


class SecretarySensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry, key):
        self.secretary = entry.runtime_data
        self.key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = {
            "active": "Открытые задачи",
            "overdue": "Просроченные задачи",
            "next": "Ближайшее напоминание",
            "pending": "Сообщения в очереди",
            "status": "Статус обработки",
        }[key]
        self._attr_icon = "mdi:clipboard-text-clock-outline"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}
        if key == "next":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    async def async_added_to_hass(self):
        self.async_on_remove(self.secretary.listen(self.async_write_ha_state))

    @property
    def native_value(self):
        tasks = active_tasks(self.secretary.state)
        if self.key == "active":
            return len(tasks)
        if self.key == "overdue":
            return sum(
                bool(t.get("due_at") or t.get("due_date"))
                and reminder_expired(t, self.secretary.now())
                for t in tasks
            )
        if self.key == "next":
            return min(
                (timestamp(t["next_reminder_at"]) for t in tasks if t["next_reminder_at"]),
                default=None,
            )
        if self.key == "pending":
            return sum(m["status"] != "processed" for m in self.secretary.state["messages"])
        if not self.secretary.options["enabled"]:
            return "Приостановлен"
        errors = {
            "internal_error": "Ошибка обработки; сообщения сохранены",
            "invalid_plan": "Нужно уточнить задачу",
            "telegram_unavailable": "Не удалось отправить уведомление",
            **ERRORS,
        }
        return errors.get(self.secretary.last_error, self.secretary.last_error) or (
            "Обрабатываю сообщение" if self.secretary.parser.process else "Готов"
        )
