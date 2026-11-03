"""Tests for what an update of a config entry may reach."""

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options

_REASON = "Asked for by the user in this test."


@pytest.fixture
async def source(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> MockConfigEntry:
    """Provide an entry that an entity belongs to, with changes to entries allowed."""
    for component in ("websocket_api", "config"):
        assert await async_setup_component(hass, component, {})
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    entity_registry.async_get_or_create(
        "sensor", "test", "one", suggested_object_id="one", config_entry=entry
    )
    await apply_options(hass, init_integration, write_subjects=["config_entries"])
    return entry


async def _update(call_tool: CallTool, entry: MockConfigEntry, **fields: Any) -> None:
    await call_tool(
        "manage_config_entry",
        entry_id=entry.entry_id,
        operation="update",
        fields=fields,
        reason=_REASON,
    )


async def test_polling_of_something_protected_stays_on(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    source: MockConfigEntry,
) -> None:
    """A protected entity would go on showing an old value, as if it were disabled."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        protected_entities=["sensor.one"],
    )
    with pytest.raises(HomeAssistantError) as err:
        await _update(call_tool, source, pref_disable_polling=True)
    assert err.value.translation_key == "in_use_by_protected"
    assert source.pref_disable_polling is False


async def test_a_field_that_is_not_known_counts_as_changing_something(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    source: MockConfigEntry,
) -> None:
    """A field Home Assistant adds later should not get past this unseen."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        protected_entities=["sensor.one"],
    )
    with pytest.raises(HomeAssistantError) as err:
        await _update(call_tool, source, title="Other", pref_new_thing=True)
    assert err.value.translation_key == "in_use_by_protected"
    assert source.title != "Other"


async def test_what_changes_nothing_running_stays_possible(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    source: MockConfigEntry,
) -> None:
    """A new name, or what happens to entities still to come, leaves these alone."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        protected_entities=["sensor.one"],
    )
    await _update(call_tool, source, title="Other", pref_disable_new_entities=True)
    assert source.title == "Other"
    assert source.pref_disable_new_entities is True


async def test_polling_can_be_turned_off_where_nothing_is_protected(
    call_tool: CallTool, source: MockConfigEntry
) -> None:
    """The check keeps what is protected, and nothing else."""
    await _update(call_tool, source, pref_disable_polling=True)
    assert source.pref_disable_polling is True
