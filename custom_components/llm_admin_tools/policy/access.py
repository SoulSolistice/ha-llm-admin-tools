"""Who may use the tools: active administrators, identified by the request."""

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import llm


async def async_is_admin(hass: HomeAssistant, llm_context: llm.LLMContext) -> bool:
    """Return if the request comes from an active administrator.

    A request without a user, such as a voice pipeline or an internal caller, is
    refused. The API also states that it requires an administrator, but Home
    Assistant only checks that from 2026.11, and only when an API is requested, so
    it does not guard a tool kept from before.
    """
    context = llm_context.context
    if context is None or context.user_id is None:
        return False
    user = await hass.auth.async_get_user(context.user_id)
    return user is not None and user.is_active and user.is_admin


async def async_require_admin(hass: HomeAssistant, llm_context: llm.LLMContext) -> None:
    """Raise unless the request comes from an active administrator."""
    if not await async_is_admin(hass, llm_context):
        raise Unauthorized(context=llm_context.context)
