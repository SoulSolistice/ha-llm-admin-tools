"""Tool that looks up the services a client may call."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant

from ..queries.services import async_describe_services
from .base import ReadOnlyTool
from .params import PAGING, TEXT, paginate


class FindServicesTool(ReadOnlyTool):
    """Find the services the owner allows, with the fields they take."""

    name = "find_services"
    title = "Find services"
    description = (
        "List the services the owner allows call_service to call, with what each does "
        "and the fields it takes. Services that are not allowed are not listed. A "
        "service that takes entities gets them in entity_ids of call_service."
    )
    # Only reads, but says nothing unless the owner allows calling services.
    subjects = ("services",)
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "domain", description="Only services of this domain, such as light."
            ): TEXT,
            probatio.Optional(
                "search", description="Words to find in the name or description."
            ): TEXT,
            **PAGING,
        }
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of services."""
        domain = args.get("domain")
        services = await async_describe_services(
            hass,
            self._runtime.writes,
            domain.strip().lower() if domain is not None else None,
            args.get("search"),
            scrub=self._runtime.redactor.scrub_text,
        )
        return paginate(services, args)
