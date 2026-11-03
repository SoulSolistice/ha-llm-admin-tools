"""Tests for what the settings hide, seen through the tools."""

from dataclasses import dataclass
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.llm_admin_tools.const import (
    CONF_ASSIST_EXPOSED_ONLY,
    CONF_ENABLED_TOOLS,
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITIES,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_EXCLUDED_LABELS,
    CONF_EXCLUDED_LOGGERS,
)
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers.entity import entity_sources
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options

_FREE_ENTITY = "light.free"


@dataclass(slots=True, frozen=True)
class World:
    """A boiler on a device in the cellar, and a light that belongs to nothing."""

    entry: MockConfigEntry
    other_entry_id: str
    cellar: str
    hall: str
    device: str
    boiler: str


@pytest.fixture
def world(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> World:
    """Create what the settings can hide."""
    other_entry = MockConfigEntry(domain="other", title="Other")
    other_entry.add_to_hass(hass)
    cellar = area_registry.async_create("Cellar")
    hall = area_registry.async_create("Hall")
    device = device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={("other", "boiler")},
        name="Boiler",
    )
    device_registry.async_update_device(device.id, area_id=cellar.id)
    boiler = entity_registry.async_get_or_create(
        "sensor",
        "other",
        "boiler",
        config_entry=other_entry,
        device_id=device.id,
        original_name="Temperature",
    )
    hass.states.async_set(boiler.entity_id, "1")
    hass.states.async_set(_FREE_ENTITY, "on")
    return World(
        init_integration,
        other_entry.entry_id,
        cellar.id,
        hall.id,
        device.id,
        boiler.entity_id,
    )


async def _listed(call_tool: CallTool) -> list[str]:
    """Return the entity IDs a client can list."""
    result = await call_tool("list_entities")
    return [item["entity_id"] for item in result.data["items"]]


async def _assert_hidden(call_tool: CallTool, entity_id: str) -> None:
    """Check an entity looks as if it did not exist, whichever tool asks."""
    assert entity_id not in await _listed(call_tool)
    with pytest.raises(ServiceValidationError) as err:
        await call_tool("get_entity", entity_id=entity_id)
    assert err.value.translation_key == "entity_not_found"


async def test_everything_is_visible_by_default(
    world: World, call_tool: CallTool
) -> None:
    """Without settings nothing is hidden."""
    assert await _listed(call_tool) == [_FREE_ENTITY, world.boiler]
    assert (await call_tool("list_devices")).data["total_count"] == 1
    assert (await call_tool("list_areas")).data["total_count"] == 2


