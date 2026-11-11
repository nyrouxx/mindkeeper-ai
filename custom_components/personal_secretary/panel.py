"""Private calendar panel and authenticated WebSocket API."""

from pathlib import Path
from uuid import uuid4

import probatio as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME, VERSION
from .google_sync import validate_selection
from .model import InvalidPlan, validate_day_settings, validate_labels, validate_repeat_interval
from .telegram import public_settings

PATH = "personal-secretary"
FIELDS = (
    "id",
    "title",
    "notes",
    "due_at",
    "end_at",
    "due_date",
    "end_date",
    "status",
    "category",
    "priority",
    "classification_pending",
    "revision",
    "repeat_until_ack",
    "reminder_ack_at",
    "completed_at",
    "completion_reason",
)


def secretary(hass):
    entries = hass.config_entries.async_entries(DOMAIN)
    if (
        len(entries) != 1
        or not getattr(entries[0], "runtime_data", None)
        or entries[0].runtime_data.stopping
    ):
        raise InvalidPlan("Секретарь пока недоступен")
    return entries[0].runtime_data


def snapshot(sec):
    return {
        "tasks": [{key: task.get(key) for key in FIELDS} for task in sec.state["tasks"].values()],
        "categories": sec.options["categories"],
        "priorities": sec.options["priorities"],
        "enabled": sec.options["enabled"],
        "agenda_enabled": sec.options["agenda_enabled"],
        "agenda_status": sec.agenda.status(),
        "repeat_interval_minutes": sec.options["repeat_interval_minutes"],
        "all_day_reminder_mode": sec.options["all_day_reminder_mode"],
        "all_day_reminder_time": sec.options["all_day_reminder_time"],
        "google": sec.google_sync.public(),
        "telegram": public_settings(sec.hass, sec.telegram_route),
        "timezone": sec.hass.config.time_zone,
        "pending": sum(m["status"] == "pending" for m in sec.state["messages"]),
        "error": bool(sec.last_error),
        "conversation": [
            {"text": m["text"], "reply": m.get("reply", ""), "status": m["status"]}
            for m in sec.state["messages"]
            if m.get("origin") == "panel"
        ][-8:],
    }


async def setup_panel(hass):
    if not hass.data.get(DOMAIN + "_panel"):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    "/personal_secretary/icon.png",
                    str(Path(__file__).parent / "brand/icon.png"),
                    cache_headers=False,
                ),
                StaticPathConfig(
                    "/personal_secretary/panel.js",
                    str(Path(__file__).parent / "frontend/panel.js"),
                    cache_headers=False,
                ),
            ]
        )
        for command in (
            ws_subscribe,
            ws_message,
            ws_change,
            ws_settings,
            ws_telegram,
            ws_agenda,
            ws_google,
        ):
            websocket_api.async_register_command(hass, command)
        hass.data[DOMAIN + "_panel"] = True
    if not hass.data.get(DOMAIN + "_panel_active"):
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PATH,
            webcomponent_name="personal-secretary-panel",
            sidebar_title=NAME,
            sidebar_icon="mdi:brain",
            require_admin=True,
            module_url=f"/personal_secretary/panel.js?v={VERSION}",
        )
        hass.data[DOMAIN + "_panel_active"] = True


def remove_panel(hass):
    frontend.async_remove_panel(hass, PATH)
    hass.data[DOMAIN + "_panel_active"] = False


@websocket_api.websocket_command({"type": DOMAIN + "/subscribe"})
@websocket_api.require_admin
@callback
def ws_subscribe(hass, connection, msg):
    try:
        sec = secretary(hass)
    except InvalidPlan as error:
        connection.send_error(msg["id"], "not_ready", str(error))
        return

    @callback
    def changed():
        connection.send_message(websocket_api.event_message(msg["id"], snapshot(sec)))

    connection.subscriptions[msg["id"]] = sec.listen(changed)
    connection.send_result(msg["id"])
    changed()


