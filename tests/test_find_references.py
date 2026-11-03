"""Tests for finding what uses something, and what names what does not exist."""

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.policy.limits import MAX_REPORT_USES
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options

_REASON = "Asked for by the user in this test."


def _automation(name: str, variables: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": name,
        "alias": name,
        "triggers": [{"trigger": "event", "event_type": "nothing"}],
        "actions": [{"variables": variables}],
    }


@pytest.fixture
async def core(hass: HomeAssistant) -> None:
    """Have the commands of core available."""
    for component in ("websocket_api", "config", "lovelace"):
        assert await async_setup_component(hass, component, {})
    await hass.async_block_till_done()


async def _automations(hass: HomeAssistant, *automations: dict[str, Any]) -> None:
    assert await async_setup_component(
        hass, "automation", {"automation": list(automations)}
    )
    await hass.async_block_till_done()


def _ids(data: dict[str, Any]) -> list[str]:
    return [row["id"] for row in data["items"]]


@pytest.mark.usefixtures("core", "init_integration")
async def test_finds_what_uses_an_entity(
    hass: HomeAssistant, call_tool: CallTool, entity_registry: er.EntityRegistry
) -> None:
    """By its ID or the ID of its registry entry, and not by a longer ID."""
    entry = entity_registry.async_get_or_create(
        "sensor", "test", "one", suggested_object_id="one"
    )
    await _automations(
        hass,
        _automation("by_name", {"entity": "sensor.one"}),
        _automation("by_entry", {"entity": entry.id}),
        _automation("longer", {"entity": "sensor.one_more"}),
    )
    data = (await call_tool("find_references", target="sensor.one")).data
    assert data["target"] == "sensor.one"
    assert _ids(data) == ["automation.by_entry", "automation.by_name"]
    assert data["items"][0]["uses"] == ["sensor.one"]
    assert "not_checked" not in data
    assert "how_found" in data


@pytest.mark.usefixtures("core", "init_integration")
async def test_finds_what_uses_a_device_and_what_is_on_it(
    hass: HomeAssistant, call_tool: CallTool, entity_registry: er.EntityRegistry
) -> None:
    """A device is named by its own ID, or by what belongs to it."""
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("test", "device")}
    )
    entity_registry.async_get_or_create(
        "sensor", "test", "one", suggested_object_id="one", device_id=device.id
    )
    await _automations(
        hass,
        _automation("by_device", {"device": device.id}),
        _automation("by_entity", {"entity": "sensor.one"}),
        _automation("other", {"entity": "sensor.other"}),
    )
    data = (await call_tool("find_references", target=device.id)).data
    assert _ids(data) == ["automation.by_device", "automation.by_entity"]


async def test_something_hidden_is_looked_for_as_plain_text(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    core: None,
    entity_registry: er.EntityRegistry,
) -> None:
    """It gets the answer of what does not exist, or the answer would give it away."""
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("test", "device")}
    )
    entry = entity_registry.async_get_or_create(
        "sensor",
        "test",
        "one",
        suggested_object_id="hidden",
        device_id=device.id,
    )
    await _automations(
        hass,
        _automation("by_name", {"entity": "sensor.hidden"}),
        _automation("by_entry", {"entity": entry.id}),
    )
    await apply_options(hass, init_integration, excluded_entities=["sensor.hidden"])
    data = (await call_tool("find_references", target="sensor.hidden")).data
    assert _ids(data) == ["automation.by_name"]
    assert (await call_tool("find_references", target=device.id)).data["items"] == []
    nothing = (await call_tool("find_references", target="sensor.not_there")).data
    assert nothing["items"] == []


async def test_reads_only_what_a_client_may_read(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    core: None,
) -> None:
    """Tools the owner turned off keep what they read, and the answer says so."""
    await _automations(hass, _automation("named", {"entity": "sensor.one"}))
    await apply_options(hass, init_integration, enabled_tools=["find_references"])
    data = (await call_tool("find_references", target="sensor.one")).data
    assert data["items"] == []
    assert data["not_checked"] == ["automations, scripts and scenes", "dashboards"]


