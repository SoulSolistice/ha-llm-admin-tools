"""Tool for general facts about the instance."""

from collections.abc import Mapping
from typing import Any, override

from homeassistant.core import HomeAssistant

from ..queries.system import async_get_system_summary
from .base import ReadOnlyTool


class GetSystemInfoTool(ReadOnlyTool):
    """Get the versions, platform and state of Home Assistant."""

    name = "get_system_info"
    title = "Get system information"
    description = (
        "Return the versions of Home Assistant, the frontend, Python, the operating "
        "system and any Supervisor, with the installation type, platform, time zone "
        "and running state."
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return the summary of the instance."""
        return await async_get_system_summary(hass)
