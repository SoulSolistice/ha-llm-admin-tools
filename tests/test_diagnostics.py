"""Tests for diagnostics."""

import json

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.llm_admin_tools.diagnostics import (
    async_get_config_entry_diagnostics,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import REDACTED

from .conftest import SECRET_VALUE, CallTool, apply_options


async def test_diagnostics(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
) -> None:
    """Diagnostics say what is served, and hold no secret or user."""
    await call_tool("list_areas")
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    assert "get_entity" in result["tools"]
    assert result["known_secret_values"] == 1
    assert result["secret_files"] == [
        {"name": "secrets.yaml", "found": True, "values": 1}
    ]
    assert result["recent_calls"][-1]["tool"] == "list_areas"
    assert "user_id" not in result["recent_calls"][-1]
    assert SECRET_VALUE not in json.dumps(result)


async def test_diagnostics_count_settings_without_showing_them(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """What the user chose to hide is itself not revealed."""
    await apply_options(
        hass,
        init_integration,
        enabled_tools=["get_entity"],
        excluded_entities=["sensor.private_thing"],
        extra_redacted_text=["my-private-text"],
        redact_network=True,
    )
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    assert result["tools"] == ["get_entity"]
    assert result["settings"]["excluded_entities"] == 1
    assert result["settings"]["extra_redacted_text"] == 1
    assert result["settings"]["redact_network"] is True
    text = json.dumps(result)
    assert "sensor.private_thing" not in text
    assert "my-private-text" not in text


async def test_diagnostics_leave_out_text_from_clients(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """People post these in public, and a reason or a target can say anything."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["light"],
    )
    async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.room_of_emma", "off")
    await call_tool(
        "call_service",
        domain="light",
        service="turn_on",
        entity_ids=["light.room_of_emma"],
        reason="Emma asked for this",
    )
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    text = json.dumps(result)
    assert "emma" not in text.lower()
    write = result["recent_writes"][-1]
    assert write["tool"] == "call_service"
    assert write["success"] is True
    assert write["summary"] == write["reason"] == REDACTED
    assert result["recent_calls"][-1]["reason"] == REDACTED
