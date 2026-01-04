from __future__ import annotations

import asyncio
import time
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .const import DOMAIN, DEFAULT_PORT_PRIMARY, DEFAULT_PORT_FALLBACK


async def _probe_port(host: str, port: int, timeout: float = 1.5) -> bool:
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.write(b"VERSION\r\n")
        await writer.drain()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break
            line = raw.decode(errors="ignore").strip()
            if not line or line.startswith("E "):
                continue
            payload = line[2:] if (len(line) > 2 and line[1] == " " and line[0] in ("N", "S")) else line
            if payload.upper().startswith("VERSION"):
                return True
        return False
    except Exception:
        return False
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _select_controller_port(host: str, preferred: int | None) -> int | None:
    """
    Controller: prøv først brugerens port.
    Hvis den fejler, prøv 9622 derefter 9621.
    """
    tried: list[int] = []
    if preferred:
        tried.append(preferred)
        if await _probe_port(host, preferred):
            return preferred

    for p in (DEFAULT_PORT_FALLBACK, DEFAULT_PORT_PRIMARY):  # 9622 -> 9621
        if p in tried:
            continue
        if await _probe_port(host, p):
            return p
    return None


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            preferred_port = user_input.get(CONF_PORT)

            port = await _select_controller_port(host, preferred_port)
            if port is None:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{DOMAIN}_{host}")
                self._abort_if_unique_id_configured()

                data = {CONF_HOST: host, CONF_PORT: port}
                # Brug controller-IP som titel; du kan ændre i UI senere
                return self.async_create_entry(title=host, data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT_PRIMARY): int,  # default 9621
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
