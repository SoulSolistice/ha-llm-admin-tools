"""Tools for the device and area registries."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant

from ..queries.registries import (
    CATEGORY_KIND,
    REGISTRY_ITEM_KINDS,
    list_areas,
    list_devices,
    list_registry_items,
)
from .base import ReadOnlyTool
from .params import PAGING, TEXT, paginate


class ListDevicesTool(ReadOnlyTool):
    """List the devices of the device registry."""

    name = "list_devices"
    title = "List devices"
    description = (
        "List devices with their manufacturer, model, version and area. A field that "
        "is not set is absent."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional("area_id", description="Only this area."): TEXT,
            **PAGING,
        }
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of devices."""
        return paginate(list_devices(hass, self.exposure, args.get("area_id")), args)


class ListAreasTool(ReadOnlyTool):
    """List the areas of the area registry."""

    name = "list_areas"
    title = "List areas"
    description = "List areas with their floor and labels."
    parameters = probatio.Schema(PAGING)

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of areas."""
        return paginate(list_areas(hass, self.exposure), args)


class ListRegistryItemsTool(ReadOnlyTool):
    """List the floors, labels or categories."""

    name = "list_registry_items"
    title = "List floors, labels or categories"
    description = (
        "List the floors, the labels, or the categories of one scope. A category "
        "belongs to a scope such as automation or script."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("kind", description="What to list."): probatio.In(
                REGISTRY_ITEM_KINDS
            ),
            probatio.Optional(
                "scope", description=f"Required for {CATEGORY_KIND}."
            ): TEXT,
            **PAGING,
        }
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of items."""
        items = list_registry_items(
            hass, self.exposure, args["kind"], args.get("scope")
        )
        return paginate(items, args)
