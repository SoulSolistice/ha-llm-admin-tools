"""Tests for reading which actions were called."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component

from .conftest import (
    HA_2026_11,
    SECRET_VALUE,
    CallTool,
    apply_options,
    async_setup_logbook,
)

_TOOL = "get_action_history"
_QUERY = "custom_components.llm_admin_tools.queries.action_history"
_START = "2027-01-15T00:00:00+00:00"
_END = "2027-01-16T00:00:00+00:00"
_REASON = "Asked for by the user in this test."


async def _calls(call_tool: CallTool, **args: Any) -> dict[str, Any]:
    return dict((await call_tool(_TOOL, **args)).data)


async def _unavailable(call_tool: CallTool) -> dict[str, str]:
    report = (await call_tool("get_capabilities")).data
    return dict(report["tools"]["unavailable"])


@pytest.mark.skipif(HA_2026_11, reason="Home Assistant keeps it from 2026.11")
@pytest.mark.usefixtures("init_integration")
async def test_needs_2026_11(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Before, there is nothing to read, and the report says why."""
    assert "2026.11" in (await _unavailable(call_tool))[_TOOL]
    with pytest.raises(HomeAssistantError) as err:
        await call_tool(_TOOL)
    assert err.value.translation_key == "command_unavailable"


@pytest.mark.skipif(not HA_2026_11, reason="Home Assistant keeps it from 2026.11")
async def test_lists_who_called_what(
    recorder_mock: Any,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_admin_user: User,
    call_tool: CallTool,
) -> None:
    """Calls from anywhere, newest first, with what a client can tell them by."""
    await async_setup_logbook(hass)
    await hass.async_block_till_done()
    assert _TOOL not in await _unavailable(call_tool)
    async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "alarm_control_panel", "alarm_disarm")
    hass.states.async_set("light.a", "off")
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_disarm",
        {"entity_id": "alarm_control_panel.home", "code": SECRET_VALUE},
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    await hass.services.async_call("light", "turn_on", {}, blocking=True)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["light"],
    )
    done = await call_tool(
        "call_service",
        domain="light",
        service="turn_on",
        entity_ids=["light.a"],
        reason=_REASON,
    )
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    calls = await _calls(call_tool)
    assert [call["action"] for call in calls["items"]] == [
        "light.turn_on",
        "light.turn_on",
        "alarm_control_panel.alarm_disarm",
    ]
    ours, other, disarm = calls["items"]
    assert ours["context_id"] == done.data["context_id"]
    assert ours["through_these_tools"] is True
    assert ours["by_a_user"] is True
    assert "through_these_tools" not in other
    assert "by_a_user" not in other
    assert disarm["by_a_user"] is True
    assert disarm["data"]["entity_id"] == "alarm_control_panel.home"
    assert SECRET_VALUE not in str(calls)

    only = await _calls(call_tool, action="alarm_control_panel")
    assert [call["action"] for call in only["items"]] == [disarm["action"]]
    page = await _calls(call_tool, action="light.turn_on", limit=1)
    assert page["total_count"] == 2
    assert page["next_offset"] == 1


@pytest.mark.skipif(not HA_2026_11, reason="Home Assistant keeps it from 2026.11")
async def test_says_what_a_call_ran_in(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A call from an automation names it, so a client can tell why it happened."""
    await async_setup_logbook(hass)
    async_mock_service(hass, "light", "turn_on")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "evening",
                "alias": "Evening",
                "triggers": [{"trigger": "event", "event_type": "evening"}],
                "actions": [{"action": "light.turn_on"}],
            }
        },
    )
    await hass.async_block_till_done()
    hass.bus.async_fire("evening")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    (call,) = (await _calls(call_tool))["items"]
    assert call["caused_by"]["entity_id"] == "automation.evening"
    assert "context_id" not in call["caused_by"]


@pytest.mark.usefixtures("init_integration")
async def test_reads_what_core_returns(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Only the calls, newest first; what changed in them is history's to tell."""
    entries = [
        {
            "when": 1_800_000_000.0,
            "domain": "light",
            "service": "turn_on",
            "service_data": {},
            "context_id": "01OLDER",
        },
        {"when": 1_800_000_001.0, "entity_id": "light.a", "state": "on"},
        {
            "when": 1_800_000_002.0,
            "domain": "switch",
            "service": "turn_off",
            "service_data": {"entity_id": "switch.b"},
            "context_id": "01NEWER",
            "context_user_id": "abc",
            "context_entity_id": "automation.night",
            "context_event_type": "automation_triggered",
        },
    ]
    run = AsyncMock(return_value=entries)
    with (
        patch(f"{_QUERY}.async_run_command", run),
        patch(
            "custom_components.llm_admin_tools.tools.action_history."
            "action_history_kept",
            return_value=True,
        ),
    ):
        assert _TOOL not in await _unavailable(call_tool)
        calls = await _calls(call_tool, start=_START, end=_END)
        only = await _calls(call_tool, action="light")
        await apply_options(hass, init_integration, excluded_domains=["camera"])
        assert "hidden" in (await _unavailable(call_tool))[_TOOL]

    newer, older = calls["items"]
    assert newer == {
        "when": "2027-01-15T08:00:02+00:00",
        "action": "switch.turn_off",
        "data": {"entity_id": "switch.b"},
        "by_a_user": True,
        "caused_by": {
            "entity_id": "automation.night",
            "event_type": "automation_triggered",
        },
        "context_id": "01NEWER",
    }
    assert older == {
        "when": "2027-01-15T08:00:00+00:00",
        "action": "light.turn_on",
        "context_id": "01OLDER",
    }
    assert [call["action"] for call in only["items"]] == ["light.turn_on"]
    assert run.await_args_list[0].args[2:] == (
        "logbook/get_action_calls",
        {"start_time": _START, "end_time": _END},
    )


@pytest.mark.usefixtures("init_integration")
async def test_unavailable_while_hidden(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A call names what it acted on, which could be hidden."""
    await apply_options(hass, init_integration, excluded_domains=["camera"])
    with pytest.raises(ServiceValidationError) as err:
        await call_tool(_TOOL)
    assert err.value.translation_key == "unavailable_while_hidden"


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    ("args", "key"),
    [
        ({"start": "not a time"}, "invalid_time"),
        (
            {"start": "2026-01-02T00:00:00+00:00", "end": "2026-01-01T00:00:00+00:00"},
            "invalid_time_window",
        ),
        (
            {"start": "2026-01-01T00:00:00+00:00", "end": "2026-02-01T00:00:00+00:00"},
            "time_window_too_long",
        ),
    ],
)
async def test_bad_window(call_tool: CallTool, args: dict[str, str], key: str) -> None:
    """Bad time windows are refused before anything is read."""
    with pytest.raises(ServiceValidationError) as err:
        await call_tool(_TOOL, **args)
    assert err.value.translation_key == key
