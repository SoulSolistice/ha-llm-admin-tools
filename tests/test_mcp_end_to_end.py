"""Tests through the real MCP Server integration, as a client would reach the tools."""

from http import HTTPStatus
from typing import Any

from aiohttp.test_utils import TestClient
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.llm_admin_tools.const import DOMAIN
from custom_components.llm_admin_tools.policy.writes import WRITE_SUBJECTS
from custom_components.llm_admin_tools.tools import TOOL_CLASSES
from custom_components.llm_admin_tools.tools.catalog import (
    TOOL_CLASSES_FOR_CHANGES,
    WRITE_TOOL_CLASSES,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .conftest import CORE_CHECKS_ADMIN, HA_2026_11, apply_options

_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
_API_URL = f"/api/mcp/{DOMAIN}"
# Merged APIs prefix tools with the slug of the API, from 2026.11 only on a conflict.
_NAMESPACE = "llm-admin-tools"


@pytest.fixture
async def mcp_server(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Set up the MCP Server integration next to ours."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})
    entry = MockConfigEntry(
        domain="mcp_server",
        data={
            "all_llm_apis": True,
            "llm_hass_api": [],
            "require_admin": True,
            "read_only": False,
        },
        minor_version=99,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _rpc(
    client: TestClient, url: str, method: str, params: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any] | None]:
    """Send one JSON-RPC request and return the status and decoded body."""
    response = await client.post(
        url,
        headers=_HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    if response.status != HTTPStatus.OK:
        return response.status, None
    return response.status, await response.json()


async def test_admin_lists_read_only_tools(
    hass: HomeAssistant, mcp_server: None, hass_client: ClientSessionGenerator
) -> None:
    """A client sees every tool with its schema and read-only hint."""
    client = await hass_client()
    status, body = await _rpc(client, _API_URL, "tools/list")
    assert status == HTTPStatus.OK
    assert body is not None
    tools = {tool["name"]: tool for tool in body["result"]["tools"]}
    assert len(tools) == len(TOOL_CLASSES)
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools.values())
    assert all(not tool["annotations"]["destructiveHint"] for tool in tools.values())
    limit = tools["list_entities"]["inputSchema"]["properties"]["limit"]
    assert limit["maximum"] == 500
    assert tools["get_entity"]["inputSchema"]["required"] == ["entity_id"]
    assert tools["list_definitions"]["inputSchema"]["properties"]["kind"]["enum"] == [
        "automation",
        "script",
        "scene",
    ]
    history = tools["get_entity_history"]["inputSchema"]["properties"]
    assert history["limit"]["maximum"] == 1000
    extended = tools["get_extended_history"]["inputSchema"]
    assert set(extended["required"]) == {"reason", "entity_id", "start"}
    assert extended["properties"]["reason"]["minLength"] == 10
    listing = tools["list_entities"]["inputSchema"]["properties"]
    assert listing["include_diagnostic"]["type"] == "boolean"
    assert tools["get_entity"]["inputSchema"]["properties"]["attributes"]["type"] == (
        "array"
    )


async def test_admin_calls_a_tool(
    hass: HomeAssistant, mcp_server: None, hass_client: ClientSessionGenerator
) -> None:
    """A call returns redacted data."""
    hass.states.async_set("sensor.a", "1", {"api_key": "abc"})
    client = await hass_client()
    status, body = await _rpc(
        client,
        _API_URL,
        "tools/call",
        {"name": "get_entity", "arguments": {"entity_id": "sensor.a"}},
    )
    assert status == HTTPStatus.OK
    assert body is not None
    assert body["result"]["isError"] is False
    text = body["result"]["content"][0]["text"]
    assert "**REDACTED**" in text
    assert "abc" not in text


async def test_error_text_is_translated(
    hass: HomeAssistant, mcp_server: None, hass_client: ClientSessionGenerator
) -> None:
    """A failing call tells the client what is wrong in words."""
    client = await hass_client()
    _, body = await _rpc(
        client,
        _API_URL,
        "tools/call",
        {"name": "get_entity", "arguments": {"entity_id": "sensor.none"}},
    )
    assert body is not None
    assert body["result"]["isError"] is True
    assert "sensor.none was not found" in body["result"]["content"][0]["text"]


async def test_settings_reach_the_client(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mcp_server: None,
    hass_client: ClientSessionGenerator,
) -> None:
    """A client sees only the chosen tools, and a hidden entity looks absent."""
    hass.states.async_set("sensor.hidden", "1")
    await apply_options(
        hass,
        init_integration,
        enabled_tools=["get_entity"],
        excluded_entities=["sensor.hidden"],
    )
    client = await hass_client()
    _, body = await _rpc(client, _API_URL, "tools/list")
    assert body is not None
    assert [tool["name"] for tool in body["result"]["tools"]] == ["get_entity"]

    _, body = await _rpc(
        client,
        _API_URL,
        "tools/call",
        {"name": "get_entity", "arguments": {"entity_id": "sensor.hidden"}},
    )
    assert body is not None
    assert body["result"]["isError"] is True
    assert "sensor.hidden was not found" in body["result"]["content"][0]["text"]


async def test_tools_cannot_write_through_arguments(
    hass: HomeAssistant, mcp_server: None, hass_client: ClientSessionGenerator
) -> None:
    """Unknown arguments are refused rather than ignored."""
    client = await hass_client()
    _, body = await _rpc(
        client,
        _API_URL,
        "tools/call",
        {"name": "list_entities", "arguments": {"state_to_set": "off"}},
    )
    assert body is not None
    assert body["result"]["isError"] is True


async def test_non_admin_is_refused_on_api_endpoint(
    hass: HomeAssistant,
    mcp_server: None,
    hass_client: ClientSessionGenerator,
    hass_read_only_access_token: str,
) -> None:
    """The per-API endpoint refuses a non-admin."""
    client = await hass_client(hass_read_only_access_token)
    status, _ = await _rpc(client, _API_URL, "tools/list")
    assert status == (
        HTTPStatus.FORBIDDEN if CORE_CHECKS_ADMIN else HTTPStatus.UNAUTHORIZED
    )


async def test_anonymous_is_refused(
    hass: HomeAssistant,
    mcp_server: None,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Without a token nothing is served."""
    client = await hass_client_no_auth()
    status, _ = await _rpc(client, _API_URL, "tools/list")
    assert status == HTTPStatus.UNAUTHORIZED


async def test_merged_endpoint_namespaces_tools(
    hass: HomeAssistant, mcp_server: None, hass_client: ClientSessionGenerator
) -> None:
    """The combined endpoint serves our tools next to Assist's."""
    client = await hass_client()
    status, body = await _rpc(client, "/api/mcp", "tools/list")
    assert status == HTTPStatus.OK
    assert body is not None
    names = {tool["name"] for tool in body["result"]["tools"]}
    assert ("get_entity" if HA_2026_11 else f"{_NAMESPACE}__get_entity") in names
    assert any(name.endswith("GetLiveContext") for name in names)


async def test_merged_endpoint_gives_non_admin_none_of_our_tools(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_read_only_access_token: str,
) -> None:
    """With the server's admin requirement off, our own check still holds."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})
    entry = MockConfigEntry(
        domain="mcp_server",
        data={
            "all_llm_apis": True,
            "llm_hass_api": [],
            "require_admin": False,
            "read_only": False,
        },
        minor_version=99,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client(hass_read_only_access_token)
    status, body = await _rpc(client, "/api/mcp", "tools/list")
    assert status == HTTPStatus.OK
    assert body is not None
    if CORE_CHECKS_ADMIN:
        # From 2026.11 Home Assistant refuses a non-admin every API merged with this.
        assert "result" not in body
        assert body["error"]
        return
    names = {tool["name"] for tool in body["result"]["tools"]}
    assert any(name.endswith("GetLiveContext") for name in names)
    assert not [name for name in names if "get_entity" in name]


@pytest.mark.skipif(not HA_2026_11, reason="The option is new in 2026.11")
@pytest.mark.usefixtures("mcp_server")
async def test_read_only_server_is_explained(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """The server takes the change tools away, so the report says what that means."""
    await apply_options(
        hass, init_integration, write_subjects=["services"], allowed_services=["light"]
    )
    (server,) = hass.config_entries.async_entries("mcp_server")
    hass.config_entries.async_update_entry(
        server, data={**server.data, "read_only": True}
    )
    assert await hass.config_entries.async_reload(server.entry_id)
    await hass.async_block_till_done()
    client = await hass_client()

    _status, body = await _rpc(client, "/api/mcp", "tools/list")
    assert body is not None
    names = {tool["name"] for tool in body["result"]["tools"]}
    assert "call_service" not in names
    _status, body = await _rpc(
        client, "/api/mcp", "tools/call", {"name": "get_capabilities", "arguments": {}}
    )
    assert body is not None
    report = body["result"]["content"][0]["text"]
    assert "call_service" in report
    assert "Read-only" in report


@pytest.mark.usefixtures("mcp_server")
async def test_changes_through_mcp(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """A client sees what the write tools take, and gets their refusals as errors."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=list(WRITE_SUBJECTS),
        allowed_services=["light"],
    )
    client = await hass_client()
    _status, body = await _rpc(client, _API_URL, "tools/list")
    assert body is not None
    tools = {tool["name"]: tool for tool in body["result"]["tools"]}
    assert len(tools) == len(TOOL_CLASSES) + len(TOOL_CLASSES_FOR_CHANGES)
    for tool_class in WRITE_TOOL_CLASSES:
        tool = tools[tool_class.name]
        assert not tool["annotations"]["readOnlyHint"]
        assert tool["annotations"]["destructiveHint"]
        # Only a service call can reach outside Home Assistant.
        assert tool["annotations"]["openWorldHint"] is (
            tool_class.name == "call_service"
        )
        assert "reason" in tool["inputSchema"]["required"]
        assert "dry_run" in tool["inputSchema"]["properties"]

    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.a", "off")
    args = {
        "domain": "light",
        "service": "turn_on",
        "entity_ids": ["light.a"],
        "reason": "Asked for by the user in this test.",
    }
    _status, done = await _rpc(
        client, _API_URL, "tools/call", {"name": "call_service", "arguments": args}
    )
    assert done is not None
    assert not done["result"].get("isError")
    assert calls
    _status, refused = await _rpc(
        client,
        _API_URL,
        "tools/call",
        {"name": "call_service", "arguments": {**args, "domain": "shell_command"}},
    )
    assert refused is not None
    assert refused["result"]["isError"]
