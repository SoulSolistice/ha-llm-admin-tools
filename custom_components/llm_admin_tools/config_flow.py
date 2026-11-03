"""Config flow for the LLM Admin Tools integration."""

from typing import Any, override

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.core import callback

from .const import DOMAIN, LLM_API_NAME
from .options_flow import LlmAdminToolsOptionsFlow


class LlmAdminToolsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Confirm the setup; the integration has nothing to configure or connect to.

    The manifest sets ``single_config_entry``, which aborts a second setup.
    """

    VERSION = 1

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> LlmAdminToolsOptionsFlow:
        """Create the settings menu."""
        return LlmAdminToolsOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm what they are enabling."""
        if user_input is None:
            return self.async_show_form(step_id="user")
        return self.async_create_entry(title=LLM_API_NAME, data={})