@websocket_api.websocket_command({"type": DOMAIN + "/message", vol.Required("text"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_message(hass, connection, msg):
    try:
        content = msg["text"].strip()
        if not content or len(content) > 8000:
            raise InvalidPlan("Напиши, что хочешь запланировать")
        await secretary(hass).submit(content)
    except InvalidPlan as error:
        connection.send_error(msg["id"], "invalid_input", str(error))
    else:
        connection.send_result(msg["id"])


@websocket_api.websocket_command({"type": DOMAIN + "/change", vol.Required("operation"): dict})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_change(hass, connection, msg):
    try:
        sec = secretary(hass)
        op = msg["operation"]
        if set(op) - {
            "action",
            "task_id",
            "title",
            "notes",
            "due_at",
            "end_at",
            "due_date",
            "end_date",
            "category",
            "priority",
            "clear_dates",
            "revision",
            "repeat_until_ack",
            "classify_fields",
        }:
            raise InvalidPlan("Не удалось сохранить изменение")
        if op.get("task_id"):
            current = sec.state["tasks"].get(op["task_id"])
            if not current or op.pop("revision", None) != current["revision"]:
                raise InvalidPlan("Это дело уже изменилось. Открой его ещё раз.")
        for key in ("due_at", "end_at"):
            if op.get(key):
                date = dt_util.parse_datetime(op[key])
                if date is None:
                    raise InvalidPlan("Не удалось понять дату")
                op[key] = (
                    date if date.tzinfo else date.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
                ).isoformat()
        await sec.change(
            [op],
            "panel:" + uuid4().hex,
            expected={k: t["revision"] for k, t in sec.state["tasks"].items()},
        )
    except (InvalidPlan, ValueError, TypeError) as error:
        connection.send_error(msg["id"], "invalid_input", str(error))
    else:
        connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        "type": DOMAIN + "/settings",
        vol.Required("categories"): list,
        vol.Required("priorities"): list,
        vol.Optional("repeat_interval_minutes"): vol.Any(int, float),
        vol.Optional("agenda_enabled"): bool,
        vol.Optional("all_day_reminder_mode"): str,
        vol.Optional("all_day_reminder_time"): str,
        vol.Optional("google_calendar"): str,
        vol.Optional("google_sync_mode"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings(hass, connection, msg):
    try:
        sec = secretary(hass)
        options = {
            **sec.options,
            "categories": validate_labels(msg["categories"]),
            "priorities": validate_labels(msg["priorities"]),
        }
        if "repeat_interval_minutes" in msg:
            options["repeat_interval_minutes"] = validate_repeat_interval(
                msg["repeat_interval_minutes"]
            )
        if "agenda_enabled" in msg:
            options["agenda_enabled"] = msg["agenda_enabled"]
        options["all_day_reminder_mode"], options["all_day_reminder_time"] = validate_day_settings(
            msg.get("all_day_reminder_mode", options["all_day_reminder_mode"]),
            msg.get("all_day_reminder_time", options["all_day_reminder_time"]),
        )
        if "google_calendar" in msg or "google_sync_mode" in msg:
            options["google_calendar"], options["google_sync_mode"] = validate_selection(
                hass,
                msg.get("google_calendar", options["google_calendar"]),
                msg.get("google_sync_mode", options["google_sync_mode"]),
            )
        hass.config_entries.async_update_entry(sec.entry, options=options)
    except InvalidPlan as error:
        connection.send_error(msg["id"], "invalid_input", str(error))
    else:
        connection.send_result(msg["id"])


@websocket_api.websocket_command({"type": DOMAIN + "/telegram", vol.Required("route"): dict})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_telegram(hass, connection, msg):
    try:
        await secretary(hass).configure_telegram(msg["route"])
    except InvalidPlan as error:
        connection.send_error(msg["id"], "invalid_input", str(error))
    else:
        connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {"type": DOMAIN + "/agenda", vol.Optional("recreate", default=False): bool}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_agenda(hass, connection, msg):
    try:
        sec = secretary(hass)
        await sec.agenda.sync(force=True, recreate=msg["recreate"])
        if not sec.options["agenda_enabled"]:
            raise InvalidPlan("Сначала включи сообщение с делами и сохрани настройки")
        if sec.agenda.status() != "ready":
            raise InvalidPlan(
                "Не получилось обновить сообщение. Проверь подключение Telegram и попробуй снова."
            )
    except InvalidPlan as error:
        connection.send_error(msg["id"], "invalid_input", str(error))
    else:
        connection.send_result(msg["id"])


@websocket_api.websocket_command({"type": DOMAIN + "/google_sync"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_google(hass, connection, msg):
    try:
        sec = secretary(hass)
        if not sec.options["google_calendar"]:
            raise InvalidPlan("Сначала выберите календарь Google и сохраните настройки")
        await sec.google_sync.sync(force=True)
        if sec.google_sync.error:
            raise InvalidPlan(sec.google_sync.error)
    except InvalidPlan as error:
        connection.send_error(msg["id"], "sync_failed", str(error))
    else:
        connection.send_result(msg["id"])
