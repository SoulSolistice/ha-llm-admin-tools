"""Which tools and which of Home Assistant's data the settings let through."""

from collections.abc import Callable

from homeassistant.components.homeassistant.exposed_entities import (
    async_should_expose,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import entity_sources

from .settings import ExposureSettings

# The assistant that the Expose page of Assist configures.
_ASSISTANT = "conversation"
# Home Assistant names a logger after each of these, with their name in its records.
_DOMAINS_WITH_OWN_LOGGER = ("automation", "script")


def _parent_device(
    hass: HomeAssistant, device: dr.ChildDeviceEntry
) -> dr.DeviceEntry | None:
    """Return the device a child device is a part of."""
    return dr.async_get(hass).async_get(
        device.parent_device_id, include_child_devices=False
    )


def _entity_device(
    hass: HomeAssistant, entry: er.RegistryEntry
) -> dr.AnyDeviceEntry | None:
    """Return the device of a registry entry, if it still exists."""
    if entry.device_id is None:
        return None
    return dr.async_get(hass).async_get(entry.device_id)


def device_area_id(hass: HomeAssistant, device: dr.AnyDeviceEntry) -> str | None:
    """Return the area of a device; a child device without one is in its parent's."""
    if device.area_id is not None or not isinstance(device, dr.ChildDeviceEntry):
        return device.area_id
    parent = _parent_device(hass, device)
    return parent.area_id if parent is not None else None


def entity_area_id(hass: HomeAssistant, entry: er.RegistryEntry) -> str | None:
    """Return the area of an entity, inherited from its device if it has none."""
    if entry.area_id is not None:
        return entry.area_id
    device = _entity_device(hass, entry)
    return device_area_id(hass, device) if device is not None else None


class ExposurePolicy:
    """Answer whether something may be shown, without saying why it may not.

    What is excluded behaves as if it did not exist, so that a client cannot
    tell a hidden entity from a mistyped one.
    """

    def __init__(self, hass: HomeAssistant, settings: ExposureSettings) -> None:
        """Initialize the policy."""
        self._hass = hass
        self.settings = settings

    def tool_enabled(self, name: str) -> bool:
        """Return if a tool is offered."""
        enabled = self.settings.enabled_tools
        return enabled is None or name in enabled

    def area_allowed(self, area_id: str | None) -> bool:
        """Return if an area may be shown."""
        return area_id not in self.settings.excluded_areas

    def integration_allowed(self, entry_id: str | None) -> bool:
        """Return if a config entry and what it provides may be shown."""
        return entry_id not in self.settings.excluded_integrations

    @callback
    def logger_filter(self) -> Callable[[str], bool]:
        """Return what says if the records of a logger may be shown.

        What is hidden can only be asked in the event loop, and the log is also read
        outside it, so this settles once what the filter then goes by.
        """
        excluded = self.settings.excluded_loggers
        hidden = {
            f"homeassistant.components.{entity_id}"
            for entity_id in {
                *self._hass.states.async_entity_ids(_DOMAINS_WITH_OWN_LOGGER),
                *(
                    entity_id
                    for entity_id in self.settings.excluded_entities
                    if entity_id.partition(".")[0] in _DOMAINS_WITH_OWN_LOGGER
                ),
            }
            if not self.entity_allowed(entity_id)
        }

        def allowed(name: str) -> bool:
            return name not in hidden and not any(text in name for text in excluded)

        return allowed

    def _labels_allowed(self, labels: set[str]) -> bool:
        return self.settings.excluded_labels.isdisjoint(labels)

    def _device_source_allowed(self, device: dr.AnyDeviceEntry) -> bool:
        """Return if the integration and labels of a device allow showing it.

        What hides a device also hides the parts it is made of.
        """
        if not (
            self.integration_allowed(device.config_entry_id)
            and self._labels_allowed(device.labels)
        ):
            return False
        if not isinstance(device, dr.ChildDeviceEntry):
            return True
        parent = _parent_device(self._hass, device)
        return parent is None or self._device_source_allowed(parent)

    def device_allowed(self, device: dr.AnyDeviceEntry) -> bool:
        """Return if a device may be shown."""
        return self.area_allowed(
            device_area_id(self._hass, device)
        ) and self._device_source_allowed(device)

    def _registry_entry_allowed(self, entry: er.RegistryEntry) -> bool:
        """Return if a registry entry and the device it belongs to allow showing it.

        An entity's own area overrides that of its device, so it is the resolved
        area that counts, not the device's.
        """
        if not (
            self.integration_allowed(entry.config_entry_id)
            and self._labels_allowed(entry.labels)
        ):
            return False
        if not self.area_allowed(entity_area_id(self._hass, entry)):
            return False
        device = _entity_device(self._hass, entry)
        return device is None or self._device_source_allowed(device)

    def _entity_source_allowed(self, entity_id: str) -> bool:
        """Return if the integration that added an entity allows showing it.

        This also covers an entity without a registry entry, which has no other
        link to its config entry. Some, such as sun.sun, have no link at all, and then
        count as part of each entry of the integration that provides them.
        """
        if not self.settings.excluded_integrations:
            return True
        source = entity_sources(self._hass).get(entity_id)
        if source is None:
            return True
        if (entry_id := source.get("config_entry")) is not None:
            return self.integration_allowed(entry_id)
        return all(
            self.integration_allowed(entry.entry_id)
            for entry in self._hass.config_entries.async_entries(source["domain"])
        )

    @callback
    def entity_allowed(self, entity_id: str) -> bool:
        """Return if an entity may be shown."""
        settings = self.settings
        if (
            entity_id in settings.excluded_entities
            or entity_id.partition(".")[0] in settings.excluded_domains
        ):
            return False
        entry = er.async_get(self._hass).async_get(entity_id)
        if entry is not None and not self._registry_entry_allowed(entry):
            return False
        if not self._entity_source_allowed(entity_id):
            return False
        return not settings.assist_exposed_only or async_should_expose(
            self._hass, _ASSISTANT, entity_id
        )
