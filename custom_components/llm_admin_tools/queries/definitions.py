"""Read how automations, scripts and scenes are defined."""

from collections.abc import Iterator
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback, valid_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES, EntityComponent

from ..errors import invalid_input, read_failed
from ..policy.exposure import ExposurePolicy

DEFINITION_KINDS = ("automation", "script", "scene")


def component_of(hass: HomeAssistant, kind: str) -> EntityComponent[Any] | None:
    """Return the component of a kind, if it is loaded."""
    return hass.data.get(DATA_INSTANCES, {}).get(kind)


@callback
def list_definitions(
    hass: HomeAssistant, policy: ExposurePolicy, kind: str
) -> list[dict[str, Any]]:
    """Return the automations, scripts or scenes, without their configuration."""
    if (component := component_of(hass, kind)) is None:
        return []
    summaries = [
        {
            "entity_id": entity.entity_id,
            "name": entity.name,
            "state": entity.state,
            "unique_id": entity.unique_id,
        }
        for entity in component.entities
        if policy.entity_allowed(entity.entity_id)
    ]
    return sorted(summaries, key=lambda summary: summary["entity_id"])


def _scene_config(entity: Any) -> dict[str, Any] | None:
    """Return the configuration of a scene, which it only holds in parsed form."""
    if (config := getattr(entity, "scene_config", None)) is None:
        return None
    return {
        "id": config.id,
        "name": config.name,
        "icon": config.icon,
        "entities": {
            entity_id: {**state.attributes, "state": state.state}
            for entity_id, state in config.states.items()
        },
    }


def _config_of(kind: str, entity: Any) -> dict[str, Any] | None:
    """Return how an automation, script or scene is defined, if it holds that."""
    if kind == "scene":
        return _scene_config(entity)
    return getattr(entity, "raw_config", None)


@callback
def visible_definitions(
    hass: HomeAssistant, policy: ExposurePolicy
) -> list[tuple[str, str, Any]]:
    """Return the kind, ID and configuration of what a client may read."""
    return [
        (kind, entity.entity_id, config)
        for kind in DEFINITION_KINDS
        if (component := component_of(hass, kind)) is not None
        for entity in component.entities
        if policy.entity_allowed(entity.entity_id)
        and (config := _config_of(kind, entity)) is not None
    ]


@callback
def get_definition(
    hass: HomeAssistant, policy: ExposurePolicy, kind: str, entity_id: str
) -> dict[str, Any]:
    """Return the configuration of one automation, script or scene."""
    if entity_id.partition(".")[0] != kind:
        raise invalid_input("definition_kind_mismatch", entity_id=entity_id, kind=kind)
    component = component_of(hass, kind)
    entity = component.get_entity(entity_id) if component is not None else None
    if entity is None or not policy.entity_allowed(entity_id):
        raise invalid_input("entity_not_found", entity_id=entity_id)
    if (config := _config_of(kind, entity)) is None:
        raise read_failed("definition_unavailable", entity_id=entity_id)
    registry_ids = _registry_ids(hass, policy, config)
    return {
        "entity_id": entity_id,
        "kind": kind,
        "config": config,
        **({"registry_ids": registry_ids} if registry_ids else {}),
    }


def _named_entities(value: Any) -> Iterator[str]:
    """Return each text under entity_id, at any depth of a configuration."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == ATTR_ENTITY_ID:
                yield from (
                    name
                    for name in (item if isinstance(item, list) else [item])
                    if isinstance(name, str)
                )
            else:
                yield from _named_entities(item)
    elif isinstance(value, list):
        for item in value:
            yield from _named_entities(item)


@callback
def _registry_ids(
    hass: HomeAssistant, policy: ExposurePolicy, config: Any
) -> dict[str, str]:
    """Return the entity ID of each entity that is named by its registry entry.

    Device triggers, conditions and actions do that, which says nothing to a reader.
    The configuration stays as it is, so that it can be written back.
    """
    registry = er.async_get(hass)
    found = {}
    for name in _named_entities(config):
        if valid_entity_id(name) or (entry := registry.async_get(name)) is None:
            continue
        if policy.entity_allowed(entry.entity_id):
            found[name] = entry.entity_id
    return found
