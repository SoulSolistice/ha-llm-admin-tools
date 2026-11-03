"""Tests for looking up the services a client may call."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.llm_admin_tools.const import DOMAIN
from custom_components.llm_admin_tools.policy import limits
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import llm
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.setup import async_setup_component

from .conftest import SECRET_VALUE, CallTool, apply_options

_TOOL = "find_services"


async def _allow(
    hass: HomeAssistant, entry: MockConfigEntry, *services: str, **options: Any
) -> None:
    """Let a client call these services."""
    await apply_options(
        hass,
        entry,
        write_subjects=["services"],
        allowed_services=list(services),
        **options,
    )


def _register(hass: HomeAssistant, domain: str, service: str, **schema: Any) -> None:
    """Add a service with a description."""
    async_mock_service(hass, domain, service)
    async_set_service_schema(
        hass, domain, service, {"name": service, "fields": {}, **schema}
    )


async def test_only_offered_with_services_allowed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
) -> None:
    """It describes what call_service can do, so it comes and goes with it."""

    async def names() -> list[str]:
        instance = await llm.async_get_api(hass, DOMAIN, admin_context)
        return [tool.name for tool in instance.tools]

    assert _TOOL not in await names()
    await apply_options(hass, init_integration, write_subjects=["helpers"])
    assert _TOOL not in await names()
    await _allow(hass, init_integration, "light")
    assert _TOOL in await names()
    report = await llm.async_get_api(hass, DOMAIN, admin_context)
    assert next(tool for tool in report.tools if tool.name == _TOOL).annotations
    assert next(
        tool for tool in report.tools if tool.name == _TOOL
    ).annotations.read_only


async def test_lists_only_what_is_allowed(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A service that is not allowed looks as if it does not exist."""
    _register(hass, "light", "turn_on")
    _register(hass, "light", "turn_off")
    _register(hass, "lock", "unlock")
    _register(hass, "homeassistant", "restart")
    _register(hass, "homeassistant", "stop")
    _register(hass, "shell_command", "run")
    await _allow(
        hass,
        init_integration,
        "light",
        "lock.lock",
        "homeassistant.restart",
        "homeassistant.stop",
        "shell_command",
    )
    data = (await call_tool(_TOOL)).data
    assert [item["service"] for item in data["items"]] == [
        "homeassistant.restart",
        "light.turn_off",
        "light.turn_on",
    ]


async def test_describes_fields(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A client learns what to send, also of fields the UI groups into sections."""
    async_mock_service(hass, "light", "turn_on")
    async_set_service_schema(
        hass,
        "light",
        "turn_on",
        {
            "name": "Turn on",
            "description": "Turns a light on.",
            "target": {"entity": [{"domain": ["light"]}]},
            "fields": {
                "my_brightness": {
                    "description": "How bright.",
                    "required": True,
                    "example": 50,
                    "selector": {"number": {"min": 0, "max": 100}},
                },
                "advanced_fields": {
                    "fields": {"my_flash": {"selector": {"boolean": None}}},
                },
            },
        },
    )
    await _allow(hass, init_integration, "light")
    item = (await call_tool(_TOOL)).data["items"][0]
    assert item["service"] == "light.turn_on"
    assert item["description"] == "Turns a light on."
    assert item["takes_entities"] is True
    assert item["returns_data"] is False
    assert item["fields"]["my_brightness"] == {
        "description": "How bright.",
        "required": True,
        "example": 50,
        "selector": {"number": {"min": 0, "max": 100}},
    }
    assert item["fields"]["my_flash"] == {"selector": {"boolean": None}}


async def test_fills_in_placeholders(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A service can leave parts of its translated text to be filled in."""
    hass.services.async_register(
        "my_domain",
        "report",
        AsyncMock(),
        description_placeholders={"what": "a light"},
    )
    await _allow(hass, init_integration, "my_domain")
    with patch(
        "custom_components.llm_admin_tools.queries.services.translation.async_get_translations",
        return_value={
            "component.my_domain.services.report.description": "Turns {what} on."
        },
    ):
        item = (await call_tool(_TOOL)).data["items"][0]
    assert item["description"] == "Turns a light on."


async def test_says_if_a_service_answers(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A client can tell where it gets data back."""
    async_mock_service(
        hass, "my_domain", "report", supports_response=SupportsResponse.ONLY
    )
    await _allow(hass, init_integration, "my_domain")
    item = (await call_tool(_TOOL)).data["items"][0]
    assert item["returns_data"] is True
    assert item["takes_entities"] is False


async def test_uses_translations(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Core's own services keep their text in translations."""
    assert await async_setup_component(hass, "homeassistant", {})
    await _allow(hass, init_integration, "homeassistant.restart")
    item = (await call_tool(_TOOL)).data["items"][0]
    assert item["service"] == "homeassistant.restart"
    assert item["name"]
    assert item["description"]


async def test_filters(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Domain and words narrow the result, and paging holds."""
    _register(hass, "light", "turn_on", description="Lights something up.")
    _register(hass, "light", "toggle", description="Flips a light.")
    _register(hass, "switch", "turn_on", description="Closes a relay.")
    await _allow(hass, init_integration, "light", "switch")

    async def services(**args: Any) -> list[str]:
        items = (await call_tool(_TOOL, **args)).data["items"]
        return [item["service"] for item in items]

    assert await services(domain=" LIGHT ") == ["light.toggle", "light.turn_on"]
    assert await services(search="RELAY") == ["switch.turn_on"]
    assert await services(search="light flips") == ["light.toggle"]
    assert await services(search="light.turn_on") == ["light.turn_on"]
    assert await services(search="nothing like this") == []
    paged = (await call_tool(_TOOL, limit=2)).data
    assert paged["total_count"] == 3
    assert paged["next_offset"] == 2


async def test_search_finds_nothing_that_is_withheld(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A script can say anything of itself, which goes through redaction."""
    _register(hass, "script", "open_up", description=f"Uses {SECRET_VALUE}.")
    await _allow(hass, init_integration, "script")
    assert (await call_tool(_TOOL, search="Uses")).data["total_count"] == 1
    assert (await call_tool(_TOOL, search=SECRET_VALUE)).data["total_count"] == 0
    assert SECRET_VALUE not in str((await call_tool(_TOOL)).data)


async def test_a_long_description_leaves_out_selectors(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Options of a field can be many, and the rest still says what to send."""
    options = ["option"] * limits.MAX_SERVICE_DESCRIPTION_CHARACTERS
    _register(
        hass,
        "light",
        "choose",
        fields={
            "my_options": {
                "description": "The options.",
                "selector": {"select": {"options": options}},
            }
        },
    )
    await _allow(hass, init_integration, "light")
    item = (await call_tool(_TOOL)).data["items"][0]
    assert item["selectors_left_out"] is True
    assert item["fields"]["my_options"] == {"description": "The options."}


@pytest.mark.usefixtures("init_integration")
async def test_report_lists_it_with_the_changes(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """The report names it where it names call_service."""
    await _allow(hass, init_integration, "light")
    report = (await call_tool("get_capabilities")).data
    assert {"call_service", _TOOL} <= set(report["writes"]["tools"])
