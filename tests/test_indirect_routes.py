"""Tests for what reaches something other than what a client names."""

from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import entity_sources
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options

_REASON = "Asked for by the user in this test."
_TRIGGER = [{"trigger": "event", "event_type": "go"}]


@pytest.fixture(autouse=True)
def use_temp_config(hass: HomeAssistant, tmp_path: Path) -> None:
    """Keep definition files out of the tree the tests run in, set up as by default."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include automations.yaml\n", encoding="utf-8"
    )
    (tmp_path / "automations.yaml").touch()


@pytest.fixture
async def scripts(hass: HomeAssistant) -> None:
    """Have scripts, each with a service of its own."""
    assert await async_setup_component(hass, "automation", {})
    assert await async_setup_component(
        hass,
        "script",
        {"script": {name: {"sequence": []} for name in ("free", "kept", "quiet")}},
    )
    await hass.async_block_till_done()


async def _refusal(call_tool: CallTool, tool: str, /, **args: Any) -> str | None:
    with pytest.raises(Exception) as err:
        await call_tool(tool, reason=_REASON, **args)
    return getattr(err.value, "translation_key", None)


async def _service_refusal(
    call_tool: CallTool, service: str, **args: Any
) -> str | None:
    domain, _, name = service.partition(".")
    return await _refusal(
        call_tool, "call_service", domain=domain, service=name, **args
    )


async def _definition_refusal(
    call_tool: CallTool, *actions: dict[str, Any], **extra: Any
) -> str | None:
    return await _refusal(
        call_tool,
        "write_definition",
        kind="automation",
        operation="create",
        dry_run=True,
        config={"alias": "T", "triggers": _TRIGGER, "actions": [*actions], **extra},
    )


async def _definition_dry_run(call_tool: CallTool, *actions: dict[str, Any]) -> None:
    await call_tool(
        "write_definition",
        kind="automation",
        operation="create",
        dry_run=True,
        reason=_REASON,
        config={"alias": "T", "triggers": _TRIGGER, "actions": [*actions]},
    )


@pytest.mark.usefixtures("scripts")
async def test_the_service_of_a_script_keeps_to_protection(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Running a script by its service is the same as running the entity."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services", "automations"],
        allowed_services=["script"],
        protected_entities=["script.kept"],
    )
    assert await _service_refusal(call_tool, "script.kept") == "entity_protected"
    assert (
        await _definition_refusal(call_tool, {"action": "script.kept"})
        == "entity_protected"
    )
    await call_tool(
        "call_service", domain="script", service="free", reason=_REASON, dry_run=True
    )
    await _definition_dry_run(call_tool, {"action": "script.free"})
    await _definition_dry_run(
        call_tool, {"action": "script.turn_on", "target": {"entity_id": "script.free"}}
    )


@pytest.mark.usefixtures("scripts")
async def test_the_service_of_a_hidden_script_looks_like_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """It is not listed, and is refused in the same words as one that does not exist."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services", "automations"],
        allowed_services=["script"],
        excluded_entities=["script.quiet"],
    )
    assert await _service_refusal(call_tool, "script.quiet") == "service_not_found"
    assert await _service_refusal(call_tool, "script.nothing") == "service_not_found"
    assert (
        await _definition_refusal(call_tool, {"action": "script.quiet"})
        == await _definition_refusal(call_tool, {"action": "script.nothing"})
        == "entity_not_found"
    )
    found = (await call_tool("find_services", domain="script")).data["items"]
    names = {service["service"] for service in found}
    assert "script.free" in names
    assert "script.turn_on" in names
    assert "script.quiet" not in names


@pytest.mark.usefixtures("scripts")
async def test_a_renamed_script_is_still_known_by_its_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    entity_registry: er.EntityRegistry,
) -> None:
    """The service is named after the unique ID, which stays, and no new name shows."""
    entity_registry.async_update_entity("script.kept", new_entity_id="script.moved")
    entity_registry.async_update_entity("script.quiet", new_entity_id="script.gone")
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations"],
        allowed_services=["script"],
        protected_entities=["script.moved"],
        excluded_entities=["script.gone"],
    )
    assert (
        await _definition_refusal(call_tool, {"action": "script.kept"})
        == "entity_protected"
    )
    with pytest.raises(Exception, match=r"script\.quiet") as err:
        await _definition_refusal_raising(call_tool, "script.quiet")
    assert "script.gone" not in str(err.value)


async def _definition_refusal_raising(call_tool: CallTool, service: str) -> None:
    await _definition_dry_run(call_tool, {"action": service})


@pytest.mark.parametrize(
    "option", ["protected_entities", "excluded_entities", "protected_domains"]
)
async def test_a_group_cannot_stand_in_for_what_it_holds(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    option: str,
) -> None:
    """A service called on a group reaches its members, at any depth."""
    assert await async_setup_component(hass, "automation", {})
    await hass.async_block_till_done()
    async_mock_service(hass, "input_boolean", "turn_on")
    name = "switch" if option == "protected_domains" else "switch.guarded"
    hass.states.async_set("switch.guarded", "off")
    hass.states.async_set("group.inner", "off", {"entity_id": ["switch.guarded"]})
    hass.states.async_set(
        "group.outer", "off", {"entity_id": ("group.inner", "group.outer", "light.a")}
    )
    hass.states.async_set("group.one", "off", {"entity_id": "switch.guarded"})
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services", "automations"],
        allowed_services=["input_boolean"],
        **{option: [name]},
    )
    for group in ("group.outer", "group.one"):
        assert (
            await _service_refusal(
                call_tool, "input_boolean.turn_on", entity_ids=[group]
            )
            == "target_holds_protected"
        )
    assert (
        await _definition_refusal(
            call_tool,
            {"action": "input_boolean.turn_on", "target": {"entity_id": "group.outer"}},
        )
        == "target_holds_protected"
    )


