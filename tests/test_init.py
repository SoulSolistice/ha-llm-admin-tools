"""Tests for setting up and unloading the integration."""

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm


def _api_ids(hass: HomeAssistant) -> set[str]:
    return {api.id for api in llm.async_get_apis(hass)}


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The API exists exactly as long as the entry is loaded."""
    assert init_integration.state is ConfigEntryState.LOADED
    assert DOMAIN in _api_ids(hass)

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert DOMAIN not in _api_ids(hass)


async def test_setup_fails_closed_without_secret_values(hass: HomeAssistant) -> None:
    """No tool is served if secret values cannot be withheld."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.llm_admin_tools.async_load_secret_files",
        side_effect=HomeAssistantError,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    # The file may be back once a disk or a share is, so Home Assistant retries.
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert DOMAIN not in _api_ids(hass)
