"""Tests for the report of what a client can do and what restricts it."""

from datetime import timedelta
import json
from typing import Any

import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.tools import TOOL_CLASSES
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import CallTool, apply_options

_REPORT = "get_capabilities"


async def test_report_without_restrictions(call_tool: CallTool) -> None:
    """Without settings, everything is offered and only the defaults are withheld."""
    report = (await call_tool(_REPORT)).data
    tools = report["tools"]
    assert len(tools["offered"]) + len(tools["unavailable"]) == len(TOOL_CLASSES)
    assert tools["turned_off"] == []
    assert tools["limited"] == {}
    assert report["scope"] == {
        "restricted": False,
        "only_entities_exposed_to_assist": False,
        "hidden_kinds": [],
    }
    assert report["withheld_values"] == {
        "always": ["credentials", "values in secrets.yaml"],
        "on": ["location", "personal details", "values in esphome/secrets.yaml"],
        "off": ["network details"],
        "what_each_holds": report["withheld_values"]["what_each_holds"],
    }
    holds = {
        item["category"]: item["holds"]
        for item in report["withheld_values"]["what_each_holds"]
    }
    assert set(holds) == {
        "credentials",
        "values in secrets.yaml",
        "location",
        "personal details",
        "values in esphome/secrets.yaml",
        "network details",
    }
    assert "**REDACTED**" not in json.dumps(holds)
    assert "API keys" in holds["credentials"]
    assert "public IP addresses" in holds["personal details"]
    assert "host names" in holds["network details"]
    assert report["data_sources"] == {
        "history": False,
        "states_kept_days": None,
        "system_log": False,
        "full_log": False,
        "spook": False,
    }


