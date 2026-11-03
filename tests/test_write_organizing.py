# ruff: noqa: PLR0917
"""Tests for changing entities, devices, helpers, dashboards and integration entries."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from custom_components.llm_admin_tools.const import DOMAIN
from custom_components.llm_admin_tools.core_commands import async_run_command
from homeassistant.auth.models import User
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options

_REASON = "Asked for by the user in this test."


class _TestFlow(ConfigFlow):
    """Let core recognize the version of the entries of the test integration."""

    VERSION = 1


async def _refusal(call_tool: CallTool, tool: str, **args: Any) -> str | None:
    with pytest.raises(Exception) as err:
        await call_tool(tool, reason=_REASON, **args)
    return getattr(err.value, "translation_key", None)


@pytest.fixture
async def websocket(hass: HomeAssistant) -> None:
    """Have the commands of core available."""
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "config", {})
    await hass.async_block_till_done()


@pytest.fixture
def entity(entity_registry: er.EntityRegistry) -> er.RegistryEntry:
    """Return a registered entity."""
    return entity_registry.async_get_or_create(
        "light", "test", "one", suggested_object_id="one"
    )


@pytest.fixture
def device(hass: HomeAssistant, device_registry: dr.DeviceRegistry) -> dr.DeviceEntry:
    """Return a registered device."""
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    return device_registry.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("test", "device")}
    )


@pytest.mark.usefixtures("websocket")
async def test_update_entity(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    entity: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
) -> None:
    """Only what is stated changes, and the result allows undoing it."""
    await apply_options(hass, init_integration, write_subjects=["entities"])
    area = area_registry.async_create("Kitchen")
    result = await call_tool(
        "update_entity",
        entity_id="light.one",
        name="Named",
        area_id=area.id,
        reason=_REASON,
    )
    assert result.data["before"]["name"] is None
    assert result.data["result"]["name"] == "Named"
    changed = entity_registry.async_get("light.one")
    assert changed is not None
    assert changed.name == "Named"
    assert changed.area_id == area.id
    summary = init_integration.runtime_data.journal.recent()[-1]["summary"]
    assert summary == "update entity light.one: area_id, name"


@pytest.mark.usefixtures("websocket")
async def test_core_refuses_what_it_refuses(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    entity: er.RegistryEntry,
) -> None:
    """A change core does not accept comes back as an error that says why."""
    await apply_options(hass, init_integration, write_subjects=["entities"])
    translation_key = await _refusal(
        call_tool, "update_entity", entity_id="light.one", new_entity_id="switch.other"
    )
    assert translation_key == "command_refused"


@pytest.mark.usefixtures("websocket")
async def test_entity_limits(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    entity: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Neither what is hidden nor protected can be reached or moved into."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["entities"],
        excluded_entities=["light.hidden"],
        protected_entities=["light.kept"],
        excluded_areas=["secret_room"],
        protected_areas=["kept_room"],
        excluded_labels=["secret"],
        protected_labels=["kept"],
    )
    update = "update_entity"
    for name in ("hidden", "kept", "only_a_state"):
        hass.states.async_set(f"light.{name}", "off")
    for args, expected in (
        ({"entity_id": "light.hidden"}, "entity_not_found"),
        ({"entity_id": "light.mistyped"}, "entity_not_found"),
        ({"entity_id": "light.kept"}, "entity_protected"),
        ({"entity_id": "light.only_a_state"}, "entity_not_registered"),
        ({"entity_id": "light.one", "area_id": "mistyped"}, "area_not_found"),
        ({"entity_id": "light.one", "labels": ["mistyped"]}, "label_not_found"),
        ({"entity_id": "light.one", "area_id": "secret_room"}, "area_not_found"),
        ({"entity_id": "light.one", "area_id": "kept_room"}, "area_protected"),
        ({"entity_id": "light.one", "labels": ["secret"]}, "label_not_found"),
        ({"entity_id": "light.one", "labels": ["kept"]}, "label_protected"),
    ):
        assert await _refusal(call_tool, update, **args) == expected


@pytest.mark.usefixtures("websocket")
async def test_delete_entity(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    entity: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Deleting asks for its own permission."""
    await apply_options(hass, init_integration, write_subjects=["entities"])
    args = {"entity_id": "light.one", "operation": "delete"}
    assert await _refusal(call_tool, "update_entity", **args) == "delete_not_allowed"
    assert entity_registry.async_get("light.one") is not None

    await apply_options(
        hass, init_integration, write_subjects=["entities"], write_allow_delete=True
    )
    await call_tool("update_entity", reason=_REASON, **args)
    assert entity_registry.async_get("light.one") is None


