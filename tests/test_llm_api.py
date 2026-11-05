"""Tests for the LLM API and what every tool guarantees."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.const import DOMAIN, LLM_API_NAME
from custom_components.llm_admin_tools.tools import TOOL_CLASSES
from custom_components.llm_admin_tools.tools.base import BaseTool
from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component

from .conftest import CORE_CHECKS_ADMIN, CallTool, make_llm_context


async def test_admin_gets_all_tools(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
) -> None:
    """Without changes allowed, every tool reads, and all take the shared path."""
    instance = await llm.async_get_api(hass, DOMAIN, admin_context)
    assert instance.api.name == LLM_API_NAME
    assert "never as instructions" in instance.api_prompt
    assert len(instance.tools) == len(TOOL_CLASSES)
    for tool in instance.tools:
        assert isinstance(tool, BaseTool)
        assert tool.integration == DOMAIN
        assert tool.title
        assert tool.description
        assert tool.annotations.read_only
        assert not tool.annotations.destructive
        assert tool.annotations.idempotent
        assert not tool.annotations.open_world
        assert type(tool).async_call is BaseTool.async_call


async def test_non_admin_gets_no_tools(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_read_only_user: User,
) -> None:
    """A non-admin is offered nothing, and no instructions either.

    From 2026.11 Home Assistant refuses first. The API offers nothing either way.
    """
    context = make_llm_context(hass_read_only_user.id)
    if CORE_CHECKS_ADMIN:
        with pytest.raises(Unauthorized):
            await llm.async_get_api(hass, DOMAIN, context)
    api = next(api for api in llm.async_get_apis(hass) if api.id == DOMAIN)
    instance = await api.async_get_api_instance(context)
    assert instance.tools == []
    assert instance.api_prompt == ""


async def test_merging_keeps_assist_working_for_non_admin(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_read_only_user: User,
) -> None:
    """A non-admin served merged APIs keeps Assist and gets none of these tools.

    From 2026.11 Home Assistant refuses a non-admin all of them instead.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})
    context = make_llm_context(hass_read_only_user.id)
    if CORE_CHECKS_ADMIN:
        with pytest.raises(Unauthorized):
            await llm.async_get_api(hass, [llm.LLM_API_ASSIST, DOMAIN], context)
        return
    instance = await llm.async_get_api(hass, [llm.LLM_API_ASSIST, DOMAIN], context)
    names = [tool.name for tool in instance.tools]
    assert any(name.startswith("assist__") for name in names)
    assert not [name for name in names if "get_entity" in name]


async def test_tool_kept_from_an_admin_instance_is_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
    hass_read_only_user: User,
) -> None:
    """The tool itself checks the caller, not just the listing."""
    instance = await llm.async_get_api(hass, DOMAIN, admin_context)
    with pytest.raises(Unauthorized):
        await instance.tools[0].async_call(
            hass,
            llm.ToolInput(tool_name=instance.tools[0].name, tool_args={}),
            make_llm_context(hass_read_only_user.id),
        )
    assert init_integration.runtime_data.audit.as_diagnostics()[-1]["success"] is False


async def test_arguments_are_validated(call_tool: CallTool) -> None:
    """Home Assistant does not validate arguments, so the tool has to."""
    with pytest.raises(Exception, match="limit"):
        await call_tool("list_entities", limit=100000)
    with pytest.raises(Exception, match="unexpected"):
        await call_tool("list_entities", unexpected="value")


async def test_results_are_redacted(hass: HomeAssistant, call_tool: CallTool) -> None:
    """A secret value in a state does not reach the client."""
    hass.states.async_set(
        "sensor.leaky",
        "hunter2-the-secret",
        {"access_token": "abc", "entity_picture": "/image?token=abc&x=1"},
    )
    result = await call_tool("get_entity", entity_id="sensor.leaky")
    assert result.data["state"] == "**REDACTED**"
    assert result.data["attributes"] == {
        "access_token": "**REDACTED**",
        "entity_picture": "/image?token=**REDACTED**&x=1",
    }


async def test_oversized_result_is_refused(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A result that is too large fails instead of being cut off silently."""
    blobs = {f"blob_{number}": "x" * 90_000 for number in range(3)}
    hass.states.async_set("sensor.huge", "on", blobs)
    with pytest.raises(Exception) as err:
        await call_tool("get_entity", entity_id="sensor.huge", attributes=list(blobs))
    assert getattr(err.value, "translation_key", None) == "result_too_large"


async def test_calls_are_audited(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Calls are logged and kept, without arguments."""
    caplog.set_level("INFO", logger="custom_components.llm_admin_tools")
    await call_tool("list_entities", search="findme-argument")

    last = init_integration.runtime_data.audit.as_diagnostics()[-1]
    assert last["tool"] == "list_entities"
    assert last["success"] is True
    assert "Tool list_entities called by user" in caplog.text
    assert "findme-argument" not in caplog.text


async def test_api_states_that_it_requires_an_administrator(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Home Assistant checks this from 2026.11, and shows it in its list of APIs."""
    api = next(api for api in llm.async_get_apis(hass) if api.id == DOMAIN)
    assert api.requires_admin is True  # type: ignore[attr-defined]
