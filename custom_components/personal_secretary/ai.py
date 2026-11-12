"""Selectable AI backends, private credentials and bounded, tool-free requests."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import urlsplit

import aiohttp
from homeassistant.helpers.storage import Store

from .codex import INSTRUCTIONS, CodexError, CodexParser, plan_schema
from .const import DOMAIN

PROVIDERS = {
    "codex": {
        "label": "Codex · вход через ChatGPT",
        "model": "default",
        "base_url": "",
        "hint": "Вход в ChatGPT через Codex. Отдельный API-токен не нужен; действуют лимиты аккаунта.",
        "url": "https://developers.openai.com/codex/auth",
    },
    "custom": {
        "label": "Ручная настройка API",
        "model": "",
        "base_url": "",
        "custom_url": True,
        "hint": "Укажи базовый адрес API, модель и токен сервиса. Нужен совместимый API с JSON-ответами. Для локального сервера токен может не требоваться.",
        "url": "",
    },
}

AI_DATA = "ai_configurations"
CUSTOM_DEFAULTS = {
    "model": "",
    "base_url": "",
    "api_key": "",
    "api_format": "chat_completions",
    "response_format": "json_object",
    "project_id": "",
    "extra_body": {},
}
# Version 0.7.0 presets are read only during migration, never offered in the UI.
LEGACY_DEFAULTS = {
    "gemini": ("gemini-2.5-flash-lite", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "deepseek": ("deepseek-flash", "https://api.deepseek.com/v1"),
    "qwen": ("qwen-plus", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    "yandex": ("yandexgpt/rc", "https://ai.api.cloud.yandex.net/v1"),
    "openai": ("gpt-6-luna", "https://api.openai.com/v1"),
    "ollama": ("qwen3:8b", "http://localhost:11434/v1"),
}
RESERVED_BODY = {
    "model",
    "messages",
    "instructions",
    "input",
    "prompt",
    "stream",
    "tools",
    "tool_choice",
    "functions",
    "function_call",
    "response_format",
    "text",
    "store",
    "previous_response_id",
    "conversation",
    "background",
}

ERRORS = {
    "ai_provider": "Выбери доступного AI-провайдера.",
    "ai_model": "Укажи название модели из кабинета провайдера.",
    "ai_endpoint": "Укажи базовый адрес API без логина, пароля, query-параметров и фрагмента.",
    "ai_key": "Добавь API-ключ выбранного провайдера.",
    "ai_key_destination": "Для другого сервера введи токен заново или удали сохранённый токен.",
    "ai_auth": "API отклонил ключ или не разрешает доступ к модели. Проверь ключ, права и доступность сервиса в твоём регионе.",
    "ai_quota": "Лимит запросов или квота API исчерпаны. Проверь лимиты и тариф в кабинете провайдера.",
    "ai_request": "API отклонил запрос. Проверь модель, адрес сервера и поддержку JSON-ответов.",
    "ai_unavailable": "AI-сервис недоступен. Проверь адрес и соединение.",
    "ai_storage": "Не удалось сохранить настройки AI. Предыдущие настройки сохранены.",
    "busy": "Сейчас разбираю сообщение. Попробуй проверить подключение чуть позже.",
    "timeout": "AI не ответил вовремя.",
    "invalid_response": "AI вернул неполный или неподходящий ответ. Попробуй другую модель.",
    "codex_unavailable": "Codex недоступен. Проверь установку, вход в ChatGPT и лимиты аккаунта.",
}


class AIError(CodexError):
    """Only fixed, non-secret error codes can cross the HTTP/UI boundary."""


def error_message(error):
    return ERRORS.get(str(error), "Не удалось проверить подключение к AI.")


def checked_plan(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("reply"), str)
        or not isinstance(value.get("operations"), list)
        or len(value["operations"]) > 50
        or any(not isinstance(op, dict) for op in value["operations"])
    ):
        raise AIError("invalid_response")
    return value


def migrate_configurations(stored, provider):
    """Retain Codex and one manual connection, preserving the active old preset."""
    result = {}
    if "codex" in stored:
        result["codex"] = {"model": stored["codex"].get("model", "default")}
    source = dict(stored.get("custom", {}))
    if provider in LEGACY_DEFAULTS:
        model, url = LEGACY_DEFAULTS[provider]
        source = {"model": model, "base_url": url, **stored.get(provider, {})}
        if provider == "openai":
            source["api_format"] = "responses"
        if provider in ("gemini", "yandex", "ollama"):
            source["response_format"] = "json_schema"
        if provider == "yandex":
            source["project_id"] = source.get("folder_id", "")
            if not source["model"].startswith("gpt://"):
                source["model"] = f"gpt://{source['project_id']}/{source['model']}"
        if provider == "deepseek":
            source["extra_body"] = {"thinking": {"type": "disabled"}}
        if provider == "qwen":
            source["extra_body"] = {"enable_thinking": False}
        provider = "custom"
    if source:
        result["custom"] = {
            **CUSTOM_DEFAULTS,
            **{k: v for k, v in source.items() if k in CUSTOM_DEFAULTS},
        }
    return result, provider


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class AISettings:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.legacy_store = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.ai", private=True, atomic_writes=True
        )
        self.configurations = {}
        self.generation = 0
        self._lock = asyncio.Lock()

    @property
    def provider(self):
        return self.entry.options.get("ai_provider", "codex")

    async def load(self):
        migrated_this_boot = self.hass.data.setdefault(DOMAIN + "_ai_migrations", set())
        if AI_DATA in self.entry.data:
            self.configurations = deepcopy(self.entry.data[AI_DATA])
            # Config entries save asynchronously. Keep the old file during the
            # migration boot; delete only after a subsequent Core start has
            # loaded the migrated entry from disk. Reloads in that boot are safe.
            if self.entry.entry_id not in migrated_this_boot:
                await self.legacy_store.async_remove()
            return
        stored = await self.legacy_store.async_load()
        if stored or self.provider in LEGACY_DEFAULTS:
            profiles, provider = migrate_configurations(
                (stored or {}).get("providers", {}), self.provider
            )
            self._commit(profiles, provider)
            migrated_this_boot.add(self.entry.entry_id)

    def configuration(self, provider=None):
        provider = self.provider if provider is None else provider
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise AIError("ai_provider")
        defaults = (
            {"model": self.entry.options.get("model", "default")}
            if provider == "codex"
            else CUSTOM_DEFAULTS
        )
        return deepcopy(
            {
                **defaults,
                **{k: v for k, v in self.configurations.get(provider, {}).items() if k in defaults},
            }
        )

    def validate(self, selection):
        provider = selection.get("provider")
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise AIError("ai_provider")
        result = self.configuration(provider)
        previous_url = result.get("base_url", "")
        for key in ("model", "base_url", "api_format", "response_format", "project_id"):
            if key in selection and key in result:
                value = selection[key]
                if not isinstance(value, str):
                    raise AIError("ai_request")
                result[key] = value.strip()
        if (
            not result["model"]
            or len(result["model"]) > 300
            or any(c.isspace() for c in result["model"])
        ):
            raise AIError("ai_model")
        if provider == "codex":
            return provider, result
        key = selection.get("api_key", "")
        if not isinstance(key, str) or len(key) > 4096 or any(c in key for c in "\r\n"):
            raise AIError("ai_key")
        if selection.get("clear_key"):
            result["api_key"] = ""
        if key.strip():
            result["api_key"] = key.strip()
        result["base_url"] = result["base_url"].rstrip("/")
        try:
            parsed = urlsplit(result["base_url"])
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.port != 0
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and not any(c.isspace() for c in result["base_url"])
                and len(result["base_url"]) <= 2048
            )
        except ValueError:
            valid = False
        if not valid:
            raise AIError("ai_endpoint")
        if (
            result["api_key"]
            and not key.strip()
            and previous_url
            and origin(previous_url) != origin(result["base_url"])
        ):
            raise AIError("ai_key_destination")
        if result["api_format"] not in ("chat_completions", "responses") or result[
            "response_format"
        ] not in ("json_object", "json_schema"):
            raise AIError("ai_request")
        if len(result["project_id"]) > 100 or any(c.isspace() for c in result["project_id"]):
            raise AIError("ai_request")
        if "extra_body" in selection:
            result["extra_body"] = deepcopy(selection["extra_body"])
        try:
            valid = (
                isinstance(result["extra_body"], dict)
                and not RESERVED_BODY.intersection(result["extra_body"])
                and len(json.dumps(result["extra_body"], allow_nan=False)) <= 8192
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise AIError("ai_request")
        return provider, result

    def _commit(self, profiles, provider):
        try:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, AI_DATA: deepcopy(profiles)},
                options={**self.entry.options, "ai_provider": provider},
            )
        except Exception:
            raise AIError("ai_storage") from None
        self.configurations = profiles
        self.generation += 1

    async def configure(self, selection):
        async with self._lock:
            provider, configuration = self.validate(selection)
            self._commit({**self.configurations, provider: configuration}, provider)

    def public(self):
        return {
            "provider": self.provider,
            "providers": [{"value": key, **spec} for key, spec in PROVIDERS.items()],
            "configurations": {
                key: {
                    **{k: v for k, v in self.configuration(key).items() if k != "api_key"},
                    "key_configured": bool(self.configuration(key).get("api_key")),
                }
                for key in PROVIDERS
            },
        }


class HttpParser:
    def __init__(self, session_factory, timeout=180):
        self.session_factory = session_factory
        self.timeout = timeout
        self.process = None

    async def parse(self, context, provider, configuration):
        if self.process is not None:
            raise AIError("busy")
        self.process = asyncio.current_task()
        try:
            return await self._parse(context, provider, dict(configuration))
        finally:
            self.process = None

    async def _parse(self, context, provider, config):
        schema = plan_schema(context)
        prompt = (
            INSTRUCTIONS
            + '\nПример JSON без изменений: {"reply":"Хорошо","operations":[]}\nJSON Schema:\n'
            + json.dumps(schema, ensure_ascii=False)
        )
        data = json.dumps(context, ensure_ascii=False)
        headers = {"Accept": "application/json"}
        if config["api_key"]:
            headers["Authorization"] = "Bearer " + config["api_key"]
        structured = {"name": "mindkeeper_plan", "schema": schema, "strict": True}
        if config["api_format"] == "responses":
            route = "/responses"
            body = {
                "model": config["model"],
                "instructions": prompt,
                "input": data,
                "text": {"format": {"type": "json_schema", **structured}},
                "store": False,
                "max_output_tokens": 8192,
            }
        else:
            route = "/chat/completions"
            model = config["model"]
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": data},
                ],
                "stream": False,
                "max_tokens": 8192,
                "response_format": {"type": "json_object"},
            }
            if config["response_format"] == "json_schema":
                body["response_format"] = {"type": "json_schema", "json_schema": structured}
        if config["project_id"]:
            headers["OpenAI-Project"] = config["project_id"]
        body.update(config["extra_body"])
        try:
            async with self.session_factory().post(
                config["base_url"] + route,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise AIError("ai_auth")
                if response.status in (402, 429):
                    raise AIError("ai_quota")
                if response.status >= 500:
                    raise AIError("ai_unavailable")
                if response.status != 200:
                    raise AIError("ai_request")
                raw = bytearray()
                async for block in response.content.iter_chunked(16384):
                    raw.extend(block)
                    if len(raw) > 262144:
                        raise AIError("invalid_response")
                result = json.loads(raw)
                if config["api_format"] == "responses":
                    if result.get("status") != "completed":
                        raise AIError("invalid_response")
                    text = "".join(
                        part["text"]
                        for item in result["output"]
                        if item.get("type") == "message"
                        for part in item.get("content", [])
                        if part.get("type") == "output_text"
                    )
                else:
                    choice = result["choices"][0]
                    if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
                        raise AIError("invalid_response")
                    text = choice["message"]["content"]
                return checked_plan(json.loads(text))
        except (aiohttp.ClientError, OSError, TimeoutError) as error:
            raise AIError(
                "timeout" if isinstance(error, TimeoutError) else "ai_unavailable"
            ) from None
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise AIError("invalid_response") from None

    async def stop(self):
        task = self.process
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class AIParser:
    def __init__(self, hass, settings, session_factory=None):
        self.settings = settings
        self.codex = CodexParser(hass.config.path(".personal_secretary"))
        if session_factory is None:

            def session_factory():
                from homeassistant.helpers.aiohttp_client import async_get_clientsession

                return async_get_clientsession(hass)

        self.http = HttpParser(session_factory)
        self._active = None

    @property
    def process(self):
        return self._active or self.codex.process or self.http.process

    async def parse(self, context):
        provider = self.settings.provider
        provider, config = self.settings.validate({"provider": provider})
        return await self._run(context, provider, config)

    async def _run(self, context, provider, config):
        if self.process:
            raise AIError("busy")
        # Reserve the whole turn, including Codex's asynchronous preparation
        # before its subprocess exists. UI tests cannot overlap a real message.
        self._active = asyncio.current_task()
        try:
            if provider == "codex":
                self.codex.model = config["model"]
                return checked_plan(await self.codex.parse(context))
            return await self.http.parse(context, provider, config)
        finally:
            self._active = None

    async def test(self, selection):
        if self.process:
            raise AIError("busy")
        provider, config = self.settings.validate(selection)
        context = {
            "now": datetime.now(UTC).isoformat(),
            "timezone": "UTC",
            "categories": ["Повседнев"],
            "priorities": ["Обычная"],
            "tasks": [],
            "history": [],
            "message": "Это проверка подключения. Ответь «Подключение работает», без операций.",
            "reply_task_id": None,
            "classification_only": False,
            "repeat_interval_minutes": 10,
            "all_day_reminder_mode": "after_hour",
            "all_day_reminder_time": "09:00",
        }
        result = await self._run(context, provider, config)
        if result["operations"] or not result["reply"].strip():
            raise AIError("invalid_response")
        return {"provider": provider, "message": "Подключение работает"}

    async def stop(self):
        task = self._active
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.http.stop()
        await self.codex.stop()
