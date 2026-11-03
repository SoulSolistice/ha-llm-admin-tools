"""Translated errors raised by queries and tools."""

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN


def invalid_input(key: str, **placeholders: str) -> ServiceValidationError:
    """Return the error for a request that cannot be answered as asked."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def read_failed(key: str, **placeholders: str) -> HomeAssistantError:
    """Return the error for something that could not be read or done."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )
