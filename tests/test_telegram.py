import json
import unittest
from datetime import timedelta
from types import SimpleNamespace

from support import Hass, Store, component, entry, event, install_ha_fakes
from test_model import NOW

install_ha_fakes()
t = component("telegram")
c = component("coordinator")


def bot(uid, chats, platform="polling", disabled_by=None):
    return SimpleNamespace(
        entry_id=uid,
        title=f"Bot {uid}",
        disabled_by=disabled_by,
        data={"platform": platform, "api_key": "PRIVATE_BOT_TOKEN"},
        runtime_data=SimpleNamespace(bot=SimpleNamespace(username=uid + "_bot")),
        subentries={
            str(chat): SimpleNamespace(title=f"Chat {chat}", data={"chat_id": chat})
            for chat in chats
        },
    )


class ConfigEntries:
    def __init__(self):
        self.bots = [
            bot("bot", [42, -10042]),
            bot("other", [42, 84, -10084]),
            bot("broadcast", [7], "broadcast"),
            bot("disabled", [9], disabled_by="user"),
        ]

    def async_entries(self, domain):
        return self.bots if domain == "telegram_bot" else []

    def async_update_entry(self, target, data):
        target.data = data


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.hass = Hass()
        self.hass.config_entries = ConfigEntries()

    def test_only_enabled_receiving_bots_are_choices(self):
        self.assertEqual(set(t.bot_choices(self.hass)), {"bot", "other"})
        self.assertIn("@bot_bot", t.bot_choices(self.hass)["bot"]["label"])

    def test_chats_belong_to_selected_bot_and_include_groups(self):
        choices = t.chat_choices(self.hass, "bot")
        self.assertEqual(set(choices), {"bot:42", "bot:-10042"})
        self.assertIsNone(choices["bot:-10042"]["user_id"])

    def test_private_sender_is_recipient_even_if_spoofed(self):
        route = t.validate_route(
            self.hass, {"bot_entry_id": "bot", "chat_id": "42", "user_id": "999"}
        )
        self.assertEqual(route["user_id"], 42)

    def test_group_requires_positive_sender(self):
        for uid in (None, 0, -42, True, "someone"):
            with self.assertRaises(t.InvalidPlan):
                t.validate_route(
                    self.hass, {"bot_entry_id": "bot", "chat_id": -10042, "user_id": uid}
                )
        self.assertEqual(
            t.validate_route(
                self.hass, {"bot_entry_id": "bot", "chat_id": -10042, "user_id": "42"}
            )["user_id"],
            42,
        )

    def test_unknown_or_cross_bot_recipient_rejected(self):
        for route in (
            {"bot_entry_id": "missing", "chat_id": 42},
            {"bot_entry_id": "bot", "chat_id": 84},
            {"bot_entry_id": "bot", "chat_id": 42.5},
        ):
            with self.assertRaises(t.InvalidPlan):
                t.validate_route(self.hass, route)

    def test_snapshot_never_contains_bot_token(self):
        snapshot = t.public_settings(
            self.hass, {"bot_entry_id": "bot", "chat_id": 42, "user_id": 42}
        )
        self.assertNotIn("PRIVATE_BOT_TOKEN", json.dumps(snapshot))
        self.assertTrue(snapshot["available"])


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Store.database.clear()
        self.hass = Hass()
        self.hass.config_entries = ConfigEntries()
        self.entry = entry()
        self.s = c.Secretary(self.hass, self.entry)
        self.s.now = lambda: NOW
        self.s.kick = lambda: None

    async def switch(self, chat_id=84):
        await self.s.configure_telegram(
            {"bot_entry_id": "other", "chat_id": chat_id, "user_id": chat_id}
        )

    async def test_switch_rejects_previous_and_accepts_new_route(self):
        await self.switch()
        await self.s.receive(event())
        self.assertEqual(self.s.state["messages"], [])
        await self.s.receive(event(bot={"config_entry_id": "other"}, chat_id=84, user_id=84))
        self.assertEqual(len(self.s.state["messages"]), 1)

    async def test_same_message_id_in_another_chat_is_not_duplicate(self):
        await self.s.receive(event())
        await self.switch()
        await self.s.receive(event(bot={"config_entry_id": "other"}, chat_id=84, user_id=84))
        self.assertEqual(len(self.s.state["messages"]), 2)
        self.assertNotEqual(self.s.state["messages"][0]["key"], self.s.state["messages"][1]["key"])

    async def test_group_ignores_other_members_and_private_chat(self):
        await self.s.configure_telegram({"bot_entry_id": "bot", "chat_id": -10042, "user_id": 42})
        await self.s.receive(event(chat_id=-10042, user_id=43))
        await self.s.receive(event())
        self.assertEqual(self.s.state["messages"], [])
        await self.s.receive(event(chat_id=-10042, user_id=42))
        self.assertEqual(len(self.s.state["messages"]), 1)

    async def test_reply_finishes_in_its_original_chat_after_switch(self):
        async def parse(_):
            await self.switch()
            return {"reply": "Запомнил.", "operations": []}

        self.s.parser.parse = parse
        await self.s.receive(event())
        await self.s.process(self.s.state["messages"][0])
        await self.s.deliver()
        payload = self.hass.services.calls[0][2]
        self.assertEqual((payload["config_entry_id"], payload["chat_id"]), ("bot", 42))
        self.assertEqual(self.s.telegram_route["chat_id"], 84)

    async def test_future_reminder_goes_to_current_selected_chat(self):
        await self.s.change(
            [
                {
                    "action": "create",
                    "title": "A",
                    "due_at": (NOW + timedelta(days=1)).isoformat(),
                    "category": "Работа",
                    "priority": "Обычная",
                }
            ],
            "test",
        )
        self.s.enqueue("reminder", "Test", "1", True)
        await self.switch()
        await self.s.deliver()
        payload = self.hass.services.calls[0][2]
        self.assertEqual((payload["config_entry_id"], payload["chat_id"]), ("other", 84))

    async def test_reply_message_id_from_another_bot_does_not_bind_task(self):
        self.s.enqueue("r", "Test", "1")
        await self.s.deliver()
        mid = self.s.state["outbox"]["r"]["message_ids"][0]
        await self.switch(42)
        self.assertIsNone(
            self.s.reply_task({"reply_to": mid, "telegram_route": self.s.telegram_route})
        )
        self.assertEqual(
            self.s.reply_task(
                {"reply_to": mid, "telegram_route": {"bot_entry_id": "bot", "chat_id": 42}}
            ),
            "1",
        )

    async def test_failed_old_message_error_is_sent_to_its_source(self):
        async def parse(_):
            await self.switch()
            raise c.CodexError("timeout")

        self.s.parser.parse = parse
        await self.s.receive(event())
        await self.s.process(self.s.state["messages"][0])
        await self.s.deliver()
        self.assertEqual(self.hass.services.calls[0][2]["chat_id"], 42)

    async def test_invalid_switch_keeps_existing_route(self):
        before = dict(self.entry.data)
        with self.assertRaises(t.InvalidPlan):
            await self.s.configure_telegram({"bot_entry_id": "bot", "chat_id": 84})
        self.assertEqual(self.entry.data, before)
