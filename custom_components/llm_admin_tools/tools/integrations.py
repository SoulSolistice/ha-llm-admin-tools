"""Tool for the status of set up integrations."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant

from ..queries.integrations import list_integrations
from .base import ReadOnlyTool
from .params import PAGING, TEXT, paginate


class ListIntegrationsTool(ReadOnlyTool):
    """List config entries and whether they loaded."""

    name = "list_integrations"
    title = "List integrations"
    description = (
        "List the set up integrations with their state and failure reason. Their "
        "configuration is not available."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional("domain", description="Only this integration."): TEXT,
            **PAGING,
        }
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of config entries."""
        return paginate(
            list_integrations(hass, self.exposure, args.get("domain")), args
        )
