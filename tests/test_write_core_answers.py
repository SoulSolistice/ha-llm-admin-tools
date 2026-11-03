"""Tests for what core says about a change, which a dry run must already tell."""

import logging
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.core_commands import (
    async_run_command,
    check_command,
)
from homeassistant.auth.models import User
from homeassistant.components.websocket_api.connection import current_connection
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    category_registry as cr,
    entity_registry as er,
)
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options

_REASON = "Asked for by the user in this test."


async def _refusal(call_tool: CallTool, tool: str, **args: Any) -> str | None:
    with pytest.raises(Exception) as err:
        await call_tool(tool, reason=_REASON, **args)
    return getattr(err.value, "translation_key", None)


@pytest.fixture(autouse=True)
async def commands(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Have the commands of core available, and everything allowed."""
    for component in ("websocket_api", "config", "input_boolean", "lovelace"):
        assert await async_setup_component(hass, component, {})
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["areas", "categories", "dashboards", "entities", "helpers"],
        write_allow_delete=True,
    )


@pytest.mark.parametrize(
    "request_args",
    [
        (
            "write_helper",
            {"kind": "input_boolean", "operation": "create", "fields": {"nope": 1}},
        ),
        (
            "write_registry_item",
            {"kind": "area", "operation": "create", "fields": {"nope": 1}},
        ),
        ("write_dashboard", {"operation": "create", "fields": {"nope": 1}}),
        (
            "write_registry_item",
            {"kind": "category", "operation": "delete", "item_id": "any"},
        ),
    ],
)
async def test_dry_run_gets_the_answer_of_core_and_nothing_is_logged(
    call_tool: CallTool,
    caplog: pytest.LogCaptureFixture,
    request_args: tuple[str, dict[str, Any]],
) -> None:
    """A mistake of a client is told to the client, and is no error of the system."""
    tool, args = request_args
    for dry_run in (True, False):
        assert (
            await _refusal(call_tool, tool, dry_run=dry_run, **args)
            == "command_refused"
        )
    errors = [r for r in caplog.get_records("call") if r.levelno >= logging.ERROR]
    assert not errors


async def test_a_command_of_something_not_loaded_is_unavailable(
    hass: HomeAssistant, hass_admin_user: User
) -> None:
    """That is known before anything runs too."""
    with pytest.raises(Exception) as err:
        check_command(hass, "counter/list")
    assert getattr(err.value, "translation_key", None) == "command_unavailable"


async def test_a_command_without_fields_needs_no_schema(hass: HomeAssistant) -> None:
    """Core takes False for a command that has nothing but a type."""
    command = "lovelace/dashboards/list"
    with patch.dict(hass.data["websocket_api"], {command: (None, False)}):
        check_command(hass, command)


async def test_command_leaves_the_context_as_it_was(
    hass: HomeAssistant, hass_admin_user: User
) -> None:
    """A connection marks the context as its own, and this one is not a connection."""
    await async_run_command(hass, hass_admin_user.id, "input_boolean/list")
    assert current_connection.get() is None


async def test_category_takes_its_scope_where_all_operations_do(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Creating, changing and deleting a category all take the same argument."""
    tool = "write_registry_item"
    created = await call_tool(
        tool,
        kind="category",
        operation="create",
        scope="automation",
        fields={"name": "Mine"},
        reason=_REASON,
    )
    category_id = created.data["result"]["category_id"]
    assert cr.async_get(hass).async_get_category(
        scope="automation", category_id=category_id
    )
    updated = await call_tool(
        tool,
        kind="category",
        operation="update",
        scope="automation",
        item_id=category_id,
        fields={"name": "Other"},
        reason=_REASON,
    )
    assert updated.data["before"]["name"] == "Mine"
    assert updated.data["summary"] == f"update category {category_id}: name, scope"
    await call_tool(
        tool,
        kind="category",
        operation="delete",
        scope="automation",
        item_id=category_id,
        reason=_REASON,
    )
    assert not cr.async_get(hass).async_list_categories(scope="automation")


@pytest.mark.parametrize(
    "item",
    [
        {"kind": "floor", "item_id": "nope"},
        {"kind": "category", "item_id": "nope", "scope": "automation"},
    ],
)
async def test_unknown_item_is_refused_by_a_dry_run_too(
    call_tool: CallTool,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    item: Any,
) -> None:
    """Without this, a dry run would call it fine, and doing it would fail."""
    await apply_options(hass, init_integration, write_subjects=["categories", "floors"])
    assert (
        await _refusal(
            call_tool,
            "write_registry_item",
            operation="update",
            fields={"name": "x"},
            dry_run=True,
            **item,
        )
        == "registry_item_not_found"
    )


async def test_summary_says_what_was_stated(
    call_tool: CallTool, hass: HomeAssistant, area_registry: ar.AreaRegistry
) -> None:
    """The journal does not show the fields this integration adds itself."""
    area = area_registry.async_create("Room")
    deleted = await call_tool(
        "write_registry_item",
        kind="area",
        operation="delete",
        item_id=area.id,
        reason=_REASON,
    )
    assert deleted.data["summary"] == f"delete area {area.id}"
    created = await call_tool(
        "write_helper",
        kind="input_boolean",
        operation="create",
        fields={"name": "Made"},
        reason=_REASON,
    )
    updated = await call_tool(
        "write_helper",
        kind="input_boolean",
        operation="update",
        item_id=created.data["result"]["id"],
        fields={"name": "Other"},
        reason=_REASON,
    )
    assert updated.data["summary"] == "update input_boolean made: name"


async def test_aliases_can_be_undone(
    call_tool: CallTool, entity_registry: er.EntityRegistry
) -> None:
    """Core keeps the name of the entity among the aliases, as a marker."""
    entity = entity_registry.async_get_or_create(
        "light", "test", "one", suggested_object_id="one"
    )
    entity_registry.async_update_entity(entity.entity_id, aliases=[er.COMPUTED_NAME])
    changed = await call_tool(
        "update_entity", entity_id="light.one", aliases=["Lamp"], reason=_REASON
    )
    assert changed.data["before"]["aliases"] == [None]
    undone = await call_tool(
        "update_entity",
        entity_id="light.one",
        aliases=changed.data["before"]["aliases"],
        reason=_REASON,
    )
    assert undone.data["before"]["aliases"] == ["Lamp"]
    current = entity_registry.async_get("light.one")
    assert current is not None
    assert list(current.aliases) == [er.COMPUTED_NAME]


async def test_dashboard_that_is_not_there(
    call_tool: CallTool, hass: HomeAssistant
) -> None:
    """Saving to one or changing one that does not exist fails at once."""
    tool = "write_dashboard"
    for args in (
        {"operation": "save_config", "url_path": "nope", "config": {"views": []}},
        {"operation": "delete_config", "url_path": "nope"},
        {"operation": "update", "dashboard_id": "nope", "fields": {"title": "x"}},
        {"operation": "delete", "dashboard_id": "nope"},
    ):
        assert (
            await _refusal(call_tool, tool, dry_run=True, **args)
            == "dashboard_not_found"
        )


async def test_dashboard_change_tells_what_was_there(call_tool: CallTool) -> None:
    """That allows undoing it."""
    created = await call_tool(
        "write_dashboard",
        operation="create",
        fields={"url_path": "made-up", "title": "Made"},
        reason=_REASON,
    )
    updated = await call_tool(
        "write_dashboard",
        operation="update",
        dashboard_id=created.data["result"]["id"],
        fields={"title": "Other"},
        reason=_REASON,
    )
    assert updated.data["before"]["title"] == "Made"
    assert (
        updated.data["summary"]
        == f"update dashboard {created.data['result']['id']}: title"
    )
