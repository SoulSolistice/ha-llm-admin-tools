"""Tests for what a review found to get past the settings."""

from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.policy.redaction import REDACTED, Redactor
from custom_components.llm_admin_tools.tools.write_base import _holds_redacted_value
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options

_REASON = "Asked for by the user in this test."
_TRIGGER = [{"trigger": "event", "event_type": "go"}]
_JAVASCRIPT_CARD = {
    "type": "custom:config-template-card",
    "entities": ["sun.sun"],
    "card": {"type": "markdown", "content": "${'htt' + 'ps:/' + '/x.example'}"},
}


@pytest.fixture(autouse=True)
def use_temp_config(hass: HomeAssistant, tmp_path: Path) -> None:
    """Keep definition files out of the tree the tests run in."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include automations.yaml\n", encoding="utf-8"
    )
    (tmp_path / "automations.yaml").touch()


async def _refusal(call_tool: CallTool, name: str, **args: Any) -> str | None:
    with pytest.raises(Exception) as err:
        await call_tool(name, reason=_REASON, dry_run=True, **args)
    return getattr(err.value, "translation_key", None)


def _dashboard(*cards: dict[str, Any]) -> dict[str, Any]:
    return {"views": [{"cards": list(cards)}]}


async def _save_dashboard(call_tool: CallTool, config: dict[str, Any]) -> Any:
    return await call_tool(
        "write_dashboard",
        operation="save_config",
        url_path="default",
        config=config,
        reason=_REASON,
        dry_run=True,
    )


@pytest.mark.parametrize(
    "card",
    [
        _JAVASCRIPT_CARD,
        {"type": " Custom:button-card"},
        {"type": "tile", "entity": "sun.sun", "features": [{"type": "custom:x"}]},
        {"type": "cus\ttom:x"},
    ],
)
async def test_custom_cards_need_the_owner_to_allow_them(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    card: dict[str, Any],
) -> None:
    """A custom card is code in the browser, and some run their configuration."""
    assert await async_setup_component(hass, "lovelace", {})
    await apply_options(hass, init_integration, write_subjects=["dashboards"])
    with pytest.raises(Exception) as err:
        await _save_dashboard(call_tool, _dashboard(card))
    assert getattr(err.value, "translation_key", None) == "custom_card_refused"
    report = (await call_tool("get_capabilities")).data
    assert report["writes"]["custom_cards_allowed"] is False

    await apply_options(
        hass,
        init_integration,
        write_subjects=["dashboards"],
        write_allow_custom_cards=True,
    )
    assert (await _save_dashboard(call_tool, _dashboard(card))).data["dry_run"]
    report = (await call_tool("get_capabilities")).data
    assert report["writes"]["custom_cards_allowed"] is True


async def test_built_in_cards_need_nothing_allowed(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A type that is not text, or not custom, is left to the frontend."""
    assert await async_setup_component(hass, "lovelace", {})
    await apply_options(hass, init_integration, write_subjects=["dashboards"])
    config = _dashboard({"type": "markdown", "content": "Hello"}, {"type": 5})
    assert (await _save_dashboard(call_tool, config)).data["dry_run"]


@pytest.mark.parametrize(
    "part",
    [
        {
            "triggers": [
                {
                    "trigger": "numeric_state",
                    "entity_id": "sensor.seen",
                    "above": "sensor.hidden",
                }
            ]
        },
        {
            "conditions": [
                {
                    "condition": "numeric_state",
                    "entity_id": "sensor.seen",
                    "below": "sensor.hidden",
                }
            ]
        },
        {"triggers": [{"trigger": "time", "at": "input_datetime.hidden"}]},
        {"triggers": [{"trigger": "time", "at": ["07:00:00", "sensor.hidden"]}]},
        {"conditions": [{"condition": "time", "after": "input_datetime.hidden"}]},
        {"conditions": [{"condition": "time", "before": "input_datetime.hidden"}]},
        {
            "triggers": [
                {
                    "trigger": "zone",
                    "entity_id": "sensor.seen",
                    "zone": "zone.hidden",
                    "event": "enter",
                }
            ]
        },
        {
            "conditions": [
                {
                    "condition": "state",
                    "entity_id": "sensor.seen",
                    "state": "input_text.hidden",
                }
            ]
        },
    ],
)
async def test_what_a_trigger_compares_with_must_be_visible(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    part: dict[str, Any],
) -> None:
    """Whether the automation runs would otherwise tell what a hidden entity says."""
    assert await async_setup_component(hass, "automation", {})
    hidden = ["sensor.hidden", "input_datetime.hidden", "zone.hidden"]
    for entity_id in (*hidden, "input_text.hidden", "sensor.seen"):
        hass.states.async_set(entity_id, "5")
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations"],
        excluded_entities=[*hidden, "input_text.hidden"],
    )
    config = {"alias": "Test", "triggers": _TRIGGER, "actions": [], **part}
    assert (
        await _refusal(
            call_tool,
            "write_definition",
            kind="automation",
            operation="create",
            config=config,
        )
        == "entity_not_found"
    )


