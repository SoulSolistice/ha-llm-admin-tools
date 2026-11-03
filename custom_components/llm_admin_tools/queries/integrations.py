"""Read the set up integrations from their config entries."""

from typing import Any

from homeassistant.core import HomeAssistant, callback

from ..policy.exposure import ExposurePolicy


@callback
def list_integrations(
    hass: HomeAssistant, policy: ExposurePolicy, domain: str | None
) -> list[dict[str, Any]]:
    """Return the status of config entries.

    Their ``data`` and ``options`` are never read: they hold passwords and tokens
    under names no redaction list can fully predict.
    """
    summaries = [
        {
            "entry_id": entry.entry_id,
            "domain": entry.domain,
            "title": entry.title,
            "state": entry.state,
            "reason": entry.reason,
            "source": entry.source,
            "disabled_by": entry.disabled_by,
        }
        for entry in hass.config_entries.async_entries(domain)
        if policy.integration_allowed(entry.entry_id)
    ]
    return sorted(
        summaries, key=lambda summary: (summary["domain"], summary["entry_id"])
    )
