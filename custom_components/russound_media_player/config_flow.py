import asyncio
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SOURCE, CONF_NAME

_LOGGER = logging.getLogger(__name__)
DOMAIN = "russound_media_player"

class RussoundConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Russound Media Player."""
    VERSION = 1

    async def _test_connection(self, host):
        """Test connection to Russound by scanning ports 9622 and 9621."""
        # We test the highest port first as requested
        for port in [9622, 9621]:
            try:
                _LOGGER.debug("Testing Russound connection at %s:%s", host, port)
                # Short timeout to keep the UI responsive
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), 
                    timeout=3.0
                )
                writer.close()
                await writer.wait_closed()
                _LOGGER.info("Found active Russound port: %s", port)
                return port
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                _LOGGER.debug("Port %s not responding, skipping...", port)
                continue
        return None

    async def async_step_user(self, user_input=None):
        """Handle the initial setup step with automated port discovery."""
        errors = {}
        
        if user_input is not None:
            # Automatic port discovery
            discovered_port = await self._test_connection(user_input[CONF_HOST])
            
            if discovered_port:
                # Store the discovered port in the configuration data
                user_input[CONF_PORT] = discovered_port
                return self.async_create_entry(
                    title=user_input[CONF_NAME], 
                    data=user_input
                )
            
            # If no ports answer, trigger the error message in the UI
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user", 
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default="192.168.40."): str,
                vol.Required(CONF_SOURCE, default=0): int,
                vol.Required(CONF_NAME, default="Russound "): str,
            }),
            errors=errors
        )