@pytest.mark.usefixtures("websocket")
async def test_update_and_delete_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    device: dr.DeviceEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A device is changed through core, which asks its integration about deleting."""
    await apply_options(
        hass, init_integration, write_subjects=["devices"], write_allow_delete=True
    )
    result = await call_tool(
        "update_device", device_id=device.id, name_by_user="Mine", reason=_REASON
    )
    assert result.data["before"]["name_by_user"] is None
    changed = device_registry.async_get(device.id)
    assert changed is not None
    assert changed.name_by_user == "Mine"
    # The integration of this device does not support removing devices.
    translation_key = await _refusal(
        call_tool, "update_device", device_id=device.id, operation="delete"
    )
    assert translation_key == "command_refused"
    assert (
        await _refusal(call_tool, "update_device", device_id="nope")
        == "device_not_found"
    )


@pytest.mark.usefixtures("websocket")
async def test_protected_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    device: dr.DeviceEntry,
) -> None:
    """Protecting an integration protects its devices, and hiding hides them."""
    options = {"write_subjects": ["devices"]}
    await apply_options(
        hass,
        init_integration,
        **options,
        protected_integrations=[device.config_entry_id],
    )
    translation_key = await _refusal(call_tool, "update_device", device_id=device.id)
    assert translation_key == "device_protected"
    await apply_options(
        hass,
        init_integration,
        **options,
        excluded_integrations=[device.config_entry_id],
    )
    translation_key = await _refusal(call_tool, "update_device", device_id=device.id)
    assert translation_key == "device_not_found"


@pytest.mark.usefixtures("websocket")
@pytest.mark.parametrize(
    ("kind", "subject", "fields", "new_fields"),
    [
        ("area", "areas", {"name": "Room"}, {"name": "Room 2"}),
        ("floor", "floors", {"name": "Up", "level": 1}, {"name": "Up 2"}),
        ("label", "labels", {"name": "Tag"}, {"name": "Tag 2"}),
        (
            "category",
            "categories",
            {"name": "Cat", "scope": "automation"},
            {"name": "2"},
        ),
    ],
)
async def test_registry_item_life_cycle(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    kind: str,
    subject: str,
    fields: dict[str, Any],
    new_fields: dict[str, Any],
) -> None:
    """Create, change and delete go the same way for all of them."""
    await apply_options(
        hass, init_integration, write_subjects=[subject], write_allow_delete=True
    )
    tool = "write_registry_item"
    created = await call_tool(
        tool, kind=kind, operation="create", fields=fields, reason=_REASON
    )
    item_id = created.data["result"][f"{kind}_id"]
    scope = {"scope": fields["scope"]} if "scope" in fields else {}
    updated = await call_tool(
        tool,
        kind=kind,
        operation="update",
        item_id=item_id,
        fields=new_fields,
        reason=_REASON,
        **scope,
    )
    assert updated.data["result"]["name"] == new_fields["name"]
    await call_tool(
        tool, kind=kind, operation="delete", item_id=item_id, reason=_REASON, **scope
    )


@pytest.mark.usefixtures("websocket")
async def test_registry_items_can_be_read(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """What is hidden is left out; a category needs its scope."""
    floor_registry.async_create("Up", aliases={"Top"})
    shown = label_registry.async_create("Shown")
    hidden = label_registry.async_create("Hidden")
    await apply_options(hass, init_integration, excluded_labels=[hidden.label_id])

    floors = await call_tool("list_registry_items", kind="floor")
    assert floors.data["items"][0]["aliases"] == ["Top"]
    labels = await call_tool("list_registry_items", kind="label")
    assert [item["label_id"] for item in labels.data["items"]] == [shown.label_id]
    with pytest.raises(Exception) as err:
        await call_tool("list_registry_items", kind="category")
    assert getattr(err.value, "translation_key", None) == "category_scope_required"
    categories = await call_tool("list_registry_items", kind="category", scope="x")
    assert categories.data["items"] == []


@pytest.mark.usefixtures("websocket")
async def test_registry_item_limits(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    entity_registry: er.EntityRegistry,
    entity: er.RegistryEntry,
) -> None:
    """Protected items stay as they are, with what they stand for."""
    floor = floor_registry.async_create("Up")
    area_registry.async_create("Elsewhere")
    kept = area_registry.async_create("Kept", floor_id=floor.floor_id)
    busy = area_registry.async_create("Busy")
    label = lr.async_get(hass).async_create("Busy label").label_id
    entity_registry.async_update_entity("light.one", area_id=busy.id, labels={label})
    await apply_options(
        hass,
        init_integration,
        write_subjects=["areas", "floors", "labels"],
        write_allow_delete=True,
        protected_areas=[kept.id],
        protected_entities=["light.one"],
        excluded_entities=["light.hidden"],
    )
    tool = "write_registry_item"
    for args, expected in (
        ({"kind": "area", "item_id": kept.id}, "area_protected"),
        ({"kind": "floor", "item_id": floor.floor_id}, "area_protected"),
    ):
        assert await _refusal(call_tool, tool, operation="update", **args) == expected
    for args in (
        {"kind": "area", "item_id": busy.id},
        {"kind": "label", "item_id": label},
    ):
        assert (
            await _refusal(call_tool, tool, operation="delete", **args)
            == "in_use_by_protected"
        )
    assert (
        await _refusal(call_tool, tool, kind="area", operation="update")
        == "registry_item_id_required"
    )


@pytest.mark.usefixtures("websocket")
async def test_registry_item_cannot_refer_to_what_is_hidden(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A new item cannot be linked to what the client may not see."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["areas"],
        excluded_entities=["sensor.hidden"],
        excluded_labels=["secret"],
    )
    tool = "write_registry_item"
    assert (
        await _refusal(
            call_tool,
            tool,
            kind="area",
            operation="create",
            fields={"name": "New", "temperature_entity_id": "sensor.hidden"},
        )
        == "entity_not_found"
    )
    assert (
        await _refusal(
            call_tool,
            tool,
            kind="area",
            operation="create",
            fields={"name": "New", "labels": ["secret"]},
        )
        == "label_not_found"
    )


@pytest.mark.usefixtures("websocket")
async def test_each_kind_of_item_has_its_own_subject(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Allowing labels does not allow areas."""
    await apply_options(hass, init_integration, write_subjects=["labels"])
    assert (
        await _refusal(
            call_tool,
            "write_registry_item",
            kind="area",
            operation="create",
            fields={"name": "New"},
        )
        == "write_subject_off"
    )


