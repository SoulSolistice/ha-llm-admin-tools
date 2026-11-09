"""Tests for the settings menu."""

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.const import (
    CONF_ALLOWED_SERVICES,
    CONF_ASSIST_EXPOSED_ONLY,
    CONF_ENABLED_TOOLS,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_EXTRA_REDACTED_TEXT,
    CONF_PROTECTED_ENTITIES,
    CONF_REDACT_LOCATION,
    CONF_REDACT_NETWORK,
    CONF_WRITE_ALLOW_DELETE,
    CONF_WRITE_SUBJECTS,
)
from custom_components.llm_admin_tools.tools import TOOL_CLASSES
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType

from .conftest import CallTool, apply_options


async def _open_step(
    hass: HomeAssistant, entry: MockConfigEntry, step: str
) -> FlowResult:
    """Open the menu and go to one of its steps."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step
    return result


async def _save_step(
    hass: HomeAssistant, entry: MockConfigEntry, step: str, user_input: dict[str, Any]
) -> FlowResult:
    """Fill in one step, save it and wait for the reload that applies it."""
    result = await _open_step(hass, entry, step)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()
    return result


def _suggested(result: FlowResult, key: str) -> Any:
    """Return what a form shows for a field."""
    for marker in result["data_schema"].schema:
        if marker == key:
            return (marker.description or {}).get("suggested_value")
    raise AssertionError(f"{key} is not in the form")


def _selector_options(result: FlowResult, key: str) -> list[Any]:
    """Return the choices a form offers for a select field."""
    for marker, selector in result["data_schema"].schema.items():
        if marker == key:
            return selector.config["options"]
    raise AssertionError(f"{key} is not in the form")


async def test_menu(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The menu offers one step per kind of decision."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [
        "tools",
        "scope",
        "redaction",
        "writes",
        "recent_activity",
    ]


async def test_menu_without_anything_to_show(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The record of what clients did is only there while the entry is loaded."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert "recent_activity" not in result["menu_options"]


async def test_recent_activity_starts_empty(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Nothing has happened yet, and leaving goes back to the menu."""
    result = await _open_step(hass, init_integration, "recent_activity")
    assert result["description_placeholders"] == {"changes": "-", "reads": "-"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert init_integration.options == {}


async def test_recent_activity_shows_text_from_clients_as_plain_text(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Rendered, an image in a reason would make the browser of the owner fetch it."""
    runtime = init_integration.runtime_data
    runtime.journal.record("old", "first", None, True, None)
    for number in range(60):
        runtime.journal.record("tool", f"change {number}", None, True, None)
    runtime.journal.record(
        "tool", "newest\nchange", None, False, "```\n![x](http://example.com/x.png)"
    )
    runtime.audit.record("list_areas", None, True)
    runtime.audit.record("call_service", None, True, "shown with the change")
    runtime.audit.record("get_extended_history", None, True, "needs more")

    result = await _open_step(hass, init_integration, "recent_activity")
    changes = result["description_placeholders"]["changes"]
    lines = changes.splitlines()
    assert lines[0] == lines[-1] == "```"
    assert lines[1].endswith(" failed newest change")
    assert lines[2] == "    " + "'" * 3 + " ![x](http://example.com/x.png)"
    assert "change 59" in lines[3]
    assert "first" not in changes
    reads = result["description_placeholders"]["reads"]
    assert "needs more" in reads
    assert "get_extended_history" in reads
    assert "shown with the change" not in reads
    assert "list_areas" not in reads


async def test_tools_start_all_selected(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Before the user chooses, the form shows what is actually in force."""
    result = await _open_step(hass, init_integration, "tools")
    assert _suggested(result, CONF_ENABLED_TOOLS) == [
        tool_class.name for tool_class in TOOL_CLASSES
    ]


async def test_all_tools_are_not_saved_as_a_list(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A tool that a later version adds should not start off: nobody turned it off."""
    await apply_options(
        hass,
        init_integration,
        **{CONF_ENABLED_TOOLS: ["list_areas"], CONF_REDACT_NETWORK: True},
    )
    selected = _suggested(
        await _open_step(hass, init_integration, "tools"), CONF_ENABLED_TOOLS
    )
    assert selected == ["list_areas"]
    await _save_step(
        hass,
        init_integration,
        "tools",
        {CONF_ENABLED_TOOLS: [tool.name for tool in TOOL_CLASSES]},
    )
    assert init_integration.options == {CONF_REDACT_NETWORK: True}


async def test_tools_saved_and_applied(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Saving reloads the entry, which applies the choice."""
    result = await _save_step(
        hass, init_integration, "tools", {CONF_ENABLED_TOOLS: ["list_areas"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {CONF_ENABLED_TOOLS: ["list_areas"]}
    await call_tool("list_areas")
    with pytest.raises(Exception, match="not found"):
        await call_tool("list_entities")


async def test_tools_cannot_all_be_turned_off(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An empty list is refused, and the form keeps what was entered."""
    result = await _save_step(hass, init_integration, "tools", {CONF_ENABLED_TOOLS: []})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENABLED_TOOLS: "no_tools_selected"}
    assert init_integration.options == {}


async def test_scope_saved_and_applied(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A domain chosen here is hidden from the tools."""
    hass.states.async_set("light.a", "on")
    result = await _open_step(hass, init_integration, "scope")
    assert "light" in _selector_options(result, CONF_EXCLUDED_DOMAINS)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXCLUDED_DOMAINS: ["light"]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (await call_tool("list_entities")).data["total_count"] == 0


async def test_scope_accepts_a_domain_without_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A domain can be hidden before it has any entity."""
    await _save_step(
        hass, init_integration, "scope", {CONF_EXCLUDED_DOMAINS: ["camera"]}
    )
    assert init_integration.options[CONF_EXCLUDED_DOMAINS] == ["camera"]


async def test_cleared_field_removes_the_saved_value(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A form submits nothing for an empty field, which must not keep the old value."""
    await apply_options(hass, init_integration, **{CONF_EXCLUDED_DOMAINS: ["light"]})
    result = await _open_step(hass, init_integration, "scope")
    assert _suggested(result, CONF_EXCLUDED_DOMAINS) == ["light"]
    await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert init_integration.options[CONF_EXCLUDED_DOMAINS] == []


async def test_steps_keep_each_others_settings(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Saving one step leaves what the other steps saved."""
    await _save_step(
        hass, init_integration, "tools", {CONF_ENABLED_TOOLS: ["list_areas"]}
    )
    await _save_step(hass, init_integration, "scope", {CONF_ASSIST_EXPOSED_ONLY: True})
    await _save_step(hass, init_integration, "redaction", {CONF_REDACT_NETWORK: True})
    assert init_integration.options[CONF_ENABLED_TOOLS] == ["list_areas"]
    assert init_integration.options[CONF_ASSIST_EXPOSED_ONLY] is True
    assert init_integration.options[CONF_REDACT_NETWORK] is True


async def test_integrations_can_be_chosen(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Config entries are named by their title and domain."""
    other = MockConfigEntry(domain="other", title="Other thing")
    other.add_to_hass(hass)
    result = await _open_step(hass, init_integration, "scope")
    assert {
        "value": other.entry_id,
        "label": "Other thing (other)",
    } in _selector_options(result, CONF_EXCLUDED_INTEGRATIONS)


async def test_removed_integration_does_not_block_saving(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An entry deleted after it was chosen stays a valid choice."""
    await apply_options(
        hass, init_integration, **{CONF_EXCLUDED_INTEGRATIONS: ["removed"]}
    )
    result = await _save_step(
        hass, init_integration, "scope", {CONF_EXCLUDED_INTEGRATIONS: ["removed"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_redaction_saved_and_applied(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Categories can be turned off and text added."""
    hass.states.async_set(
        "sensor.a", "1", {"latitude": 52.5, "note": "my-private-text"}
    )
    before = await call_tool("get_entity", entity_id="sensor.a")
    assert before.data["attributes"]["latitude"] == "**REDACTED**"
    assert before.data["attributes"]["note"] == "my-private-text"

    await _save_step(
        hass,
        init_integration,
        "redaction",
        {
            CONF_REDACT_LOCATION: False,
            CONF_EXTRA_REDACTED_TEXT: ["my-private-text"],
        },
    )
    after = await call_tool("get_entity", entity_id="sensor.a")
    assert after.data["attributes"]["latitude"] == 52.5
    assert after.data["attributes"]["note"] == "**REDACTED**"


async def test_removed_tool_does_not_block_saving(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A saved tool that an update removed is no longer offered as selected."""
    await apply_options(
        hass, init_integration, **{CONF_ENABLED_TOOLS: ["removed", "list_areas"]}
    )
    result = await _open_step(hass, init_integration, "tools")
    assert _suggested(result, CONF_ENABLED_TOOLS) == ["list_areas"]


async def test_changes_start_off_and_can_be_allowed(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Saved settings take effect, without touching those of the other steps."""
    await _save_step(hass, init_integration, "scope", {CONF_ASSIST_EXPOSED_ONLY: True})
    form = await _open_step(hass, init_integration, "writes")
    assert form["type"] is FlowResultType.FORM
    assert init_integration.runtime_data.writes.settings.subjects == frozenset()
    choices = form["data_schema"].schema[CONF_ALLOWED_SERVICES].config["options"]
    assert choices[0] == "*"

    await _save_step(
        hass,
        init_integration,
        "writes",
        {
            CONF_WRITE_SUBJECTS: ["services"],
            CONF_WRITE_ALLOW_DELETE: True,
            CONF_ALLOWED_SERVICES: ["*", "homeassistant.restart"],
            CONF_PROTECTED_ENTITIES: ["light.kept"],
        },
    )
    assert init_integration.options[CONF_ASSIST_EXPOSED_ONLY] is True
    settings = init_integration.runtime_data.writes.settings
    assert settings.subjects == {"services"}
    assert settings.allow_delete is True
    assert settings.allowed_services == {"*", "homeassistant.restart"}
    assert settings.protected_entities == {"light.kept"}


@pytest.mark.parametrize(
    "entry",
    [
        "homeassistant",
        "hassio",
        "shell_command",
        "shell_command.run",
        "homeassistant.stop",
        "hassio.addon_stdin",
        "llm_admin_tools",
        " Python_Script ",
    ],
)
async def test_what_can_never_be_allowed_is_refused_when_saving(
    hass: HomeAssistant, init_integration: MockConfigEntry, entry: str
) -> None:
    """The owner hears about it right away, instead of it silently not working."""
    form = await _open_step(hass, init_integration, "writes")
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {CONF_ALLOWED_SERVICES: [entry]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ALLOWED_SERVICES: "service_not_possible"}
    assert CONF_ALLOWED_SERVICES not in init_integration.options
