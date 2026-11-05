"""Tests for what every change goes through, whatever it changes."""

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.llm_admin_tools.const import DOMAIN
from custom_components.llm_admin_tools.core_commands import async_run_command
from custom_components.llm_admin_tools.policy.limits import MAX_WRITES_PER_MINUTE
from custom_components.llm_admin_tools.tools import build_tools
from custom_components.llm_admin_tools.tools.base import BaseTool
from custom_components.llm_admin_tools.tools.catalog import WRITE_TOOL_CLASSES
from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options, make_llm_context

_REASON = "Asked for by the user in this test."
_LIGHT_ON: dict[str, Any] = {
    "domain": "light",
    "service": "turn_on",
    "entity_ids": ["light.a"],
    "reason": _REASON,
}


@pytest.fixture(autouse=True)
def light(hass: HomeAssistant) -> None:
    """Have the entity that the changes in these tests act on."""
    hass.states.async_set("light.a", "off")


async def _allow_services(
    hass: HomeAssistant, entry: MockConfigEntry, *services: str, **options: Any
) -> None:
    await apply_options(
        hass,
        entry,
        write_subjects=["services"],
        allowed_services=list(services),
        **options,
    )


async def test_nothing_can_change_by_default(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
) -> None:
    """Without a choice by the owner, no tool that changes anything is offered."""
    instance = await llm.async_get_api(hass, DOMAIN, admin_context)
    assert all(tool.annotations.read_only for tool in instance.tools)
    assert "They only read" in instance.api_prompt
    with pytest.raises(HomeAssistantError):
        await instance.async_call_tool(
            llm.ToolInput(tool_name="call_service", tool_args=_LIGHT_ON)
        )


