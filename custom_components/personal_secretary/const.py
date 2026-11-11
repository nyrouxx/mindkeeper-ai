"""Personal secretary defaults."""

DOMAIN = "personal_secretary"
NAME = "mindKeeper AI"
VERSION = "0.6.2"
DEFAULTS = {
    "enabled": True,
    "model": "default",
    "agenda_enabled": True,
    "repeat_interval_minutes": 10,
    "all_day_reminder_mode": "after_hour",
    "all_day_reminder_time": "09:00",
    "google_calendar": "",
    "google_sync_mode": "bidirectional",
    "categories": ["Повседнев", "Отдых", "Стройка", "Здоровье", "Работа"],
    "priorities": ["Обычная", "Важная"],
}