async def test_what_a_trigger_compares_with_can_be_a_value(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A number, a time and a state that is plain text name no entity."""
    assert await async_setup_component(hass, "automation", {})
    hass.states.async_set("sensor.seen", "5")
    hass.states.async_set("sensor.limit", "7")
    await apply_options(
        hass,
        init_integration,
        write_subjects=["automations"],
        excluded_entities=["sensor.hidden"],
    )
    config = {
        "alias": "Test",
        "triggers": [
            {"trigger": "numeric_state", "entity_id": "sensor.seen", "above": 3},
            {
                "trigger": "numeric_state",
                "entity_id": "sensor.seen",
                "below": "sensor.limit",
            },
            {"trigger": "time", "at": "07:00:00"},
        ],
        "conditions": [
            {"condition": "state", "entity_id": "sensor.seen", "state": "sensor.x"}
        ],
        "actions": [],
    }
    result = await call_tool(
        "write_definition",
        kind="automation",
        operation="create",
        config=config,
        reason=_REASON,
        dry_run=True,
    )
    assert result.data["dry_run"] is True


def test_coordinates_under_the_names_phones_use() -> None:
    """The apps for phones give where a phone is as Location."""
    redacted = Redactor().redact(
        {
            "Location": [52.37, 4.89],
            "location": [52.37, 4.89],
            "coordinates": [52.37, 4.89],
            "location_name": "Home",
        }
    )
    assert redacted == {
        "Location": REDACTED,
        "location": REDACTED,
        "coordinates": REDACTED,
        "location_name": "Home",
    }


def test_values_withheld_for_what_they_are_now() -> None:
    """They come and go, next to the values that are always withheld."""
    redactor = Redactor({"hunter2-the-secret"})
    redactor.set_live_values({"before-hunter2-the-secret", "9431-open"})
    assert redactor.scrub_text("x before-hunter2-the-secret 9431-open") == (
        f"x {REDACTED} {REDACTED}"
    )
    redactor.set_live_values(())
    assert redactor.scrub_text("9431-open hunter2-the-secret") == (
        f"9431-open {REDACTED}"
    )


@pytest.mark.parametrize("domain", ["input_text", "text"])
async def test_a_password_held_as_a_state_is_withheld(
    hass: HomeAssistant, call_tool: CallTool, domain: str
) -> None:
    """Its key is state, which says nothing, so it is known by what it is."""
    entity_id = f"{domain}.alarm"
    hass.states.async_set(entity_id, "9431-open", {"mode": "password"})
    hass.states.async_set("input_text.note", "9431-open is the code", {"mode": "text"})
    hass.states.async_set("input_text.short", "7", {"mode": "password"})
    hass.states.async_set("input_text.gone", "unavailable", {"mode": "password"})

    assert (await call_tool("get_entity", entity_id=entity_id)).data["state"] == (
        REDACTED
    )
    note = (await call_tool("get_entity", entity_id="input_text.note")).data
    assert note["state"] == f"{REDACTED} is the code"
    listed = (await call_tool("list_entities", state="9431-open")).data
    assert listed["items"] == []
    short = (await call_tool("get_entity", entity_id="input_text.short")).data
    assert short["state"] == "7"
    gone = (await call_tool("get_entity", entity_id="input_text.gone")).data
    assert gone["state"] == "unavailable"

    with pytest.raises(Exception) as err:
        await call_tool("get_entity", entity_id="input_text.9431-open")
    assert "9431-open" not in str(err.value)


async def test_a_password_set_by_a_call_is_withheld_in_its_result(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """The result gives the state before and after, and both are passwords."""
    assert await async_setup_component(
        hass,
        "input_text",
        {"input_text": {"alarm": {"mode": "password", "initial": "old-9431"}}},
    )
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["services"],
        allowed_services=["input_text"],
    )
    result = await call_tool(
        "call_service",
        domain="input_text",
        service="set_value",
        entity_ids=["input_text.alarm"],
        data={"value": "new-2718"},
        reason=_REASON,
    )
    assert result.data["before"] == {"input_text.alarm": REDACTED}
    assert result.data["result"]["after"] == {"input_text.alarm": REDACTED}


async def test_the_history_of_a_password_is_withheld(
    hass: HomeAssistant,
    recorder_mock: Any,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
) -> None:
    """What it was before is no longer its state, so it would not be recognized."""
    hass.states.async_set("input_text.alarm", "9431-open", {"mode": "password"})
    hass.states.async_set("input_text.note", "hello", {"mode": "text"})
    for name, args in (
        ("get_entity_history", {"entity_ids": ["input_text.alarm"]}),
        (
            "get_extended_history",
            {
                "entity_id": "input_text.alarm",
                "start": "2026-01-01T00:00:00+00:00",
                "reason": _REASON,
            },
        ),
    ):
        with pytest.raises(Exception) as err:
            await call_tool(name, **args)
        assert getattr(err.value, "translation_key", None) == "history_withheld"
    readable = await call_tool("get_entity_history", entity_ids=["input_text.note"])
    assert readable.data["entities"]["input_text.note"]["exists"] is True

    await apply_options(hass, init_integration, excluded_entities=["input_text.alarm"])
    hidden = await call_tool("get_entity_history", entity_ids=["input_text.alarm"])
    assert hidden.data["entities"]["input_text.alarm"]["exists"] is False


async def test_model_context_protocol_entries_cannot_be_changed(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """They decide which tools other servers add, so they are not a client's."""
    entry = MockConfigEntry(domain="mcp", title="Remote")
    entry.add_to_hass(hass)
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        write_allow_delete=True,
    )
    for operation in ("delete", "disable", "update"):
        assert (
            await _refusal(
                call_tool,
                "manage_config_entry",
                entry_id=entry.entry_id,
                operation=operation,
            )
            == "config_entry_protected"
        )


