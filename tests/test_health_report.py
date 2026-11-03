"""Tests for the repairs, among them Spook's, and the report on what is wrong."""

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.core_commands import async_run_command
from custom_components.llm_admin_tools.policy.limits import (
    MAX_HEALTH_EXAMPLES,
    MAX_HEALTH_ITEMS,
)
from custom_components.llm_admin_tools.queries.repairs import _severity_rank
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from .conftest import SECRET_VALUE, CallTool, apply_options

_REPAIRS = "custom_components.llm_admin_tools.queries.repairs"
_TRACES = "custom_components.llm_admin_tools.queries.traces"
_TEXTS = {
    "component.spook.issues.gone.title": "Unknown in {automation}",
    "component.spook.issues.gone.description": "Look at {automation}.",
    "component.spook.issues.fixable.title": "Unused",
    "component.spook.issues.fixable.fix_flow.step.confirm.description": "Remove it.",
}


def _issue(
    hass: HomeAssistant,
    domain: str,
    issue_id: str,
    key: str = "gone",
    severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
    **placeholders: str,
) -> None:
    ir.async_create_issue(
        hass,
        domain,
        issue_id,
        is_fixable=key == "fixable",
        severity=severity,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


@pytest.fixture
def texts() -> Any:
    """Provide what would be in the translations of Spook."""
    with patch(
        f"{_REPAIRS}.translation.async_get_translations", AsyncMock(return_value=_TEXTS)
    ):
        yield


@pytest.fixture
async def core(hass: HomeAssistant) -> None:
    """Have the commands of core available."""
    for component in ("websocket_api", "config", "lovelace", "system_log"):
        assert await async_setup_component(hass, component, {})
    await hass.async_block_till_done()


@pytest.mark.usefixtures("init_integration", "texts")
async def test_lists_repairs_with_the_most_severe_first(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Without Spook this works the same, for what others report."""
    _issue(hass, "spook", "first", automation="Morning", entities="- `sensor.gone`")
    _issue(hass, "other", "worst", "unknown", ir.IssueSeverity.CRITICAL)
    _issue(hass, "spook", "fixable", "fixable")
    data = (await call_tool("list_repairs")).data
    assert data["spook"] is False
    assert data["total_count"] == 3
    assert data["items"][0]["issue_id"] == "worst"
    assert "title" not in data["items"][0]
    by_id = {item["issue_id"]: item for item in data["items"]}
    assert by_id["first"]["title"] == "Unknown in Morning"
    assert by_id["first"]["details"]["entities"] == "- `sensor.gone`"
    assert "description" not in by_id["first"]
    assert by_id["fixable"]["can_be_fixed"] is True
    assert "can_be_fixed" not in by_id["first"]


@pytest.mark.usefixtures("init_integration", "texts")
async def test_descriptions_on_request(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """They are long, and a repair that can be fixed keeps its in another place."""
    _issue(hass, "spook", "first", automation="Morning")
    _issue(hass, "spook", "fixable", "fixable")
    data = (await call_tool("list_repairs", include_descriptions=True)).data
    by_id = {item["issue_id"]: item for item in data["items"]}
    assert by_id["first"]["description"] == "Look at Morning."
    assert by_id["fixable"]["description"] == "Remove it."


@pytest.mark.usefixtures("init_integration", "texts")
async def test_filters_repairs(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Spook's are asked for by its domain, and what is ignored on purpose."""
    hass.config.components.add("spook")
    _issue(hass, "spook", "first", automation="Morning")
    _issue(hass, "spook", "ignored", automation="Evening")
    _issue(hass, "other", "theirs")
    ir.async_ignore_issue(hass, "spook", "ignored", True)

    spook = (await call_tool("list_repairs", domain="spook")).data
    assert spook["spook"] is True
    assert [item["issue_id"] for item in spook["items"]] == ["first"]

    every = (await call_tool("list_repairs", include_ignored=True)).data
    assert every["total_count"] == 3
    assert [i["ignored"] for i in every["items"] if i["issue_id"] == "ignored"] == [
        True
    ]


@pytest.mark.usefixtures("texts")
async def test_repairs_keep_what_is_hidden(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Those that name it, or come from a hidden integration, look as if never there."""
    source = MockConfigEntry(domain="secretive")
    source.add_to_hass(hass)
    hass.states.async_set("sensor.hidden", "1")
    _issue(hass, "spook", "names_it", automation="Morning", entities="`sensor.hidden`")
    _issue(hass, "spook", "names_gone", automation="Noon", entities="`sensor.gone`")
    _issue(hass, "secretive", "theirs")
    await apply_options(
        hass,
        init_integration,
        excluded_entities=["sensor.hidden"],
        excluded_integrations=[source.entry_id],
    )
    data = (await call_tool("list_repairs")).data
    assert [item["issue_id"] for item in data["items"]] == ["names_gone"]


def test_a_repair_without_severity_comes_last() -> None:
    """One read back from storage may have none."""
    assert _severity_rank(None) > _severity_rank(ir.IssueSeverity.WARNING)


@pytest.mark.usefixtures("core", "init_integration", "texts")
async def test_report_on_a_working_system(call_tool: CallTool) -> None:
    """Everything could be read, and nothing is wrong."""
    report = (await call_tool("get_health_report")).data
    assert report["repairs"]["count"] == 0
    assert report["repairs"]["spook"] is False
    assert report["failing_integrations"] == []
    assert report["unavailable_entities"]["count"] == 0
    assert report["failed_runs"] == {"count": 0, "items": []}
    assert report["missing_references"]["count"] == 0
    assert "not_checked" not in report


@pytest.mark.usefixtures("core", "init_integration", "texts")
async def test_report_says_what_is_wrong(
    hass: HomeAssistant, call_tool: CallTool, entity_registry: er.EntityRegistry
) -> None:
    """Each part is short, and says where to go on."""
    failed = MockConfigEntry(
        domain="broken", title="Broken", state=ConfigEntryState.SETUP_ERROR
    )
    failed.add_to_hass(hass)
    MockConfigEntry(domain="fine", state=ConfigEntryState.LOADED).add_to_hass(hass)
    _issue(hass, "spook", "first", automation="Morning")
    for number in range(MAX_HEALTH_ITEMS + 1):
        _issue(hass, "other", f"more{number}")

    for number in range(MAX_HEALTH_EXAMPLES + 1):
        entry = entity_registry.async_get_or_create(
            "sensor", "broken", f"one{number}", suggested_object_id=f"one{number}"
        )
        hass.states.async_set(entry.entity_id, "unavailable")
    hass.states.async_set("sensor.free", "unavailable")
    hass.states.async_set("sensor.fine", "1")

    logger = logging.getLogger("test.health")
    logger.warning("once")
    for _ in range(3):
        logger.error("again and again")
    try:
        raise ValueError(f"password={SECRET_VALUE}")
    except ValueError:
        logger.exception("it failed")
    await hass.async_block_till_done()

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "names_gone",
                    "alias": "names gone",
                    "triggers": [{"trigger": "event", "event_type": "nothing"}],
                    "actions": [{"variables": {"entity": "sensor.gone"}}],
                },
                {
                    "id": "fails",
                    "alias": "fails",
                    "triggers": [{"trigger": "event", "event_type": "fail_now"}],
                    "actions": [{"action": "no_such_domain.no_such_service"}],
                },
            ]
        },
    )
    await hass.async_block_till_done()
    hass.bus.async_fire("fail_now")
    await hass.async_block_till_done()

    report = (await call_tool("get_health_report")).data

    repairs = report["repairs"]
    # The automation that failed reports the unknown service as a repair of its own.
    assert repairs["by_domain"] == {
        "automation": 1,
        "other": MAX_HEALTH_ITEMS + 1,
        "spook": 1,
    }
    assert repairs["count"] == MAX_HEALTH_ITEMS + 3
    assert len(repairs["items"]) == MAX_HEALTH_ITEMS
    assert sorted(repairs["items"][0]) in (
        ["domain", "issue_id", "severity"],
        ["domain", "issue_id", "severity", "title"],
    )

    assert [i["domain"] for i in report["failing_integrations"]] == ["broken"]

    unavailable = report["unavailable_entities"]
    assert unavailable["count"] == MAX_HEALTH_EXAMPLES + 2
    assert unavailable["integrations_count"] == 2
    largest = unavailable["by_integration"][0]
    assert largest["integration"] == "broken"
    assert len(largest["examples"]) == MAX_HEALTH_EXAMPLES
    assert "integration" not in unavailable["by_integration"][1]

    records = report["recurring_log_records"]
    assert SECRET_VALUE not in str(records)
    by_message = {record["message"]: record for record in records}
    assert by_message["again and again"]["count"] == 3
    assert "ValueError" in by_message["it failed"]["exception"]
    assert "exception" not in by_message["once"]

    runs = report["failed_runs"]["items"]
    assert [run["entity_id"] for run in runs] == ["automation.fails"]
    assert runs[0]["outcome"] == "error"

    names = {row["id"] for row in report["missing_references"]["items"]}
    assert "automation.names_gone" in names


@pytest.mark.usefixtures("core", "texts")
async def test_report_reads_only_what_a_client_may_read(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Tools the owner turned off keep what they read."""
    _issue(hass, "spook", "first", automation="Morning")
    await apply_options(hass, init_integration, enabled_tools=["get_health_report"])
    report = (await call_tool("get_health_report")).data
    assert report == {
        "not_checked": [
            "repairs",
            "failing_integrations",
            "unavailable_entities",
            "recurring_log_records",
            "failed_runs",
            "missing_references",
        ]
    }


@pytest.mark.usefixtures("core", "texts")
async def test_report_leaves_out_what_could_show_what_is_hidden(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """The other parts still come, without what is hidden."""
    hass.states.async_set("sensor.hidden", "unavailable")
    await apply_options(hass, init_integration, excluded_entities=["sensor.hidden"])
    report = (await call_tool("get_health_report")).data
    assert report["not_checked"] == ["failed_runs", "missing_references"]
    assert report["unavailable_entities"]["count"] == 0


@pytest.mark.usefixtures("init_integration", "texts")
async def test_report_without_what_it_could_read(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A report stands on its own if the system log or the commands are not there."""
    for component in ("websocket_api", "config"):
        assert await async_setup_component(hass, component, {})
    assert await async_setup_component(hass, "script", {})
    await hass.async_block_till_done()
    with patch(
        f"{_TRACES}.async_run_command", AsyncMock(side_effect=HomeAssistantError("no"))
    ):
        report = (await call_tool("get_health_report")).data
    assert report["not_checked"] == ["recurring_log_records"]
    assert report["failed_runs"] == {
        "count": 0,
        "items": [],
        "not_checked": ["script"],
    }
    assert report["missing_references"]["not_checked"] == ["dashboards"]


@pytest.mark.usefixtures("core", "init_integration", "texts")
async def test_report_leaves_out_runs_of_what_was_deleted(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Core keeps those until it restarts, and nobody can act on them."""
    assert await async_setup_component(hass, "script", {})
    await hass.async_block_till_done()
    failed = {
        "item_id": "deleted",
        "run_id": "1",
        "script_execution": "error",
        "timestamp": {"start": "2026-11-03T06:00:00+00:00"},
    }
    with patch(f"{_TRACES}.async_run_command", AsyncMock(return_value=[failed])):
        report = (await call_tool("get_health_report")).data
    assert report["failed_runs"] == {"count": 0, "items": []}


@pytest.mark.usefixtures("core", "init_integration", "texts")
async def test_report_says_when_runs_could_not_be_looked_at(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """No runs that failed, and not being able to see them, are different answers."""
    for component, config in (
        (
            "automation",
            {
                "automation": [
                    {
                        "id": "fails",
                        "alias": "fails",
                        "triggers": [{"trigger": "event", "event_type": "fail_now"}],
                        "actions": [{"action": "no_such_domain.no_such_service"}],
                    }
                ]
            },
        ),
        ("script", {}),
    ):
        assert await async_setup_component(hass, component, config)
    await hass.async_block_till_done()
    hass.bus.async_fire("fail_now")
    await hass.async_block_till_done()

    async def only_automations(
        hass: HomeAssistant, user_id: str | None, command: str, data: dict[str, Any]
    ) -> Any:
        if data["domain"] == "script":
            raise HomeAssistantError("no")
        return await async_run_command(hass, user_id, command, data)

    with patch(f"{_TRACES}.async_run_command", only_automations):
        runs = (await call_tool("get_health_report")).data["failed_runs"]
    assert [run["entity_id"] for run in runs["items"]] == ["automation.fails"]
    assert runs["not_checked"] == ["script"]
