"""Describe what the settings restrict and what can be read, naming nothing hidden."""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance

from ..policy.limits import MAX_SERVICE_ENTITIES, MAX_WRITES_PER_MINUTE
from ..policy.settings import ExposureSettings, WriteSettings
from ..policy.writes import (
    NAMED_SERVICES_ONLY_DOMAINS,
    NEVER_DOMAINS,
    NEVER_SERVICES,
    NEVER_TOUCHED,
)
from .full_log import full_log_available
from .history import recorder_running
from .logs import system_log_available
from .repairs import spook_active


def scope_restrictions(settings: ExposureSettings) -> dict[str, Any]:
    """Return which kinds of things are hidden, never which ones or how many."""
    kinds = {
        "areas": settings.excluded_areas,
        "domains": settings.excluded_domains,
        "entities": settings.excluded_entities,
        "integrations": settings.excluded_integrations,
        "labels": settings.excluded_labels,
        "loggers": settings.excluded_loggers,
    }
    hidden_kinds = [kind for kind, excluded in kinds.items() if excluded]
    return {
        "restricted": bool(hidden_kinds) or settings.assist_exposed_only,
        "only_entities_exposed_to_assist": settings.assist_exposed_only,
        "hidden_kinds": hidden_kinds,
    }


_WITHHELD_MEANS = {
    "credentials": (
        "Passwords, tokens, API keys, cookies and the like, under such a name or "
        "recognized in text"
    ),
    "values in secrets.yaml": "Each value of 6 or more characters from that file",
    "location": "Coordinates and altitude",
    "personal details": (
        "Email addresses, user names, phone numbers and public IP addresses"
    ),
    "network details": (
        "Private IP addresses, MAC addresses, network names and host names"
    ),
    "values in esphome/secrets.yaml": (
        "Each value of 6 or more characters from that file"
    ),
    "rules set by the owner": (
        "Key names and text the owner chose, which this report does not name"
    ),
}


def withheld_values(settings: ExposureSettings) -> dict[str, Any]:
    """Return which categories of values are withheld, never the rules themselves.

    A client cannot tell that something got through if it does not know what to expect.
    """
    categories = {
        "location": settings.redact_location,
        "personal details": settings.redact_personal,
        "network details": settings.redact_network,
        "values in esphome/secrets.yaml": settings.scrub_esphome_secrets,
    }
    custom = bool(settings.extra_redacted_keys or settings.extra_redacted_text)
    always = ["credentials", "values in secrets.yaml"]
    on = [
        *(category for category, withheld in categories.items() if withheld),
        *(["rules set by the owner"] if custom else []),
    ]
    off = [category for category, withheld in categories.items() if not withheld]
    return {
        "always": always,
        "on": on,
        "off": off,
        # A list, as "credentials" as a key would be withheld itself.
        "what_each_holds": [
            {"category": category, "holds": _WITHHELD_MEANS[category]}
            for category in (*always, *on, *off)
        ],
    }


def data_sources(hass: HomeAssistant) -> dict[str, Any]:
    """Return which optional sources can be read at the moment."""
    recorder = recorder_running(hass)
    return {
        "history": recorder,
        "states_kept_days": get_instance(hass).keep_days if recorder else None,
        "system_log": system_log_available(hass),
        "full_log": full_log_available(hass),
        "spook": spook_active(hass),
    }


NO_SERVICES_ALLOWED = (
    "No service is allowed. call_service is refused, and so is anything written "
    "that calls one, such as an automation or a dashboard button. Only the owner can "
    "allow them."
)
_SERVICES_ALLOWED = (
    "A domain allows all its services, a name such as light.turn_on only that one, "
    "and * those of every domain except never.domains_allowed_per_service_only. "
    "Automations, scripts and dashboard buttons that are written may call only these "
    "too. find_services lists them with their fields."
)


def write_capabilities(
    settings: WriteSettings, tools: list[str], *, templates_allowed: bool
) -> dict[str, Any]:
    """Return what can be changed, with which kinds of things are protected only."""
    if not settings.subjects:
        return {
            "enabled": False,
            "note": "The owner has not enabled changes. A client cannot change that.",
        }
    protected = {
        "areas": settings.protected_areas,
        "domains": settings.protected_domains,
        "entities": settings.protected_entities,
        "integrations": settings.protected_integrations,
        "labels": settings.protected_labels,
    }
    return {
        "enabled": True,
        "tools": tools,
        "subjects": sorted(settings.subjects),
        "delete_allowed": settings.allow_delete,
        "templates_allowed": templates_allowed,
        "custom_cards_allowed": settings.allow_custom_cards,
        "allowed_services": sorted(settings.allowed_services),
        "allowed_services_mean": (
            _SERVICES_ALLOWED if settings.allowed_services else NO_SERVICES_ALLOWED
        ),
        "protected_kinds": [kind for kind, names in protected.items() if names],
        "never": {
            "domains": sorted(NEVER_DOMAINS),
            "services": sorted(NEVER_SERVICES),
            "domains_allowed_per_service_only": sorted(NAMED_SERVICES_ONLY_DOMAINS),
            "things": NEVER_TOUCHED,
        },
        "limits": {
            "changes_per_minute": MAX_WRITES_PER_MINUTE,
            "entities_per_service_call": MAX_SERVICE_ENTITIES,
        },
        "every_change": {
            "needs": "a reason, which the owner reads",
            "dry_run": "reports what would happen, without doing it",
        },
        "note": (
            "Definitions run later with all rights, so they may only use what is "
            "allowed here. Allowing a service that runs other things, such as a "
            "script, allows whatever those do."
        ),
    }
