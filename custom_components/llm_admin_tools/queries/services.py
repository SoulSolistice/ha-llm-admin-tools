"""Describe the services a client may call, and nothing about the others."""

from collections.abc import Callable, Iterator
import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import translation
from homeassistant.helpers.service import async_get_all_descriptions

from ..policy.limits import MAX_SERVICE_DESCRIPTION_CHARACTERS
from ..policy.writes import WritePolicy

# Clients read these to choose what to call, for which any language will do.
_LANGUAGE = "en"


def _fields(description: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield the fields of a service, also those that the UI groups into sections."""
    for name, field in description.get("fields", {}).items():
        if "fields" in field:
            yield from field["fields"].items()
        else:
            yield name, field


def _describe(
    domain: str, service: str, description: dict[str, Any], texts: dict[str, str]
) -> dict[str, Any]:
    """Return what a client needs to call a service, leaving out what is not set."""
    key = f"component.{domain}.services.{service}"
    placeholders = description.get("description_placeholders", {})

    def text(config: dict[str, Any], item: str, translation_key: str) -> str:
        """Return a text of the description itself, or else its translation."""
        value: str = config.get(item) or texts.get(translation_key, "")
        for placeholder, replacement in placeholders.items():
            value = value.replace(f"{{{placeholder}}}", replacement)
        return value

    fields = {}
    for name, field in _fields(description):
        field_key = f"{key}.fields.{name}"
        details = {
            "name": text(field, "name", f"{field_key}.name"),
            "description": text(field, "description", f"{field_key}.description"),
            "required": field.get("required", False),
            "example": field.get("example"),
            "selector": field.get("selector"),
        }
        fields[name] = {item: value for item, value in details.items() if value}
    found = {
        "service": f"{domain}.{service}",
        "name": text(description, "name", f"{key}.name"),
        "description": text(description, "description", f"{key}.description"),
        "takes_entities": "target" in description,
        "returns_data": "response" in description,
        "fields": fields,
    }
    if len(json.dumps(found, default=str)) > MAX_SERVICE_DESCRIPTION_CHARACTERS:
        # Some selectors list so many options that they would fill the result.
        for details in fields.values():
            details.pop("selector", None)
        found["selectors_left_out"] = True
    return found


def _matches(
    found: dict[str, Any], words: list[str], scrub: Callable[[str], str]
) -> bool:
    """Return if what a client would see of a service holds every word."""
    text = scrub(f"{found['service']} {found['name']} {found['description']}")
    return all(word in text.casefold() for word in words)


async def async_describe_services(
    hass: HomeAssistant,
    policy: WritePolicy,
    domain: str | None,
    search: str | None,
    *,
    scrub: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Return the matching services a client may call, sorted by name."""
    descriptions = await async_get_all_descriptions(hass)
    allowed = {
        (service_domain, service): description
        for service_domain, services in descriptions.items()
        if domain is None or service_domain == domain
        for service, description in services.items()
        if policy.service_allowed(service_domain, service)
        and policy.service_visible(service_domain, service)
    }
    texts = await translation.async_get_translations(
        hass, _LANGUAGE, "services", {service_domain for service_domain, _ in allowed}
    )
    words = search.casefold().split() if search is not None else []
    return [
        found
        for (service_domain, service), description in sorted(allowed.items())
        if _matches(
            found := _describe(service_domain, service, description, texts),
            words,
            scrub,
        )
    ]