async def test_a_group_of_what_may_be_changed_can_be_a_target(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Only what is hidden or protected stops it, and odd attributes hold no names."""
    calls = async_mock_service(hass, "input_boolean", "turn_on")
    hass.states.async_set("switch.guarded", "off")
    hass.states.async_set("group.fine", "off", {"entity_id": ["light.a", 3]})
    hass.states.async_set("sensor.odd", "1", {"entity_id": 3})
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["input_boolean"],
        protected_entities=["switch.guarded"],
    )
    await call_tool(
        "call_service",
        domain="input_boolean",
        service="turn_on",
        entity_ids=["group.fine", "sensor.odd"],
        reason=_REASON,
    )
    assert len(calls) == 1


async def test_a_group_with_something_protected_can_still_be_renamed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    entity_registry: er.EntityRegistry,
) -> None:
    """A name of the group does not reach its members, as a service call does."""
    for component in ("websocket_api", "config"):
        assert await async_setup_component(hass, component, {})
    entity_registry.async_get_or_create(
        "group", "test", "one", suggested_object_id="named"
    )
    hass.states.async_set("switch.guarded", "off")
    hass.states.async_set("group.named", "off", {"entity_id": ["switch.guarded"]})
    await apply_options(
        hass,
        init_integration,
        write_subjects=["entities"],
        protected_entities=["switch.guarded"],
    )
    await call_tool(
        "update_entity",
        entity_id="group.named",
        name="Another",
        reason=_REASON,
    )
    assert entity_registry.async_get("group.named").name == "Another"


_TEMPLATE_READ = {"variables": {"seen": "{{ state_attr('zone.home', 'latitude') }}"}}


async def test_templates_are_off_until_the_owner_turns_them_on(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Rendered in a run, a trace gives back what a template read, withheld or not."""
    assert await async_setup_component(hass, "automation", {})
    await hass.async_block_till_done()
    options = {"write_subjects": ["automations"]}
    await apply_options(hass, init_integration, **options)
    assert await _definition_refusal(call_tool, _TEMPLATE_READ) == "templates_off"
    assert (
        await _definition_refusal(call_tool, {"variables": {"{{ 'a' }}": 1}})
        == "templates_off"
    )
    assert (
        await _definition_refusal(
            call_tool,
            {"variables": {"a": 1}},
            conditions=[{"condition": "template", "value_template": "{{ true }}"}],
        )
        == "templates_off"
    )
    report = (await call_tool("get_capabilities")).data
    assert report["writes"]["templates_allowed"] is False

    await apply_options(hass, init_integration, **options, write_allow_templates=True)
    await _definition_dry_run(call_tool, _TEMPLATE_READ)
    report = (await call_tool("get_capabilities")).data
    assert report["writes"]["templates_allowed"] is True


async def test_turning_templates_on_changes_nothing_while_anything_is_hidden(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Hiding is a promise that the switch does not get to break."""
    assert await async_setup_component(hass, "automation", {})
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations"],
        write_allow_templates=True,
        excluded_entities=["light.hidden"],
    )
    assert await _definition_refusal(call_tool, _TEMPLATE_READ) == "template_refused"
    report = (await call_tool("get_capabilities")).data
    assert report["writes"]["templates_allowed"] is False


@pytest.fixture
async def sun(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> MockConfigEntry:
    """Provide an entry with entities that the registries do not link to it."""
    for component in ("websocket_api", "config"):
        assert await async_setup_component(hass, component, {})
    entry = MockConfigEntry(domain="sun")
    entry.add_to_hass(hass)
    sources = entity_sources(hass)
    sources["sun.sun"] = {"domain": "sun", "custom_component": False}
    sources["sensor.of_entry"] = {
        "domain": "other",
        "custom_component": False,
        "config_entry": entry.entry_id,
    }
    sources["sensor.of_nothing"] = {"domain": "other", "custom_component": False}
    for entity_id in ("sun.sun", "sensor.of_entry", "sensor.of_nothing"):
        hass.states.async_set(entity_id, "1")
    return entry


async def test_a_removal_reports_what_has_no_registry_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    sun: MockConfigEntry,
) -> None:
    """Those would otherwise go without being named."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        write_allow_delete=True,
    )
    result = await call_tool(
        "manage_config_entry",
        entry_id=sun.entry_id,
        operation="delete",
        dry_run=True,
        reason=_REASON,
    )
    assert result.data["affects"]["entities"]["items"] == [
        "sensor.of_entry",
        "sun.sun",
    ]


async def test_protection_holds_for_what_has_no_registry_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    sun: MockConfigEntry,
) -> None:
    """Protecting the entity keeps the entry it depends on."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        write_allow_delete=True,
        protected_entities=["sun.sun"],
    )
    assert (
        await _refusal(
            call_tool, "manage_config_entry", entry_id=sun.entry_id, operation="delete"
        )
        == "in_use_by_protected"
    )


async def test_hiding_an_integration_hides_what_only_names_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    sun: MockConfigEntry,
) -> None:
    """An entity that names no entry counts as part of each entry of its integration."""
    await apply_options(hass, init_integration, excluded_integrations=[sun.entry_id])
    shown = (await call_tool("list_entities", limit=100)).data["items"]
    ids = {entity["entity_id"] for entity in shown}
    assert "sun.sun" not in ids
    assert "sensor.of_entry" not in ids
    assert "sensor.of_nothing" in ids
    assert "light.not_known" not in ids
