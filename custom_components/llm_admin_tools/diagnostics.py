"""Diagnostics for the LLM Admin Tools integration."""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .models import LlmAdminToolsConfigEntry
from .tools import build_tools

# Text from clients, naming what is in the home. People post diagnostics in public, so
# this stays in the settings of the integration, under recent activity.
_TO_REDACT = {"reason", "summary"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LlmAdminToolsConfigEntry
) -> dict[str, Any]:
    """Return what is served and how much is excluded, never the values."""
    runtime = entry.runtime_data
    settings = runtime.exposure.settings
    writes = runtime.writes.settings
    data: dict[str, Any] = {
        "tools": [tool.name for tool in build_tools(runtime)],
        "known_secret_values": runtime.redactor.secret_value_count,
        "secret_files": [
            {"name": file.name, "found": file.found, "values": len(file.values)}
            for file in runtime.secret_files
        ],
        "writes": {
            "subjects": sorted(writes.subjects),
            "allow_delete": writes.allow_delete,
            "allow_templates": writes.allow_templates,
            "allow_custom_cards": writes.allow_custom_cards,
            "allowed_services": sorted(writes.allowed_services),
            "protected_areas": len(writes.protected_areas),
            "protected_domains": len(writes.protected_domains),
            "protected_entities": len(writes.protected_entities),
            "protected_integrations": len(writes.protected_integrations),
            "protected_labels": len(writes.protected_labels),
        },
        "settings": {
            "assist_exposed_only": settings.assist_exposed_only,
            "redact_location": settings.redact_location,
            "redact_network": settings.redact_network,
            "redact_personal": settings.redact_personal,
            "excluded_areas": len(settings.excluded_areas),
            "excluded_domains": len(settings.excluded_domains),
            "excluded_entities": len(settings.excluded_entities),
            "excluded_integrations": len(settings.excluded_integrations),
            "excluded_labels": len(settings.excluded_labels),
            "excluded_loggers": len(settings.excluded_loggers),
            "extra_redacted_keys": len(settings.extra_redacted_keys),
            "extra_redacted_text": len(settings.extra_redacted_text),
        },
        "recent_calls": runtime.audit.as_diagnostics(),
        "recent_writes": runtime.journal.recent(),
    }
    return async_redact_data(data, _TO_REDACT)