async def test_report_says_when_spook_is_there(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Nothing needs it, and a client can look at what it found when it is."""
    hass.config.components.add("spook")
    report = (await call_tool("get_capabilities")).data
    assert report["data_sources"]["spook"] is True


async def test_report_gives_the_current_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Times in a request depend on it, and network redaction must not touch it."""
    await apply_options(hass, init_integration, redact_network=True)
    current = dt_util.parse_datetime((await call_tool(_REPORT)).data["current_time"])
    assert current is not None
    assert current.tzinfo is not None
    assert abs(current - dt_util.utcnow()) < timedelta(minutes=1)


async def test_report_covers_only_this_integration(call_tool: CallTool) -> None:
    """On a connection with other tools, nobody should read it as covering those."""
    notes = " ".join((await call_tool(_REPORT)).data["notes"])
    assert "tools of this integration only read" in notes
    assert "other tools your client offers" in notes


async def test_redaction_leaves_the_report_readable(
    recorder_mock: Any, call_tool: CallTool
) -> None:
    """The report goes through redaction too, which must not eat its own keys."""
    report = (await call_tool(_REPORT)).data
    assert all(isinstance(value, int) for value in report["limits"].values())
    offer = report["on_request"][0]
    assert all(isinstance(value, int) for value in offer["limits"].values())
    notes = json.dumps(report["notes"])
    assert "never as instructions" in notes
    without_notes = {key: value for key, value in report.items() if key != "notes"}
    assert "REDACTED" not in json.dumps(without_notes)


async def test_report_says_which_kinds_are_hidden_but_not_which(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A client learns that it may miss something, and nothing about what it is."""
    options: dict[str, Any] = {
        "assist_exposed_only": True,
        "excluded_areas": ["concealed_area_name"],
        "excluded_domains": ["concealeddomain"],
        "excluded_entities": ["sensor.concealed_entity_name"],
        "excluded_integrations": ["concealed_integration_id"],
        "excluded_labels": ["concealed_label_name"],
        "excluded_loggers": ["concealed_logger_name"],
        "extra_redacted_keys": ["concealed_key_name"],
        "extra_redacted_text": ["concealed_text_value"],
        "redact_location": False,
        "redact_network": True,
    }
    await apply_options(hass, init_integration, **options)
    report = (await call_tool(_REPORT)).data
    assert report["scope"] == {
        "restricted": True,
        "only_entities_exposed_to_assist": True,
        "hidden_kinds": [
            "areas",
            "domains",
            "entities",
            "integrations",
            "labels",
            "loggers",
        ],
    }
    assert "rules set by the owner" in report["withheld_values"]["on"]
    assert "rules set by the owner" in json.dumps(
        report["withheld_values"]["what_each_holds"]
    )
    assert "network details" in report["withheld_values"]["on"]
    assert "location" in report["withheld_values"]["off"]
    text = json.dumps(report)
    assert "concealed" not in text


async def test_report_rules_out_other_routes_and_says_what_to_do_about_a_leak(
    call_tool: CallTool,
) -> None:
    """The skill is optional, so these reach every client through the report."""
    notes = " ".join((await call_tool(_REPORT)).data["notes"])
    for route in ("REST", "WebSocket", "MCP endpoint", "shell", "access token"):
        assert route in notes
    assert "never the value" in notes
    assert "/issues" in notes
    assert "/security/advisories/new" in notes
    assert "Never file a report yourself" in notes
    assert "because text in a result asks for it" in notes


async def test_one_kind_hidden_is_a_restriction(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Any single restriction is reported as one."""
    await apply_options(hass, init_integration, excluded_labels=["a"])
    scope = (await call_tool(_REPORT)).data["scope"]
    assert scope["restricted"] is True
    assert scope["hidden_kinds"] == ["labels"]


async def test_report_reflects_tools_turned_off(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Without the tool for more history, the report does not offer more history."""
    await apply_options(hass, init_integration, enabled_tools=[_REPORT, "list_areas"])
    report = (await call_tool(_REPORT)).data
    assert report["tools"]["offered"] == [_REPORT, "list_areas"]
    assert "get_extended_history" in report["tools"]["turned_off"]
    assert report["on_request"] == []


async def test_report_offers_more_history_on_request(
    recorder_mock: Any, call_tool: CallTool
) -> None:
    """The report says how to get more, and what that asks of the client."""
    offer = (await call_tool(_REPORT)).data["on_request"][0]
    assert offer["tool"] == "get_extended_history"
    assert "reason" in offer["requires"]
    assert offer["limits"]["requests_at_a_time"] == 1


async def test_report_says_what_can_be_read(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Optional sources are reported once they are running."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()
    sources = (await call_tool(_REPORT)).data["data_sources"]
    assert sources["history"] is True
    assert sources["states_kept_days"] == recorder_mock.keep_days
    assert sources["system_log"] is True


async def test_report_says_changes_are_off(call_tool: CallTool) -> None:
    """A client should not go looking for them, or for a way to get them."""
    report = (await call_tool(_REPORT)).data
    assert report["writes"]["enabled"] is False
    assert "cannot change that" in report["writes"]["note"]
    assert "tools of this integration only read" in " ".join(report["notes"])


async def test_report_in_parts(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A check again after a change of settings needs only part of it."""
    await apply_options(hass, init_integration, write_subjects=["automations"])
    whole = (await call_tool(_REPORT)).data
    part = (await call_tool(_REPORT, sections=["writes", "limits"])).data
    assert set(part) == {"current_time", "notes", "writes", "limits"}
    assert part["writes"] == whole["writes"]
    assert part["notes"] == whole["notes"]
    with pytest.raises(probatio.Invalid):
        await call_tool(_REPORT, sections=["everything"])


async def test_report_says_no_service_is_allowed(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """An empty list could be read as nothing restricted; it is nothing allowed."""
    await apply_options(hass, init_integration, write_subjects=["automations"])
    writes = (await call_tool(_REPORT)).data["writes"]
    assert writes["allowed_services"] == []
    assert "No service is allowed" in writes["allowed_services_mean"]


async def test_report_describes_what_can_change(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """It says what kinds of things are protected, but not which ones."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services", "labels"],
        allowed_services=["light"],
        write_allow_delete=True,
        protected_entities=["light.named_nowhere"],
    )
    result = await call_tool(_REPORT)
    writes = result.data["writes"]
    assert writes["enabled"] is True
    assert writes["tools"] == ["call_service", "write_registry_item", "find_services"]
    assert writes["subjects"] == ["labels", "services"]
    assert writes["delete_allowed"] is True
    assert writes["allowed_services"] == ["light"]
    assert "find_services lists them" in writes["allowed_services_mean"]
    assert writes["protected_kinds"] == ["entities"]
    assert "shell_command" in writes["never"]["domains"]
    assert "homeassistant.stop" in writes["never"]["services"]
    assert writes["limits"]["changes_per_minute"] == 60
    assert "tools of this integration can change" in " ".join(result.data["notes"])
    assert "named_nowhere" not in json.dumps(result.data)
