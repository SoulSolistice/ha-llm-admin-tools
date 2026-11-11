"""The user's choices about what the tools expose, read from the entry options."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..const import (
    CONF_ALLOWED_SERVICES,
    CONF_ASSIST_EXPOSED_ONLY,
    CONF_ENABLED_TOOLS,
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITIES,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_EXCLUDED_LABELS,
    CONF_EXCLUDED_LOGGERS,
    CONF_EXTRA_REDACTED_KEYS,
    CONF_EXTRA_REDACTED_TEXT,
    CONF_PROTECTED_AREAS,
    CONF_PROTECTED_DOMAINS,
    CONF_PROTECTED_ENTITIES,
    CONF_PROTECTED_INTEGRATIONS,
    CONF_PROTECTED_LABELS,
    CONF_REDACT_LOCATION,
    CONF_REDACT_NETWORK,
    CONF_REDACT_PERSONAL,
    CONF_SCRUB_ESPHOME_SECRETS,
    CONF_WRITE_ALLOW_CUSTOM_CARDS,
    CONF_WRITE_ALLOW_DELETE,
    CONF_WRITE_ALLOW_TEMPLATES,
    CONF_WRITE_SUBJECTS,
)
from .limits import MIN_WITHHELD_TEXT_LENGTH

DEFAULT_REDACT_LOCATION = True
DEFAULT_REDACT_NETWORK = False
DEFAULT_REDACT_PERSONAL = True
DEFAULT_SCRUB_ESPHOME_SECRETS = True


@dataclass(slots=True, frozen=True, kw_only=True)
class ExposureSettings:
    """What is exposed; credentials are always withheld and are not a setting."""

    # None until the user chooses; from then on, a tool added later stays off.
    enabled_tools: frozenset[str] | None = None

    assist_exposed_only: bool = False
    excluded_areas: frozenset[str] = frozenset()
    excluded_domains: frozenset[str] = frozenset()
    excluded_entities: frozenset[str] = frozenset()
    excluded_integrations: frozenset[str] = frozenset()
    excluded_labels: frozenset[str] = frozenset()
    excluded_loggers: frozenset[str] = frozenset()

    extra_redacted_keys: frozenset[str] = frozenset()
    extra_redacted_text: frozenset[str] = frozenset()
    redact_location: bool = DEFAULT_REDACT_LOCATION
    redact_network: bool = DEFAULT_REDACT_NETWORK
    redact_personal: bool = DEFAULT_REDACT_PERSONAL
    scrub_esphome_secrets: bool = DEFAULT_SCRUB_ESPHOME_SECRETS

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> ExposureSettings:
        """Return the settings stored in config entry options."""

        def texts(key: str) -> frozenset[str]:
            return frozenset(text for text in options.get(key, ()) if text.strip())

        enabled_tools = options.get(CONF_ENABLED_TOOLS)
        return cls(
            enabled_tools=frozenset(enabled_tools)
            if enabled_tools is not None
            else None,
            assist_exposed_only=options.get(CONF_ASSIST_EXPOSED_ONLY, False),
            excluded_areas=texts(CONF_EXCLUDED_AREAS),
            excluded_domains=texts(CONF_EXCLUDED_DOMAINS),
            excluded_entities=texts(CONF_EXCLUDED_ENTITIES),
            excluded_integrations=texts(CONF_EXCLUDED_INTEGRATIONS),
            excluded_labels=texts(CONF_EXCLUDED_LABELS),
            excluded_loggers=texts(CONF_EXCLUDED_LOGGERS),
            extra_redacted_keys=texts(CONF_EXTRA_REDACTED_KEYS),
            extra_redacted_text=frozenset(
                text
                for text in texts(CONF_EXTRA_REDACTED_TEXT)
                if len(text) >= MIN_WITHHELD_TEXT_LENGTH
            ),
            redact_location=options.get(CONF_REDACT_LOCATION, DEFAULT_REDACT_LOCATION),
            redact_network=options.get(CONF_REDACT_NETWORK, DEFAULT_REDACT_NETWORK),
            redact_personal=options.get(CONF_REDACT_PERSONAL, DEFAULT_REDACT_PERSONAL),
            scrub_esphome_secrets=options.get(
                CONF_SCRUB_ESPHOME_SECRETS, DEFAULT_SCRUB_ESPHOME_SECRETS
            ),
        )


@dataclass(slots=True, frozen=True, kw_only=True)
class WriteSettings:
    """What may be changed; nothing can be until the owner chooses."""

    subjects: frozenset[str] = frozenset()
    allow_delete: bool = False
    allow_templates: bool = False
    # Custom cards run their own code in the browser of whoever opens a dashboard.
    allow_custom_cards: bool = False
    # A domain such as light, or one service such as light.turn_on.
    allowed_services: frozenset[str] = frozenset()

    protected_areas: frozenset[str] = frozenset()
    protected_domains: frozenset[str] = frozenset()
    protected_entities: frozenset[str] = frozenset()
    protected_integrations: frozenset[str] = frozenset()
    protected_labels: frozenset[str] = frozenset()

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> WriteSettings:
        """Return the settings stored in config entry options."""

        def texts(key: str) -> frozenset[str]:
            return frozenset(options.get(key, ()))

        return cls(
            subjects=texts(CONF_WRITE_SUBJECTS),
            allow_delete=options.get(CONF_WRITE_ALLOW_DELETE, False),
            allow_templates=options.get(CONF_WRITE_ALLOW_TEMPLATES, False),
            allow_custom_cards=options.get(CONF_WRITE_ALLOW_CUSTOM_CARDS, False),
            allowed_services=frozenset(
                entry.strip().lower()
                for entry in options.get(CONF_ALLOWED_SERVICES, ())
            ),
            protected_areas=texts(CONF_PROTECTED_AREAS),
            protected_domains=texts(CONF_PROTECTED_DOMAINS),
            protected_entities=texts(CONF_PROTECTED_ENTITIES),
            protected_integrations=texts(CONF_PROTECTED_INTEGRATIONS),
            protected_labels=texts(CONF_PROTECTED_LABELS),
        )

    def protection_as_scope(self) -> ExposureSettings:
        """Return what is protected in the form of a scope, to reuse how that works.

        What hides an entity from reading is what protects it from a change: its own
        name, its domain, its area, its labels, its integration or those of its device.
        """
        return ExposureSettings(
            excluded_areas=self.protected_areas,
            excluded_domains=self.protected_domains,
            excluded_entities=self.protected_entities,
            excluded_integrations=self.protected_integrations,
            excluded_labels=self.protected_labels,
        )
