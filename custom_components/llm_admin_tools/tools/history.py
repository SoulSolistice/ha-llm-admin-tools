"""Tool for the history of entities."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..errors import invalid_input
from ..models import RuntimeData
from ..policy.credential_states import require_history_readable
from ..policy.limits import (
    DEFAULT_HISTORY_WINDOW,
    HISTORY_WINDOW_SLACK,
    MAX_HISTORY_ENTITIES,
    MAX_HISTORY_POINTS_PER_ENTITY,
    MAX_HISTORY_SUMMARY_POINTS,
    MAX_HISTORY_WINDOW,
)
from ..queries.history import async_get_history, recorder_running
from .base import ReadOnlyTool
from .params import DATA_NOTICE, TEXT, parse_time, require_valid_entity_id


def _resolve_window(args: Mapping[str, Any]) -> tuple[datetime, datetime]:
    """Return the requested time window, enforcing its maximum length.

    The newest values of a quiet entity can be far back, so a limit looks as far back
    as it can unless told otherwise.
    """
    end = parse_time(args["end"]) if "end" in args else dt_util.utcnow()
    default_window = MAX_HISTORY_WINDOW if "limit" in args else DEFAULT_HISTORY_WINDOW
    start = parse_time(args["start"]) if "start" in args else end - default_window
    if start >= end:
        raise invalid_input("invalid_time_window")
    if end - start > MAX_HISTORY_WINDOW + HISTORY_WINDOW_SLACK:
        raise invalid_input("time_window_too_long", days=str(MAX_HISTORY_WINDOW.days))
    return start, end


class GetEntityHistoryTool(ReadOnlyTool):
    """Get the history of entities over time."""

    name = "get_entity_history"
    title = "Get entity history"
    description = (
        f"Return the history of up to {MAX_HISTORY_ENTITIES} entities, without "
        f"attributes, for a time window of up to {MAX_HISTORY_WINDOW.days} days, "
        f"by default the last {int(DEFAULT_HISTORY_WINDOW.total_seconds() // 3600)} "
        f"hours. An entity with up to {MAX_HISTORY_SUMMARY_POINTS} changes returns "
        "them as points, and state_at_start is the value it held when the window "
        "began, if known. A busier numeric entity returns buckets across the window, "
        "with min, mean, max, last and change where known: source says if they come "
        "from statistics, which cover covered_from to covered_until and end a little "
        "before the present, or from states. Other busy entities return their newest "
        "points. Use limit for the exact newest values instead of a summary, by "
        f"default from the last {MAX_HISTORY_WINDOW.days} days. If "
        "older_changes_left_out is true, ask for older points with end set to "
        "covered_from. If newer_changes_left_out is true, the buckets only reach "
        "covered_until, so ask for a later window starting there. exists is false for "
        "an entity Home Assistant does not know. get_capabilities gives the current "
        f"time, and what can be requested beyond these limits. {DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Required(
                "entity_ids", description="The entity IDs."
            ): probatio.All([TEXT], probatio.Length(min=1, max=MAX_HISTORY_ENTITIES)),
            probatio.Optional(
                "start", description="Start of the window as an ISO 8601 time."
            ): TEXT,
            probatio.Optional(
                "end", description="End of the window as an ISO 8601 time."
            ): TEXT,
            probatio.Optional(
                "limit",
                description="Return this many newest values of each entity, exactly.",
            ): probatio.All(
                int, probatio.Range(min=1, max=MAX_HISTORY_POINTS_PER_ENTITY)
            ),
        }
    )

    @classmethod
    @override
    def unavailable_because(
        cls, hass: HomeAssistant, runtime: RuntimeData
    ) -> str | None:
        """Return why this cannot work right now."""
        return None if recorder_running(hass) else "The recorder is not running."

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return the history per entity."""
        for entity_id in args["entity_ids"]:
            require_valid_entity_id(entity_id)
            require_history_readable(hass, self.exposure, entity_id)
        start, end = _resolve_window(args)
        return {
            "start": start,
            "end": end,
            "entities": await async_get_history(
                hass,
                self.exposure,
                args["entity_ids"],
                start,
                end,
                limit=args.get("limit"),
            ),
        }
