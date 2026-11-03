"""Tool for far more history than usual, requested on purpose and with a reason."""

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..errors import invalid_input
from ..models import RuntimeData
from ..policy.limits import (
    DEFAULT_EXTENDED_HISTORY_POINTS,
    MAX_EXTENDED_HISTORY_POINTS,
    MAX_EXTENDED_RESULT_CHARACTERS,
    MAX_EXTENDED_STATISTICS_ROWS,
    MAX_TEXT_FILTER_LENGTH,
    MIN_REASON_LENGTH,
)
from ..queries.extended_history import (
    STATISTICS_PERIOD_SECONDS,
    async_get_extended_changes,
    async_get_extended_statistics,
)
from ..queries.history import recorder_running
from .base import ReadOnlyTool
from .params import DATA_NOTICE, TEXT, parse_time, require_valid_entity_id

_VALUES_CHANGES = "changes"
_VALUES_STATISTICS = "statistics"


def _resolve_end(args: Mapping[str, Any], start: datetime) -> datetime:
    """Return the end of the window, which must come after its start."""
    end = parse_time(args["end"]) if "end" in args else dt_util.utcnow()
    if start >= end:
        raise invalid_input("invalid_time_window")
    return end


class GetExtendedHistoryTool(ReadOnlyTool):
    """Get far more history of one entity, when the usual tool cannot answer."""

    name = "get_extended_history"
    title = "Get extended history on request"
    description = (
        "Return far more history of one entity than get_entity_history, without its "
        "limit on the length of the window. Use this only when get_entity_history "
        "cannot answer the question: it delays Home Assistant and fills your context, "
        "and reason is recorded for the owner. values changes returns exact changes, "
        "oldest first from start, up to limit, and state_at_start is the value it held "
        "when the window began, if known. If newer_changes_left_out is true, continue "
        "with start set to covered_until. values statistics returns one row "
        "per period, reaching back further than states do, for sensors with a state "
        "class. Hidden entities and withheld values stay so. "
        f"{DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Required(
                "reason",
                description="Why get_entity_history is not enough for this question.",
            ): probatio.All(
                str,
                probatio.Length(min=MIN_REASON_LENGTH, max=MAX_TEXT_FILTER_LENGTH),
            ),
            probatio.Required("entity_id", description="The entity ID."): TEXT,
            probatio.Required(
                "start", description="Start of the window as an ISO 8601 time."
            ): TEXT,
            probatio.Optional(
                "end", description="End of the window as an ISO 8601 time."
            ): TEXT,
            probatio.Optional(
                "values",
                description="Exact changes, or rows of statistics.",
                default=_VALUES_CHANGES,
            ): probatio.In((_VALUES_CHANGES, _VALUES_STATISTICS)),
            probatio.Optional(
                "limit",
                description="How many changes to return, for values changes.",
                default=DEFAULT_EXTENDED_HISTORY_POINTS,
            ): probatio.All(
                int, probatio.Range(min=1, max=MAX_EXTENDED_HISTORY_POINTS)
            ),
            probatio.Optional(
                "period",
                description="The length of a row, for values statistics.",
                default="hour",
            ): probatio.In(tuple(STATISTICS_PERIOD_SECONDS)),
        }
    )
    max_result_characters = MAX_EXTENDED_RESULT_CHARACTERS

    @classmethod
    @override
    def unavailable_because(
        cls, hass: HomeAssistant, runtime: RuntimeData
    ) -> str | None:
        """Return why this cannot work right now."""
        return None if recorder_running(hass) else "The recorder is not running."

    def __init__(self, runtime: RuntimeData) -> None:
        """Initialize the tool."""
        super().__init__(runtime)
        # However many a client sends, Home Assistant only has one of these to serve.
        self._one_at_a_time = asyncio.Lock()

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return the history of the entity."""
        entity_id = require_valid_entity_id(args["entity_id"])
        start = parse_time(args["start"])
        end = _resolve_end(args, start)
        async with self._one_at_a_time:
            read = hass.async_create_task(
                async_get_extended_changes(
                    hass, self.exposure, entity_id, start, end, limit=args["limit"]
                )
                if args["values"] == _VALUES_CHANGES
                else self._async_read_statistics(hass, args, start, end)
            )
            try:
                history = await asyncio.shield(read)
            except asyncio.CancelledError:
                # A client that gives up does not stop what runs in the executor, so
                # the next one still has to wait for that.
                await asyncio.gather(read, return_exceptions=True)
                raise
        return {"start": start, "end": end, "entity_id": entity_id, **history}

    async def _async_read_statistics(
        self,
        hass: HomeAssistant,
        args: Mapping[str, Any],
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Return rows of statistics, of a window that holds no more than allowed."""
        period = args["period"]
        seconds = STATISTICS_PERIOD_SECONDS[period]
        if (end - start).total_seconds() / seconds > MAX_EXTENDED_STATISTICS_ROWS:
            raise invalid_input(
                "time_window_too_long",
                days=str(seconds * MAX_EXTENDED_STATISTICS_ROWS // 86400),
            )
        return await async_get_extended_statistics(
            hass, self.exposure, args["entity_id"], start, end, period=period
        )


def extended_history_offer() -> dict[str, Any]:
    """Return how a client can get more history, for the capability report."""
    return {
        "tool": GetExtendedHistoryTool.name,
        "use_when": "get_entity_history cannot answer the question",
        "requires": "a reason, which is recorded for the owner",
        "costs": "it delays Home Assistant and fills your context",
        "unchanged": "hidden entities and withheld values stay so",
        "limits": {
            "changes_per_request": MAX_EXTENDED_HISTORY_POINTS,
            "statistics_rows_per_request": MAX_EXTENDED_STATISTICS_ROWS,
            "result_characters": MAX_EXTENDED_RESULT_CHARACTERS,
            "requests_at_a_time": 1,
        },
    }
