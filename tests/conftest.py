"""Fixtures for the LLM Admin Tools tests."""

from collections.abc import Awaitable, Callable, Generator
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.const import DOMAIN
from custom_components.llm_admin_tools.policy.secret_values import SecretFile
from homeassistant.auth.models import User
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm

SECRET_VALUE = "hunter2-the-secret"
# From 2026.11, Home Assistant refuses an API that requires an admin before it runs.
CORE_CHECKS_ADMIN = hasattr(llm, "async_get_authorized_api_instance")
HA_2026_11 = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 11)
# What the setup of a config entry returns when it worked. From 2026.11 that is None,
# and True fails for an integration that is not custom; up to 2026.10 None fails.
SETUP_SUCCEEDED = None if HA_2026_11 else True

type CallTool = Callable[..., Awaitable[llm.ToolResult]]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_db_url: str,
    enable_custom_integrations: None,
) -> None:
    """Let Home Assistant load the integration from custom_components.

    The recorder fixtures must resolve before ``hass``, which the second one uses.
    """


@pytest.fixture(autouse=True)
def fail_on_deprecated_usage(caplog: pytest.LogCaptureFixture) -> Generator[None]:
    """Fail a test in which Home Assistant reports this integration's API use.

    For a custom integration these reports are only log lines, so nothing else
    would catch them. Home Assistant reports each one once per process.
    """
    yield
    reports = [
        record.getMessage()
        for record in caplog.get_records("call")
        if f"custom integration '{DOMAIN}'" in record.getMessage()
    ]
    assert not reports


@pytest.fixture
def mock_secret_values() -> Generator[None]:
    """Provide secret values without a secrets.yaml."""
    with patch(
        "custom_components.llm_admin_tools.async_load_secret_files",
        return_value=[SecretFile("secrets.yaml", True, frozenset({SECRET_VALUE}))],
    ):
        yield


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_secret_values: None
) -> MockConfigEntry:
    """Set up the integration."""
    entry = MockConfigEntry(domain=DOMAIN, title="LLM Admin Tools")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def apply_options(
    hass: HomeAssistant, entry: MockConfigEntry, **options: Any
) -> None:
    """Change the settings of a loaded entry, as saving the settings menu does."""
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


def make_llm_context(user_id: str | None) -> llm.LLMContext:
    """Return the context of an LLM request by a user."""
    return llm.LLMContext(
        platform="test",
        context=Context(user_id=user_id),
        language="en",
        assistant="conversation",
        device_id=None,
    )


@pytest.fixture
def admin_context(hass_admin_user: User) -> llm.LLMContext:
    """Return the context of a request by an administrator."""
    return make_llm_context(hass_admin_user.id)


@pytest.fixture
def call_tool(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
) -> CallTool:
    """Return a function that calls a tool as an administrator."""

    async def _call(name: str, /, **args: Any) -> llm.ToolResult:
        instance = await llm.async_get_api(hass, DOMAIN, admin_context)
        return await instance.async_call_tool(
            llm.ToolInput(tool_name=name, tool_args=args)
        )

    return _call
