"""Small HA boundary fakes. Business and scheduler code run unchanged."""

import asyncio
import importlib
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
package = ModuleType("secretary_under_test")
package.__path__ = [str(ROOT / "custom_components/personal_secretary")]
sys.modules[package.__name__] = package


def component(name):
    return importlib.import_module("secretary_under_test." + name)


class Store:
    database = {}

    def __init__(self, hass, version, key, **kwargs):
        self.key = key

    async def async_load(self):
        return deepcopy(self.database.get(self.key))

    async def async_save(self, value):
        self.database[self.key] = deepcopy(value)

    async def async_remove(self):
        self.database.pop(self.key, None)


def install_ha_fakes():
    for name in (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.event",
        "homeassistant.helpers.storage",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        sys.modules.setdefault(name, ModuleType(name))
    sys.modules["homeassistant.core"].callback = lambda f: f
    sys.modules["homeassistant.helpers.event"].async_track_time_interval = lambda *a: lambda: None
    sys.modules["homeassistant.helpers.storage"].Store = Store
    dt = sys.modules["homeassistant.util.dt"]
    dt.DEFAULT_TIME_ZONE = ZoneInfo("Europe/Moscow")
    dt.now = lambda: datetime.now(dt.DEFAULT_TIME_ZONE)


class Services:
    def __init__(self):
        self.calls = []
        self.counter = 1000
        self.fail = False

    async def async_call(self, domain, service, data, **kwargs):
        if self.fail:
            raise ConnectionError("synthetic service failure")
        self.calls.append((domain, service, deepcopy(data)))
        self.counter += 1
        return {"chats": [{"chat_id": data.get("chat_id"), "message_id": self.counter}]}


class Hass:
    def __init__(self):
        self.data = {}
        self.config = SimpleNamespace(
            path=lambda *parts: str(ROOT.joinpath(*parts)), time_zone="Europe/Moscow"
        )
        self.services = Services()
        self.bus = SimpleNamespace(async_listen=lambda *args: lambda: None)

    def async_create_background_task(self, coro, name):
        return asyncio.create_task(coro, name=name)


def entry():
    return SimpleNamespace(
        entry_id="test",
        data={"bot_entry_id": "bot", "chat_id": 42, "user_id": 42},
        options={"agenda_enabled": False},
        async_on_unload=lambda _: None,
    )


def event(kind="telegram_text", **values):
    return SimpleNamespace(
        event_type=kind,
        data={
            "bot": {"config_entry_id": "bot"},
            "chat_id": 42,
            "user_id": 42,
            "id": 1,
            "text": "test",
            **values,
        },
    )
