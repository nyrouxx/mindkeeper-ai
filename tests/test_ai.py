"""Provider routing, private credentials, bounded transport and live switching."""

import asyncio
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import Hass, Store, component, entry, event, install_ha_fakes

install_ha_fakes()
ai = component("ai")


def context():
    return {
        "categories": ["Работа"],
        "priorities": ["Обычная"],
        "tasks": [{"title": "Private real task"}],
        "history": [{"text": "Private history"}],
        "message": "Завтра встреча",
        "now": "2026-11-11T12:00:00+03:00",
    }


PLAN = {"reply": "Хорошо, запомнил", "operations": []}


def chat_result(plan=None, finish="stop"):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {"content": json.dumps(PLAN if plan is None else plan)},
            }
        ]
    }


class Response:
    def __init__(self, value=None, status=200, raw=None):
        self.status = status
        self.raw = (
            raw
            if raw is not None
            else json.dumps(chat_result() if value is None else value).encode()
        )
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for offset in range(0, len(self.raw), size):
            yield self.raw[offset : offset + size]


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **options):
        self.calls.append((url, options))
        return self.response


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Store.database.clear()
        self.hass, self.entry = Hass(), entry()
        self.hass.config_entries = SimpleNamespace(
            async_update_entry=lambda entry, **kw: entry.__dict__.update(kw)
        )
        self.settings = ai.AISettings(self.hass, self.entry)

    async def test_profiles_and_keys_survive_switch_and_reload_without_exposure(self):
        original = deepcopy(self.entry.data)
        await self.settings.configure({"provider": "gemini", "api_key": "PRIVATE_GOOGLE_KEY"})
        await self.settings.configure({"provider": "deepseek", "api_key": "PRIVATE_DEEPSEEK_KEY"})
        await self.settings.configure({"provider": "gemini", "api_key": "", "model": "other-model"})
        self.assertEqual(self.settings.configuration()["api_key"], "PRIVATE_GOOGLE_KEY")
        self.assertEqual(self.entry.data, original)
        public = json.dumps(self.settings.public())
        for private in ["PRIVATE_GOOGLE_KEY", "PRIVATE_DEEPSEEK_KEY"]:
            self.assertNotIn(private, public)
            self.assertNotIn(private, json.dumps(self.entry.options))
        restored = ai.AISettings(self.hass, self.entry)
        await restored.load()
        self.assertEqual(restored.configuration()["model"], "other-model")
        self.assertEqual(restored.configuration("deepseek")["api_key"], "PRIVATE_DEEPSEEK_KEY")
        self.assertTrue(restored.public()["configurations"]["gemini"]["key_configured"])
        with self.assertRaises(ai.AIError):
            self.settings.validate(
                {"provider": "openai"}
            )  # Never reuse a different provider's key.

    async def test_failed_private_save_keeps_active_provider_and_original_keys(self):
        await self.settings.configure({"provider": "gemini", "api_key": "OLD_KEY"})
        self.settings.store.async_save = AsyncMock(side_effect=OSError("private path details"))
        with self.assertRaisesRegex(ai.AIError, "^ai_storage$"):
            await self.settings.configure({"provider": "deepseek", "api_key": "NEW_KEY"})
        self.assertEqual(self.settings.provider, "gemini")
        self.assertEqual(self.settings.configuration()["api_key"], "OLD_KEY")
        self.assertNotIn("deepseek", self.settings.configurations)

    async def test_concurrent_profile_updates_keep_both_credentials(self):
        original = self.settings.store.async_save

        async def slow(value):
            await asyncio.sleep(0.01)
            await original(value)

        self.settings.store.async_save = slow
        await asyncio.gather(
            self.settings.configure({"provider": "gemini", "api_key": "KEY_A"}),
            self.settings.configure({"provider": "deepseek", "api_key": "KEY_B"}),
        )
        self.assertEqual(self.settings.configuration("gemini")["api_key"], "KEY_A")
        self.assertEqual(self.settings.configuration("deepseek")["api_key"], "KEY_B")

    async def test_removing_key_disconnects_provider_without_an_implicit_fallback(self):
        await self.settings.configure({"provider": "gemini", "api_key": "PRIVATE_KEY"})
        await self.settings.configure({"provider": "gemini", "clear_key": True})
        self.assertEqual(self.settings.provider, "gemini")
        self.assertFalse(self.settings.public()["configurations"]["gemini"]["key_configured"])
        with self.assertRaisesRegex(ai.AIError, "^ai_key$"):
            self.settings.validate({"provider": "gemini"})

    async def test_public_projection_ignores_unknown_private_fields(self):
        self.settings.configurations["gemini"] = {
            "api_key": "PRIVATE_KEY",
            "unexpected": "PRIVATE_OTHER",
        }
        public = json.dumps(self.settings.public())
        self.assertNotIn("PRIVATE", public)

    async def test_endpoint_validation_and_preset_key_destination(self):
        _, config = self.settings.validate(
            {"provider": "gemini", "api_key": "KEY", "base_url": "https://wrong.example"}
        )
        self.assertEqual(config["base_url"], ai.PROVIDERS["gemini"]["base_url"])
        for url in [
            "file:///tmp/key",
            "https://user:password@example.test/v1",
            "https://example.test/v1?key=secret",
            "https://example.test/v1#secret",
            "https://example.test:bad",
        ]:
            with self.subTest(url=url), self.assertRaises(ai.AIError):
                self.settings.validate({"provider": "custom", "model": "test", "base_url": url})
        for provider in ["ollama", "custom"]:
            _, config = self.settings.validate(
                {
                    "provider": provider,
                    "model": "local-model",
                    "base_url": "http://192.168.1.20:11434/v1/",
                }
            )
            self.assertEqual(config["base_url"], "http://192.168.1.20:11434/v1")
        with self.assertRaises(ai.AIError):
            self.settings.validate(
                {"provider": "qwen", "api_key": "KEY", "base_url": "http://example.test"}
            )
        for invalid in [None, [], "missing"]:
            with self.assertRaises(ai.AIError):
                self.settings.validate({"provider": invalid})

    async def test_codex_default_keeps_existing_model_option(self):
        self.entry.options["model"] = "my-existing-model"
        parser = ai.AIParser(
            self.hass, self.settings, session_factory=lambda: self.fail("No HTTP for Codex")
        )
        parser.codex.parse = AsyncMock(return_value=PLAN)
        self.assertEqual(await parser.parse(context()), PLAN)
        self.assertEqual(parser.codex.model, "my-existing-model")

    async def test_api_mode_works_without_a_codex_binary_or_login(self):
        await self.settings.configure({"provider": "gemini", "api_key": "KEY"})
        session = Session(Response())
        parser = ai.AIParser(self.hass, self.settings, session_factory=lambda: session)
        parser.codex.parse = AsyncMock(side_effect=AssertionError("Must not invoke Codex"))
        self.assertEqual(await parser.parse(context()), PLAN)
        parser.codex.parse.assert_not_awaited()

    async def test_connection_check_sends_synthetic_context_and_does_not_activate_provider(self):
        session = Session(Response())
        parser = ai.AIParser(self.hass, self.settings, session_factory=lambda: session)
        await parser.test({"provider": "gemini", "api_key": "TEST_KEY"})
        self.assertEqual(self.settings.provider, "codex")
        self.assertFalse(self.settings.configurations)
        sent = json.loads(session.calls[0][1]["json"]["messages"][1]["content"])
        self.assertEqual(sent["tasks"], [])
        self.assertEqual(sent["history"], [])

    async def test_connection_check_cannot_overlap_codex_preparation(self):
        parser = ai.AIParser(self.hass, self.settings)
        began, finish = asyncio.Event(), asyncio.Event()

        async def preparing(_):
            began.set()
            await finish.wait()
            return PLAN

        parser.codex.parse = preparing
        task = asyncio.create_task(parser.parse(context()))
        await began.wait()
        self.assertIsNone(parser.codex.process)
        with self.assertRaisesRegex(ai.AIError, "^busy$"):
            await parser.test({"provider": "codex"})
        finish.set()
        self.assertEqual(await task, PLAN)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def request(self, provider="gemini", response=None):
        session = Session(response or Response())
        parser = ai.HttpParser(lambda: session)
        config = {
            "model": "test-model",
            "api_key": "PRIVATE_API_KEY",
            "base_url": ai.PROVIDERS[provider]["base_url"],
            "folder_id": "folder-123",
        }
        return parser, config, session

    async def test_gemini_structured_response_is_bounded_and_credentials_are_not_in_prompt(self):
        parser, config, session = self.request()
        result = await parser.parse(context(), "gemini", config)
        self.assertEqual(result, PLAN)
        url, request = session.calls[0]
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(request["headers"]["Authorization"], "Bearer PRIVATE_API_KEY")
        self.assertNotIn("PRIVATE_API_KEY", json.dumps(request["json"]))
        self.assertFalse(request["allow_redirects"])
        self.assertNotIn("tools", request["json"])
        schema = request["json"]["response_format"]["json_schema"]["schema"]
        self.assertIn(
            "Работа", schema["properties"]["operations"]["items"]["properties"]["category"]["enum"]
        )
        self.assertIsNone(parser.process)

    async def test_openai_uses_responses_and_disables_server_storage(self):
        response = {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(PLAN)}]}
            ],
        }
        parser, config, session = self.request("openai", Response(response))
        self.assertEqual(await parser.parse(context(), "openai", config), PLAN)
        url, request = session.calls[0]
        self.assertTrue(url.endswith("/responses"))
        self.assertFalse(request["json"]["store"])
        self.assertEqual(request["json"]["text"]["format"]["type"], "json_schema")

    async def test_qwen_deepseek_yandex_and_local_transports(self):
        for provider in ["deepseek", "qwen", "yandex", "ollama", "custom"]:
            with self.subTest(provider=provider):
                parser, config, session = self.request(provider)
                if provider in ["custom", "ollama"]:
                    config["api_key"] = ""
                self.assertEqual(await parser.parse(context(), provider, config), PLAN)
                body = session.calls[0][1]["json"]
                if provider == "yandex":
                    self.assertEqual(body["model"], "gpt://folder-123/test-model")
                if provider == "deepseek":
                    self.assertEqual(body["thinking"], {"type": "disabled"})
                if provider == "qwen":
                    self.assertFalse(body["enable_thinking"])
                if provider in ["custom", "ollama"]:
                    self.assertNotIn("Authorization", session.calls[0][1]["headers"])

    async def test_provider_errors_expose_no_response_body_or_credentials(self):
        for status, code in [
            (401, "ai_auth"),
            (403, "ai_auth"),
            (402, "ai_quota"),
            (429, "ai_quota"),
            (500, "ai_unavailable"),
            (302, "ai_request"),
            (400, "ai_request"),
        ]:
            with self.subTest(status=status):
                parser, config, session = self.request(
                    response=Response(
                        {"error": "PRIVATE_API_KEY and private server response"}, status
                    )
                )
                with self.assertRaisesRegex(ai.AIError, "^" + code + "$") as caught:
                    await parser.parse(context(), "gemini", config)
                self.assertNotIn("PRIVATE", ai.error_message(caught.exception))
                self.assertEqual(len(session.calls), 1)  # No silent fallback or paid retry.

    async def test_partial_refused_malformed_and_oversized_outputs_are_never_applied(self):
        responses = [
            Response(chat_result(finish="length")),
            Response(
                {
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": None, "refusal": "no"}}
                    ]
                }
            ),
            Response(chat_result({"reply": "ok", "operations": "create things"})),
            Response(chat_result({"reply": "ok", "operations": [None]})),
            Response(raw=b"not json"),
            Response(raw=b" " * 262145),
        ]
        for response in responses:
            parser, config, _ = self.request(response=response)
            with self.assertRaisesRegex(ai.AIError, "^invalid_response$"):
                await parser.parse(context(), "gemini", config)
        parser, config, _ = self.request("openai", Response({"status": "incomplete", "output": []}))
        with self.assertRaises(ai.AIError):
            await parser.parse(context(), "openai", config)

    async def test_stop_cancels_inflight_http_request(self):
        began = asyncio.Event()

        class WaitingResponse(Response):
            async def __aenter__(self):
                began.set()
                await asyncio.Event().wait()

        parser, config, _ = self.request(response=WaitingResponse())
        task = asyncio.create_task(parser.parse(context(), "gemini", config))
        await began.wait()
        self.assertIsNotNone(parser.process)
        with self.assertRaisesRegex(ai.AIError, "^busy$"):
            await parser.parse(context(), "gemini", config)
        await parser.stop()
        self.assertTrue(task.cancelled())
        self.assertIsNone(parser.process)


class SwitchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_inflight_old_provider_result_is_discarded_without_losing_message(self):
        coordinator = component("coordinator")
        secretary = coordinator.Secretary(Hass(), entry())
        secretary.kick = lambda: None
        await secretary.receive(event(text="Завтра в магазин"))

        async def changed_provider(_context):
            secretary.ai.generation += 1
            return {
                "reply": "Old provider reply",
                "operations": [{"action": "create", "title": "Old provider task"}],
            }

        secretary.parser.parse = changed_provider
        await secretary.process(secretary.state["messages"][0])
        self.assertFalse(secretary.state["tasks"])
        self.assertEqual(secretary.state["messages"][0]["status"], "pending")
        self.assertEqual(secretary.state["messages"][0]["attempts"], 0)
        self.assertFalse(secretary.state["outbox"])


if __name__ == "__main__":
    unittest.main()