async def test_picture_of_a_person_stays_within_home_assistant(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A browser showing the person would otherwise load it from outside."""
    assert await async_setup_component(hass, "person", {})
    await apply_options(hass, init_integration, write_subjects=["persons"])
    for picture in ("https://tracker.example/p.png", "//tracker.example/p.png"):
        assert (
            await _refusal(
                call_tool,
                "write_helper",
                kind="person",
                operation="create",
                fields={"name": "Bea", "picture": picture},
            )
            == "picture_link_refused"
        )
    for picture in ("/local/bea.jpg", None):
        result = await call_tool(
            "write_helper",
            kind="person",
            operation="create",
            fields={"name": "Bea", "picture": picture},
            reason=_REASON,
            dry_run=True,
        )
        assert result.data["dry_run"] is True


def test_marker_as_a_key_is_refused_too() -> None:
    """Redaction scrubs keys, so one can come back as the marker."""
    assert _holds_redacted_value({REDACTED: 1})
    assert _holds_redacted_value({"a": [{f"x {REDACTED}": 1}]})
    assert not _holds_redacted_value({"a": [{"b": 1}]})


async def test_hidden_members_of_a_group_are_left_out(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A group lists what it stands for, which would name what is hidden."""
    hass.states.async_set("light.secret", "on")
    hass.states.async_set(
        "light.all", "on", {"entity_id": ["light.secret", "light.open", 5]}
    )
    hass.states.async_set("light.one", "on", {"entity_id": "light.secret"})
    hass.states.async_set("light.other", "on", {"entity_id": "light.open"})
    await apply_options(hass, init_integration, excluded_entities=["light.secret"])
    group = (await call_tool("get_entity", entity_id="light.all")).data
    assert group["attributes"]["entity_id"] == ["light.open", 5]
    single = (await call_tool("get_entity", entity_id="light.one")).data
    assert "entity_id" not in single["attributes"]
    open_one = (await call_tool("get_entity", entity_id="light.other")).data
    assert open_one["attributes"]["entity_id"] == "light.open"
