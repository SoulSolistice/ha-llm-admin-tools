"""Tests that a client cannot tell what is hidden from what does not exist."""

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options

_REASON = "Asked for by the user in this test."


async def _refusal(call_tool: CallTool, tool: str, **args: Any) -> Any:
    """Return the kind of refusal, and what it says with the name taken out."""
    with pytest.raises(Exception) as err:
        await call_tool(tool, reason=_REASON, **args)
    placeholders = getattr(err.value, "translation_placeholders", None) or {}
    return getattr(err.value, "translation_key", None), sorted(placeholders)


@pytest.fixture(autouse=True)
async def hidden_things(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """Have one of each kind of thing hidden."""
    for component in ("websocket_api", "config", "input_boolean", "automation"):
        assert await async_setup_component(hass, component, {})
    await hass.async_block_till_done()
    async_mock_service(hass, "light", "turn_on")
    entity_registry.async_get_or_create(
        "light", "test", "hidden", suggested_object_id="hidden"
    )
    hass.states.async_set("light.hidden", "off")
    entity_registry.async_get_or_create(
        "light", "test", "visible", suggested_object_id="visible"
    )
    hass.states.async_set("light.visible", "off")
    area = area_registry.async_create("Secret room")
    label = label_registry.async_create("Secret label")
    await apply_options(
        hass,
        init_integration,
        write_subjects=["areas", "entities", "services", "automations", "floors"],
        allowed_services=["light"],
        excluded_entities=["light.hidden"],
        excluded_areas=[area.id],
        excluded_labels=[label.label_id],
    )


@pytest.mark.parametrize(
    ("hidden", "missing"),
    [
        (
            (
                "call_service",
                {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_ids": ["light.hidden"],
                },
            ),
            (
                "call_service",
                {"domain": "light", "service": "turn_on", "entity_ids": ["light.none"]},
            ),
        ),
        (
            ("update_entity", {"entity_id": "light.hidden", "name": "x"}),
            ("update_entity", {"entity_id": "light.none", "name": "x"}),
        ),
        (
            ("update_entity", {"entity_id": "light.visible", "area_id": "secret_room"}),
            ("update_entity", {"entity_id": "light.visible", "area_id": "none"}),
        ),
        (
            (
                "update_entity",
                {"entity_id": "light.visible", "labels": ["secret_label"]},
            ),
            ("update_entity", {"entity_id": "light.visible", "labels": ["none"]}),
        ),
        (
            (
                "write_registry_item",
                {
                    "kind": "area",
                    "operation": "update",
                    "item_id": "secret_room",
                    "fields": {"name": "x"},
                },
            ),
            (
                "write_registry_item",
                {
                    "kind": "area",
                    "operation": "update",
                    "item_id": "none",
                    "fields": {"name": "x"},
                },
            ),
        ),
        (
            (
                "write_registry_item",
                {
                    "kind": "area",
                    "operation": "create",
                    "fields": {"name": "x", "temperature_entity_id": "light.hidden"},
                },
            ),
            (
                "write_registry_item",
                {
                    "kind": "area",
                    "operation": "create",
                    "fields": {"name": "x", "temperature_entity_id": "light.none"},
                },
            ),
        ),
    ],
)
async def test_same_answer(call_tool: CallTool, hidden: Any, missing: Any) -> None:
    """What differs is only the name that the client itself asked about."""
    hidden_tool, hidden_args = hidden
    missing_tool, missing_args = missing
    assert await _refusal(call_tool, hidden_tool, **hidden_args) == await _refusal(
        call_tool, missing_tool, **missing_args
    )


async def test_same_answer_in_a_definition(call_tool: CallTool) -> None:
    """Looking at something in a condition tells as much as acting on it."""
    answers = [
        await _refusal(
            call_tool,
            "write_definition",
            kind="automation",
            operation="create",
            config={
                "alias": "Test",
                "triggers": [{"trigger": "state", "entity_id": entity_id}],
                "actions": [],
            },
        )
        for entity_id in ("light.hidden", "light.none")
    ]
    assert answers[0] == answers[1] == ("entity_not_found", ["entity_id"])


async def test_floor_does_not_name_the_hidden_area_in_it(
    call_tool: CallTool,
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """A refusal that names the area would tell what it is called."""
    floor = floor_registry.async_create("Up")
    area_registry.async_update("secret_room", floor_id=floor.floor_id)
    answer = await _refusal(
        call_tool,
        "write_registry_item",
        kind="floor",
        operation="update",
        item_id=floor.floor_id,
        fields={"name": "x"},
    )
    assert answer == ("in_use_by_protected", [])


async def test_a_visible_entity_can_still_be_changed(
    call_tool: CallTool, entity_registry: er.EntityRegistry
) -> None:
    """All this keeps nothing from what exists and may be seen."""
    await call_tool(
        "update_entity", entity_id="light.visible", name="Named", reason=_REASON
    )
    entry = entity_registry.async_get("light.visible")
    assert entry is not None
    assert entry.name == "Named"
