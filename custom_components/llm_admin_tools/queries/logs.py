"""Read the warnings and errors collected by the system log."""

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from ..errors import read_failed
from ..policy.exposure import ExposurePolicy

# The key the system_log integration stores its handler under.
_DATA_SYSTEM_LOG = "system_log"

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _contains(record: dict[str, Any], needle: str, scrub: Callable[[str], str]) -> bool:
    """Return if what a client would see of a record holds a text, ignoring case.

    Looking in the record as it is would let a client recover what is withheld, one
    character at a time, from whether a search finds anything.
    """
    texts = [*record["message"], record["exception"]]
    return any(needle in scrub(text).casefold() for text in texts)


def _with_iso_times(record: dict[str, Any]) -> dict[str, Any]:
    """Return a record with its epoch times in the format the other tools use."""
    return {
        **record,
        "timestamp": dt_util.utc_from_timestamp(record["timestamp"]),
        "first_occurred": dt_util.utc_from_timestamp(record["first_occurred"]),
    }


@callback
def system_log_available(hass: HomeAssistant) -> bool:
    """Return if the system log integration is collecting records."""
    return _DATA_SYSTEM_LOG in hass.data


@callback
def get_system_log(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    min_level: str,
    logger: str | None,
    search: str | None,
    *,
    scrub: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Return the matching log records, newest first.

    Filters compare what a client would see, see ``_contains``. Whether a logger may be
    shown is the owner's choice, which goes by its real name.
    """
    handler = hass.data.get(_DATA_SYSTEM_LOG)
    if handler is None:
        raise read_failed("system_log_unavailable")

    allowed_levels = LOG_LEVELS[LOG_LEVELS.index(min_level) :]
    needle = search.casefold() if search is not None else None
    records: list[dict[str, Any]] = handler.records.to_list()
    logger_allowed = policy.logger_filter()
    return [
        _with_iso_times(record)
        for record in records
        if record["level"] in allowed_levels
        and logger_allowed(record["name"])
        and (logger is None or logger in scrub(record["name"]))
        and (needle is None or _contains(record, needle, scrub))
    ]
