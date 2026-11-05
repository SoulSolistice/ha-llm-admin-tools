"""Tests for reading how automations and scripts ran."""

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.queries.traces import started_run
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from .conftest import SECRET_VALUE, CallTool, apply_options

_EVENT = "llm_admin_tools_test"


@pytest.fixture
async def core(hass: HomeAssistant) -> None:
    """Have the commands of core available, and something that has run."""
    for component in ("websocket_api", "config"):
        assert await async_setup_component(hass, component, {})
    hass.states.async_set("input_boolean.gate", "off")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "runs",
                    "alias": "runs",
                    "triggers": [{"trigger": "event", "event_type": _EVENT}],
                    "actions": [
                        {"variables": {"password": SECRET_VALUE, "kept": "plain"}},
                        {"delay": {"seconds": 0}},
                    ],
                },
                {
                    "id": "stops",
                    "alias": "stops",
                    "triggers": [{"trigger": "event", "event_type": _EVENT}],
                    "conditions": [
                        {
                            "condition": "state",
                            "entity_id": "input_boolean.gate",
                            "state": "on",
                        }
                    ],
                    "actions": [{"delay": {"seconds": 0}}],
                },
                {
                    "alias": "no id",
                    "triggers": [{"trigger": "event", "event_type": _EVENT}],
                    "actions": [{"delay": {"seconds": 0}}],
                },
            ]
        },
    )
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "ran": {"sequence": [{"delay": {"seconds": 0}}]},
                "calls": {"sequence": [{"action": "script.ran"}]},
            }
        },
    )
    await hass.async_block_till_done()
    hass.bus.async_fire(_EVENT)
    await hass.async_block_till_done()
    await hass.services.async_call("script", "ran", blocking=True)
    await hass.async_block_till_done()


async def _runs(call_tool: CallTool, entity_id: str, **args: Any) -> dict[str, Any]:
    return dict(
        (await call_tool("get_automation_trace", entity_id=entity_id, **args)).data
    )


@pytest.mark.usefixtures("core", "init_integration")
async def test_lists_how_runs_went(call_tool: CallTool) -> None:
    """Why one did not act is in how it ended."""
    ran = await _runs(call_tool, "automation.runs")
    assert ran["total_count"] == 1
    run = ran["items"][0]
    assert run["outcome"] == "finished"
    assert run["state"] == "stopped"
    assert "last_step" in run
    assert "start" in run
    assert "event" in run["trigger"]

    stopped = await _runs(call_tool, "automation.stops")
    assert stopped["items"][0]["outcome"] == "failed_conditions"

    script = await _runs(call_tool, "script.ran")
    assert script["items"][0]["outcome"] == "finished"


@pytest.mark.usefixtures("core", "init_integration")
async def test_shows_the_steps_of_a_run(call_tool: CallTool) -> None:
    """Variables are long, and withheld values stay so."""
    run_id = (await _runs(call_tool, "automation.runs"))["items"][0]["run_id"]

    short = await _runs(call_tool, "automation.runs", run_id=run_id)
    assert short["entity_id"] == "automation.runs"
    assert [step["path"] for step in short["steps"]][:2] == ["trigger/0", "action/0"]
    assert not any("variables" in step for step in short["steps"])

    full = await _runs(
        call_tool, "automation.runs", run_id=run_id, include_variables=True
    )
    assert any("variables" in step for step in full["steps"])
    assert SECRET_VALUE not in str(full)
    assert "plain" in str(full)


@pytest.mark.usefixtures("core", "init_integration")
async def test_follows_a_run_into_the_script_it_started(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """The step names the run of the script, which can be asked for in turn."""
    await hass.services.async_call("script", "calls", blocking=True)
    await hass.async_block_till_done()
    run_id = (await _runs(call_tool, "script.calls"))["items"][0]["run_id"]
    steps = (await _runs(call_tool, "script.calls", run_id=run_id))["steps"]
    started = next(step["started"] for step in steps if "started" in step)
    assert started["entity_id"] == "script.ran"
    child = await _runs(call_tool, "script.ran", run_id=started["run_id"])
    assert child["outcome"] == "finished"


@pytest.mark.usefixtures("core")
async def test_a_started_run_is_named_only_when_it_may_be_seen(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The tool needs nothing hidden; what it builds on does not rely on that."""
    policy = init_integration.runtime_data.exposure
    child = {"domain": "script", "item_id": "ran", "run_id": "1"}
    assert started_run(hass, policy, child) == {
        "entity_id": "script.ran",
        "run_id": "1",
    }
    for other in (
        {**child, "domain": "light"},
        {**child, "item_id": "deleted"},
        None,
    ):
        assert started_run(hass, policy, other) is None
    await apply_options(hass, init_integration, excluded_entities=["script.ran"])
    assert started_run(hass, init_integration.runtime_data.exposure, child) is None


@pytest.mark.usefixtures("core", "init_integration")
@pytest.mark.parametrize(
    ("entity_id", "key"),
    [
        ("light.kitchen", "trace_kind"),
        ("automation.not_there", "entity_not_found"),
        ("automation.no_id", "trace_needs_id"),
    ],
)
async def test_refuses_what_has_no_traces(
    call_tool: CallTool, entity_id: str, key: str
) -> None:
    """Each says why."""
    with pytest.raises(HomeAssistantError) as err:
        await _runs(call_tool, entity_id)
    assert err.value.translation_key == key


@pytest.mark.usefixtures("core")
async def test_needs_nothing_to_be_hidden(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A trace holds the values of whatever a run looked at."""
    await apply_options(hass, init_integration, excluded_entities=["sensor.hidden"])
    with pytest.raises(HomeAssistantError) as err:
        await _runs(call_tool, "automation.runs")
    assert err.value.translation_key == "unavailable_while_hidden"
