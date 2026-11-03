"""Group the records of the log, so that what repeats is read once."""

from collections.abc import Callable
import re
import time
from typing import Any

from ..policy.limits import MAX_HEALTH_TEXT_CHARACTERS

# What differs between repeats of one message: numbers, addresses and hexadecimal IDs.
_VARIABLE = re.compile(r"\b0x[0-9a-f]{1,32}\b|\b[0-9a-f]{8,64}\b|\b\d{1,20}\b", re.I)
# A pattern holds on to the interpreter while it runs, so with thousands of records in a
# row the event loop would have to wait its turn each time.
_RECORDS_BETWEEN_PAUSES = 50


def summarize_records(
    records: list[dict[str, Any]], scrub: Callable[[str], str]
) -> list[dict[str, Any]]:
    """Return records grouped by what they say, what repeats most first.

    Records come newest first. Withheld values go before anything is compared or cut, as
    in the rest of what reads the log. A traceback is known by its last line.
    """
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for number, record in enumerate(records):
        if number % _RECORDS_BETWEEN_PAUSES == 0:
            time.sleep(0)
        lines = scrub(record["message"]).splitlines() or [""]
        first = lines[0][:MAX_HEALTH_TEXT_CHARACTERS]
        last = lines[-1][:MAX_HEALTH_TEXT_CHARACTERS] if len(lines) > 1 else None
        logger = scrub(record["logger"])
        key = (
            record["level"],
            logger,
            _VARIABLE.sub("#", first),
            _VARIABLE.sub("#", last or ""),
        )
        group = groups.setdefault(
            key,
            {
                "level": record["level"],
                "logger": logger,
                "message": first,
                **({"last_line": last} if last else {}),
                "count": 0,
                "last": record["timestamp"],
            },
        )
        group["count"] += 1
        group["first"] = record["timestamp"]
    return sorted(groups.values(), key=lambda group: group["count"], reverse=True)
