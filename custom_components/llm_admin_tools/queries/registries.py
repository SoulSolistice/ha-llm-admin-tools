"""Read the device and area registries."""

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    category_registry as cr,
    device_registry as dr,
    floor_registry as fr,
    label_registry as lr,
)

from ..errors import invalid_input
from ..policy.exposure import ExposurePolicy, device_area_id
from .compact import without_empty_fields

CATEGORY_KIND = "category"
REGISTRY_ITEM_KINDS = ("floor", "label", CATEGORY_KIND)


def _device_summary(hass: HomeAssistant, device: dr.AnyDeviceEntry) -> dict[str, Any]:
    """Return the facts of a device, leaving out serials and connection details.

    A child device is a part of its parent and has no product details of its own.
    """
    summary: dict[str, Any] = {
        "device_id": device.id,
        "name": device.name_by_user or device.name,
        "area_id": device_area_id(hass, device),
        "disabled_by": device.disabled_by,
        "config_entry_id": device.config_entry_id,
    }
    if isinstance(device, dr.ChildDeviceEntry):
        summary["parent_device_id"] = device.parent_device_id
    else:
        summary |= {
            "manufacturer": device.manufacturer,
            "model": device.model,
            "sw_version": device.sw_version,
            "hw_version": device.hw_version,
        }
    return without_empty_fields(summary)


@callback
def list_devices(
    hass: HomeAssistant, policy: ExposurePolicy, area_id: str | None
) -> list[dict[str, Any]]:
    """Return the devices and child devices, which entities can both belong to."""
    registry = dr.async_get(hass)
    devices: list[dr.AnyDeviceEntry] = [*registry.devices, *registry.child_devices]
    summaries = [
        _device_summary(hass, device)
        for device in devices
        if (area_id is None or device_area_id(hass, device) == area_id)
        and policy.device_allowed(device)
    ]
    return sorted(summaries, key=lambda summary: summary["device_id"])


@callback
def list_areas(hass: HomeAssistant, policy: ExposurePolicy) -> list[dict[str, Any]]:
    """Return the areas."""
    return sorted(
        (
            {
                "area_id": area.id,
                "name": area.name,
                "floor_id": area.floor_id,
                "labels": sorted(area.labels - policy.settings.excluded_labels),
            }
            for area in ar.async_get(hass).areas.values()
            if policy.area_allowed(area.id)
        ),
        key=lambda summary: summary["area_id"],
    )


@callback
def list_registry_items(
    hass: HomeAssistant, policy: ExposurePolicy, kind: str, scope: str | None
) -> list[dict[str, Any]]:
    """Return the floors, the labels that may be seen, or the categories of a scope."""
    if kind == "floor":
        return [
            {
                "floor_id": floor.floor_id,
                "name": floor.name,
                "level": floor.level,
                "icon": floor.icon,
                "aliases": sorted(alias for alias in floor.aliases if alias),
            }
            for floor in fr.async_get(hass).async_list_floors()
        ]
    if kind == "label":
        return [
            {
                "label_id": label.label_id,
                "name": label.name,
                "color": label.color,
                "icon": label.icon,
                "description": label.description,
            }
            for label in lr.async_get(hass).async_list_labels()
            if label.label_id not in policy.settings.excluded_labels
        ]
    if scope is None:
        raise invalid_input("category_scope_required")
    return [
        {
            "category_id": category.category_id,
            "name": category.name,
            "icon": category.icon,
        }
        for category in cr.async_get(hass).async_list_categories(scope=scope)
    ]
