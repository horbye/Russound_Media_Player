from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, ["media_player"])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, ["media_player"])


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device_entry) -> bool:
    """
    Tillad manuel sletning af devices i UI, når de ikke længere har entities.
    (Hjælper, hvis der stadig ligger gamle devices tilbage i registry.)
    """
    ent_reg = er.async_get(hass)
    return len(er.async_entries_for_device(ent_reg, device_entry.id)) == 0