async def test_enabled_tools_do_not_enable_changes(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Choosing all tools in the tools step says nothing about changes."""
    await apply_options(hass, init_integration, enabled_tools=["call_service"])
    assert build_tools(init_integration.runtime_data) == []


@pytest.mark.parametrize(
    ("subject", "tool_names"),
    [
        ("services", {"call_service"}),
        ("automations", {"write_definition"}),
        ("scripts", {"write_definition"}),
        ("scenes", {"write_definition"}),
        ("entities", {"update_entity"}),
        ("devices", {"update_device"}),
        ("areas", {"write_registry_item"}),
        ("floors", {"write_registry_item"}),
        ("labels", {"write_registry_item"}),
        ("categories", {"write_registry_item"}),
        ("helpers", {"write_helper"}),
        ("persons", {"write_helper"}),
        ("dashboards", {"write_dashboard"}),
        ("config_entries", {"manage_config_entry"}),
    ],
)
async def test_each_subject_adds_only_its_tools(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
    subject: str,
    tool_names: set[str],
) -> None:
    """The prompt says changes are possible, and the tools say what they are."""
    await apply_options(hass, init_integration, write_subjects=[subject])
    instance = await llm.async_get_api(hass, DOMAIN, admin_context)
    writing = {tool.name for tool in instance.tools if not tool.annotations.read_only}
    assert writing == tool_names
    assert "allowed some changes" in instance.api_prompt


def test_write_tools_share_the_one_path() -> None:
    """No tool that changes something has a path of its own."""
    for tool_class in WRITE_TOOL_CLASSES:
        assert tool_class.async_call is BaseTool.async_call
        assert tool_class.annotations.destructive
        assert not tool_class.annotations.read_only


async def test_write_tool_refuses_a_non_admin(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
    hass_read_only_user: User,
) -> None:
    """A tool kept from an admin's list still checks who calls it."""
    await _allow_services(hass, init_integration, "light")
    calls = async_mock_service(hass, "light", "turn_on")
    instance = await llm.async_get_api(hass, DOMAIN, admin_context)
    tool = next(tool for tool in instance.tools if tool.name == "call_service")
    with pytest.raises(Unauthorized):
        await tool.async_call(
            hass,
            llm.ToolInput(tool_name=tool.name, tool_args=_LIGHT_ON),
            make_llm_context(hass_read_only_user.id),
        )
    assert not calls


async def test_a_reason_is_required(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A change has to say why, in more than a word."""
    await _allow_services(hass, init_integration, "light")
    async_mock_service(hass, "light", "turn_on")
    args = {key: value for key, value in _LIGHT_ON.items() if key != "reason"}
    with pytest.raises(Exception, match="reason"):
        await call_tool("call_service", **args)
    with pytest.raises(Exception, match="reason"):
        await call_tool("call_service", **args, reason="x")


async def test_dry_run_changes_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A dry run reports what would happen, and leaves no trace in the journal."""
    await _allow_services(hass, init_integration, "light")
    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.a", "off")
    result = await call_tool("call_service", **_LIGHT_ON, dry_run=True)
    assert result.data == {
        "dry_run": True,
        "summary": "light.turn_on on light.a",
        "before": {"light.a": "off"},
    }
    assert not calls
    assert not init_integration.runtime_data.journal.recent()


async def test_dry_run_is_refused_like_the_real_thing(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A dry run that passes is one that would have passed."""
    await _allow_services(hass, init_integration, "switch")
    with pytest.raises(Exception) as err:
        await call_tool("call_service", **_LIGHT_ON, dry_run=True)
    assert getattr(err.value, "translation_key", None) == "service_not_allowed"


@pytest.mark.parametrize(
    "data",
    [
        {"message": "**REDACTED**"},
        {"nested": {"deep": ["fine", "**REDACTED**"]}},
    ],
)
async def test_a_withheld_value_cannot_be_written_back(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    data: dict[str, Any],
) -> None:
    """Saving a result as read would replace the real value with the marker."""
    await _allow_services(hass, init_integration, "light")
    calls = async_mock_service(hass, "light", "turn_on")
    with pytest.raises(Exception) as err:
        await call_tool("call_service", **_LIGHT_ON, data=data)
    assert getattr(err.value, "translation_key", None) == "redacted_value"
    assert not calls


async def test_changes_are_journaled(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """The journal says what changed and why, with credentials taken out."""
    await _allow_services(hass, init_integration, "light")
    calls = async_mock_service(hass, "light", "turn_on")
    result = await call_tool(
        "call_service",
        **{**_LIGHT_ON, "reason": "User wants it, hunter2-the-secret  stays out."},
    )
    record = init_integration.runtime_data.journal.recent()[-1]
    # Logbook and Action history know the call by it.
    assert record["context_id"] == result.data["context_id"] == calls[0].context.id
    assert record["tool"] == "call_service"
    assert record["summary"] == "light.turn_on on light.a"
    assert record["success"] is True
    assert record["reason"] == "User wants it, **REDACTED** stays out."
    assert "user_id" not in record


async def test_failed_change_is_journaled(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A change that was tried and failed may have done part of it."""
    await _allow_services(hass, init_integration, "light")

    async def fail(call: Any) -> None:
        raise HomeAssistantError("It broke")

    hass.services.async_register("light", "turn_on", fail)
    with pytest.raises(HomeAssistantError):
        await call_tool("call_service", **_LIGHT_ON)
    assert init_integration.runtime_data.journal.recent()[-1]["success"] is False


async def test_refused_change_is_not_a_change(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """What was refused is in the record of calls, but changed nothing."""
    await _allow_services(hass, init_integration, "switch")
    with pytest.raises(HomeAssistantError):
        await call_tool("call_service", **_LIGHT_ON)
    runtime = init_integration.runtime_data
    assert not runtime.journal.recent()
    last = runtime.audit.as_diagnostics()[-1]
    assert last["success"] is False
    assert last["reason"] == _REASON


async def test_journal_survives_a_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Reads cannot push out changes, and a restart does not lose them."""
    await _allow_services(hass, init_integration, "light")
    async_mock_service(hass, "light", "turn_on")
    await call_tool("call_service", **_LIGHT_ON)
    for _ in range(101):
        await call_tool("list_areas")
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    records = init_integration.runtime_data.journal.recent()
    assert [record["tool"] for record in records] == ["call_service"]


async def test_journal_is_removed_with_the_integration(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    hass_storage: dict[str, Any],
) -> None:
    """Nothing stays behind."""
    await _allow_services(hass, init_integration, "light")
    async_mock_service(hass, "light", "turn_on")
    await call_tool("call_service", **_LIGHT_ON)
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert f"{DOMAIN}.write_journal" in hass_storage
    assert await hass.config_entries.async_remove(init_integration.entry_id)
    assert f"{DOMAIN}.write_journal" not in hass_storage


async def test_changes_are_limited_in_rate(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A loop in a client stops, and a minute later it can go on."""
    await _allow_services(hass, init_integration, "light")
    async_mock_service(hass, "light", "turn_on")
    with patch("custom_components.llm_admin_tools.journal.time.monotonic") as monotonic:
        monotonic.return_value = 1000
        for _ in range(MAX_WRITES_PER_MINUTE):
            await call_tool("call_service", **_LIGHT_ON)
        with pytest.raises(Exception) as err:
            await call_tool("call_service", **_LIGHT_ON)
        assert getattr(err.value, "translation_key", None) == "write_rate_limited"
        monotonic.return_value = 1061
        await call_tool("call_service", **_LIGHT_ON)


async def test_only_listed_commands_can_be_run(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_admin_user: User
) -> None:
    """Nothing reaches the commands for users, tokens or dashboard resources."""
    for command in ("config/auth/delete", "auth/long_lived_access_token", "get_states"):
        with pytest.raises(HomeAssistantError) as err:
            await async_run_command(hass, hass_admin_user.id, command)
        assert getattr(err.value, "translation_key", None) == "command_not_allowed"


async def test_command_needs_a_known_user(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Without a user, or before the websocket API runs, there is nobody to act for."""
    with pytest.raises(HomeAssistantError) as err:
        await async_run_command(hass, None, "counter/list")
    assert getattr(err.value, "translation_key", None) == "command_unavailable"
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "counter", {})
    with pytest.raises(HomeAssistantError) as err:
        await async_run_command(hass, None, "counter/list")
    assert getattr(err.value, "translation_key", None) == "command_unavailable"
