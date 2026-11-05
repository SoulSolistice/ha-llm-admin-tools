"""Read the entities the policy allows, whether or not exposed to Assist."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er

from ..errors import invalid_input
from ..policy.exposure import ExposurePolicy, entity_area_id
from .compact import without_empty_fields


@dataclass(slots=True, frozen=True, kw_only=True)
class EntityFilter:
    """Conditions an entity has to meet to be listed."""

    domain: str | None = None
    area_id: str | None = None
    device_id: str | None = None
    state: str | None = None
    search: str | None = None
    include_diagnostic: bool = False


@callback
def _other_names(hass: HomeAssistant, entry: er.RegistryEntry | None) -> list[str]:
    """Return the names an entity is also known by.

    Those are its aliases, and from 2026.11 the name it had before names were made of
    parts, which users and older definitions may still use.
    """
    if entry is None:
        return []
    compat_name: str | None = getattr(entry, "compat_name", None)
    return [
        *er.async_get_entity_aliases(hass, entry, allow_empty=True),
        *([compat_name] if compat_name else []),
    ]


def _matches(
    state: State,
    other_names: list[str],
    area_id: str | None,
    device_id: str | None,
    conditions: EntityFilter,
    *,
    scrub: Callable[[str], str],
) -> bool:
    """Return if what a client would see of an entity meets the conditions.

    Comparing with the values as they are would confirm a guess at what is withheld.
    """
    if conditions.area_id is not None and area_id != conditions.area_id:
        return False
    if conditions.device_id is not None and device_id != conditions.device_id:
        return False
    if conditions.state is not None and scrub(state.state) != conditions.state:
        return False
    if conditions.search is None:
        return True
    needle = conditions.search.casefold()
    return any(
        needle in scrub(name).casefold()
        for name in (state.entity_id, state.name, *other_names)
    )


def _is_background(entry: er.RegistryEntry | None) -> bool:
    """Return if an entity is one that users do not normally look at.

    These are often half of an instance, and rarely what a question is about.
    """
    return entry is not None and (
        entry.entity_category is not None or entry.hidden_by is not None
    )


@callback
def list_entities(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    conditions: EntityFilter,
    include_last_changed: bool,
    scrub: Callable[[str], str],
) -> tuple[list[dict[str, Any]], int]:
    """Return summaries of entities that have a state, sorted by entity ID.

    Also return how many matched and were left out for being background entities.
    Unlike what the policy hides, a client can ask for those, so it gets told.
    """
    entity_registry = er.async_get(hass)
    summaries = []
    left_out = 0
    for state in hass.states.async_all(conditions.domain):
        if not policy.entity_allowed(state.entity_id):
            continue
        entry = entity_registry.async_get(state.entity_id)
        area_id = entity_area_id(hass, entry) if entry is not None else None
        device_id = entry.device_id if entry is not None else None
        other_names = _other_names(hass, entry)
        if not _matches(
            state, other_names, area_id, device_id, conditions, scrub=scrub
        ):
            continue
        if not conditions.include_diagnostic and _is_background(entry):
            left_out += 1
            continue
        summaries.append(
            without_empty_fields(
                {
                    "entity_id": state.entity_id,
                    "name": state.name,
                    "state": state.state,
                    "device_class": state.attributes.get(ATTR_DEVICE_CLASS),
                    "unit": state.attributes.get(ATTR_UNIT_OF_MEASUREMENT),
                    "area_id": area_id,
                    "device_id": device_id,
                    "last_changed": state.last_changed
                    if include_last_changed
                    else None,
                }
            )
        )
    return sorted(summaries, key=lambda summary: summary["entity_id"]), left_out


@callback
def entity_exists(hass: HomeAssistant, entity_id: str) -> bool:
    """Return if Home Assistant knows an entity, even a disabled one."""
    return (
        hass.states.get(entity_id) is not None
        or er.async_get(hass).async_get(entity_id) is not None
    )


@callback
def get_entity(
    hass: HomeAssistant, policy: ExposurePolicy, entity_id: str
) -> dict[str, Any]:
    """Return the state, attributes and registry entry of one entity.

    An entity that is disabled has a registry entry but no state.
    """
    if not entity_exists(hass, entity_id) or not policy.entity_allowed(entity_id):
        raise invalid_input("entity_not_found", entity_id=entity_id)
    state = hass.states.get(entity_id)
    entry = er.async_get(hass).async_get(entity_id)
    return {
        "entity_id": entity_id,
        "state": state.state if state is not None else None,
        "attributes": dict(state.attributes) if state is not None else {},
        "last_changed": state.last_changed if state is not None else None,
        "last_updated": state.last_updated if state is not None else None,
        "registry": _registry_summary(hass, entry) if entry is not None else None,
    }


def _registry_summary(hass: HomeAssistant, entry: er.RegistryEntry) -> dict[str, Any]:
    """Return the registry facts that explain an entity."""
    return {
        "platform": entry.platform,
        "config_entry_id": entry.config_entry_id,
        "device_id": entry.device_id,
        "area_id": entity_area_id(hass, entry),
        "name": entry.name,
        "original_name": entry.original_name,
        "other_names": _other_names(hass, entry),
        "disabled_by": entry.disabled_by,
        "hidden_by": entry.hidden_by,
        "entity_category": entry.entity_category,
        "labels": sorted(entry.labels),
    }
