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


def manual(**changes):
    return {
        "provider": "custom",
        "model": "test-model",
        "base_url": "https://api.example.test/v1",
        **changes,
    }


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Store.database.clear()
        self.hass, self.entry = Hass(), entry()
        self.hass.config_entries = SimpleNamespace(
            async_update_entry=lambda entry, **kw: entry.__dict__.update(kw)
        )
        self.settings = ai.AISettings(self.hass, self.entry)

    async def test_only_codex_and_manual_are_offered(self):
        self.assertEqual(set(ai.PROVIDERS), {"codex", "custom"})
        self.assertEqual(
            [p["value"] for p in self.settings.public()["providers"]], ["codex", "custom"]
        )
        for legacy in ai.LEGACY_DEFAULTS:
            with self.assertRaisesRegex(ai.AIError, "^ai_provider$"):
                self.settings.validate({"provider": legacy})

    async def test_native_entry_keeps_token_across_switches_without_public_exposure(self):
        original = deepcopy(self.entry.data)
        await self.settings.configure(manual(api_key="PRIVATE_KEY"))
        await self.settings.configure({"provider": "codex", "model": "codex-model"})
        await self.settings.configure(manual(api_key="", model="other-model"))
        self.assertEqual(self.settings.configuration()["api_key"], "PRIVATE_KEY")
        self.assertEqual({k: v for k, v in self.entry.data.items() if k != ai.AI_DATA}, original)
        self.assertEqual(self.entry.data[ai.AI_DATA]["custom"]["api_key"], "PRIVATE_KEY")
        self.assertNotIn("PRIVATE", json.dumps(self.settings.public()))
        self.assertNotIn("PRIVATE", json.dumps(self.entry.options))
        self.assertFalse(Store.database)  # No new per-integration secret file.
        restored = ai.AISettings(self.hass, self.entry)
        await restored.load()
        self.assertEqual(restored.configuration()["model"], "other-model")
        self.assertEqual(restored.configuration("codex")["model"], "codex-model")
        self.assertTrue(restored.public()["configurations"]["custom"]["key_configured"])

    async def test_failed_entry_update_keeps_original_settings_and_safe_error(self):
        await self.settings.configure(manual(api_key="OLD_KEY"))
        self.hass.config_entries.async_update_entry = lambda *a, **kw: (_ for _ in ()).throw(
            OSError("private path")
        )
        with self.assertRaisesRegex(ai.AIError, "^ai_storage$"):
            await self.settings.configure(manual(api_key="NEW_KEY"))
        self.assertEqual(self.settings.configuration()["api_key"], "OLD_KEY")

    async def test_concurrent_changes_preserve_both_connection_settings(self):
        await asyncio.gather(
            self.settings.configure(manual(api_key="PRIVATE_KEY")),
            self.settings.configure({"provider": "codex", "model": "codex-model"}),
        )
        self.assertEqual(self.settings.configuration("custom")["api_key"], "PRIVATE_KEY")
        self.assertEqual(self.settings.configuration("codex")["model"], "codex-model")

    async def test_clear_key_and_public_projection(self):
        await self.settings.configure(manual(api_key="PRIVATE_KEY"))
        self.settings.configurations["custom"]["unexpected"] = "PRIVATE_OTHER"
        self.assertNotIn("PRIVATE", json.dumps(self.settings.public()))
        await self.settings.configure(manual(clear_key=True))
        self.assertFalse(self.settings.public()["configurations"]["custom"]["key_configured"])
        self.assertEqual(self.settings.configuration()["api_key"], "")
        self.assertEqual(self.settings.provider, "custom")

    async def test_changing_server_cannot_silently_send_saved_token_elsewhere(self):
        await self.settings.configure(manual(api_key="PRIVATE_KEY"))
        for url in [
            "https://other.test/v1",
            "http://api.example.test/v1",
            "https://api.example.test:8443/v1",
        ]:
            with self.subTest(url=url), self.assertRaisesRegex(ai.AIError, "^ai_key_destination$"):
                self.settings.validate(manual(base_url=url))
        self.settings.validate(manual(base_url="https://api.example.test/other"))
        self.settings.validate(manual(base_url="https://other.test/v1", api_key="NEW_KEY"))
        _, config = self.settings.validate(
            manual(base_url="http://local-server:11434/v1", clear_key=True)
        )
        self.assertEqual(config["api_key"], "")

    async def test_invalid_urls_and_unsafe_request_overrides_are_rejected(self):
        for url in [
            "file:///tmp/key",
            "https://user:password@example.test/v1",
            "https://example.test/v1?key=secret",
            "https://example.test/v1#secret",
            "https://example.test:bad",
        ]:
            with self.subTest(url=url), self.assertRaisesRegex(ai.AIError, "^ai_endpoint$"):
                self.settings.validate(manual(base_url=url))
        for params in [
            {"tools": []},
            {"model": "other"},
            {"messages": []},
            {"store": True},
            [],
            {"number": float("nan")},
            {"blob": "x" * 8193},
        ]:
            with (
                self.subTest(params=str(params)[:50]),
                self.assertRaisesRegex(ai.AIError, "^ai_request$"),
            ):
                self.settings.validate(manual(extra_body=params))
        for changes in [
            {"api_format": "bad"},
            {"response_format": "bad"},
            {"project_id": "bad\nheader"},
            {"api_key": "bad\nheader"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ai.AIError):
                self.settings.validate(manual(**changes))
        _, config = self.settings.validate(manual(base_url="http://192.168.1.20:11434/v1/"))
        self.assertEqual(config["base_url"], "http://192.168.1.20:11434/v1")

    async def test_active_legacy_presets_migrate_to_manual_with_protocol_details(self):
        for provider in ai.LEGACY_DEFAULTS:
            with self.subTest(provider=provider):
                self.entry = entry()
                self.entry.options["ai_provider"] = provider
                settings = ai.AISettings(self.hass, self.entry)
                Store.database[settings.legacy_store.key] = {
                    "providers": {provider: {"api_key": "PRIVATE_KEY", "folder_id": "folder-123"}}
                }
                await settings.load()
                self.assertEqual(settings.provider, "custom")
                config = settings.configuration()
                self.assertEqual(config["api_key"], "PRIVATE_KEY")
                self.assertEqual(config["base_url"], ai.LEGACY_DEFAULTS[provider][1])
                self.assertNotIn("PRIVATE", json.dumps(settings.public()))
                if provider == "openai":
                    self.assertEqual(config["api_format"], "responses")
                if provider == "yandex":
                    self.assertEqual(config["model"], "gpt://folder-123/yandexgpt/rc")
                    self.assertEqual(config["project_id"], "folder-123")
                if provider == "deepseek":
                    self.assertEqual(config["extra_body"], {"thinking": {"type": "disabled"}})
                if provider == "qwen":
                    self.assertEqual(config["extra_body"], {"enable_thinking": False})

    async def test_migration_keeps_recovery_file_until_next_core_boot(self):
        self.entry.options["ai_provider"] = "custom"
        key = self.settings.legacy_store.key
        Store.database[key] = {
            "providers": {
                "custom": {**manual(), "api_key": "PRIVATE_KEY"},
                "gemini": {"api_key": "UNUSED_KEY"},
            }
        }
        await self.settings.load()
        self.assertIn(key, Store.database)
        self.assertNotIn("gemini", self.entry.data[ai.AI_DATA])
        reload = ai.AISettings(self.hass, self.entry)
        await reload.load()
        self.assertIn(key, Store.database)
        # New Core instance has now loaded the migrated config entry from disk.
        self.hass.data.clear()
        restored = ai.AISettings(self.hass, self.entry)
        await restored.load()
        self.assertNotIn(key, Store.database)
        self.assertEqual(restored.configuration()["api_key"], "PRIVATE_KEY")

    async def test_codex_default_keeps_existing_model_option(self):
        self.entry.options["model"] = "my-existing-model"
        parser = ai.AIParser(
            self.hass, self.settings, session_factory=lambda: self.fail("No HTTP for Codex")
        )
        parser.codex.parse = AsyncMock(return_value=PLAN)
        self.assertEqual(await parser.parse(context()), PLAN)
        self.assertEqual(parser.codex.model, "my-existing-model")

    async def test_api_mode_works_without_a_codex_binary_or_login(self):
        await self.settings.configure(manual(api_key="KEY"))
        session = Session(Response())
        parser = ai.AIParser(self.hass, self.settings, session_factory=lambda: session)
        parser.codex.parse = AsyncMock(side_effect=AssertionError("Must not invoke Codex"))
        self.assertEqual(await parser.parse(context()), PLAN)
        parser.codex.parse.assert_not_awaited()

    async def test_connection_check_sends_synthetic_context_and_does_not_activate_provider(self):
        session = Session(Response())
        parser = ai.AIParser(self.hass, self.settings, session_factory=lambda: session)
        await parser.test(manual(api_key="TEST_KEY"))
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
    def request(self, api_format="chat_completions", response=None):
        session = Session(response or Response())
        parser = ai.HttpParser(lambda: session)
        config = {
            "model": "test-model",
            "api_key": "PRIVATE_API_KEY",
            "base_url": "https://api.example.test/v1",
            "api_format": api_format,
            "response_format": "json_schema",
            "project_id": "project-123",
            "extra_body": {},
        }
        return parser, config, session

    async def test_manual_structured_response_is_bounded_and_token_is_not_in_prompt(self):
        parser, config, session = self.request()
        result = await parser.parse(context(), "custom", config)
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

    async def test_manual_responses_format_disables_server_storage(self):
        response = {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(PLAN)}]}
            ],
        }
        parser, config, session = self.request("responses", Response(response))
        self.assertEqual(await parser.parse(context(), "custom", config), PLAN)
        url, request = session.calls[0]
        self.assertTrue(url.endswith("/responses"))
        self.assertFalse(request["json"]["store"])
        self.assertEqual(request["json"]["text"]["format"]["type"], "json_schema")

    async def test_manual_parameters_and_local_server_without_token(self):
        parser, config, session = self.request()
        config.update(
            api_key="", response_format="json_object", extra_body={"enable_thinking": False}
        )
        self.assertEqual(await parser.parse(context(), "custom", config), PLAN)
        body = session.calls[0][1]["json"]
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(session.calls[0][1]["headers"]["OpenAI-Project"], "project-123")
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
                    await parser.parse(context(), "custom", config)
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
                await parser.parse(context(), "custom", config)
        parser, config, _ = self.request(
            "responses", Response({"status": "incomplete", "output": []})
        )
        with self.assertRaises(ai.AIError):
            await parser.parse(context(), "custom", config)

    async def test_stop_cancels_inflight_http_request(self):
        began = asyncio.Event()

        class WaitingResponse(Response):
            async def __aenter__(self):
                began.set()
                await asyncio.Event().wait()

        parser, config, _ = self.request(response=WaitingResponse())
        task = asyncio.create_task(parser.parse(context(), "custom", config))
        await began.wait()
        self.assertIsNotNone(parser.process)
        with self.assertRaisesRegex(ai.AIError, "^busy$"):
            await parser.parse(context(), "custom", config)
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
