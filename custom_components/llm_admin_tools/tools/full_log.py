"""Tool for the whole log, requested on purpose and with a reason."""

import asyncio
from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant

from ..errors import invalid_input
from ..models import RuntimeData
from ..policy.limits import (
    MAX_EXTENDED_RESULT_CHARACTERS,
    MAX_FULL_LOG_LINES,
    MAX_LOG_RECORD_CHARACTERS,
    MAX_TEXT_FILTER_LENGTH,
    MIN_REASON_LENGTH,
)
from ..queries.full_log import (
    FullLogQuery,
    async_read_full_log,
    full_log_available,
)
from ..queries.logs import LOG_LEVELS
from .base import ReadOnlyTool
from .params import DATA_NOTICE, PAGING, TEXT, paginate, parse_time

# What get_full_log and summarize_log can narrow the log down by.
LOG_FILTERS: dict[Any, Any] = {
    probatio.Optional("logger", description="Text to find in the logger name."): TEXT,
    probatio.Optional(
        "search", description="Text to find in the message or traceback."
    ): TEXT,
    probatio.Optional(
        "start", description="Oldest time to return, as an ISO 8601 time."
    ): TEXT,
    probatio.Optional(
        "end", description="Newest time to return, as an ISO 8601 time."
    ): TEXT,
}


def full_log_query(args: Mapping[str, Any]) -> FullLogQuery:
    """Return which records were asked for."""
    start = parse_time(args["start"]) if "start" in args else None
    end = parse_time(args["end"]) if "end" in args else None
    if start is not None and end is not None and start >= end:
        raise invalid_input("invalid_time_window")
    return FullLogQuery(
        min_level=args["min_level"],
        logger=args.get("logger"),
        search=args.get("search"),
        start=start,
        end=end,
    )


def _shortened(record: dict[str, Any]) -> dict[str, Any]:
    """Return a record with a long message cut in the middle.

    What went wrong is usually at the end of a traceback, and what led to it at the
    start.
    """
    message: str = record["message"]
    left_out = len(message) - MAX_LOG_RECORD_CHARACTERS
    if left_out <= 0:
        return record
    half = MAX_LOG_RECORD_CHARACTERS // 2
    return {
        **record,
        "message": f"{message[:half]}\n…\n{message[-half:]}",
        "characters_left_out": left_out,
    }


class GetFullLogTool(ReadOnlyTool):
    """Get the records of the log that the system log does not keep."""

    name = "get_full_log"
    title = "Get the full log on request"
    description = (
        "Return the records of the Home Assistant log, newest first, including INFO "
        "and DEBUG, which get_system_log does not keep. Use this only when "
        "get_system_log and summarize_log cannot answer the question: it fills your "
        "context, and reason is recorded for the owner. DEBUG records only exist for "
        "what the owner turned "
        "debug logging on for. covers_from is how far back the log was read. Hidden "
        "loggers and withheld values stay so. "
        f"{DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Required(
                "reason",
                description="Why get_system_log is not enough for this question.",
            ): probatio.All(
                str,
                probatio.Length(min=MIN_REASON_LENGTH, max=MAX_TEXT_FILTER_LENGTH),
            ),
            probatio.Optional(
                "min_level", description="The lowest level to return.", default="DEBUG"
            ): probatio.In(LOG_LEVELS),
            **LOG_FILTERS,
            **PAGING,
        }
    )
    max_result_characters = MAX_EXTENDED_RESULT_CHARACTERS

    @classmethod
    @override
    def unavailable_because(
        cls, hass: HomeAssistant, runtime: RuntimeData
    ) -> str | None:
        """Return why this cannot work right now."""
        if full_log_available(hass):
            return None
        return "There is no log file, and no Supervisor to ask for the log."

    def __init__(self, runtime: RuntimeData) -> None:
        """Initialize the tool."""
        super().__init__(runtime)
        # The log is read whole each time, however many a client asks for at once.
        self._one_at_a_time = asyncio.Lock()

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of records."""
        query = full_log_query(args)
        async with self._one_at_a_time:
            log = await async_read_full_log(
                hass, self.exposure, query, scrub=self._runtime.redactor.scrub_text
            )
        return {
            **paginate(log["records"], args),
            "covers_from": log["covers_from"],
            "older_records_left_out": log["older_records_left_out"],
        }

    @override
    def reduce_result(
        self, result: dict[str, Any], args: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return the page with long messages cut."""
        return {**result, "items": [_shortened(item) for item in result["items"]]}


def full_log_offer() -> dict[str, Any]:
    """Return how a client can get the whole log, for the capability report."""
    return {
        "tool": GetFullLogTool.name,
        "use_when": "get_system_log and summarize_log cannot answer the question",
        "requires": "a reason, which is recorded for the owner",
        "costs": "it fills your context",
        "unchanged": "hidden loggers and withheld values stay so",
        "limits": {
            "lines_read": MAX_FULL_LOG_LINES,
            "record_characters_before_shortening": MAX_LOG_RECORD_CHARACTERS,
            "result_characters": MAX_EXTENDED_RESULT_CHARACTERS,
            "requests_at_a_time": 1,
        },
    }
