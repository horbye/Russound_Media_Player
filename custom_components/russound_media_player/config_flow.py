import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SOURCE, CONF_NAME

DOMAIN = "russound_media_player"

class RussoundConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Russound Media Player."""
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial setup step."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_NAME], 
                data=user_input
            )

        return self.async_show_form(
            step_id="user", 
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default="192.168.40.102"): str,
                vol.Required(CONF_PORT, default=9621): int,
                vol.Required(CONF_SOURCE, default=5): int,
                vol.Required(CONF_NAME, default="Russound Media Player"): str,
            })
        )
