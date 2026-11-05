"""Tests for what tells a client how to go about its work."""

from pathlib import Path
import re
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.const import DOMAIN
from custom_components.llm_admin_tools.tools import TOOL_CLASSES
from custom_components.llm_admin_tools.tools.catalog import TOOL_CLASSES_FOR_CHANGES
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component

from .conftest import HA_2026_11, CallTool, apply_options, async_setup_logbook

_REPORT = "get_capabilities"
_SKILL = Path(__file__).parents[1] / "skills" / "ha-llm-admin-tools" / "SKILL.md"
_ALL_TOOLS = {tool.name for tool in (*TOOL_CLASSES, *TOOL_CLASSES_FOR_CHANGES)}


def _guide(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["goal"]: task for task in report["start_here"]}


async def _report(call_tool: CallTool) -> dict[str, Any]:
    return dict((await call_tool(_REPORT)).data)


async def test_says_what_does_not_work_and_why(call_tool: CallTool) -> None:
    """A source that is not running takes the tools for it out of what is offered."""
    tools = (await _report(call_tool))["tools"]
    assert set(tools["unavailable"]) == {
        "get_action_history",
        "get_entity_history",
        "get_extended_history",
        "get_system_log",
        "summarize_log",
        "get_full_log",
    }
    assert "recorder" in tools["unavailable"]["get_entity_history"]
    assert "System Log" in tools["unavailable"]["get_system_log"]
    assert "no log file" in tools["unavailable"]["get_full_log"]
    assert "no log file" in tools["unavailable"]["summarize_log"]
    assert not set(tools["unavailable"]) & set(tools["offered"])


async def test_what_runs_is_offered(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool, tmp_path: Path
) -> None:
    """With every source there, nothing is unavailable, and the offers are made."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()
    hass.data[KEY_DATA_LOGGING] = str(tmp_path / "home-assistant.log")
    if HA_2026_11:
        await async_setup_logbook(hass)
    report = await _report(call_tool)
    before_2026_11 = set() if HA_2026_11 else {"get_action_history"}
    assert set(report["tools"]["unavailable"]) == before_2026_11
    assert report["tools"]["limited"] == {}
    offered = report["tools"]["offered"]
    assert len(offered) == len(TOOL_CLASSES) - len(before_2026_11)
    assert [offer["tool"] for offer in report["on_request"]] == [
        "get_extended_history",
        "get_full_log",
    ]
    assert "summarize_log" in report["on_request"][1]["use_when"]


async def test_says_what_does_not_work_while_something_is_hidden(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """The reason says that something is, and nothing about what."""
    await apply_options(hass, init_integration, excluded_entities=["sensor.concealed"])
    report = await _report(call_tool)
    tools = report["tools"]
    assert "hidden" in tools["unavailable"]["get_automation_trace"]
    assert "get_automation_trace" not in tools["offered"]
    assert "target" in tools["limited"]["find_references"]
    assert "failed runs" in tools["limited"]["get_health_report"]
    assert "find_references" in tools["offered"]
    assert "concealed" not in str(report)

    guide = _guide(report)
    assert "Find what names an entity that does not exist" not in guide
    assert guide[
        "Find what uses an entity or device, instead of reading every definition"
    ]
    why = guide["Find out why an automation or script did or did not run"]
    assert why["tools"] == ["list_definitions", "get_definition"]


async def test_says_so_when_the_full_log_is_turned_off(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """The summary reads the same log, so it goes along, and so does the offer."""
    hass.data[KEY_DATA_LOGGING] = "home-assistant.log"
    await apply_options(
        hass, init_integration, enabled_tools=[_REPORT, "summarize_log"]
    )
    report = await _report(call_tool)
    assert "get_full_log" in report["tools"]["turned_off"]
    assert "turned off" in report["tools"]["unavailable"]["summarize_log"]
    assert report["tools"]["offered"] == [_REPORT]
    assert report["on_request"] == []
    assert report["start_here"] == []


async def test_guide_starts_with_the_cheapest(call_tool: CallTool) -> None:
    """A client should not read everything to answer what one call can."""
    guide = _guide(await _report(call_tool))
    assert guide["See what is wrong"]["tools"] == ["get_health_report", "list_repairs"]
    assert guide["Find what names an entity that does not exist"]["how"]
    assert "Read the logs, the cheapest first" not in guide
    assert "See how a value changed" not in guide
    assert "Call a service" not in guide
    assert "Change or remove something" not in guide
    assert "See what Spook found" not in guide


async def test_guide_has_the_logs_in_order(
    hass: HomeAssistant, call_tool: CallTool, tmp_path: Path
) -> None:
    """The full log comes last, as it costs the most."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()
    hass.data[KEY_DATA_LOGGING] = str(tmp_path / "home-assistant.log")
    guide = _guide(await _report(call_tool))
    assert guide["Read the logs, the cheapest first"]["tools"] == [
        "get_system_log",
        "summarize_log",
        "get_full_log",
    ]