@pytest.fixture
async def helpers(hass: HomeAssistant, websocket: None) -> None:
    """Have helpers available."""
    assert await async_setup_component(hass, "input_boolean", {})
    assert await async_setup_component(hass, "counter", {})
    assert await async_setup_component(hass, "person", {})
    await hass.async_block_till_done()


@pytest.mark.usefixtures("helpers")
async def test_helper_life_cycle(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A helper made through core exists at once, and can be changed and deleted."""
    await apply_options(
        hass, init_integration, write_subjects=["helpers"], write_allow_delete=True
    )
    created = await call_tool(
        "write_helper",
        kind="input_boolean",
        operation="create",
        fields={"name": "Made"},
        reason=_REASON,
    )
    item_id = created.data["result"]["id"]
    await hass.async_block_till_done()
    assert hass.states.get("input_boolean.made") is not None

    listed = await call_tool("list_helpers", kind="input_boolean")
    assert listed.data["items"][0]["entity_id"] == "input_boolean.made"

    updated = await call_tool(
        "write_helper",
        kind="input_boolean",
        operation="update",
        item_id=item_id,
        fields={"name": "Changed"},
        reason=_REASON,
    )
    assert updated.data["before"]["name"] == "Made"
    await call_tool(
        "write_helper",
        kind="input_boolean",
        operation="delete",
        item_id=item_id,
        reason=_REASON,
    )
    await hass.async_block_till_done()
    assert hass.states.get("input_boolean.made") is None


@pytest.mark.usefixtures("helpers")
async def test_helper_limits(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A protected one stays, a hidden one looks like it does not exist."""
    await apply_options(hass, init_integration, write_subjects=["helpers"])
    created = await call_tool(
        "write_helper",
        kind="counter",
        operation="create",
        fields={"name": "Made"},
        reason=_REASON,
    )
    item_id = created.data["result"]["id"]
    await hass.async_block_till_done()

    args = {"kind": "counter", "operation": "update", "fields": {"name": "x"}}
    assert (
        await _refusal(call_tool, "write_helper", **args) == "registry_item_id_required"
    )
    assert (
        await _refusal(call_tool, "write_helper", item_id="nope", **args)
        == "helper_not_found"
    )
    await apply_options(
        hass,
        init_integration,
        write_subjects=["helpers"],
        protected_entities=["counter.made"],
    )
    assert (
        await _refusal(call_tool, "write_helper", item_id=item_id, **args)
        == "entity_protected"
    )
    await apply_options(
        hass,
        init_integration,
        write_subjects=["helpers"],
        excluded_entities=["counter.made"],
    )
    listed = await call_tool("list_helpers", kind="counter")
    assert listed.data["items"] == []
    assert (
        await _refusal(call_tool, "write_helper", item_id=item_id, **args)
        == "helper_not_found"
    )


@pytest.mark.usefixtures("helpers")
async def test_persons_have_their_own_subject(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A person is more than a helper."""
    await apply_options(hass, init_integration, write_subjects=["helpers"])
    args = {"kind": "person", "operation": "create", "fields": {"name": "P"}}
    assert await _refusal(call_tool, "write_helper", **args) == "write_subject_off"


@pytest.mark.usefixtures("helpers")
async def test_person_trackers_are_kept(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Core clears them if left out, and hidden ones cannot be named by a client."""
    await apply_options(hass, init_integration, write_subjects=["persons"])
    trackers = ["device_tracker.shown", "device_tracker.hidden"]
    created = await call_tool(
        "write_helper",
        kind="person",
        operation="create",
        fields={"name": "Pat", "device_trackers": trackers},
        reason=_REASON,
    )
    item_id = created.data["result"]["id"]
    await hass.async_block_till_done()
    assert (
        await _refusal(
            call_tool,
            "write_helper",
            kind="person",
            operation="create",
            fields={"name": "Sam", "user_id": "someone"},
        )
        == "person_user_refused"
    )

    await apply_options(
        hass,
        init_integration,
        write_subjects=["persons"],
        excluded_entities=["device_tracker.hidden"],
    )
    listed = await call_tool("list_helpers", kind="person")
    assert listed.data["items"][0]["device_trackers"] == ["device_tracker.shown"]

    updated = await call_tool(
        "write_helper",
        kind="person",
        operation="update",
        item_id=item_id,
        fields={"name": "Renamed"},
        reason=_REASON,
    )
    assert updated.data["before"]["device_trackers"] == ["device_tracker.shown"]
    assert updated.data["result"]["device_trackers"] == ["device_tracker.shown"]
    hass.states.async_set("device_tracker.shown", "home")
    replaced = await call_tool(
        "write_helper",
        kind="person",
        operation="update",
        item_id=item_id,
        fields={"name": "Pat", "device_trackers": ["device_tracker.shown"]},
        reason=_REASON,
    )
    assert replaced.data["result"]["device_trackers"] == ["device_tracker.shown"]
    for tracker in ("device_tracker.hidden", "device_tracker.mistyped"):
        assert (
            await _refusal(
                call_tool,
                "write_helper",
                kind="person",
                operation="update",
                item_id=item_id,
                fields={"name": "Pat", "device_trackers": [tracker]},
            )
            == "entity_not_found"
        )

    await apply_options(
        hass, init_integration, write_subjects=["persons"], write_allow_delete=True
    )
    listed = await call_tool("list_helpers", kind="person")
    assert listed.data["items"][0]["device_trackers"] == trackers
    await call_tool(
        "write_helper",
        kind="person",
        operation="delete",
        item_id=item_id,
        reason=_REASON,
    )
    assert (await call_tool("list_helpers", kind="person")).data["items"] == []


@pytest.fixture
async def lovelace(hass: HomeAssistant, websocket: None) -> None:
    """Have dashboards available."""
    assert await async_setup_component(hass, "lovelace", {})
    await hass.async_block_till_done()


@pytest.mark.usefixtures("lovelace")
async def test_dashboard_life_cycle(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A dashboard is made, filled, read, emptied and deleted."""
    await apply_options(
        hass, init_integration, write_subjects=["dashboards"], write_allow_delete=True
    )
    tool = "write_dashboard"
    created = await call_tool(
        tool,
        operation="create",
        fields={"url_path": "made-up", "title": "Made"},
        reason=_REASON,
    )
    dashboard_id = created.data["result"]["id"]
    listed = await call_tool("list_dashboards")
    assert [item["url_path"] for item in listed.data["items"]] == ["made-up"]

    config = {"views": [{"title": "One"}]}
    saved = await call_tool(
        tool,
        operation="save_config",
        url_path="made-up",
        config=config,
        reason=_REASON,
    )
    assert "before" not in saved.data
    read = await call_tool("get_dashboard", url_path="made-up")
    assert read.data["config"] == config

    again = await call_tool(
        tool,
        operation="save_config",
        url_path="made-up",
        config={"views": []},
        reason=_REASON,
    )
    assert again.data["before"] == config
    await call_tool(tool, operation="delete_config", url_path="made-up", reason=_REASON)
    await call_tool(
        tool,
        operation="update",
        dashboard_id=dashboard_id,
        fields={"title": "Renamed"},
        reason=_REASON,
    )
    await call_tool(tool, operation="delete", dashboard_id=dashboard_id, reason=_REASON)
    assert (await call_tool("list_dashboards")).data["items"] == []


@pytest.mark.usefixtures("lovelace")
async def test_dashboard_requests_that_cannot_be_right(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Deleting needs its permission, and each operation what it acts on."""
    await apply_options(hass, init_integration, write_subjects=["dashboards"])
    tool = "write_dashboard"
    assert (
        await _refusal(call_tool, tool, operation="save_config")
        == "dashboard_path_required"
    )
    assert (
        await _refusal(call_tool, tool, operation="save_config", url_path="a-b")
        == "dashboard_config_required"
    )
    assert (
        await _refusal(call_tool, tool, operation="update")
        == "registry_item_id_required"
    )
    assert (
        await _refusal(call_tool, tool, operation="delete_config")
        == "delete_not_allowed"
    )
    assert await _refusal(call_tool, tool, operation="delete") == "delete_not_allowed"


async def test_config_entry_operations(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    websocket: None,
) -> None:
    """An entry can be reloaded, turned off and on, renamed and deleted."""
    mock_integration(
        hass,
        MockModule(
            "test",
            async_setup_entry=AsyncMock(return_value=True),
            async_unload_entry=AsyncMock(return_value=True),
        ),
    )
    mock_platform(hass, "test.config_flow", None)
    source = MockConfigEntry(domain="test", title="Before")
    source.add_to_hass(hass)
    with mock_config_flow("test", _TestFlow):
        await _run_config_entry_operations(hass, init_integration, call_tool, source)


async def _run_config_entry_operations(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    source: MockConfigEntry,
) -> None:
    await hass.config_entries.async_setup(source.entry_id)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        write_allow_delete=True,
    )
    tool = "manage_config_entry"
    args = {"entry_id": source.entry_id, "reason": _REASON}

    reloaded = await call_tool(tool, operation="reload", **args)
    assert reloaded.data["before"]["title"] == "Before"
    assert source.state is ConfigEntryState.LOADED

    await call_tool(tool, operation="disable", **args)
    assert source.disabled_by == "user"
    await call_tool(tool, operation="enable", **args)
    assert source.disabled_by is None

    await call_tool(tool, operation="update", fields={"title": "After"}, **args)
    assert source.title == "After"

    await call_tool(tool, operation="delete", **args)
    assert hass.config_entries.async_get_entry(source.entry_id) is None


async def test_config_entry_limits(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    websocket: None,
) -> None:
    """A client cannot touch what limits it, and the owner can protect more."""
    hidden = MockConfigEntry(domain="test")
    hidden.add_to_hass(hass)
    kept = MockConfigEntry(domain="test")
    kept.add_to_hass(hass)
    mcp = MockConfigEntry(domain="mcp_server")
    mcp.add_to_hass(hass)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        write_allow_delete=True,
        excluded_integrations=[hidden.entry_id],
        protected_integrations=[kept.entry_id],
    )
    tool = "manage_config_entry"
    for entry_id, expected in (
        ("nope", "config_entry_not_found"),
        (hidden.entry_id, "config_entry_not_found"),
        (kept.entry_id, "config_entry_protected"),
        (mcp.entry_id, "config_entry_protected"),
        (init_integration.entry_id, "config_entry_protected"),
    ):
        for operation in ("disable", "delete"):
            assert (
                await _refusal(call_tool, tool, entry_id=entry_id, operation=operation)
                == expected
            )
    assert hass.config_entries.async_get_entry(init_integration.entry_id) is not None
    assert init_integration.domain == DOMAIN


async def test_command_answering_too_late(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    websocket: None,
    entity: er.RegistryEntry,
) -> None:
    """A command may still take effect, and the client is told so."""
    await apply_options(hass, init_integration, write_subjects=["entities"])
    with (
        patch(
            "custom_components.llm_admin_tools.core_commands.CORE_COMMAND_TIMEOUT", 0
        ),
        patch(
            "homeassistant.components.websocket_api.connection.ActiveConnection.async_handle"
        ),
    ):
        translation_key = await _refusal(
            call_tool, "update_entity", entity_id="light.one", name="x"
        )
    assert translation_key == "command_timed_out"


async def test_command_answers_only_once(
    hass: HomeAssistant, hass_admin_user: User, websocket: None
) -> None:
    """Core can send its answer in encoded form, and only the first one counts."""

    def answer_twice(connection: ActiveConnection, message: dict[str, Any]) -> None:
        connection.send_message(b'{"id": 1, "success": true, "result": 1}')
        connection.send_message(b'{"id": 1, "success": true, "result": 2}')

    assert await async_setup_component(hass, "counter", {})
    with patch.object(ActiveConnection, "async_handle", answer_twice):
        result = await async_run_command(hass, hass_admin_user.id, "counter/list")
    assert result == 1


@pytest.mark.usefixtures("websocket")
@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("counter", {"name": "Made"}),
        ("input_boolean", {"name": "Made"}),
        ("input_button", {"name": "Made"}),
        ("input_datetime", {"name": "Made", "has_date": True, "has_time": False}),
        ("input_number", {"name": "Made", "min": 0, "max": 10}),
        ("input_select", {"name": "Made", "options": ["a", "b"]}),
        ("input_text", {"name": "Made"}),
        ("schedule", {"name": "Made", "monday": [{"from": "07:00", "to": "08:00"}]}),
        ("tag", {"name": "Made", "tag_id": "made"}),
        ("timer", {"name": "Made"}),
        ("zone", {"name": "Made", "latitude": 1.5, "longitude": 2.5}),
    ],
)
async def test_every_kind_of_helper_works_through_core(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    kind: str,
    fields: dict[str, Any],
) -> None:
    """All of them are reached the same way, and can be read back."""
    assert await async_setup_component(hass, kind, {})
    await hass.async_block_till_done()
    await apply_options(
        hass, init_integration, write_subjects=["helpers"], write_allow_delete=True
    )
    tool = "write_helper"
    created = await call_tool(
        tool, kind=kind, operation="create", fields=fields, reason=_REASON
    )
    item_id = created.data["result"]["id"]
    await hass.async_block_till_done()
    listed = await call_tool("list_helpers", kind=kind)
    # A tag's is withheld, and whoever made it knows it.
    expected = None if kind == "tag" else item_id
    assert [item.get("id") for item in listed.data["items"]] == [expected]

    new_fields = {key: value for key, value in fields.items() if key != "tag_id"}
    updated = await call_tool(
        tool,
        kind=kind,
        operation="update",
        item_id=item_id,
        fields={**new_fields, "name": "Renamed"},
        reason=_REASON,
    )
    assert updated.data["result"]["name"] == "Renamed"
    await call_tool(
        tool, kind=kind, operation="delete", item_id=item_id, reason=_REASON
    )
    assert (await call_tool("list_helpers", kind=kind)).data["items"] == []


@pytest.mark.usefixtures("helpers")
async def test_domain_rules_hold_without_an_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """What does not exist yet has no entity, but it does have a domain."""
    args = {"kind": "counter", "operation": "create", "fields": {"name": "x"}}
    await apply_options(
        hass,
        init_integration,
        write_subjects=["helpers"],
        protected_domains=["counter"],
    )
    assert await _refusal(call_tool, "write_helper", **args) == "domain_protected"
    await apply_options(
        hass,
        init_integration,
        write_subjects=["helpers"],
        excluded_domains=["counter"],
    )
    assert await _refusal(call_tool, "write_helper", **args) == "write_subject_off"
    assert (await call_tool("list_helpers", kind="counter")).data["items"] == []
