"""Tests for calling services, and what the settings let through."""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .conftest import CallTool, apply_options

_REASON = "Asked for by the user in this test."


async def _call(call_tool: CallTool, service: str, **args: Any) -> Any:
    domain, _, name = service.partition(".")
    return await call_tool(
        "call_service", domain=domain, service=name, reason=_REASON, **args
    )


async def _refusal(call_tool: CallTool, service: str, **args: Any) -> str | None:
    with pytest.raises(Exception) as err:
        await _call(call_tool, service, **args)
    return getattr(err.value, "translation_key", None)


@pytest.fixture
async def allow_light(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Let clients call light services."""
    await apply_options(
        hass, init_integration, write_subjects=["services"], allowed_services=["light"]
    )


@pytest.mark.usefixtures("allow_light")
async def test_call_goes_through(hass: HomeAssistant, call_tool: CallTool) -> None:
    """The entities are passed on, and the caller is who the logbook names."""
    calls: list[ServiceCall] = []

    async def turn_on(call: ServiceCall) -> None:
        calls.append(call)
        hass.states.async_set("light.a", "on")

    hass.services.async_register("light", "turn_on", turn_on)
    hass.states.async_set("light.a", "off")
    result = await _call(
        call_tool, "light.turn_on", entity_ids=["light.a"], data={"brightness": 5}
    )
    assert result.data["done"] is True
    assert result.data["before"] == {"light.a": "off"}
    assert result.data["result"] == {"finished": True, "after": {"light.a": "on"}}
    assert calls[0].data == {"entity_id": ["light.a"], "brightness": 5}
    assert calls[0].context.user_id is not None


@pytest.mark.usefixtures("allow_light")
async def test_call_returns_the_response(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """What a service answers is returned, redacted like anything else."""

    async def answer(call: ServiceCall) -> dict[str, Any]:
        return {"api_key": "abc", "value": 1}

    hass.services.async_register(
        "light", "report", answer, supports_response=SupportsResponse.ONLY
    )
    result = await _call(call_tool, "light.report")
    assert result.data["result"]["response"] == {"api_key": "**REDACTED**", "value": 1}


@pytest.mark.usefixtures("allow_light")
async def test_slow_call_goes_on(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Stopping something halfway is worse than not knowing how it ended."""
    release = asyncio.Event()
    finished: list[bool] = []

    async def slow(call: ServiceCall) -> None:
        await release.wait()
        finished.append(True)

    hass.services.async_register("light", "slow", slow)
    with patch(
        "custom_components.llm_admin_tools.actions.services.SERVICE_CALL_TIMEOUT", 0.01
    ):
        result = await _call(call_tool, "light.slow")
    assert result.data["result"] == {"finished": False}
    release.set()
    await hass.async_block_till_done()
    assert finished


@pytest.mark.usefixtures("allow_light")
async def test_service_must_exist(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Being allowed does not make it exist."""
    assert await _refusal(call_tool, "light.nope") == "service_not_found"


@pytest.mark.usefixtures("allow_light")
@pytest.mark.parametrize(
    "service",
    [
        "switch.turn_on",
        "shell_command.anything",
        "SHELL_COMMAND.anything",
        "homeassistant.restart",
        "hassio.host_shutdown",
    ],
)
async def test_other_services_are_refused(call_tool: CallTool, service: str) -> None:
    """Allowing a domain says nothing about others, however they are written."""
    assert await _refusal(call_tool, service) == "service_not_allowed"


@pytest.mark.parametrize(
    ("allowed", "service", "permitted"),
    [
        (["light.turn_on"], "light.turn_on", True),
        (["light.turn_on"], "light.turn_off", False),
        ([" Light "], "light.turn_on", True),
        (["homeassistant.restart"], "homeassistant.restart", True),
        (["homeassistant.restart"], "homeassistant.reload_all", False),
        # Every domain, as if each were named: so not those named one at a time.
        (["*"], "light.turn_on", True),
        (["*"], "script.anything", True),
        (["*"], "homeassistant.restart", False),
        (["*", "homeassistant.restart"], "homeassistant.restart", True),
        (["*"], "hassio.backup_full", False),
        (["*"], "homeassistant.stop", False),
        (["*"], "shell_command.run", False),
        (["*"], "llm_admin_tools.anything", False),
        # Even if they get into the saved settings: the settings menu refuses them.
        (["homeassistant"], "homeassistant.restart", False),
        (["hassio"], "hassio.backup_full", False),
        (["homeassistant.stop"], "homeassistant.stop", False),
        (["shell_command"], "shell_command.run", False),
        (["shell_command.run"], "shell_command.run", False),
        (["python_script"], "python_script.run", False),
        (["pyscript.run"], "pyscript.run", False),
        (["command_line"], "command_line.reload", False),
        (["llm_admin_tools"], "llm_admin_tools.anything", False),
        (["mcp_server"], "mcp_server.anything", False),
        (["hassio.addon_stdin"], "hassio.addon_stdin", False),
        (["hassio.host_reboot"], "hassio.host_reboot", False),
    ],
)
async def test_allowed_services(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    allowed: list[str],
    service: str,
    permitted: bool,
) -> None:
    """Some can only be allowed one at a time, and some never."""
    await apply_options(
        hass, init_integration, write_subjects=["services"], allowed_services=allowed
    )
    domain, _, name = service.partition(".")
    policy = init_integration.runtime_data.writes
    assert policy.service_allowed(domain, name) is permitted


@pytest.mark.parametrize(
    "key",
    ["entity_id", "device_id", "area_id", "floor_id", "label_id", "target", "exclude"],
)
@pytest.mark.usefixtures("allow_light")
async def test_targets_cannot_be_in_data(
    hass: HomeAssistant, call_tool: CallTool, key: str
) -> None:
    """They would get around the check of entity_ids."""
    calls = async_mock_service(hass, "light", "turn_on")
    assert (
        await _refusal(call_tool, "light.turn_on", data={key: "x"})
        == "target_in_service_data"
    )
    assert not calls


@pytest.mark.usefixtures("allow_light")
async def test_malformed_and_all_entities_are_refused(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Only entities named one by one."""
    async_mock_service(hass, "light", "turn_on")
    for entity_id in ("all", "light.a,light.b", "not an id"):
        assert (
            await _refusal(call_tool, "light.turn_on", entity_ids=[entity_id])
            == "invalid_entity_id"
        )


@pytest.mark.usefixtures("allow_light")
async def test_too_many_entities_are_refused(call_tool: CallTool) -> None:
    """A mistake can only reach so far."""
    with pytest.raises(Exception, match="entity_ids"):
        await _call(
            call_tool,
            "light.turn_on",
            entity_ids=[f"light.a{index}" for index in range(51)],
        )


async def test_hidden_looks_unknown_and_protected_says_so(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A client cannot tell hidden from mistyped, but is told about protected."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["light"],
        excluded_entities=["light.hidden"],
        protected_entities=["light.kept"],
    )
    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.hidden", "off")
    hass.states.async_set("light.kept", "off")
    for entity_id, expected in (
        ("light.hidden", "entity_not_found"),
        ("LIGHT.HIDDEN", "entity_not_found"),
        ("light.mistyped", "entity_not_found"),
        ("light.kept", "entity_protected"),
    ):
        assert (
            await _refusal(call_tool, "light.turn_on", entity_ids=[entity_id])
            == expected
        )
    assert not calls


async def test_nested_entities_are_checked(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A scene made on the fly names what it changes in its data."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["scene"],
        protected_entities=["light.kept"],
    )
    calls = async_mock_service(hass, "scene", "apply")
    for data in (
        {"entities": {"light.kept": "on"}},
        {"entities": ["light.kept"]},
        {"snapshot_entities": ["light.kept"]},
    ):
        assert await _refusal(call_tool, "scene.apply", data=data) == "entity_protected"
    assert not calls
    await _call(call_tool, "scene.apply", data={"entities": {"light.free": "on"}})
    assert calls


async def test_templates_in_service_data_are_only_text(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A service call does not render them, so they can read nothing."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["notify"],
        excluded_entities=["light.hidden"],
    )
    calls = async_mock_service(hass, "notify", "send")
    await _call(call_tool, "notify.send", data={"message": "{{ 1 + 1 }}"})
    assert calls[0].data["message"] == "{{ 1 + 1 }}"
