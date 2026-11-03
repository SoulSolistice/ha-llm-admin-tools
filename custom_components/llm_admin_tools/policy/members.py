"""What changes along with a device or a config entry."""

from dataclasses import dataclass, replace

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import entity_sources


@dataclass(slots=True, frozen=True)
class Members:
    """The entities and devices that a change reaches, each of them once."""

    entities: list[er.RegistryEntry]
    devices: list[dr.AnyDeviceEntry]
    # Those without a link in the registry, known only from what provides them.
    unregistered_entities: tuple[str, ...] = ()

    @property
    def entity_ids(self) -> list[str]:
        """Return all the entities, sorted."""
        return sorted(
            {
                *(entry.entity_id for entry in self.entities),
                *self.unregistered_entities,
            }
        )


def _with_descendants(
    hass: HomeAssistant, devices: list[dr.AnyDeviceEntry]
) -> list[dr.AnyDeviceEntry]:
    """Return devices and the child devices below them.

    A child device takes its area from its parent, and is disabled or removed with it.
    """
    registry = dr.async_get(hass)
    found: dict[str, dr.AnyDeviceEntry] = {device.id: device for device in devices}
    waiting = list(found)
    while waiting:
        for child in dr.async_entries_for_parent_device(registry, waiting.pop()):
            if child.id not in found:
                found[child.id] = child
                waiting.append(child.id)
    return list(found.values())


@callback
def members_of(
    hass: HomeAssistant,
    entities: list[er.RegistryEntry],
    devices: list[dr.AnyDeviceEntry],
) -> Members:
    """Return these, the descendants of the devices, and the entities of them all."""
    registry = er.async_get(hass)
    reached = _with_descendants(hass, devices)
    found = {entry.entity_id: entry for entry in entities}
    for device in reached:
        for entry in er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        ):
            found[entry.entity_id] = entry
    return Members(list(found.values()), reached)


@callback
def config_entry_members(hass: HomeAssistant, entry_id: str) -> Members:
    """Return what belongs to a config entry, going by the registries first.

    What is loaded right now would leave out all of an integration that failed to start.
    It does add what the registries have no link for: an entity of the entry without a
    registry entry, or one such as sun.sun that only names the integration providing it.
    """
    members = members_of(
        hass,
        er.async_entries_for_config_entry(er.async_get(hass), entry_id),
        list(dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)),
    )
    entry = hass.config_entries.async_get_entry(entry_id)
    domain = entry.domain if entry is not None else None
    known = set(members.entity_ids)
    return replace(
        members,
        unregistered_entities=tuple(
            sorted(
                entity_id
                for entity_id, source in entity_sources(hass).items()
                if entity_id not in known
                and (
                    source.get("config_entry") == entry_id
                    or (
                        source.get("config_entry") is None
                        and source["domain"] == domain
                    )
                )
            )
        ),
    )
