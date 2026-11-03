# ruff: noqa: PLR0917
"""Tests for what has to hold when a value, a client or a change misbehaves."""

import asyncio
from datetime import timedelta
import json
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.actions.services import _note_late_failure
from custom_components.llm_admin_tools.const import (
    CONF_EXTRA_REDACTED_TEXT,
    DOMAIN,
)
from custom_components.llm_admin_tools.policy.limits import (
    MAX_CHANGE_SUMMARY_CHARACTERS,
    MAX_SCRUBBED_TEXT_CHARACTERS,
)
from custom_components.llm_admin_tools.policy.redaction import REDACTED, Redactor
from custom_components.llm_admin_tools.policy.settings import ExposureSettings
from custom_components.llm_admin_tools.queries import history
from custom_components.llm_admin_tools.queries.definitions import _scene_config
from custom_components.llm_admin_tools.serialization import to_json_safe
from custom_components.llm_admin_tools.tools.params import parse_time
from custom_components.llm_admin_tools.tools.write_base import change_summary
from homeassistant.core import HomeAssistant, ServiceCall, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
    llm,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import SECRET_VALUE, CallTool, apply_options

_REASON = "Asked for by the user in this test."
_SERIAL = "SN-12345"
_MAC = "aa:bb:cc:dd:ee:ff"
_UNIQUE_ID = "unique-aabbcc"
DOMAIN_LOGGER = "custom_components.llm_admin_tools"


async def _refusal(call_tool: CallTool, tool: str, /, **args: Any) -> str | None:
    with pytest.raises(Exception) as err:
        await call_tool(tool, reason=_REASON, **args)
    return getattr(err.value, "translation_key", None)