async def test_guide_points_to_what_spook_found(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """It only comes up where Spook is there."""
    hass.config.components.add("spook")
    task = _guide(await _report(call_tool))["See what Spook found"]
    assert task["tools"] == ["list_repairs"]
    assert "domain spook" in task["how"]


async def test_guide_covers_changes_once_they_are_allowed(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Only the tools the owner allows are named, and dry_run comes with them."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services", "labels"],
        allowed_services=["light"],
    )
    guide = _guide(await _report(call_tool))
    assert guide["Call a service"]["tools"] == ["find_services", "call_service"]
    change = guide["Change or remove something"]
    assert change["tools"] == ["write_registry_item"]
    assert "dry_run" in change["how"]


async def test_guide_only_names_tools_that_exist(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """With everything allowed, what it names can be called."""
    await apply_options(
        hass, init_integration, write_subjects=["services"], allowed_services=["light"]
    )
    for task in (await _report(call_tool))["start_here"]:
        assert set(task["tools"]) <= _ALL_TOOLS


async def test_refusals_agree_with_the_report(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """What the report says does not work, does not."""
    await apply_options(hass, init_integration, excluded_entities=["sensor.concealed"])
    for name, args in (
        ("get_automation_trace", {"entity_id": "automation.one"}),
        ("find_references", {}),
    ):
        with pytest.raises(HomeAssistantError) as err:
            await call_tool(name, **args)
        assert err.value.translation_key == "unavailable_while_hidden"


async def test_prompt_points_at_the_report(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
) -> None:
    """It says the same as the description, for a client that reads it."""
    instance = await llm.async_get_api(hass, DOMAIN, admin_context)
    assert "Call get_capabilities first" in instance.api_prompt
    assert "repairs" in instance.api_prompt


def test_descriptions_point_to_what_costs_less() -> None:
    """A client that only reads the tool list still finds the cheaper way."""
    by_name = {tool.name: tool for tool in TOOL_CLASSES}
    assert "start_here" in by_name[_REPORT].description
    assert "summarize_log" in by_name["get_system_log"].description
    assert "summarize_log" in by_name["get_full_log"].description
    assert "find_references" in by_name["list_definitions"].description
    assert "find_references" in by_name["get_definition"].description
    assert "get_automation_trace" in by_name["get_definition"].description


def test_skill_can_be_uploaded() -> None:
    """Uploading refuses other fields, and a name that differs from its folder."""
    text = _SKILL.read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n.*", text, re.DOTALL)
    assert match is not None
    fields = dict(line.split(": ", 1) for line in match[1].splitlines())
    assert set(fields) == {"name", "description"}
    assert fields["name"] == _SKILL.parent.name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", fields["name"])
    assert len(fields["description"]) <= 200


def test_skill_only_names_tools_that_exist() -> None:
    """It stays apart from the code, so it should not fall behind unnoticed."""
    names = set(
        re.findall(
            r"`((?:get|list|find|summarize|write|update|call|manage)_[a-z_]+)`",
            _SKILL.read_text(encoding="utf-8"),
        )
    )
    assert _REPORT in names
    assert names <= _ALL_TOOLS
