"""Tests for writing automations, scripts and scenes, and for what they may do."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml

from .conftest import HA_2026_11, CallTool, apply_options

_REASON = "Asked for by the user in this test."
_TRIGGER = [{"trigger": "event", "event_type": "go"}]


def _automation(*actions: Any, **extra: Any) -> dict[str, Any]:
    return {"alias": "Test", "triggers": _TRIGGER, "actions": list(actions), **extra}


def _service(service: str, **fields: Any) -> dict[str, Any]:
    return {"action": service, **fields}


@pytest.fixture(autouse=True)
def use_temp_config(hass: HomeAssistant, tmp_path: Path) -> None:
    """Keep definition files out of the tree the tests run in, set up as by default."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include automations.yaml\n"
        "script: !include scripts.yaml\n"
        "scene: !include scenes.yaml\n",
        encoding="utf-8",
    )
    for name in ("automations.yaml", "scripts.yaml", "scenes.yaml"):
        (tmp_path / name).touch()


@pytest.fixture
async def definitions(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Have the components running, and everything allowed that tests do not vary."""
    assert await async_setup_component(hass, "automation", {})
    assert await async_setup_component(hass, "script", {})
    assert await async_setup_component(hass, "scene", {})
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations", "scripts", "scenes"],
        allowed_services=["light"],
        write_allow_delete=True,
    )


async def _write(call_tool: CallTool, kind: str, operation: str, **args: Any) -> Any:
    return await call_tool(
        "write_definition", kind=kind, operation=operation, reason=_REASON, **args
    )


async def _refusal(
    call_tool: CallTool, config: dict[str, Any], kind: str = "automation"
) -> str | None:
    with pytest.raises(Exception) as err:
        await _write(
            call_tool,
            kind,
            "create",
            config=config,
            **({"id": "made"} if kind == "script" else {}),
        )
    return getattr(err.value, "translation_key", None)


@pytest.mark.usefixtures("definitions")
async def test_automation_life_cycle(
    hass: HomeAssistant, call_tool: CallTool, entity_registry: er.EntityRegistry
) -> None:
    """Created, it runs; changed, it does the new thing; deleted, it is gone."""
    calls = async_mock_service(hass, "light", "turn_on")
    created = await _write(
        call_tool,
        "automation",
        "create",
        config=_automation(_service("light.turn_on", target={"entity_id": "light.a"})),
    )
    key = created.data["result"]["id"]
    entity_id = created.data["result"]["entity_id"]
    assert entity_id == "automation.test"
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is not None
    hass.bus.async_fire("go")
    await hass.async_block_till_done()
    assert len(calls) == 1

    content = load_yaml(hass.config.path("automations.yaml"))
    assert list(content[0]) == ["id", "alias", "triggers", "actions"]

    updated = await _write(
        call_tool,
        "automation",
        "update",
        id=key,
        config=_automation(id="ignored", alias="Renamed"),
    )
    assert updated.data["before"]["alias"] == "Test"
    assert load_yaml(hass.config.path("automations.yaml"))[0]["id"] == key
    assert load_yaml(hass.config.path("automations.yaml"))[0]["alias"] == "Renamed"

    deleted = await _write(call_tool, "automation", "delete", id=key)
    assert deleted.data["before"]["alias"] == "Renamed"
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None
    assert entity_registry.async_get(entity_id) is None
    assert load_yaml(hass.config.path("automations.yaml")) == []


@pytest.mark.usefixtures("definitions")
async def test_automation_next_to_another(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Others in the file stay as they are."""
    first = await _write(call_tool, "automation", "create", config=_automation())
    second = await _write(call_tool, "automation", "create", config=_automation())
    await _write(
        call_tool,
        "automation",
        "update",
        id=first.data["result"]["id"],
        config=_automation(alias="First"),
    )
    content = load_yaml(hass.config.path("automations.yaml"))
    assert [item["id"] for item in content] == [
        first.data["result"]["id"],
        second.data["result"]["id"],
    ]
    assert content[0]["alias"] == "First"


@pytest.mark.usefixtures("definitions")
async def test_script_life_cycle(hass: HomeAssistant, call_tool: CallTool) -> None:
    """A script's ID is its name, so it has to be given and be a slug."""
    config = {"alias": "Made", "sequence": [_service("light.turn_on")]}
    with pytest.raises(Exception, match=r"slug|None|string"):
        await _write(call_tool, "script", "create", config=config)
    with pytest.raises(Exception, match="slug"):
        await _write(call_tool, "script", "create", id="Not a slug!", config=config)

    created = await _write(call_tool, "script", "create", id="made", config=config)
    assert created.data["result"]["entity_id"] == "script.made"
    assert hass.states.get("script.made") is not None
    await _write(
        call_tool,
        "script",
        "update",
        id="made",
        config={**config, "alias": "Changed"},
    )
    assert load_yaml(hass.config.path("scripts.yaml"))["made"]["alias"] == "Changed"
    await _write(call_tool, "script", "delete", id="made")
    await hass.async_block_till_done()
    assert hass.states.get("script.made") is None
    assert load_yaml(hass.config.path("scripts.yaml")) == {}


@pytest.mark.usefixtures("definitions")
async def test_scene_life_cycle(hass: HomeAssistant, call_tool: CallTool) -> None:
    """A scene can be read back as what it was written as."""
    config = {"name": "Evening", "entities": {"light.a": "on"}}
    created = await _write(call_tool, "scene", "create", id="evening", config=config)
    entity_id = created.data["result"]["entity_id"]
    assert entity_id == "scene.evening"

    listed = await call_tool("list_definitions", kind="scene")
    assert [item["unique_id"] for item in listed.data["items"]] == ["evening"]
    read = await call_tool("get_definition", kind="scene", entity_id=entity_id)
    assert read.data["config"]["id"] == "evening"
    assert read.data["config"]["entities"]["light.a"]["state"] == "on"

    await _write(call_tool, "scene", "delete", id="evening")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None


@pytest.mark.usefixtures("definitions")
async def test_dry_run_writes_no_file(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Validated, and not written."""
    result = await _write(
        call_tool, "automation", "create", config=_automation(), dry_run=True
    )
    assert result.data["dry_run"] is True
    assert not Path(hass.config.path("automations.yaml")).read_text(encoding="utf-8")


@pytest.mark.usefixtures("definitions")
async def test_core_validates_the_definition(call_tool: CallTool) -> None:
    """What core would not load is not written."""
    with pytest.raises(Exception, match=r"required|extra|trigger"):
        await _write(call_tool, "automation", "create", config={"alias": "No trigger"})


@pytest.mark.usefixtures("definitions")
async def test_requests_that_cannot_be_right(call_tool: CallTool) -> None:
    """What is missing or does not match is said plainly."""
    config = _automation()
    for operation, key, conf, translation_key in (
        ("update", None, config, "definition_id_required"),
        ("update", "nope", config, "definition_not_found"),
        ("delete", "nope", None, "definition_not_found"),
        ("create", None, None, "definition_config_required"),
    ):
        args = {
            **({"id": key} if key else {}),
            **({"config": conf} if conf else {}),
        }
        with pytest.raises(Exception) as err:
            await _write(call_tool, "automation", operation, **args)
        assert getattr(err.value, "translation_key", None) == translation_key

    await _write(call_tool, "automation", "create", id="taken", config=config)
    with pytest.raises(Exception) as err:
        await _write(call_tool, "automation", "create", id="taken", config=config)
    assert getattr(err.value, "translation_key", None) == "definition_exists"


async def test_each_kind_has_its_own_subject(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Allowing scripts does not allow automations."""
    await apply_options(hass, init_integration, write_subjects=["scripts"])
    with pytest.raises(Exception) as err:
        await _write(call_tool, "automation", "create", config=_automation())
    assert getattr(err.value, "translation_key", None) == "write_subject_off"


async def test_kind_must_be_running(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Nothing is written that could not be loaded."""
    await apply_options(hass, init_integration, write_subjects=["automations"])
    with pytest.raises(Exception) as err:
        await _write(call_tool, "automation", "create", config=_automation())
    assert getattr(err.value, "translation_key", None) == "definition_kind_unavailable"


@pytest.mark.usefixtures("definitions")
async def test_delete_needs_its_own_permission(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Being able to change something does not mean being able to delete it."""
    await _write(call_tool, "automation", "create", id="kept", config=_automation())
    await apply_options(
        hass,
        init_integration,
        **{**init_integration.options, "write_allow_delete": False},
    )
    with pytest.raises(Exception) as err:
        await _write(call_tool, "automation", "delete", id="kept")
    assert getattr(err.value, "translation_key", None) == "delete_not_allowed"


@pytest.mark.usefixtures("definitions")
async def test_protected_definition_cannot_be_changed(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A hidden one looks like it does not exist; a protected one says so."""
    created = await _write(
        call_tool, "automation", "create", id="kept", config=_automation()
    )
    entity_id = created.data["result"]["entity_id"]
    options = dict(init_integration.options)
    await apply_options(
        hass, init_integration, **options, protected_entities=[entity_id]
    )
    with pytest.raises(Exception) as err:
        await _write(call_tool, "automation", "update", id="kept", config=_automation())
    assert getattr(err.value, "translation_key", None) == "entity_protected"
    await apply_options(
        hass, init_integration, **options, excluded_entities=[entity_id]
    )
    with pytest.raises(Exception) as err:
        await _write(call_tool, "automation", "delete", id="kept")
    assert getattr(err.value, "translation_key", None) == "definition_not_found"
    with pytest.raises(Exception) as err:
        await _write(call_tool, "automation", "delete", id="never_made")
    assert getattr(err.value, "translation_key", None) == "definition_not_found"
    with pytest.raises(Exception) as err:
        await _write(call_tool, "automation", "create", id="kept", config=_automation())
    assert getattr(err.value, "translation_key", None) == "definition_exists"


@pytest.mark.usefixtures("definitions")
@pytest.mark.parametrize(
    "action",
    [
        _service("shell_command.anything"),
        {"service": "shell_command.anything"},
        _service("switch.turn_on"),
        _service("SHELL_COMMAND.anything"),
        {"event": "fired"},
        {"scene": "scene.any"},
        {
            "if": [{"condition": "state", "entity_id": "light.a", "state": "on"}],
            "then": [_service("switch.turn_on")],
        },
        {
            "if": [{"condition": "state", "entity_id": "light.a", "state": "on"}],
            "then": [],
            "else": [_service("switch.turn_on")],
        },
        {"choose": [{"conditions": [], "sequence": [_service("switch.turn_on")]}]},
        {"choose": [], "default": [_service("switch.turn_on")]},
        {"repeat": {"count": 1, "sequence": [_service("switch.turn_on")]}},
        {"parallel": [_service("switch.turn_on")]},
        {"sequence": [_service("switch.turn_on")]},
    ],
)
async def test_actions_must_be_allowed_however_nested(
    call_tool: CallTool, action: dict[str, Any]
) -> None:
    """An automation may do no more than its author."""
    assert await _refusal(call_tool, _automation(action)) == "services_not_allowed"


@pytest.mark.usefixtures("definitions")
@pytest.mark.parametrize(
    "config",
    [
        _automation(_service("{{ 'shell_command.anything' }}")),
        _automation({"service_template": "shell_command.anything"}),
        _automation({"event_template": "anything"}),
        {"use_blueprint": {"path": "x.yaml", "input": {}}},
    ],
)
async def test_what_cannot_be_checked_is_refused(
    call_tool: CallTool, config: dict[str, Any]
) -> None:
    """It could run anything, and that only shows when it runs."""
    assert await _refusal(call_tool, config) == "definition_part_refused"


@pytest.mark.usefixtures("definitions")
async def test_device_actions_must_be_allowed(
    hass: HomeAssistant, call_tool: CallTool, device_registry: dr.DeviceRegistry
) -> None:
    """A device action does what a service of its domain does."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "switch")}
    )
    action = {"device_id": device.id, "domain": "switch", "type": "turn_on"}
    assert await _refusal(call_tool, _automation(action)) == "services_not_allowed"


@pytest.mark.usefixtures("definitions")
async def test_all_refused_services_are_named_at_once(call_tool: CallTool) -> None:
    """The owner can allow them in one go, rather than one after the other."""
    config = _automation(
        _service("select.select_option"),
        _service("light.turn_on"),
        {
            "if": [],
            "then": [_service("siren.turn_on"), _service("select.select_option")],
        },
        {"event": "done"},
    )
    with pytest.raises(ServiceValidationError) as err:
        await _write(call_tool, "automation", "create", config=config, dry_run=True)
    assert err.value.translation_key == "services_not_allowed"
    assert err.value.translation_placeholders == {
        "services": "event.fire, select.select_option, siren.turn_on"
    }


@pytest.mark.usefixtures("definitions")
async def test_old_key_names_are_checked_too(call_tool: CallTool) -> None:
    """Automations also take action and trigger, in their older spelling."""
    config = {
        "alias": "Old",
        "trigger": _TRIGGER,
        "action": [{"service": "switch.turn_on"}],
    }
    assert await _refusal(call_tool, config) == "services_not_allowed"


@pytest.mark.usefixtures("definitions")
async def test_scripts_are_checked_too(call_tool: CallTool) -> None:
    """A script does as much as an automation."""
    config = {"sequence": [_service("switch.turn_on")]}
    assert await _refusal(call_tool, config, "script") == "services_not_allowed"


@pytest.mark.usefixtures("definitions")
async def test_values_passed_on_are_not_actions(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Notifications have actions of their own, and that is not a service."""
    await apply_options(
        hass,
        hass.config_entries.async_entries("llm_admin_tools")[0],
        write_subjects=["automations"],
        allowed_services=["notify"],
    )
    action = _service(
        "notify.send",
        data={"message": "x", "data": {"actions": [{"action": "OPEN", "title": "x"}]}},
    )
    trigger = [{"trigger": "homeassistant", "event": "start"}]
    await _write(
        call_tool,
        "automation",
        "create",
        config={"alias": "Notify", "triggers": trigger, "actions": [action]},
    )


@pytest.fixture
async def restricted(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> dr.DeviceEntry:
    """Have something hidden, something protected, and a device that is protected."""
    assert await async_setup_component(hass, "automation", {})
    assert await async_setup_component(hass, "scene", {})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "kept")}
    )
    device_registry.async_update_device(device.id, labels={"kept"})
    for name in ("free", "hidden", "kept", "ok"):
        hass.states.async_set(f"light.{name}", "off")
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations", "scenes"],
        allowed_services=["light"],
        excluded_entities=["light.hidden"],
        protected_entities=["light.kept"],
        protected_labels=["kept"],
    )
    return device


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"target": {"entity_id": "light.kept"}}, "entity_protected"),
        ({"target": {"entity_id": ["light.ok", "light.kept"]}}, "entity_protected"),
        ({"target": {"entity_id": "light.ok, light.kept"}}, "entity_protected"),
        ({"entity_id": "LIGHT.KEPT"}, "entity_protected"),
        ({"data": {"entity_id": "light.kept"}}, "entity_protected"),
        ({"target": {"entity_id": "light.hidden"}}, "entity_not_found"),
        ({"target": {"entity_id": "light.mistyped"}}, "entity_not_found"),
        ({"target": {"entity_id": "all"}}, "all_entities_refused"),
        ({"target": {"entity_id": "{{ 'light.kept' }}"}}, "group_target_refused"),
        ({"target": {"area_id": "any"}}, "group_target_refused"),
        ({"target": {"floor_id": "any"}}, "group_target_refused"),
        ({"target": {"label_id": "any"}}, "group_target_refused"),
        ({"target": {"device_id": "nope"}}, "device_not_found"),
        (
            {
                "target": {
                    "entity_id": "light.ok",
                    "exclude": {"entity_id": "light.hidden"},
                }
            },
            "entity_not_found",
        ),
        (
            {
                "target": {
                    "entity_id": "light.kept",
                    "exclude": {"entity_id": "light.ok"},
                }
            },
            "entity_protected",
        ),
    ],
)
async def test_targets_are_checked(
    call_tool: CallTool,
    restricted: dr.DeviceEntry,
    fields: dict[str, Any],
    expected: str,
) -> None:
    """Every way to name a target leads to the same check."""
    config = _automation(_service("light.turn_on", **fields))
    assert await _refusal(call_tool, config) == expected


@pytest.mark.skipif(not HA_2026_11, reason="Targets can exclude from 2026.11")
async def test_what_a_target_excludes_only_needs_to_be_seen(
    call_tool: CallTool, restricted: dr.DeviceEntry
) -> None:
    """Leaving a protected entity out of a target protects it."""
    target = {
        "entity_id": ["light.ok", "light.free"],
        "exclude": {"entity_id": "light.kept", "area_id": "any"},
    }
    result = await _write(
        call_tool,
        "automation",
        "create",
        config=_automation(_service("light.turn_on", target=target)),
        dry_run=True,
    )
    assert result.data["dry_run"] is True


async def test_protected_device_is_checked(
    call_tool: CallTool, restricted: dr.DeviceEntry
) -> None:
    """A device can be protected, for example through its label."""
    config = _automation(_service("light.turn_on", target={"device_id": restricted.id}))
    assert await _refusal(call_tool, config) == "device_protected"


async def test_entity_named_by_registry_id_is_checked(
    hass: HomeAssistant, call_tool: CallTool, restricted: dr.DeviceEntry
) -> None:
    """Device actions name entities by the ID of their registry entry."""
    hass.states.async_remove("light.kept")
    entry = er.async_get(hass).async_get_or_create(
        "light", "test", "kept", suggested_object_id="kept"
    )
    assert entry.entity_id == "light.kept"
    config = _automation(_service("light.turn_on", target={"entity_id": entry.id}))
    assert await _refusal(call_tool, config) == "entity_protected"


async def test_scene_entities_are_checked(
    call_tool: CallTool, restricted: dr.DeviceEntry
) -> None:
    """A scene changes what it names when it is turned on."""
    config = {"name": "Sneaky", "entities": {"light.kept": "on"}}
    assert await _refusal(call_tool, config, "scene") == "entity_protected"


@pytest.mark.parametrize(
    ("part", "expected"),
    [
        (
            {"triggers": [{"trigger": "state", "entity_id": "light.hidden"}]},
            "entity_not_found",
        ),
        (
            {"triggers": [{"trigger": "state", "entity_id": "light.mistyped"}]},
            "entity_not_found",
        ),
        (
            {
                "conditions": [
                    {"condition": "state", "entity_id": "light.hidden", "state": "on"}
                ]
            },
            "entity_not_found",
        ),
        (
            {"conditions": [{"condition": "template", "value_template": "{{ 1 }}"}]},
            "template_refused",
        ),
        ({"variables": {"x": "{{ states('light.hidden') }}"}}, "template_refused"),
    ],
)
async def test_what_is_hidden_cannot_be_read_through_a_definition(
    call_tool: CallTool,
    restricted: dr.DeviceEntry,
    part: dict[str, Any],
    expected: str,
) -> None:
    """Otherwise an automation could pass on what the client may not see."""
    assert await _refusal(call_tool, {**_automation(), **part}) == expected


async def test_protected_can_still_be_looked_at(
    call_tool: CallTool, restricted: dr.DeviceEntry
) -> None:
    """Protected means it cannot be changed, not that nothing may depend on it."""
    condition = {"condition": "state", "entity_id": "light.kept", "state": "on"}
    await _write(
        call_tool,
        "automation",
        "create",
        config={
            **_automation(
                {"if": [condition], "then": [_service("light.turn_on")]},
                condition,
            ),
            "triggers": [{"trigger": "state", "entity_id": "light.kept"}],
            "conditions": [condition],
        },
    )


async def test_group_targets_and_templates_when_nothing_is_limited(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """With nothing hidden or protected, they get around nothing."""
    assert await async_setup_component(hass, "automation", {})
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations"],
        allowed_services=["light"],
        write_allow_templates=True,
    )
    await _write(
        call_tool,
        "automation",
        "create",
        config=_automation(
            _service("light.turn_on", target={"area_id": "any"}),
            _service("light.turn_on", target={"entity_id": "{{ 'light.a' }}"}),
            _service("light.turn_on", target={"device_id": "{{ 'x' }}"}),
            _service("light.turn_on", target={"entity_id": "none"}),
        ),
    )


async def test_templated_targets_when_only_protection_is_set(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Templates may stay, but not as targets: they could reach what is protected."""
    assert await async_setup_component(hass, "automation", {})
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations"],
        allowed_services=["light"],
        protected_entities=["light.kept"],
        write_allow_templates=True,
    )
    for target in ({"entity_id": "{{ 'light.kept' }}"}, {"device_id": "{{ 'x' }}"}):
        config = _automation(_service("light.turn_on", target=target))
        assert await _refusal(call_tool, config) == "group_target_refused"
    await _write(
        call_tool,
        "automation",
        "create",
        config=_automation(
            _service(
                "light.turn_on",
                target={"entity_id": "light.free"},
                data={"brightness": "{{ 1 }}"},
            )
        ),
    )


@pytest.mark.usefixtures("definitions")
async def test_scene_action_needs_the_service_and_the_scene(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Turning on a scene is calling a service on it, written another way."""
    options = dict(init_integration.options)
    await apply_options(
        hass,
        init_integration,
        **{**options, "allowed_services": ["scene"]},
        protected_entities=["scene.kept"],
    )
    assert (
        await _refusal(call_tool, _automation({"scene": "scene.kept"}))
        == "entity_protected"
    )


async def test_hidden_device_cannot_be_looked_at_either(
    call_tool: CallTool, hass: HomeAssistant, restricted: dr.DeviceEntry
) -> None:
    """A condition on a device tells something about it."""
    await _write(
        call_tool,
        "automation",
        "create",
        config={
            **_automation(),
            "conditions": [
                {
                    "condition": "device",
                    "device_id": restricted.id,
                    "domain": "light",
                    "type": "is_on",
                    "entity_id": "light.free",
                }
            ],
        },
        dry_run=True,
    )
    config = {
        **_automation(),
        "conditions": [{"condition": "device", "device_id": "nope", "domain": "light"}],
    }
    assert await _refusal(call_tool, config) == "device_not_found"


@pytest.mark.usefixtures("definitions")
async def test_deleting_what_never_got_an_entity(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A definition in the file that was never loaded has only the file to clean up."""
    Path(hass.config.path("scripts.yaml")).write_text(
        "unloaded:\n  sequence: []\n", encoding="utf-8"
    )
    await _write(call_tool, "script", "delete", id="unloaded")
    assert load_yaml(hass.config.path("scripts.yaml")) == {}


async def test_scene_without_a_definition_is_reported(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A scene of another integration has nothing that could be read or written."""
    assert await async_setup_component(hass, "scene", {})
    await hass.async_block_till_done()

    class _OtherScene:
        pass

    component = hass.data["entity_components"]["scene"]
    with (
        patch.object(component, "get_entity", return_value=_OtherScene()),
        pytest.raises(Exception) as err,
    ):
        await call_tool("get_definition", kind="scene", entity_id="scene.other")
    assert getattr(err.value, "translation_key", None) == "definition_unavailable"


async def test_with_nothing_hidden_a_definition_may_name_what_is_not_there_yet(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Without anything to tell apart, nothing is lost by accepting it."""
    assert await async_setup_component(hass, "automation", {})
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations"],
        allowed_services=["light"],
    )
    await _write(
        call_tool,
        "automation",
        "create",
        config=_automation(_service("light.turn_on", entity_id="light.coming_soon")),
        dry_run=True,
    )
