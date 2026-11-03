"""Gather what is wrong right now, as short lists to look further into."""

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from ..policy.exposure import ExposurePolicy
from ..policy.limits import (
    MAX_HEALTH_EXAMPLES,
    MAX_HEALTH_ITEMS,
    MAX_HEALTH_TEXT_CHARACTERS,
)
from .compact import without_empty_fields
from .integrations import list_integrations
from .logs import get_system_log
from .repairs import (
    RepairQuery,
    async_describe_issues,
    spook_active,
    visible_issues,
)

_FAILING = frozenset(
    {
        ConfigEntryState.FAILED_UNLOAD,
        ConfigEntryState.MIGRATION_ERROR,
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
    }
)


async def async_repairs_summary(
    hass: HomeAssistant, policy: ExposurePolicy
) -> dict[str, Any]:
    """Return how many repairs there are, and the most severe ones."""
    issues = visible_issues(hass, policy, RepairQuery())
    domains: dict[str, int] = {}
    for issue in issues:
        domains[issue.domain] = domains.get(issue.domain, 0) + 1
    described = await async_describe_issues(
        hass, issues[:MAX_HEALTH_ITEMS], descriptions=False
    )
    return {
        "count": len(issues),
        "by_domain": domains,
        "spook": spook_active(hass),
        "items": [
            {
                key: item[key]
                for key in ("issue_id", "domain", "severity", "title")
                if key in item
            }
            for item in described
        ],
    }


@callback
def failing_integrations(
    hass: HomeAssistant, policy: ExposurePolicy
) -> list[dict[str, Any]]:
    """Return the config entries that did not start or stop as they should."""
    return [
        {key: entry[key] for key in ("entry_id", "domain", "title", "state", "reason")}
        for entry in list_integrations(hass, policy, None)
        if entry["state"] in _FAILING
    ]


@callback
def unavailable_entities(hass: HomeAssistant, policy: ExposurePolicy) -> dict[str, Any]:
    """Return how many entities are unavailable, by the integration providing them."""
    registry = er.async_get(hass)
    groups: dict[str | None, list[str]] = {}
    for state in hass.states.async_all():
        if state.state == STATE_UNAVAILABLE and policy.entity_allowed(state.entity_id):
            entry = registry.async_get(state.entity_id)
            platform = entry.platform if entry is not None else None
            groups.setdefault(platform, []).append(state.entity_id)
    largest_first = sorted(
        groups.items(), key=lambda group: len(group[1]), reverse=True
    )
    return {
        "count": sum(len(entity_ids) for entity_ids in groups.values()),
        "integrations_count": len(groups),
        "by_integration": [
            without_empty_fields(
                {
                    "integration": platform,
                    "count": len(entity_ids),
                    "examples": sorted(entity_ids)[:MAX_HEALTH_EXAMPLES],
                }
            )
            for platform, entity_ids in largest_first[:MAX_HEALTH_ITEMS]
        ],
    }


@callback
def recurring_log_records(
    hass: HomeAssistant, policy: ExposurePolicy, scrub: Callable[[str], str]
) -> list[dict[str, Any]]:
    """Return what the system log has counted most often.

    Withheld values go first, as text cut short could leave part of one unrecognizable.
    """

    def short(text: str) -> str:
        return scrub(text)[:MAX_HEALTH_TEXT_CHARACTERS]

    records = sorted(
        get_system_log(hass, policy, "WARNING", None, None, scrub=scrub),
        key=lambda record: record["count"],
        reverse=True,
    )
    return [
        without_empty_fields(
            {
                "level": record["level"],
                "logger": record["name"],
                "count": record["count"],
                "last": record["timestamp"],
                "message": short(record["message"][0]),
                "exception": (
                    short(record["exception"].strip().splitlines()[-1])
                    if record["exception"].strip()
                    else None
                ),
            }
        )
        for record in records[:MAX_HEALTH_ITEMS]
    ]