async def test_excluded_entity(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """A single entity can be hidden."""
    await apply_options(hass, world.entry, **{CONF_EXCLUDED_ENTITIES: [world.boiler]})
    await _assert_hidden(call_tool, world.boiler)
    assert await _listed(call_tool) == [_FREE_ENTITY]


async def test_excluded_domain(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """All entities of a domain can be hidden."""
    await apply_options(hass, world.entry, **{CONF_EXCLUDED_DOMAINS: ["light"]})
    await _assert_hidden(call_tool, _FREE_ENTITY)
    assert await _listed(call_tool) == [world.boiler]


async def test_excluded_area(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """An area hides itself, its devices and the entities of those devices."""
    await apply_options(hass, world.entry, **{CONF_EXCLUDED_AREAS: [world.cellar]})
    await _assert_hidden(call_tool, world.boiler)
    assert (await call_tool("list_devices")).data["total_count"] == 0
    areas = await call_tool("list_areas")
    assert [area["area_id"] for area in areas.data["items"]] == [world.hall]


async def test_area_of_the_entity_itself_counts(
    hass: HomeAssistant,
    world: World,
    entity_registry: er.EntityRegistry,
    call_tool: CallTool,
) -> None:
    """An entity moved out of its device's area follows its own area."""
    entity_registry.async_update_entity(world.boiler, area_id=world.hall)

    await apply_options(hass, world.entry, **{CONF_EXCLUDED_AREAS: [world.cellar]})
    assert world.boiler in await _listed(call_tool)

    await apply_options(hass, world.entry, **{CONF_EXCLUDED_AREAS: [world.hall]})
    await _assert_hidden(call_tool, world.boiler)


async def test_excluded_label_on_entity(
    hass: HomeAssistant,
    world: World,
    entity_registry: er.EntityRegistry,
    label_registry: lr.LabelRegistry,
    call_tool: CallTool,
) -> None:
    """A label on an entity hides only that entity."""
    label = label_registry.async_create("Private")
    entity_registry.async_update_entity(world.boiler, labels={label.label_id})
    await apply_options(hass, world.entry, **{CONF_EXCLUDED_LABELS: [label.label_id]})
    await _assert_hidden(call_tool, world.boiler)
    assert (await call_tool("list_devices")).data["total_count"] == 1


async def test_excluded_label_on_device(
    hass: HomeAssistant,
    world: World,
    device_registry: dr.DeviceRegistry,
    label_registry: lr.LabelRegistry,
    call_tool: CallTool,
) -> None:
    """A label on a device hides the device and its entities."""
    label = label_registry.async_create("Private")
    device_registry.async_update_device(world.device, labels={label.label_id})
    await apply_options(hass, world.entry, **{CONF_EXCLUDED_LABELS: [label.label_id]})
    await _assert_hidden(call_tool, world.boiler)
    assert (await call_tool("list_devices")).data["total_count"] == 0


async def test_excluded_integration(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """An integration hides its entry, its devices and its entities."""
    await apply_options(
        hass, world.entry, **{CONF_EXCLUDED_INTEGRATIONS: [world.other_entry_id]}
    )
    await _assert_hidden(call_tool, world.boiler)
    assert (await call_tool("list_devices")).data["total_count"] == 0
    integrations = await call_tool("list_integrations")
    assert [item["domain"] for item in integrations.data["items"]] == [
        "llm_admin_tools"
    ]


async def test_excluded_integration_without_registry_entry(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """An entity without a registry entry is hidden through what added it."""
    hass.states.async_set("sensor.unregistered", "1")
    entity_sources(hass)["sensor.unregistered"] = {
        "domain": "other",
        "config_entry": world.other_entry_id,
    }
    assert "sensor.unregistered" in await _listed(call_tool)
    await apply_options(
        hass, world.entry, **{CONF_EXCLUDED_INTEGRATIONS: [world.other_entry_id]}
    )
    await _assert_hidden(call_tool, "sensor.unregistered")


async def test_entity_of_a_component_goes_by_the_integration_it_names(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """Without a link to a config entry, it counts as part of each of its integration.

    The entity itself can be hidden by name, and other integrations do not reach it.
    """
    hass.states.async_set("sensor.unlinked", "1")
    hass.states.async_set("sensor.elsewhere", "1")
    sources = entity_sources(hass)
    sources["sensor.unlinked"] = {"domain": "other"}
    sources["sensor.elsewhere"] = {"domain": "not_set_up"}
    await apply_options(
        hass, world.entry, **{CONF_EXCLUDED_INTEGRATIONS: [world.other_entry_id]}
    )
    listed = await _listed(call_tool)
    assert "sensor.unlinked" not in listed
    assert "sensor.elsewhere" in listed
    await apply_options(hass, world.entry)
    assert "sensor.unlinked" in await _listed(call_tool)
    await apply_options(
        hass, world.entry, **{CONF_EXCLUDED_ENTITIES: ["sensor.unlinked"]}
    )
    await _assert_hidden(call_tool, "sensor.unlinked")


async def test_excluded_integration_of_a_device_only(
    hass: HomeAssistant,
    world: World,
    entity_registry: er.EntityRegistry,
    call_tool: CallTool,
) -> None:
    """An entity of another integration on a hidden device is hidden with it."""
    foreign = entity_registry.async_get_or_create(
        "sensor", "foreign", "extra", device_id=world.device
    )
    hass.states.async_set(foreign.entity_id, "1")
    await apply_options(
        hass, world.entry, **{CONF_EXCLUDED_INTEGRATIONS: [world.other_entry_id]}
    )
    await _assert_hidden(call_tool, foreign.entity_id)


@pytest.fixture
def child_entity(
    hass: HomeAssistant,
    world: World,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> str:
    """Create an entity on a child device of the boiler."""
    child = device_registry.async_get_or_create_child(
        config_entry_id=world.other_entry_id,
        identifiers={("other", "pump")},
        name="Pump",
        parent_device_id=world.device,
    )
    entry = entity_registry.async_get_or_create(
        "sensor", "other", "pump", device_id=child.id, original_name="Speed"
    )
    hass.states.async_set(entry.entity_id, "1")
    return entry.entity_id


async def test_child_device_is_listed_in_the_area_of_its_parent(
    world: World, child_entity: str, call_tool: CallTool
) -> None:
    """A child device is found, in the area of its parent."""
    devices = await call_tool("list_devices", area_id=world.cellar)
    by_name = {item["name"]: item for item in devices.data["items"]}
    assert by_name["Pump"]["parent_device_id"] == world.device
    assert by_name["Pump"]["area_id"] == world.cellar
    assert "manufacturer" not in by_name["Pump"]
    entity = await call_tool("get_entity", entity_id=child_entity)
    assert entity.data["registry"]["area_id"] == world.cellar


async def test_excluded_area_hides_child_devices(
    hass: HomeAssistant, world: World, child_entity: str, call_tool: CallTool
) -> None:
    """A child device without an area of its own goes with its parent."""
    await apply_options(hass, world.entry, **{CONF_EXCLUDED_AREAS: [world.cellar]})
    await _assert_hidden(call_tool, child_entity)
    assert (await call_tool("list_devices")).data["total_count"] == 0


async def test_child_device_in_its_own_area(
    hass: HomeAssistant,
    world: World,
    child_entity: str,
    call_tool: CallTool,
) -> None:
    """A child device moved to another area follows that area."""
    entry = er.async_get(hass).async_get(child_entity)
    assert entry is not None
    assert entry.device_id is not None
    dr.async_get(hass).async_update_child_device(entry.device_id, area_id=world.hall)
    await apply_options(hass, world.entry, **{CONF_EXCLUDED_AREAS: [world.cellar]})
    assert child_entity in await _listed(call_tool)
    await apply_options(hass, world.entry, **{CONF_EXCLUDED_AREAS: [world.hall]})
    await _assert_hidden(call_tool, child_entity)


async def test_label_on_parent_hides_child_devices(
    hass: HomeAssistant,
    world: World,
    child_entity: str,
    label_registry: lr.LabelRegistry,
    call_tool: CallTool,
) -> None:
    """What hides a device also hides the parts it is made of."""
    label = label_registry.async_create("Private")
    dr.async_get(hass).async_update_device(world.device, labels={label.label_id})
    await apply_options(hass, world.entry, **{CONF_EXCLUDED_LABELS: [label.label_id]})
    await _assert_hidden(call_tool, child_entity)
    assert (await call_tool("list_devices")).data["total_count"] == 0


async def test_hidden_entity_has_no_history(
    recorder_mock: Any, hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """What was recorded stays hidden, and looks like an entity that never existed."""
    hass.states.async_set(world.boiler, "2")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    before = await call_tool("get_entity_history", entity_ids=[world.boiler])
    assert before.data["entities"][world.boiler]["points"]

    await apply_options(hass, world.entry, **{CONF_EXCLUDED_ENTITIES: [world.boiler]})
    result = await call_tool(
        "get_entity_history", entity_ids=[world.boiler, "sensor.never_existed"]
    )
    entities = result.data["entities"]
    assert entities[world.boiler]["points"] == []
    assert entities[world.boiler] == entities["sensor.never_existed"]

    await apply_options(
        hass, world.entry, **{CONF_EXCLUDED_DOMAINS: ["sensor", "light"]}
    )
    only_hidden = await call_tool("get_entity_history", entity_ids=[world.boiler])
    assert only_hidden.data["entities"][world.boiler]["points"] == []


async def test_excluded_logger(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """Log records of a logger can be hidden by part of its name."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()
    logging.getLogger("test.private_part.module").error("hide me")
    logging.getLogger("test.public").error("show me")
    await hass.async_block_till_done()

    await apply_options(hass, world.entry, **{CONF_EXCLUDED_LOGGERS: ["private_part"]})
    result = await call_tool("get_system_log")
    assert [item["message"] for item in result.data["items"]] == [["show me"]]


async def test_excluded_definition(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """An automation is an entity and can be hidden like one."""
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "kept": {"sequence": [{"delay": "00:00:01"}]},
                "hidden": {"sequence": [{"delay": "00:00:01"}]},
            }
        },
    )
    await hass.async_block_till_done()
    await apply_options(
        hass, world.entry, **{CONF_EXCLUDED_ENTITIES: ["script.hidden"]}
    )
    listed = await call_tool("list_definitions", kind="script")
    assert [item["entity_id"] for item in listed.data["items"]] == ["script.kept"]
    with pytest.raises(ServiceValidationError) as err:
        await call_tool("get_definition", kind="script", entity_id="script.hidden")
    assert err.value.translation_key == "entity_not_found"


async def test_assist_exposed_only(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """The tools can be limited to what is exposed to Assist."""
    assert await async_setup_component(hass, "homeassistant", {})
    async_expose_entity(hass, "conversation", _FREE_ENTITY, True)
    async_expose_entity(hass, "conversation", world.boiler, False)
    await apply_options(hass, world.entry, **{CONF_ASSIST_EXPOSED_ONLY: True})
    assert await _listed(call_tool) == [_FREE_ENTITY]
    await _assert_hidden(call_tool, world.boiler)


async def test_disabled_tool_cannot_be_called(
    hass: HomeAssistant, world: World, call_tool: CallTool
) -> None:
    """A tool that is turned off is gone, not merely unlisted."""
    await apply_options(hass, world.entry, **{CONF_ENABLED_TOOLS: ["list_areas"]})
    assert (await call_tool("list_areas")).data["total_count"] == 2
    with pytest.raises(HomeAssistantError, match="not found"):
        await call_tool("list_entities")
