"""Selectable AI backends, private credentials and bounded, tool-free requests."""

import asyncio
import json
import re
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
        "hint": "Текущий способ: вход в ChatGPT через Codex. Отдельный API-ключ не нужен; действуют лимиты аккаунта.",
        "url": "https://developers.openai.com/codex/auth",
    },
    "gemini": {
        "label": "Google Gemini",
        "model": "gemini-2.5-flash-lite",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_required": True,
        "hint": "У некоторых моделей есть бесплатная квота API. Лимиты и доступность зависят от региона и аккаунта; проверь тариф в Google AI Studio.",
        "url": "https://aistudio.google.com/docs/pricing",
    },
    "deepseek": {
        "label": "DeepSeek API",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com/v1",
        "key_required": True,
        "hint": "Облачный API оплачивается отдельно от бесплатного чата DeepSeek на сайте.",
        "url": "https://api-docs.deepseek.com/quick_start/pricing/",
    },
    "qwen": {
        "label": "Qwen · Alibaba Cloud",
        "model": "qwen-plus",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "key_required": True,
        "custom_url": True,
        "hint": "Для новых аккаунтов возможна пробная квота на 90 дней. Включи Free Quota Only в кабинете Alibaba, если нужен только бесплатный доступ. Адрес API и ключ должны относиться к одному региону/workspace.",
        "url": "https://www.alibabacloud.com/help/en/model-studio/new-free-quota",
    },
    "yandex": {
        "label": "Яндекс · AI Studio",
        "model": "yandexgpt/rc",
        "base_url": "https://ai.api.cloud.yandex.net/v1",
        "key_required": True,
        "folder_required": True,
        "hint": "Нужны API-ключ AI Studio и ID каталога. Это облачный API с отдельным тарифом, а не вход в приложение Алисы.",
        "url": "https://aistudio.yandex.ru/en/docs/ai-studio/operations/generation/completions-structured",
    },
    "openai": {
        "label": "ChatGPT · OpenAI API",
        "model": "gpt-6-luna",
        "base_url": "https://api.openai.com/v1",
        "key_required": True,
        "hint": "Нужен ключ OpenAI API. Расходы и доступные кредиты проверяются в API-кабинете. Для существующего входа через ChatGPT выбирай Codex.",
        "url": "https://platform.openai.com/api-keys",
    },
    "ollama": {
        "label": "Ollama · локальные модели",
        "model": "qwen3:8b",
        "base_url": "http://localhost:11434/v1",
        "custom_url": True,
        "hint": "Облачной платы за локальную модель нет. Нужен уже запущенный Ollama на твоём компьютере/сервере и загруженная модель. Укажи адрес, доступный из HA; localhost означает сам сервер HA.",
        "url": "https://docs.ollama.com/api/openai-compatibility",
    },
    "custom": {
        "label": "Другой OpenAI-совместимый API",
        "model": "",
        "base_url": "",
        "custom_url": True,
        "hint": "Для LM Studio, собственного сервера или другого совместимого сервиса. Нужен Chat Completions API с поддержкой JSON. Тариф зависит от сервиса.",
        "url": "",
    },
}

ERRORS = {
    "ai_provider": "Выбери доступного AI-провайдера.",
    "ai_model": "Укажи название модели из кабинета провайдера.",
    "ai_endpoint": "Укажи адрес API без логина, пароля, query-параметров и фрагмента. Для облачных API нужен HTTPS.",
    "ai_key": "Добавь API-ключ выбранного провайдера.",
    "ai_folder": "Укажи ID каталога Яндекса.",
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


class AISettings:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.store = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.ai", private=True, atomic_writes=True
        )
        self.configurations = {}
        self.generation = 0
        self._lock = asyncio.Lock()

    @property
    def provider(self):
        return self.entry.options.get("ai_provider", "codex")

    async def load(self):
        stored = await self.store.async_load() or {}
        self.configurations = stored.get("providers", {})

    def configuration(self, provider=None):
        provider = self.provider if provider is None else provider
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise AIError("ai_provider")
        spec = PROVIDERS[provider]
        result = {k: spec[k] for k in ("model", "base_url")}
        result.update(api_key="", folder_id="")
        if provider == "codex":
            result["model"] = self.entry.options.get("model", "default")
        return {
            **result,
            **{k: v for k, v in self.configurations.get(provider, {}).items() if k in result},
        }

    def validate(self, selection, *, allow_clear=False):
        provider = selection.get("provider")
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise AIError("ai_provider")
        spec = PROVIDERS[provider]
        result = self.configuration(provider)
        for key in ("model", "base_url", "folder_id"):
            if key in selection:
                value = selection[key]
                if not isinstance(value, str):
                    raise AIError("ai_request")
                result[key] = value.strip()
        key = selection.get("api_key", "")
        if not isinstance(key, str) or len(key) > 4096 or any(c in key for c in "\r\n"):
            raise AIError("ai_key")
        if selection.get("clear_key"):
            result["api_key"] = ""
        if key.strip():
            result["api_key"] = key.strip()
        if (
            not result["model"]
            or len(result["model"]) > 300
            or any(c.isspace() for c in result["model"])
        ):
            raise AIError("ai_model")
        if (
            spec.get("key_required")
            and not result["api_key"]
            and not (allow_clear and selection.get("clear_key"))
        ):
            raise AIError("ai_key")
        if spec.get("folder_required") and not re.fullmatch(
            r"[A-Za-z0-9_-]{3,100}", result["folder_id"]
        ):
            raise AIError("ai_folder")
        if provider != "codex":
            # Preset keys cannot accidentally travel to a different provider.
            if not spec.get("custom_url"):
                result["base_url"] = spec["base_url"]
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
            if not valid or (provider not in ("ollama", "custom") and parsed.scheme != "https"):
                raise AIError("ai_endpoint")
        return provider, result

    async def configure(self, selection):
        async with self._lock:
            provider, configuration = self.validate(selection, allow_clear=True)
            updated = {**self.configurations, provider: configuration}
            try:
                await self.store.async_save({"providers": updated})
            except Exception:
                raise AIError("ai_storage") from None
            self.configurations = updated
            self.generation += 1
            self.hass.config_entries.async_update_entry(
                self.entry, options={**self.entry.options, "ai_provider": provider}
            )

    def public(self):
        return {
            "provider": self.provider,
            "providers": [{"value": key, **spec} for key, spec in PROVIDERS.items()],
            "configurations": {
                key: {
                    **{k: v for k, v in self.configuration(key).items() if k != "api_key"},
                    "key_configured": bool(self.configuration(key)["api_key"]),
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
        if provider == "openai":
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
            if provider == "yandex":
                model = (
                    f"gpt://{config['folder_id']}/{model}"
                    if not model.startswith("gpt://")
                    else model
                )
                headers["OpenAI-Project"] = config["folder_id"]
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
            if provider in ("gemini", "yandex", "ollama"):
                body["response_format"] = {"type": "json_schema", "json_schema": structured}
            if provider == "deepseek":
                body["thinking"] = {"type": "disabled"}
            if provider == "qwen":
                body["enable_thinking"] = False
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
                if provider == "openai":
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
