"""Tools for entities and their current state."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant

from ..policy.limits import MAX_ATTRIBUTE_CHARACTERS
from ..queries.entities import EntityFilter, get_entity, list_entities
from ..shortening import shorten_values
from .base import ReadOnlyTool
from .params import DATA_NOTICE, PAGING, TEXT, paginate


class ListEntitiesTool(ReadOnlyTool):
    """List entities, including those not exposed to Assist."""

    name = "list_entities"
    title = "List entities"
    description = (
        "List entities with their current state, sorted by entity ID. Covers all "
        "entities, not only those exposed to Assist, apart from config, diagnostic "
        "and hidden ones unless asked for: left_out_by_default says how many matched "
        "but were left out. A field that is not set is absent. Use the filters to "
        f"narrow the result, and get_entity for attributes. {DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "domain", description="Only this domain, such as sensor."
            ): TEXT,
            probatio.Optional("area_id", description="Only this area."): TEXT,
            probatio.Optional("device_id", description="Only this device."): TEXT,
            probatio.Optional("state", description="Only this exact state."): TEXT,
            probatio.Optional(
                "search", description="Text to find in the entity ID or name."
            ): TEXT,
            probatio.Optional(
                "include_diagnostic",
                description="Also list config, diagnostic and hidden entities.",
                default=False,
            ): bool,
            probatio.Optional(
                "include_last_changed",
                description="Also return when each state last changed.",
                default=False,
            ): bool,
            **PAGING,
        }
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of matching entities."""
        conditions = EntityFilter(
            domain=args.get("domain"),
            area_id=args.get("area_id"),
            device_id=args.get("device_id"),
            state=args.get("state"),
            search=args.get("search"),
            include_diagnostic=args["include_diagnostic"],
        )
        summaries, left_out = list_entities(
            hass,
            self.exposure,
            conditions,
            args["include_last_changed"],
            self._runtime.redactor.scrub_text,
        )
        return {**paginate(summaries, args), "left_out_by_default": left_out}


class GetEntityTool(ReadOnlyTool):
    """Get everything known about one entity."""

    name = "get_entity"
    title = "Get an entity"
    description = (
        "Return the state, attributes and registry entry of one entity. Long "
        f"attributes are cut to about {MAX_ATTRIBUTE_CHARACTERS} characters and "
        "listed in shortened_attributes with their full size. Name them in attributes "
        f"to get them in full. {DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Required("entity_id", description="The entity ID."): TEXT,
            probatio.Optional(
                "attributes",
                description="Return only these attributes, in full.",
            ): [TEXT],
        }
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return the entity."""
        return get_entity(hass, self.exposure, args["entity_id"])

    @override
    def reduce_result(
        self, result: dict[str, Any], args: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return the attributes that were asked for, or else all, cut to a length."""
        attributes = result["attributes"]
        if "attributes" in args:
            selected = {
                name: attributes[name]
                for name in args["attributes"]
                if name in attributes
            }
            return {**result, "attributes": selected}
        shortened, original_sizes = shorten_values(attributes, MAX_ATTRIBUTE_CHARACTERS)
        result = {**result, "attributes": shortened}
        return (
            {**result, "shortened_attributes": original_sizes}
            if original_sizes
            else result
        )
