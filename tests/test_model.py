import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from random import Random
from zoneinfo import ZoneInfo

from support import component

m = component("model")
NOW = datetime(2026, 11, 10, 12, tzinfo=ZoneInfo("Europe/Moscow"))


class ModelTests(unittest.TestCase):
    def create(self, **fields):
        return m.apply_operations(
            m.empty_state(),
            [{"action": "create", "title": "Встреча", **fields}],
            NOW,
            "input",
            rng=Random(1),
        )[0]

    def test_exactly_one_hour_before_start_is_persisted(self):
        state = self.create(due_at=(NOW + timedelta(days=2)).isoformat())
        task = state["tasks"]["1"]
        delta = m.timestamp(task["due_at"]) - m.timestamp(task["next_reminder_at"])
        self.assertEqual(delta, timedelta(hours=1))
        changed, _ = m.apply_operations(
            state,
            [
                {
                    "action": "update",
                    "task_id": "1",
                    "title": "Новое имя",
                    "category": "Работа",
                    "priority": "Важная",
                }
            ],
            NOW,
            "edit",
        )
        self.assertEqual(changed["tasks"]["1"]["next_reminder_at"], task["next_reminder_at"])
        self.assertEqual(changed["tasks"]["1"]["reminder_generation"], task["reminder_generation"])

    def test_available_window_does_not_schedule_in_past(self):
        for minutes in (61, 90, 120, 479):
            state = self.create(due_at=(NOW + timedelta(minutes=minutes)).isoformat())
            date = m.timestamp(state["tasks"]["1"]["next_reminder_at"])
            self.assertGreaterEqual(date, NOW)
            self.assertGreaterEqual(
                m.timestamp(state["tasks"]["1"]["due_at"]) - date, timedelta(hours=1)
            )

    def test_close_event_reminds_now(self):
        state = self.create(due_at=(NOW + timedelta(minutes=20)).isoformat())
        self.assertEqual(m.timestamp(state["tasks"]["1"]["next_reminder_at"]), NOW)

    def test_explicit_no_deadline_and_past_have_no_reminder(self):
        self.assertIsNone(self.create(clear_dates=True)["tasks"]["1"]["next_reminder_at"])
        self.assertIsNone(
            self.create(due_at=(NOW - timedelta(minutes=5)).isoformat())["tasks"]["1"][
                "next_reminder_at"
            ]
        )

    def test_unspecified_time_defaults_to_today_and_reminds_after_an_hour(self):
        task = self.create()["tasks"]["1"]
        self.assertEqual(task["due_date"], "2026-11-10")
        self.assertEqual(task["end_date"], "2026-11-11")
        self.assertIsNone(task["due_at"])
        self.assertEqual(m.timestamp(task["next_reminder_at"]), NOW + timedelta(hours=1))

    def test_future_day_uses_configured_clock_and_day_time_mode_is_explicit(self):
        for mode in ("after_hour", "day_time"):
            state, _ = m.apply_operations(
                m.empty_state(),
                [{"action": "create", "title": "Магазин", "due_date": "2026-11-11"}],
                NOW,
                "new",
                {"all_day_reminder_mode": mode, "all_day_reminder_time": "10:30"},
            )
            self.assertEqual(
                m.timestamp(state["tasks"]["1"]["next_reminder_at"]),
                NOW.replace(day=11, hour=10, minute=30),
            )
        state, _ = m.apply_operations(
            m.empty_state(),
            [{"action": "create", "title": "Магазин"}],
            NOW,
            "new",
            {"all_day_reminder_mode": "day_time", "all_day_reminder_time": "09:00"},
        )
        self.assertEqual(m.timestamp(state["tasks"]["1"]["next_reminder_at"]), NOW)

    def test_move_between_all_day_and_timed_clears_other_date_type(self):
        state = self.create()
        state, _ = m.apply_operations(
            state,
            [
                {
                    "action": "update",
                    "task_id": "1",
                    "due_at": (NOW + timedelta(hours=4)).isoformat(),
                }
            ],
            NOW,
            "timed",
        )
        self.assertIsNone(state["tasks"]["1"]["due_date"])
        state, _ = m.apply_operations(
            state, [{"action": "update", "task_id": "1", "due_date": "2026-11-12"}], NOW, "day"
        )
        self.assertIsNone(state["tasks"]["1"]["due_at"])
        self.assertIsNone(state["tasks"]["1"]["end_at"])
        self.assertEqual(state["tasks"]["1"]["end_date"], "2026-11-13")

    def test_day_only_reminder_text_and_expiry_use_the_whole_day(self):
        task = self.create()["tasks"]["1"]
        self.assertIn("Сегодня в течение дня", m.reminder_text(task, NOW, NOW.tzinfo))
        self.assertFalse(m.reminder_expired(task, NOW.replace(hour=23)))
        self.assertTrue(m.reminder_expired(task, NOW.replace(day=11, hour=0)))

    def test_multiday_dates_and_dst_have_exclusive_local_midnight_end(self):
        task = self.create(due_date="2026-03-08", end_date="2026-03-10")["tasks"]["1"]
        start, end = m.task_bounds(task, ZoneInfo("America/New_York"))
        self.assertEqual(start.hour, 0)
        self.assertEqual(start.utcoffset(), timedelta(hours=-5))
        self.assertEqual(end.utcoffset(), timedelta(hours=-4))
        self.assertEqual(end.day, 10)

    def test_rejects_invalid_and_contradictory_all_day_dates(self):
        for value in ("2026-02-30", 123, "2026-11-11T12:00"):
            with self.assertRaises(m.InvalidPlan):
                self.create(due_date=value)
        with self.assertRaises(m.InvalidPlan):
            self.create(due_date="2026-11-11", due_at=NOW.isoformat())
        with self.assertRaises(m.InvalidPlan):
            self.create(due_date="2026-11-11", end_date="2026-11-10")

    def test_migration_dates_old_backlog_and_changes_only_unsent_reminders(self):
        state = self.create(clear_dates=True)
        state["schema"] = 3
        migrated = m.migrate_state(state, NOW)
        self.assertEqual(migrated["schema"], 4)
        self.assertEqual(migrated["tasks"]["1"]["due_date"], NOW.date().isoformat())
        state = self.create(due_at=(NOW + timedelta(days=2)).isoformat())
        state["schema"] = 3
        task = state["tasks"]["1"]
        task["last_notification_at"] = NOW.isoformat()
        task["next_reminder_at"] = None
        task["reminder_ack_at"] = NOW.isoformat()
        migrated = m.migrate_state(state, NOW)
        self.assertEqual(migrated["tasks"]["1"]["reminder_ack_at"], NOW.isoformat())
        self.assertIsNone(migrated["tasks"]["1"]["next_reminder_at"])

    def test_move_reschedules_and_keeps_event_duration(self):
        state = self.create(
            due_at=(NOW + timedelta(days=2)).isoformat(),
            end_at=(NOW + timedelta(days=2, hours=2)).isoformat(),
        )
        changed, _ = m.apply_operations(
            state,
            [{"action": "update", "task_id": "1", "due_at": (NOW + timedelta(days=4)).isoformat()}],
            NOW,
            "move",
        )
        task = changed["tasks"]["1"]
        self.assertEqual(task["reminder_generation"], 2)
        self.assertEqual(
            m.timestamp(task["end_at"]) - m.timestamp(task["due_at"]), timedelta(hours=2)
        )

    def test_tags_must_be_configured(self):
        for field in ("category", "priority"):
            with self.assertRaises(m.InvalidPlan):
                self.create(**{field: "Несуществующая"})

    def test_custom_categories_and_priorities(self):
        state, _ = m.apply_operations(
            m.empty_state(),
            [{"action": "create", "title": "План", "category": "Семья", "priority": "Критично"}],
            NOW,
            "input",
            {"categories": ["Семья"], "priorities": ["Обычная", "Критично"]},
        )
        self.assertFalse(state["tasks"]["1"]["classification_pending"])

    def test_manual_category_only_classifies_priority(self):
        task = self.create(category="Работа")["tasks"]["1"]
        self.assertEqual(task["classification_fields"], ["priority"])

    def test_invalid_batch_is_atomic(self):
        state = m.empty_state()
        before = deepcopy(state)
        with self.assertRaises(m.InvalidPlan):
            m.apply_operations(
                state,
                [{"action": "create", "title": "A"}, {"action": "done", "task_id": "missing"}],
                NOW,
                "input",
            )
        self.assertEqual(state, before)

    def test_close_cancels_and_reopen_reschedules(self):
        state = self.create(due_at=(NOW + timedelta(days=2)).isoformat())
        changed, _ = m.apply_operations(state, [{"action": "done", "task_id": "1"}], NOW, "done")
        self.assertIsNone(changed["tasks"]["1"]["next_reminder_at"])
        opened, _ = m.apply_operations(
            changed, [{"action": "reopen", "task_id": "1"}], NOW, "reopen"
        )
        self.assertIsNotNone(opened["tasks"]["1"]["next_reminder_at"])

    def test_migration_cancels_old_notifications_and_preserves_plans(self):
        state = self.create(due_at=(NOW + timedelta(days=3)).isoformat())
        state.pop("schema")
        state["briefings"] = {"old": "x"}
        state["daily"] = {"old": 3}
        state["outbox"] = {"old": {"status": "pending"}}
        state["tasks"]["1"]["remind_at"] = "2026-11-10T09:00:00+03:00"
        migrated = m.migrate_state(state, NOW)
        self.assertEqual(migrated["tasks"]["1"]["title"], "Встреча")
        self.assertEqual(migrated["outbox"]["old"]["status"], "cancelled")
        self.assertNotIn("briefings", migrated)
        self.assertNotIn("remind_at", migrated["tasks"]["1"])
        self.assertEqual(m.migrate_state(migrated, NOW), migrated)

    def test_label_validation_duplicates_and_limits(self):
        for labels in ([], ["Работа", "работа"], ["x" * 61], [""]):
            with self.assertRaises(m.InvalidPlan):
                m.validate_labels(labels)
        self.assertEqual(m.validate_labels([" Семья ", "Работа"]), ["Семья", "Работа"])

    def test_rejects_invalid_dates_and_event_end(self):
        for value in ("2026-11-11T12:00", "tomorrow", 123):
            with self.assertRaises(m.InvalidPlan):
                m.timestamp(value)
        with self.assertRaises(m.InvalidPlan):
            self.create(due_at=(NOW + timedelta(days=1)).isoformat(), end_at=NOW.isoformat())

    def test_upgrade_preserves_existing_schedule_and_manual_labels(self):
        state = self.create(
            due_at=(NOW + timedelta(days=2)).isoformat(), category="Работа", priority="Важная"
        )
        state["schema"] = 2
        before = deepcopy(state["tasks"]["1"])
        upgraded = m.migrate_state(state, NOW + timedelta(minutes=5))
        task = upgraded["tasks"]["1"]
        self.assertEqual(task["next_reminder_at"], before["next_reminder_at"])
        self.assertEqual(task["category"], before["category"])
        self.assertEqual(task["priority"], before["priority"])
        self.assertFalse(task["repeat_until_ack"])
        self.assertIn("agendas", upgraded)