async def _core(hass: HomeAssistant, *components: str) -> None:
    for component in ("websocket_api", "config", *components):
        assert await async_setup_component(hass, component, {})
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "text",
    [
        "a." * (MAX_SCRUBBED_TEXT_CHARACTERS // 2),
        "a." * (MAX_SCRUBBED_TEXT_CHARACTERS // 2 - 1) + "a@",
        "a" * MAX_SCRUBBED_TEXT_CHARACTERS,
        "a@" * (MAX_SCRUBBED_TEXT_CHARACTERS // 2),
        "a:" * (MAX_SCRUBBED_TEXT_CHARACTERS // 2),
        "a://" * (MAX_SCRUBBED_TEXT_CHARACTERS // 4),
        "?a-" * (MAX_SCRUBBED_TEXT_CHARACTERS // 3),
        "eyJ" + "a" * (MAX_SCRUBBED_TEXT_CHARACTERS - 3),
        "token" * (MAX_SCRUBBED_TEXT_CHARACTERS // 5),
        "token_" * (MAX_SCRUBBED_TEXT_CHARACTERS // 6),
        ("token" * 20 + " " * 20) * (MAX_SCRUBBED_TEXT_CHARACTERS // 120),
        "token: " * (MAX_SCRUBBED_TEXT_CHARACTERS // 7),
        'token: "' * (MAX_SCRUBBED_TEXT_CHARACTERS // 8),
        "auth_" * (MAX_SCRUBBED_TEXT_CHARACTERS // 5),
        "auth-" * (MAX_SCRUBBED_TEXT_CHARACTERS // 5),
        "cookie" * (MAX_SCRUBBED_TEXT_CHARACTERS // 6),
        "cookie: " * (MAX_SCRUBBED_TEXT_CHARACTERS // 8),
        "basic " * (MAX_SCRUBBED_TEXT_CHARACTERS // 6),
        "basic " + "a" * (MAX_SCRUBBED_TEXT_CHARACTERS - 6),
    ],
)
def test_the_longest_text_takes_no_time(text: str) -> None:
    """Text that nearly matches holds up all of Home Assistant while it is checked."""
    redactor = Redactor((), ExposureSettings(redact_personal=True, redact_network=True))
    started = time.monotonic()
    redactor.scrub_text(text)
    # Quadratic matching would take minutes, so this leaves room for a slow machine.
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("to 8.8.4.4", f"to {REDACTED}"),
        ("to 8.8.4.4:443 now", f"to {REDACTED}:443 now"),
        ("http://8.8.4.4:8123/x", f"http://{REDACTED}:8123/x"),
        ("from 8.8.4.4.", f"from {REDACTED}."),
        ("from 8.8.4.4: refused", f"from {REDACTED}: refused"),
        ("address:8.8.4.4", f"address:{REDACTED}"),
        ("[2001:4860:4860::8888]:443", f"[{REDACTED}]:443"),
        ("from 2001:4860:4860::8888.", f"from {REDACTED}."),
        ("2001:4860:4860::8888: refused", f"{REDACTED}: refused"),
        ("::ffff:8.8.4.4", f"::ffff:{REDACTED}"),
        ("version 1.2.3.4.5", "version 1.2.3.4.5"),
        ("at 10:34:13.", "at 10:34:13."),
        ("at 10:34:13: done", "at 10:34:13: done"),
        ("to 192.168.1.5:8123", "to 192.168.1.5:8123"),
    ],
)
def test_a_public_address_is_found_whatever_follows_it(
    text: str, expected: str
) -> None:
    """A port or a full stop after an address does not make it something else."""
    assert Redactor().scrub_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("to 192.168.1.5:8123", f"to {REDACTED}:8123"),
        ("from 192.168.1.5.", f"from {REDACTED}."),
        ("fe80::1: refused", f"{REDACTED}: refused"),
        ("up to fe80::1", f"up to {REDACTED}"),
    ],
)
def test_a_private_address_is_withheld_with_the_network_details(
    text: str, expected: str
) -> None:
    """The same goes for addresses that say where things are at home."""
    settings = ExposureSettings(redact_network=True)
    assert Redactor(settings=settings).scrub_text(text) == expected


@pytest.mark.parametrize(
    "key", ["host", "hostname", "host_name", "hostName", "ssid", "essid", "wifi_essid"]
)
def test_network_names_are_withheld_under_each_spelling(key: str) -> None:
    """Integrations do not agree on how to write these."""
    assert Redactor(settings=ExposureSettings(redact_network=True)).is_sensitive_key(
        key
    )
    assert not Redactor().is_sensitive_key(key)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://user:hunter2@example.com/a", f"https://{REDACTED}@example.com/a"),
        ("/a?auth_sig=abc&x=1", f"/a?auth_sig={REDACTED}&x=1"),
        ("eyJabcde.fghijkl.mno", REDACTED),
        ("write to me.you+x@mail.example.com now", f"write to {REDACTED} now"),
    ],
)
def test_what_is_sensitive_is_still_recognized(text: str, expected: str) -> None:
    """Bounding the patterns leaves them able to find what they are for."""
    assert Redactor().scrub_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "{'access_token': 'THE-VALUE'}",
        '{"access_token": "THE-VALUE"}',
        '{\\"access_token\\": \\"THE-VALUE\\"}',
        "access_token=THE-VALUE",
        "refresh_token: THE-VALUE",
        "client_secret=THE-VALUE",
        "clientSecret: THE-VALUE",
        "accessToken=THE-VALUE",
        "x-api-key: THE-VALUE",
        "apiKey=THE-VALUE",
        "session_id=THE-VALUE",
        "sessionId: THE-VALUE",
        "webhook_id: THE-VALUE",
        "auth: THE-VALUE",
        "oauth=THE-VALUE",
        "basicAuth=THE-VALUE",
        "auth_method: THE-VALUE",
        "{'password': 'two THE-VALUE'}",
        "{'password': 'with \\' THE-VALUE'}",
        "password: 'never closes THE-VALUE",
        "Authorization: Basic dXNlcjpwYXNz THE-VALUE",
        "{'Authorization': 'Token THE-VALUE', 'Host': 'x'}",
        'Authorization: Digest username="a", response="THE-VALUE"',
        "Cookie: sid=abc; other=THE-VALUE",
        "Set-Cookie: sid=THE-VALUE; Path=/",
        "{'Cookie': 'sid=abc; other=THE-VALUE'}",
        "auth_token THE-VALUE-THAT-IS-LONG",
        "error_token: THE-VALUE",
        "InvalidCredentialError: password=THE-VALUE",
    ],
)
def test_credentials_are_found_by_more_than_one_name(text: str) -> None:
    """Log messages name them in many ways, and quote what is in dictionaries."""
    assert "THE-VALUE" not in Redactor().scrub_text(text)


def test_basic_credentials_are_found_on_their_own() -> None:
    """Only with what no word has, so that a sentence stays readable."""
    redactor = Redactor()
    assert redactor.scrub_text("sent basic dXNlcjpwYXNz now") == (
        f"sent basic {REDACTED} now"
    )
    for sentence in ("Basic authentication failed", "a basic mode", "basic"):
        assert redactor.scrub_text(sentence) == sentence


@pytest.mark.parametrize(
    "text",
    [
        "author: Someone",
        "authenticated: true",
        "authority=none",
        "Failed to refresh the access token, trying later",
        "Unable to authenticate with the token",
        "InvalidCredentialError: Invalid login",
        "TokenExpiredException: Try again",
        "auth_error: refused",
        "state: on",
    ],
)
def test_what_only_looks_like_a_credential_stays_readable(text: str) -> None:
    """Hiding too much is accepted, but not for what is common in a log."""
    assert Redactor().scrub_text(text) == text


def test_other_values_stay_next_to_a_quoted_credential() -> None:
    """Quotes are kept, and what follows them is left alone."""
    assert Redactor().scrub_text("{'access_token': 'abc', 'state': 'on'}") == (
        f"{{'access_token': '{REDACTED}', 'state': 'on'}}"
    )


def test_a_time_that_does_not_exist_is_refused() -> None:
    """It looks like one, so it is not found out until it is read as a date."""
    with pytest.raises(HomeAssistantError) as err:
        parse_time("2026-02-30T00:00:00")
    assert err.value.translation_key == "invalid_time"


@pytest.mark.parametrize("kind", [ValueError, KeyError, RuntimeError])
async def test_unexpected_errors_say_nothing_but_their_type(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    caplog: pytest.LogCaptureFixture,
    kind: type[Exception],
) -> None:
    """Whatever they quote, a client does not get it, not even from the log."""

    async def boom(call: ServiceCall) -> None:
        raise kind(f"failed with an_unknown_name={SECRET_VALUE}")

    hass.services.async_register("testsvc", "boom", boom)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["testsvc"],
    )
    caplog.set_level(logging.WARNING)
    with pytest.raises(HomeAssistantError) as err:
        await call_tool(
            "call_service", domain="testsvc", service="boom", reason=_REASON
        )
    assert err.value.translation_key == "tool_failed"
    assert err.value.translation_placeholders == {"kind": kind.__name__}
    assert SECRET_VALUE not in str(err.value)
    assert SECRET_VALUE not in caplog.text
    assert f"failed with {kind.__name__}" in caplog.text


async def test_debug_logging_shows_the_owner_what_failed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only if the owner turned that on."""

    async def boom(call: ServiceCall) -> None:
        raise ValueError("this is what happened")

    hass.services.async_register("testsvc", "boom", boom)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["testsvc"],
    )
    caplog.set_level(logging.DEBUG, logger=DOMAIN_LOGGER)
    with pytest.raises(HomeAssistantError):
        await call_tool(
            "call_service", domain="testsvc", service="boom", reason=_REASON
        )
    assert "this is what happened" in caplog.text


async def test_a_logger_filter_goes_by_what_a_client_sees(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Going by the real name would let a client recover it, a character at a time."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()
    logging.getLogger(f"test.{SECRET_VALUE}").error("plain text")
    await hass.async_block_till_done()

    async def count(tool: str, logger: str, **args: Any) -> int:
        return (await call_tool(tool, logger=logger, **args)).data["total_count"]

    assert await count("get_system_log", f"test.{SECRET_VALUE[:4]}") == 0
    assert await count("get_system_log", f"test.{REDACTED}") == 1

    async def log_text(*_: Any, **__: Any) -> str:
        return (
            f"2026-11-02 10:00:00.100 INFO (MainThread) [test.{SECRET_VALUE}] plain\n"
        )

    hass.data["hassio"] = MagicMock(send_command=log_text)
    await apply_options(hass, init_integration)
    assert await count("get_full_log", f"test.{SECRET_VALUE[:4]}", reason=_REASON) == 0
    assert await count("get_full_log", f"test.{REDACTED}", reason=_REASON) == 1


def test_text_too_long_to_check_is_withheld() -> None:
    """What cannot be checked in time cannot be known to be harmless."""
    redactor = Redactor()
    assert redactor.scrub_text("a" * (MAX_SCRUBBED_TEXT_CHARACTERS + 1)) == REDACTED
    assert redactor.scrub_text("a" * MAX_SCRUBBED_TEXT_CHARACTERS) != REDACTED


async def test_nesting_does_not_get_a_value_past_redaction(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """As text, redaction could no longer tell what the keys of a value are."""
    node: dict[str, Any] = {"private_key": "abc-escaped-value"}
    for _ in range(40):
        node = {"n": node}
    hass.states.async_set("sensor.deep", "1", {"deep": node})
    result = await call_tool("get_entity", entity_id="sensor.deep")
    assert "abc-escaped-value" not in json.dumps(result.data, default=str)
    assert REDACTED in json.dumps(to_json_safe(node))


def test_blank_and_short_entries_withhold_nothing() -> None:
    """An older version allowed saving these, and "" is in everything."""
    settings = ExposureSettings.from_options(
        {CONF_EXTRA_REDACTED_TEXT: ["", "  ", "x", "ok"]}
    )
    assert settings.extra_redacted_text == {"ok"}
    assert Redactor((), settings).scrub_text("x y") == "x y"
    assert Redactor((), settings).scrub_text("is ok") == f"is {REDACTED}"


async def test_settings_refuse_text_that_would_remove_everything(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Blank lines are dropped, and what is too short is pointed out."""
    flow = await hass.config_entries.options.async_init(init_integration.entry_id)
    step = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "redaction"}
    )
    result = await hass.config_entries.options.async_configure(
        step["flow_id"], {CONF_EXTRA_REDACTED_TEXT: ["x"]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EXTRA_REDACTED_TEXT: "withheld_text_too_short"}
    result = await hass.config_entries.options.async_configure(
        step["flow_id"], {CONF_EXTRA_REDACTED_TEXT: ["", " ", "ok"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options[CONF_EXTRA_REDACTED_TEXT] == ["ok"]


async def test_tools_kept_from_before_stop_working(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
) -> None:
    """Who holds on to tools would keep what they could do, even after an unload."""
    await _core(hass)
    hass.states.async_set("light.x", "on")
    await apply_options(hass, init_integration, write_subjects=["areas"])
    kept = await llm.async_get_api(hass, DOMAIN, admin_context)
    read = llm.ToolInput(tool_name="get_entity", tool_args={"entity_id": "light.x"})
    assert (await kept.async_call_tool(read)).data["state"] == "on"

    await apply_options(hass, init_integration, excluded_entities=["light.x"])
    with pytest.raises(HomeAssistantError) as err:
        await kept.async_call_tool(read)
    assert err.value.translation_key == "tools_outdated"

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    with pytest.raises(HomeAssistantError) as err:
        await kept.async_call_tool(
            llm.ToolInput(
                tool_name="write_registry_item",
                tool_args={
                    "kind": "area",
                    "operation": "create",
                    "fields": {"name": "After unload"},
                    "reason": _REASON,
                },
            )
        )
    assert err.value.translation_key == "tools_outdated"
    assert ar.async_get(hass).async_get_area_by_name("After unload") is None


async def test_what_goes_wrong_cannot_quote_a_secret(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A message goes to the client just like a result does."""

    async def boom(call: ServiceCall) -> None:
        raise HomeAssistantError(f"login failed with {SECRET_VALUE} at host")

    hass.services.async_register("testsvc", "boom", boom)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["testsvc"],
    )
    with pytest.raises(HomeAssistantError) as err:
        await call_tool(
            "call_service", domain="testsvc", service="boom", reason=_REASON
        )
    assert SECRET_VALUE not in str(err.value)
    assert "login failed" in str(err.value)


async def test_searching_the_log_finds_only_what_can_be_seen(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Otherwise, withheld text could be read one character at a time."""
    withheld = "my-private-text"
    await apply_options(hass, init_integration, extra_redacted_text=[withheld])
    assert await async_setup_component(hass, "system_log", {})
    logging.getLogger("some.lib").error("login with %s failed", withheld)
    await hass.async_block_till_done()

    async def found(search: str) -> list[Any]:
        result = await call_tool("get_system_log", logger="some.lib", search=search)
        return result.data["items"]

    assert await found("login with")
    assert await found(REDACTED)
    assert not await found(f"login with {withheld[:1]}")
    assert not await found(withheld)


async def test_filtering_entities_finds_only_what_can_be_seen(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Otherwise, listing entities would confirm a guess at what is withheld."""
    hass.states.async_set("sensor.value", SECRET_VALUE)
    hass.states.async_set("sensor.named", "on", {"friendly_name": f"N {SECRET_VALUE}"})

    async def found(**conditions: str) -> list[str]:
        result = await call_tool("list_entities", **conditions)
        return [item["entity_id"] for item in result.data["items"]]

    assert await found(state=SECRET_VALUE) == []
    assert await found(search=SECRET_VALUE) == []
    assert await found(state=REDACTED) == ["sensor.value"]
    assert await found(search="named") == ["sensor.named"]


def _automation(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "alias": "x",
        "triggers": [{"trigger": "event", "event_type": "go"}],
        "actions": [action],
    }


_RENDERED_ONLY_LATER = [
    {"action": "light.turn_off", "target": "{{ {'entity_id': 'light.guarded'} }}"},
    {"action": "light.turn_off", "data": "{{ {'entity_id': 'light.guarded'} }}"},
    {"action": "light.turn_off", "data_template": "{{ {'entity_id': 'light.a'} }}"},
    {"action": "light.turn_off", "data": {"{{ 'entity_id' }}": "light.guarded"}},
]


@pytest.mark.parametrize("action", _RENDERED_ONLY_LATER)
async def test_a_template_cannot_come_to_name_what_is_protected(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    action: dict[str, Any],
) -> None:
    """Core renders the whole of data and target, and the keys in them too."""
    await _core(hass, "automation")
    args = {
        "kind": "automation",
        "operation": "create",
        "config": _automation(action),
        "dry_run": True,
    }
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations"],
        allowed_services=["light"],
        protected_entities=["light.guarded"],
        write_allow_templates=True,
    )
    assert await _refusal(call_tool, "write_definition", **args) == (
        "group_target_refused"
    )
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations"],
        allowed_services=["light"],
        write_allow_templates=True,
    )
    await call_tool("write_definition", reason=_REASON, **args)


@pytest.fixture
async def device_with_child(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> tuple[dr.DeviceEntry, er.RegistryEntry]:
    """Return a device and an entity of it, which get their name and area from it."""
    await _core(hass)
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("test", "dev")},
        connections={(dr.CONNECTION_NETWORK_MAC, _MAC)},
        serial_number=_SERIAL,
        name="Dev",
    )
    child = entity_registry.async_get_or_create(
        "light",
        "test",
        _UNIQUE_ID,
        device_id=device.id,
        config_entry=source,
        suggested_object_id="child",
    )
    hass.states.async_set(child.entity_id, "on")
    return device, child


@pytest.mark.parametrize(
    "change",
    [
        {"name_by_user": "New"},
        {"disabled_by": "user"},
        {"area_id": "elsewhere"},
        {"operation": "delete"},
    ],
)
@pytest.mark.parametrize("option", ["protected_entities", "excluded_entities"])
async def test_a_device_cannot_be_changed_over_what_it_holds(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    area_registry: ar.AreaRegistry,
    device_with_child: tuple[dr.DeviceEntry, er.RegistryEntry],
    change: dict[str, Any],
    option: str,
) -> None:
    """What is protected would be disabled, removed or moved along with its device."""
    device, child = device_with_child
    if "area_id" in change:
        change = {"area_id": area_registry.async_create("Elsewhere").id}
    await apply_options(
        hass,
        init_integration,
        write_subjects=["devices"],
        write_allow_delete=True,
        **{option: [child.entity_id]},
    )
    assert (
        await _refusal(call_tool, "update_device", device_id=device.id, **change)
        == "in_use_by_protected"
    )
    await apply_options(
        hass, init_integration, write_subjects=["devices"], write_allow_delete=True
    )
    await call_tool(
        "update_device", device_id=device.id, reason=_REASON, dry_run=True, **change
    )


async def test_an_area_cannot_be_deleted_from_under_an_inherited_entity(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    device_with_child: tuple[dr.DeviceEntry, er.RegistryEntry],
) -> None:
    """An entity is in the area of its device, without saying so itself."""
    device, child = device_with_child
    area = area_registry.async_create("Room")
    device_registry.async_update_device(device.id, area_id=area.id)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["areas"],
        write_allow_delete=True,
        protected_entities=[child.entity_id],
    )
    assert (
        await _refusal(
            call_tool,
            "write_registry_item",
            kind="area",
            operation="delete",
            item_id=area.id,
        )
        == "in_use_by_protected"
    )


async def test_a_change_tells_no_more_than_reading_does(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    device_with_child: tuple[dr.DeviceEntry, er.RegistryEntry],
) -> None:
    """Core answers with serial numbers, MAC addresses and unique IDs."""
    device, child = device_with_child
    await apply_options(hass, init_integration, write_subjects=["devices", "entities"])
    renamed = await call_tool(
        "update_device", device_id=device.id, name_by_user="New", reason=_REASON
    )
    assert renamed.data["result"]["name_by_user"] == "New"
    text = json.dumps(renamed.data, default=str)
    assert _SERIAL not in text
    assert _MAC not in text
    updated = await call_tool(
        "update_entity", entity_id=child.entity_id, name="Named", reason=_REASON
    )
    assert updated.data["result"]["name"] == "Named"
    assert _UNIQUE_ID not in json.dumps(updated.data, default=str)


def _markdown(content: str) -> dict[str, Any]:
    return {"views": [{"cards": [{"type": "markdown", "content": content}]}]}


def _button(**action: Any) -> dict[str, Any]:
    return {"views": [{"cards": [{"type": "button", "tap_action": action}]}]}


@pytest.fixture
async def dashboards(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Have a dashboard to write to."""
    await _core(hass, "lovelace")
    await apply_options(hass, init_integration, write_subjects=["dashboards"])
    await call_tool(
        "write_dashboard",
        operation="create",
        fields={"url_path": "the-one", "title": "The one"},
        reason=_REASON,
    )


@pytest.mark.usefixtures("dashboards")
@pytest.mark.parametrize(
    "config",
    [
        _markdown('<img src="https://x.example/a">'),
        _markdown('<img src="//x.example/a">'),
        _markdown('<a href="&#104;ttps:x.example">a</a>'),
        _markdown('<a href="JavaScript:alert(1)">a</a>'),
        _markdown("[[[ return fetch('x') ]]]"),
        {"views": [{"https://x.example": "as a key"}]},
        _button(action="url", url_path="wss://x.example"),
        _markdown('<img src="/\\x.example/a">'),
        _markdown('<img src="\\\\x.example/a">'),
        _markdown('<img src="///x.example/a">'),
        _markdown('<img src="ht\ttps://x.example/a">'),
        _markdown('<img src="ht\ntps://x.example/a">'),
        _markdown('<img src="/\t/x.example/a">'),
        _markdown('<img src="//[2001:db8::1]/a">'),
        _markdown('<img src="//%78.example/a">'),
        _markdown('<a href="java\tscript:alert(1)">a</a>'),
        _markdown('<a href="VBScript:x">a</a>'),
        _markdown('<a href="data:text/html,x">a</a>'),
        _markdown('<a href="data:image/svg+xml;base64,x">a</a>'),
        _markdown('<a href="data:,x">a</a>'),
        _markdown('<img src="/&sol;x.example/a">'),
        _markdown('<a href="javascript&colon;alert(1)">a</a>'),
    ],
)
async def test_a_dashboard_cannot_reach_outside(
    call_tool: CallTool, config: dict[str, Any]
) -> None:
    """It runs in the browser of whoever opens it, and sees what is hidden."""
    assert (
        await _refusal(
            call_tool,
            "write_dashboard",
            operation="save_config",
            url_path="the-one",
            config=config,
        )
        == "dashboard_link_refused"
    )


@pytest.mark.usefixtures("dashboards")
@pytest.mark.parametrize(
    "text",
    [
        "Data: 5, and more",
        "Data:\nnone yet",
        "Half // a line",
        "5/6 and a path /local/a.png",
        "R&D at home",
        "Time 10:30",
    ],
)
async def test_ordinary_text_on_a_dashboard_stays_possible(
    call_tool: CallTool, text: str
) -> None:
    """Only what a browser would read as leading elsewhere is refused."""
    await call_tool(
        "write_dashboard",
        operation="save_config",
        url_path="the-one",
        config=_markdown(text),
        reason=_REASON,
    )


async def test_dashboard_templates_only_if_allowed_and_nothing_is_hidden(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    dashboards: None,
) -> None:
    """A template in a card reads anything, and shows it to whoever opens it."""
    args = {
        "operation": "save_config",
        "url_path": "the-one",
        "config": _markdown("{{ states('light.hidden') }}"),
    }
    assert await _refusal(call_tool, "write_dashboard", **args) == "templates_off"
    await apply_options(
        hass,
        init_integration,
        write_subjects=["dashboards"],
        write_allow_templates=True,
    )
    await call_tool("write_dashboard", reason=_REASON, **args)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["dashboards"],
        write_allow_templates=True,
        excluded_entities=["light.hidden"],
    )
    assert await _refusal(call_tool, "write_dashboard", **args) == "template_refused"


@pytest.mark.parametrize(
    ("config", "refusal"),
    [
        (_button(action="perform-action", perform_action="switch.turn_off"), None),
        (
            _button(action="perform-action", perform_action="{{ 'light.turn_on' }}"),
            "definition_part_refused",
        ),
        (_button(action="call-service", service="lock.unlock"), "service_not_allowed"),
        (_button(perform_action="lock.unlock"), "service_not_allowed"),
        (
            _button(
                perform_action="light.turn_on", target={"entity_id": "light.guarded"}
            ),
            "entity_protected",
        ),
        (_button(perform_action="light.turn_on", data={"brightness": 3}), None),
    ],
)
async def test_a_button_can_only_do_what_a_client_can(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    dashboards: None,
    config: dict[str, Any],
    refusal: str | None,
) -> None:
    """Whoever presses it acts with their own access, for what the client wrote."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["dashboards"],
        allowed_services=["light", "switch.turn_off"],
        protected_entities=["light.guarded"],
    )
    args = {"operation": "save_config", "url_path": "the-one", "config": config}
    if refusal is None:
        await call_tool("write_dashboard", reason=_REASON, **args)
    else:
        assert await _refusal(call_tool, "write_dashboard", **args) == refusal


@pytest.mark.usefixtures("dashboards")
async def test_replacing_the_default_dashboard_has_to_be_stated(
    call_tool: CallTool,
) -> None:
    """A path left out by mistake would replace what everyone sees."""
    config = _markdown("Hello")
    assert (
        await _refusal(
            call_tool, "write_dashboard", operation="save_config", config=config
        )
        == "dashboard_path_required"
    )
    for path in ({}, {"url_path": "default"}):
        with pytest.raises(HomeAssistantError) as err:
            await call_tool("get_dashboard", **path)
        assert err.value.translation_key == "nothing_saved"
    saved = await call_tool(
        "write_dashboard",
        operation="save_config",
        url_path="default",
        config=config,
        reason=_REASON,
    )
    assert saved.data["done"] is True
    read = await call_tool("get_dashboard", url_path="default")
    assert read.data["config"] == config
    assert (await call_tool("get_dashboard")).data["config"] == config


async def test_a_finished_change_is_not_reported_as_failed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    dashboards: None,
) -> None:
    """A client told that a change failed would make it again."""
    big = {"views": [{"title": "x" * 90_000} for _ in range(3)]}
    args = {"operation": "save_config", "url_path": "the-one", "reason": _REASON}
    first = await call_tool("write_dashboard", config=big, **args)
    assert first.data["done"] is True
    second = await call_tool("write_dashboard", config=_markdown("small"), **args)
    assert second.data["done"] is True
    assert second.data["before_left_out"] is True
    assert "before" not in second.data
    assert init_integration.runtime_data.journal.recent()[-1]["success"] is True


async def test_a_reason_may_quote_what_was_shown(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
) -> None:
    """Only what would be saved over a withheld value is refused."""
    await _core(hass)
    await apply_options(hass, init_integration, write_subjects=["labels"])
    args: dict[str, Any] = {"kind": "label", "operation": "create"}
    created = await call_tool(
        "write_registry_item",
        fields={"name": "x"},
        reason=f"The value shows as {REDACTED} to me.",
        **args,
    )
    assert created.data["done"] is True
    assert (
        await _refusal(
            call_tool, "write_registry_item", fields={"name": REDACTED}, **args
        )
        == "redacted_value"
    )


def test_a_summary_of_a_change_stays_short() -> None:
    """Field names come from the client."""
    fields = {"a" * MAX_CHANGE_SUMMARY_CHARACTERS: 1}
    assert len(change_summary("change", fields)) == MAX_CHANGE_SUMMARY_CHARACTERS


async def test_trying_a_service_call_says_what_the_real_one_would(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A dry run that passes on data core refuses does not say much."""
    assert await async_setup_component(
        hass, "input_number", {"input_number": {"n": {"min": 0, "max": 10, "step": 1}}}
    )
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["input_number"],
    )
    args: dict[str, Any] = {
        "domain": "input_number",
        "service": "set_value",
        "entity_ids": ["input_number.n"],
        "dry_run": True,
    }
    assert (
        await _refusal(call_tool, "call_service", data={"value": "abc"}, **args)
        == "service_data_invalid"
    )
    await call_tool("call_service", data={"value": 3}, reason=_REASON, **args)


async def test_a_call_that_fails_after_the_answer_is_noted(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The client has moved on, and its message can quote anything."""

    async def late(call: ServiceCall) -> None:
        await asyncio.sleep(0.03)
        raise HomeAssistantError(SECRET_VALUE)

    hass.services.async_register("testsvc", "late", late)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["testsvc"],
    )
    with patch(
        "custom_components.llm_admin_tools.actions.services.SERVICE_CALL_TIMEOUT", 0.01
    ):
        result = await call_tool(
            "call_service", domain="testsvc", service="late", reason=_REASON
        )
    assert result.data["result"] == {"finished": False}
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()
    assert "testsvc.late failed after it took too long" in caplog.text
    assert "HomeAssistantError" in caplog.text
    assert f"{SECRET_VALUE}" not in caplog.text


async def test_a_call_cancelled_after_the_answer_is_not_a_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Shutting down cancels what still runs."""
    call = asyncio.ensure_future(asyncio.sleep(10))
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    _note_late_failure("testsvc.late", call)
    assert "testsvc.late" not in caplog.text


async def test_too_many_changes_for_any_step_is_said_honestly(
    hass: HomeAssistant,
) -> None:
    """All that can be read of a busy entity are the oldest of its shortest step."""
    end = dt_util.utcnow()
    states = [
        State(
            "sensor.a",
            str(number),
            last_changed=(moment := end - timedelta(seconds=(100 - number) * 0.01)),
            last_updated=moment,
        )
        for number in range(100)
    ]

    def read(
        _hass: HomeAssistant, _entity_id: str, since: Any, until: Any, limit: int
    ) -> list[State]:
        return [state for state in states if since <= state.last_changed <= until][
            :limit
        ]

    with patch.object(history, "read_changes", read):
        result = history._read_newest_changes(
            hass, "sensor.a", end - timedelta(hours=2), end, 2, max_states=50
        )
    assert [point["state"] for point in result["points"]] == ["48", "49"]
    assert result["older_changes_left_out"] is True
    assert result["newer_changes_left_out"] is True
    assert result["covered_until"] > states[49].last_changed


async def test_an_area_does_not_show_a_hidden_label(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """Hidden looks like not there."""
    hidden = label_registry.async_create("Hidden")
    shown = label_registry.async_create("Shown")
    area_registry.async_create("Room", labels={hidden.label_id, shown.label_id})
    await apply_options(hass, init_integration, excluded_labels=[hidden.label_id])
    result = await call_tool("list_areas")
    assert result.data["items"][0]["labels"] == [shown.label_id]


def test_a_scene_says_what_state_it_sets() -> None:
    """An attribute of the same name is not what it sets the entity to."""
    stored = State("light.a", "on", {"state": "off", "brightness": 3})
    scene = SimpleNamespace(
        scene_config=SimpleNamespace(
            id="a", name="A", icon=None, states={"light.a": stored}
        )
    )
    config = _scene_config(scene)
    assert config is not None
    assert config["entities"]["light.a"] == {"state": "on", "brightness": 3}


async def test_a_tag_keeps_its_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Whoever knows it can act as if the tag was scanned."""
    await _core(hass, "tag")
    await apply_options(hass, init_integration, write_subjects=["helpers"])
    await call_tool(
        "write_helper",
        kind="tag",
        operation="create",
        fields={"tag_id": "nfc-card-0001", "name": "Door"},
        reason=_REASON,
    )
    await hass.async_block_till_done()
    listed = await call_tool("list_helpers", kind="tag")
    assert [item["name"] for item in listed.data["items"]] == ["Door"]
    assert "nfc-card-0001" not in json.dumps(listed.data, default=str)
    assert Redactor().is_sensitive_key("tag_id")


async def test_recent_activity_survives_a_reload_and_goes_with_the_integration(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    hass_storage: dict[str, Any],
) -> None:
    """A record that a restart wipes is not much of a record."""
    key = f"{DOMAIN}.audit_log"
    await call_tool("list_areas")
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["records"][-1]["tool"] == "list_areas"
    audit = init_integration.runtime_data.audit
    assert audit.as_diagnostics()[-1]["tool"] == "list_areas"
    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("device refused {'code': '0427'}", "0427"),
        ('device refused {"pin": "0427"}', "0427"),
        ('device refused {"code" : "0427", "state": "off"}', "0427"),
        ('sent {\\"pin\\": \\"0427\\"}', "0427"),
        ("{'name': 'kept', 'code': '0427'}", "0427"),
        ("{'code': '0427", "0427"),
    ],
)
def test_a_quoted_field_is_withheld_by_its_name(
    init_integration: MockConfigEntry, text: str, secret: str
) -> None:
    """A dictionary or JSON in a message says that this is a key."""
    scrubbed = init_integration.runtime_data.redactor.scrub_text(text)
    assert secret not in scrubbed
    assert "**REDACTED**" in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "status code: 500",
        'answered {"code": 500}',
        "{'name': 'kept', 'state': 'off'}",
        "'quoted' and more",
        "the code 'abc' failed",
        "exit code=1",
    ],
)
def test_other_text_stays_readable(
    init_integration: MockConfigEntry, text: str
) -> None:
    """These words are often not credentials, and what went wrong has to be readable."""
    assert init_integration.runtime_data.redactor.scrub_text(text) == text


async def test_a_quoted_field_follows_the_settings(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """What is withheld as a key depends on what the owner chose."""
    text = "{'host': 'somewhere.lan'}"
    assert init_integration.runtime_data.redactor.scrub_text(text) == text
    await apply_options(hass, init_integration, redact_network=True)
    scrubbed = init_integration.runtime_data.redactor.scrub_text(text)
    assert "somewhere.lan" not in scrubbed


async def test_a_code_in_an_error_or_the_log_is_withheld(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """What a service raises did not come from the client, nor does what is logged."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()
    await apply_options(
        hass, init_integration, write_subjects=["services"], allowed_services=["light"]
    )
    hass.states.async_set("light.a", "off")

    async def refuse(call: ServiceCall) -> None:
        raise HomeAssistantError("device refused {'code': '0427'}")

    hass.services.async_register("light", "refuse", refuse)
    with pytest.raises(HomeAssistantError) as err:
        await call_tool(
            "call_service",
            domain="light",
            service="refuse",
            entity_ids=["light.a"],
            reason="Asked for by the user in this test.",
        )
    assert "device refused" in str(err.value)
    assert "0427" not in str(err.value)

    logging.getLogger("test.audit").error("device refused {'pin': '0427'}")
    await hass.async_block_till_done()
    records = (await call_tool("get_system_log")).data
    assert "device refused" in str(records)
    assert "0427" not in str(records)


async def test_system_log_leaves_out_the_logger_of_a_hidden_automation(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Both the log and the report read it, and neither may name what is hidden."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()
    hass.states.async_set("automation.quiet", "on")
    hass.states.async_set("automation.open", "on")
    prefix = "homeassistant.components.automation"
    logging.getLogger(f"{prefix}.quiet").error("quiet: it failed")
    logging.getLogger(f"{prefix}.open").error("open: it failed")
    await hass.async_block_till_done()
    await apply_options(hass, init_integration, excluded_entities=["automation.quiet"])
    for tool in ("get_system_log", "get_health_report"):
        dumped = json.dumps((await call_tool(tool)).data)
        assert "quiet" not in dumped
        assert "automation.open" in dumped
