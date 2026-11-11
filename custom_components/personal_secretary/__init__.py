"""Personal secretary using an existing Telegram bot and ChatGPT subscription."""

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, NAME
from .coordinator import Secretary
from .panel import remove_panel, setup_panel

PLATFORMS = [Platform.TODO, Platform.SENSOR, Platform.CALENDAR]
CONFIG_SCHEMA = vol.Schema({vol.Optional(DOMAIN): vol.Schema({})}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass, config):
    if DOMAIN in config and not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data={})
        )
    return True


async def async_setup_entry(hass, entry):
    if entry.title == "Личный секретарь":
        hass.config_entries.async_update_entry(entry, title=NAME)
    devices = dr.async_get(hass)
    device = devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    if device and device.name == "Личный секретарь":
        devices.async_update_device(device.id, name=NAME, manufacturer=NAME)
    secretary = entry.runtime_data = Secretary(hass, entry)
    await secretary.start()
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.entity_id.startswith("button.") and entity.unique_id in (
            f"{entry.entry_id}_retry",
            f"{entry.entry_id}_plans",
        ):
            registry.async_remove(entity.entity_id)
    await setup_panel(hass)

    async def shutdown(_event):
        await secretary.stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown))
    entry.async_on_unload(entry.add_update_listener(options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def options_updated(hass, entry):
    secretary = entry.runtime_data
    async with secretary.lock:
        for task in secretary.state["tasks"].values():
            if task["status"] == "active" and (
                task.get("category") not in secretary.options["categories"]
                or task.get("priority") not in secretary.options["priorities"]
            ):
                task["classification_pending"] = True
                task["classification_fields"] = [
                    key
                    for key, allowed in (
                        ("category", secretary.options["categories"]),
                        ("priority", secretary.options["priorities"]),
                    )
                    if task.get(key) not in allowed
                ]
        for message in secretary.state["messages"]:
            if message.get("origin") == "classification" and message["status"] == "error":
                message["status"] = "pending"
                message["attempts"] = 0
        secretary.queue_classification()
        await secretary.save()
    secretary.kick()


async def async_unload_entry(hass, entry):
    remove_panel(hass)
    await entry.runtime_data.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
