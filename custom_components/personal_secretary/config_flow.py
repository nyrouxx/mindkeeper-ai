"""UI settings; reuse a configured private Telegram chat."""

from pathlib import Path

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .ai import PROVIDERS, AIError
from .auth import DeviceLogin
from .const import DEFAULTS, DOMAIN, NAME
from .google_sync import calendar_choices, validate_selection
from .model import InvalidPlan, validate_day_settings, validate_labels, validate_repeat_interval
from .telegram import bot_choices, chat_choices, validate_route


def existing_chats(hass):
    return {key: value for key, value in chat_choices(hass).items() if value["chat_id"] > 0}


class SecretaryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_import(self, user_input):
        chats = existing_chats(self.hass)
        if len(chats) != 1:
            return self.async_abort(reason="select_chat")
        return await self.async_step_user({"chat": next(iter(chats))})

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("personal_secretary")
        self._abort_if_unique_id_configured()
        chats = chat_choices(self.hass)
        if not chats:
            return self.async_abort(reason="no_telegram_chat")
        if user_input is not None and user_input.get("chat") in chats:
            selected = {k: v for k, v in chats[user_input["chat"]].items() if k != "label"}
            self.telegram_route = selected
            if selected["chat_id"] < 0:
                return await self.async_step_sender()
            return self.async_create_entry(title=NAME, data=selected)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("chat"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=k,
                                    label=bot_choices(self.hass)[v["bot_entry_id"]]["label"]
                                    + " · "
                                    + v["label"],
                                )
                                for k, v in chats.items()
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_sender(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.telegram_route["user_id"] = user_input["user_id"]
            try:
                selected = validate_route(self.hass, self.telegram_route)
            except InvalidPlan:
                errors["user_id"] = "invalid_user"
            else:
                return self.async_create_entry(title=NAME, data=selected)
        return self.async_show_form(
            step_id="sender",
            data_schema=vol.Schema(
                {
                    vol.Required("user_id"): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SecretaryOptionsFlow()


class SecretaryOptionsFlow(config_entries.OptionsFlow):
    def __init__(self):
        self.login = None
        self.task = None
        self.challenge = None

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init", menu_options=["ai", "telegram", "settings", "google", "login"]
        )

    async def async_step_ai(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("provider") in PROVIDERS:
                self.ai_provider = user_input["provider"]
                return await self.async_step_ai_settings()
            errors["base"] = "ai_provider"
        return self.async_show_form(
            step_id="ai",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "provider", default=self.config_entry.runtime_data.ai.provider
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": key, "label": spec["label"]}
                                for key, spec in PROVIDERS.items()
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_ai_settings(self, user_input=None):
        sec = self.config_entry.runtime_data
        provider = self.ai_provider
        spec, values = PROVIDERS[provider], sec.ai.configuration(provider)
        errors = {}
        if user_input is not None:
            try:
                await sec.configure_ai({"provider": provider, **user_input})
            except AIError as error:
                errors["base"] = str(error)
                values.update(
                    {k: v for k, v in user_input.items() if k in values and k != "api_key"}
                )
            else:
                return self.async_create_entry(title="", data=dict(self.config_entry.options))
        fields = {vol.Required("model", default=values["model"]): selector.TextSelector()}
        if provider != "codex":
            # Never put a stored credential in a form default or result data.
            fields[vol.Optional("api_key")] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            )
            fields[vol.Optional("clear_key", default=False)] = bool
        if spec.get("custom_url"):
            fields[vol.Required("base_url", default=values["base_url"])] = selector.TextSelector()
        if provider == "custom":
            for key, choices in (
                (
                    "api_format",
                    [("chat_completions", "Chat Completions"), ("responses", "Responses")],
                ),
                ("response_format", [("json_object", "JSON"), ("json_schema", "JSON Schema")]),
            ):
                fields[vol.Optional(key, default=values[key])] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": value, "label": label} for value, label in choices],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            fields[vol.Optional("project_id", default=values["project_id"])] = (
                selector.TextSelector()
            )
            fields[vol.Optional("extra_body", default=values["extra_body"])] = (
                selector.ObjectSelector()
            )
        return self.async_show_form(
            step_id="ai_settings",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "provider": spec["label"],
                "hint": spec["hint"],
                "key_status": "Токен сохранён" if values.get("api_key") else "Токен не задан",
            },
        )

    async def async_step_google(self, user_input=None):
        values = {**DEFAULTS, **self.config_entry.options}
        errors = {}
        if user_input is not None:
            try:
                calendar, mode = validate_selection(
                    self.hass, user_input["google_calendar"], user_input["google_sync_mode"]
                )
            except InvalidPlan:
                errors["base"] = "invalid_calendar"
            else:
                return self.async_create_entry(
                    title="", data={**values, "google_calendar": calendar, "google_sync_mode": mode}
                )
        choices = [{"value": "", "label": "Синхронизация выключена"}, *calendar_choices(self.hass)]
        current = (
            values["google_calendar"]
            if values["google_calendar"] in {c["value"] for c in choices}
            else ""
        )
        return self.async_show_form(
            step_id="google",
            data_schema=vol.Schema(
                {
                    vol.Required("google_calendar", default=current): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=choices, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    ),
                    vol.Required(
                        "google_sync_mode", default=values["google_sync_mode"]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": "bidirectional", "label": "В обе стороны"},
                                {"value": "export", "label": "Только дела → Google"},
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_telegram(self, user_input=None):
        bots = bot_choices(self.hass)
        if not bots:
            return self.async_abort(reason="no_telegram_bot")
        if user_input is not None and user_input.get("bot_entry_id") in bots:
            self.telegram_bot_id = user_input["bot_entry_id"]
            return await self.async_step_telegram_chat()
        current = self.config_entry.data.get("bot_entry_id")
        default = current if current in bots else next(iter(bots))
        return self.async_show_form(
            step_id="telegram",
            data_schema=vol.Schema(
                {
                    vol.Required("bot_entry_id", default=default): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(bots.values()), mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    ),
                }
            ),
        )

    async def async_step_telegram_chat(self, user_input=None):
        chats = chat_choices(self.hass, self.telegram_bot_id)
        if not chats:
            return self.async_abort(reason="no_telegram_chat")
        if user_input is not None and user_input.get("chat") in chats:
            self.telegram_route = {
                k: v for k, v in chats[user_input["chat"]].items() if k != "label"
            }
            if self.telegram_route["chat_id"] < 0:
                return await self.async_step_telegram_sender()
            return await self._save_telegram()
        current = f"{self.telegram_bot_id}:{self.config_entry.data['chat_id']}"
        default = current if current in chats else next(iter(chats))
        return self.async_show_form(
            step_id="telegram_chat",
            data_schema=vol.Schema(
                {
                    vol.Required("chat", default=default): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=k, label=v["label"])
                                for k, v in chats.items()
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_telegram_sender(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.telegram_route["user_id"] = user_input["user_id"]
            try:
                self.telegram_route = validate_route(self.hass, self.telegram_route)
            except InvalidPlan:
                errors["user_id"] = "invalid_user"
            else:
                return await self._save_telegram()
        return self.async_show_form(
            step_id="telegram_sender",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "user_id", default=str(self.config_entry.data["user_id"])
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    async def _save_telegram(self):
        try:
            await self.config_entry.runtime_data.configure_telegram(self.telegram_route)
        except InvalidPlan:
            return self.async_abort(reason="telegram_changed")
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_settings(self, user_input=None):
        values = {key: self.config_entry.options.get(key, value) for key, value in DEFAULTS.items()}
        errors = {}
        if user_input is not None:
            values.update(user_input)
            for key in ("categories", "priorities"):
                try:
                    values[key] = validate_labels(values[key])
                except InvalidPlan:
                    errors[key] = "invalid_labels"
            try:
                values["repeat_interval_minutes"] = validate_repeat_interval(
                    values["repeat_interval_minutes"]
                )
            except InvalidPlan:
                errors["repeat_interval_minutes"] = "invalid_interval"
            try:
                validate_day_settings(
                    values["all_day_reminder_mode"], values["all_day_reminder_time"]
                )
            except InvalidPlan:
                errors["all_day_reminder_time"] = "invalid_day_time"
            if not errors:
                return self.async_create_entry(title="", data=values)
        schema = {
            vol.Required("enabled", default=values["enabled"]): bool,
            vol.Required("agenda_enabled", default=values["agenda_enabled"]): bool,
            vol.Required(
                "all_day_reminder_mode", default=values["all_day_reminder_mode"]
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "after_hour", "label": "Через час после добавления сегодня"},
                        {"value": "day_time", "label": "В заданное время выбранного дня"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                "all_day_reminder_time", default=values["all_day_reminder_time"]
            ): selector.TextSelector(),
            vol.Required(
                "repeat_interval_minutes", default=values["repeat_interval_minutes"]
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=180, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
        for key in ("categories", "priorities"):
            schema[vol.Required(key, default=values[key])] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=values[key],
                    multiple=True,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        return self.async_show_form(
            step_id="settings", data_schema=vol.Schema(schema), errors=errors
        )

    async def async_step_login(self, user_input=None):
        secretary = self.config_entry.runtime_data
        secretary.authenticating = secretary.ai.provider == "codex"
        self.login = DeviceLogin(Path(self.hass.config.path(".personal_secretary")))
        self.task = self.hass.async_create_task(self.login.start())
        return await self.async_step_login_start()

    async def async_step_login_start(self, user_input=None):
        if not self.task.done():
            return self.async_show_progress(
                step_id="login_start", progress_action="starting_login", progress_task=self.task
            )
        try:
            self.challenge = self.task.result()
        except Exception:
            self.config_entry.runtime_data.authenticating = False
            return self.async_show_progress_done(next_step_id="login_error")
        self.task = self.hass.async_create_task(self.login.complete())
        return self.async_show_progress_done(next_step_id="login_code")

    async def async_step_login_code(self, user_input=None):
        if user_input is not None:
            return await self.async_step_login_wait()
        return self.async_show_form(
            step_id="login_code",
            data_schema=vol.Schema({}),
            description_placeholders={
                "url": self.challenge["verificationUrl"],
                "code": self.challenge["userCode"],
            },
        )

    async def async_step_login_wait(self, user_input=None):
        if not self.task.done():
            return self.async_show_progress(
                step_id="login_wait", progress_action="waiting_login", progress_task=self.task
            )
        try:
            self.task.result()
        except Exception:
            return self.async_show_progress_done(next_step_id="login_error")
        return self.async_show_progress_done(next_step_id="login_finish")

    async def async_step_login_finish(self, user_input=None):
        self.config_entry.runtime_data.authenticating = False
        await self.config_entry.runtime_data.retry_failed()
        self.config_entry.runtime_data.kick()
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_login_error(self, user_input=None):
        self.config_entry.runtime_data.authenticating = False
        return self.async_abort(reason="login_failed")

    @callback
    def async_remove(self):
        if self.task and not self.task.done():
            self.task.cancel()
        if self.login:
            self.hass.async_create_task(self.login.close())
        if getattr(self.config_entry, "runtime_data", None):
            self.config_entry.runtime_data.authenticating = False
            self.config_entry.runtime_data.kick()