@pytest.mark.usefixtures("core", "init_integration")
async def test_names_repairs_that_name_the_target(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """They can see what the search cannot."""
    ir.async_create_issue(
        hass,
        "other",
        "first",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="something",
        translation_placeholders={"entities": "- `sensor.one`"},
    )
    ir.async_create_issue(
        hass,
        "other",
        "second",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="something",
        translation_placeholders={"entities": "- `sensor.two`"},
    )
    data = (await call_tool("find_references", target="sensor.one")).data
    assert data["repairs"] == [
        {"issue_id": "first", "domain": "other", "kind": "something"}
    ]


@pytest.mark.usefixtures("core", "init_integration")
async def test_finds_what_names_what_does_not_exist(
    hass: HomeAssistant, call_tool: CallTool, entity_registry: er.EntityRegistry
) -> None:
    """Without Spook this stands on its own, and is not mistaken for other names."""

    async def nothing(call: ServiceCall) -> None:
        return None

    hass.services.async_register("light", "turn_on", nothing)
    hass.states.async_set("sensor.there", "1")
    hass.states.async_set("light.there", "on")
    hass.states.async_set("image.there", "1")
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "registered",
        suggested_object_id="registered",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await _automations(
        hass,
        _automation("fine", {"entity": ["sensor.there", "sensor.registered"]}),
        _automation(
            "other_names",
            {
                "service": "light.turn_on",
                "variable": "trigger.to_state.state",
                "picture": "/local/image.png",
            },
        ),
        _automation("gone", {"entity": "sensor.gone"}),
        _automation("in_template", {"a": "{{ states.sensor.old.state }}"}),
    )
    data = (await call_tool("find_references")).data
    assert {row["id"]: row["missing"] for row in data["items"]} == {
        "automation.gone": ["sensor.gone"],
        "automation.in_template": ["sensor.old"],
    }
    assert data["items"][0]["missing_count"] == 1
    assert "spook_found" not in data
    assert "target" not in data


@pytest.mark.usefixtures("core", "init_integration")
async def test_spook_adds_what_it_found(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Its other repairs are not about this."""
    hass.config.components.add("spook")
    ir.async_create_issue(
        hass,
        "spook",
        "template_unknown",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="template_unknown_entity_references_yaml",
        translation_placeholders={
            "entity_id": "sensor.template",
            "entities": "- `climate.gone` (did you mean `climate.there`?)",
        },
    )
    ir.async_create_issue(
        hass,
        "spook",
        "unused",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="unused_labels",
        translation_placeholders={"label": "Heating"},
    )
    found = (await call_tool("find_references")).data["spook_found"]
    assert [item["kind"] for item in found] == [
        "template_unknown_entity_references_yaml"
    ]
    assert "did you mean" in found[0]["details"]["entities"]


async def test_what_does_not_exist_needs_nothing_hidden(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    core: None,
) -> None:
    """A hidden entity would have to be reported as gone, or be told from what is."""
    await apply_options(hass, init_integration, excluded_entities=["sensor.hidden"])
    with pytest.raises(HomeAssistantError) as err:
        await call_tool("find_references")
    assert err.value.translation_key == "unavailable_while_hidden"
    assert err.value.translation_placeholders == {
        "what": "find_references without a target"
    }


async def _withheld_automation(hass: HomeAssistant) -> None:
    await _automations(
        hass,
        _automation(
            "held",
            {
                "code": "0427",
                "password": "sensor.orchid42",
                "latitude": "52.1234",
                "host": "somewhere.lan",
                "entity": "sensor.gone",
            },
        ),
    )
    hass.states.async_set("sensor.there", "1")


@pytest.mark.parametrize("target", ["0427", "52.1234", "somewhere.lan", "orchid42"])
async def test_a_guess_at_something_withheld_finds_nothing(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    core: None,
    target: str,
) -> None:
    """It answers the same as a wrong guess, or the guesses could be told apart."""
    await _withheld_automation(hass)
    await apply_options(hass, init_integration, redact_network=True)
    data = (await call_tool("find_references", target=target)).data
    assert data["total_count"] == 0


@pytest.mark.usefixtures("core", "init_integration")
async def test_what_is_not_withheld_is_still_found_by_its_text(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Text that names nothing known is searched for all the same."""
    await _withheld_automation(hass)
    data = (await call_tool("find_references", target="sensor.gone")).data
    assert _ids(data) == ["automation.held"]


@pytest.mark.usefixtures("core", "init_integration")
async def test_a_withheld_value_is_not_reported_as_missing(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """What this returns is returned as it stands, also through the health report."""
    await _withheld_automation(hass)
    data = (await call_tool("find_references")).data
    assert [row["missing"] for row in data["items"]] == [["sensor.gone"]]
    assert "orchid42" not in str(data)
    report = (await call_tool("get_health_report")).data
    assert "orchid42" not in str(report)
    assert report["missing_references"]["count"] == 1


@pytest.mark.usefixtures("core", "init_integration")
async def test_something_known_is_found_under_a_withheld_key(
    hass: HomeAssistant, call_tool: CallTool, entity_registry: er.EntityRegistry
) -> None:
    """A code from a helper is a real use, which removing the helper would break."""
    entity_registry.async_get_or_create(
        "input_text", "test", "alarm", suggested_object_id="alarm"
    )
    hass.states.async_set("sensor.only_a_state", "1")
    await _automations(
        hass,
        _automation("registered", {"code": "{{ states('input_text.alarm') }}"}),
        _automation("state", {"code": "{{ states('sensor.only_a_state') }}"}),
        _automation("unknown", {"code": "{{ states('input_text.none') }}"}),
    )
    found = (await call_tool("find_references", target="input_text.alarm")).data
    assert _ids(found) == ["automation.registered"]
    found = (await call_tool("find_references", target="sensor.only_a_state")).data
    assert _ids(found) == ["automation.state"]
    assert (await call_tool("find_references", target="input_text.none")).data[
        "items"
    ] == []


async def test_something_hidden_is_not_found_under_a_withheld_key(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    core: None,
) -> None:
    """What is hidden gets the answer of what does not exist, there too."""
    hass.states.async_set("sensor.hidden", "1")
    await _automations(
        hass, _automation("held", {"code": "{{ states('sensor.hidden') }}"})
    )
    await apply_options(hass, init_integration, excluded_entities=["sensor.hidden"])
    data = (await call_tool("find_references", target="sensor.hidden")).data
    assert data["items"] == []


@pytest.mark.usefixtures("core", "init_integration")
async def test_a_guess_at_a_value_in_a_repair_finds_nothing(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """What list_repairs withholds does not count as naming anything."""
    ir.async_create_issue(
        hass,
        "other",
        "first",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="something",
        translation_placeholders={"pin": "0427", "token": "zebra77"},
    )
    for target in ("0427", "zebra77"):
        data = (await call_tool("find_references", target=target)).data
        assert data["repairs"] == []


@pytest.mark.usefixtures("core", "init_integration")
async def test_names_of_triggers_and_services_are_not_entities(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """They read like an entity ID in a domain that has entities."""
    hass.states.async_set("light.there", "on")
    hass.states.async_set("sensor.there", "1")
    await _automations(
        hass,
        _automation(
            "names",
            {
                "trigger": "light.turned_on",
                "condition": "light.is_on",
                "action": "light.turn_on",
                "service": "light.turn_off",
                "perform_action": "light.toggle",
            },
        ),
        _automation("older", {"trigger": [{"entity_id": "light.gone"}]}),
        _automation("older_action", {"action": ["sensor.gone"]}),
        _automation("elsewhere", {"value": "light.turned_on"}),
    )
    data = (await call_tool("find_references")).data
    assert {row["id"]: row["missing"] for row in data["items"]} == {
        "automation.older": ["light.gone"],
        "automation.older_action": ["sensor.gone"],
        "automation.elsewhere": ["light.turned_on"],
    }


@pytest.mark.usefixtures("core", "init_integration")
async def test_an_id_with_two_underscores_is_not_an_entity(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A chart builds IDs of its own from entity IDs like this."""
    hass.states.async_set("sensor.grid_power", "1")
    await _automations(
        hass,
        _automation("chart", {"id": "sensor.grid_power__to_auto"}),
    )
    assert (await call_tool("find_references")).data["items"] == []


@pytest.mark.usefixtures("core", "init_integration")
async def test_lists_all_that_is_missing_and_the_report_stays_short(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Otherwise the rest can only be found by reading what was to be left unread."""
    hass.states.async_set("sensor.there", "1")
    names = [f"sensor.gone_{number:02}" for number in range(MAX_REPORT_USES + 5)]
    await _automations(hass, _automation("many", {"names": names}))

    data = (await call_tool("find_references")).data
    assert data["items"][0]["missing"] == names
    assert data["items"][0]["missing_count"] == len(names)

    report = (await call_tool("get_health_report")).data["missing_references"]
    assert report["items"][0]["missing"] == names[:MAX_REPORT_USES]
    assert report["items"][0]["missing_count"] == len(names)


@pytest.mark.usefixtures("core", "init_integration")
async def test_a_template_under_the_name_of_a_service_is_looked_through(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """It can read any entity, and the text around it is not an entity of its own."""
    hass.states.async_set("light.there", "on")
    hass.states.async_set("sensor.there", "1")
    await _automations(
        hass,
        _automation(
            "templates",
            {
                "action": "light.turn_{{ 'on' if is_state('sensor.gone_a', 'on') }}",
                "service": "{{ states('sensor.gone_b') }}",
            },
        ),
    )
    data = (await call_tool("find_references")).data
    assert data["items"][0]["missing"] == ["sensor.gone_a", "sensor.gone_b"]


@pytest.mark.usefixtures("core", "init_integration")
async def test_spook_comes_with_the_first_page_only(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """What it found is long, and the same on each page."""
    hass.config.components.add("spook")
    hass.states.async_set("sensor.there", "1")
    ir.async_create_issue(
        hass,
        "spook",
        "template_unknown",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="template_unknown_entity_references_yaml",
        translation_placeholders={"entities": "- `climate.gone`"},
    )
    await _automations(
        hass,
        _automation("one", {"name": "sensor.gone"}),
        _automation("two", {"name": "sensor.gone"}),
    )
    first = (await call_tool("find_references", limit=1)).data
    assert len(first["spook_found"]) == 1
    second = (await call_tool("find_references", limit=1, offset=1)).data
    assert _ids(second) == ["automation.two"]
    assert "spook_found" not in second
