"""Tools for how automations, scripts and scenes are defined."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant

from ..queries.definitions import DEFINITION_KINDS, get_definition, list_definitions
from .base import ReadOnlyTool
from .params import DATA_NOTICE, PAGING, TEXT, paginate

_KIND = probatio.In(DEFINITION_KINDS)


class ListDefinitionsTool(ReadOnlyTool):
    """List automations, scripts or scenes."""

    name = "list_definitions"
    title = "List automations, scripts or scenes"
    description = (
        "List the automations, the scripts or the scenes, without their configuration. "
        "To find those that use an entity or device, use find_references."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("kind", description="What to list."): _KIND,
            **PAGING,
        }
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of definitions."""
        return paginate(list_definitions(hass, self.exposure, args["kind"]), args)


class GetDefinitionTool(ReadOnlyTool):
    """Get the configuration of one automation, script or scene."""

    name = "get_definition"
    title = "Get an automation, script or scene"
    description = (
        "Return the configuration of one automation, script or scene. To find those "
        "that use an entity or device, use find_references instead of reading each. "
        "To find out why one did or did not run, use get_automation_trace. A device "
        "trigger, condition or action names an entity by the ID of its registry "
        "entry, and registry_ids gives the entity ID for each. "
        f"{DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Required("kind", description="What to get."): _KIND,
            probatio.Required("entity_id", description="The entity ID."): TEXT,
        }
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return the definition."""
        return get_definition(hass, self.exposure, args["kind"], args["entity_id"])
