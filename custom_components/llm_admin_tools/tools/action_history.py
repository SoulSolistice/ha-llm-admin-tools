"""Tool that shows which actions were called, by anyone and through any route."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from ..errors import invalid_input
from ..models import RuntimeData
from ..policy.limits import (
    DEFAULT_HISTORY_WINDOW,
    HISTORY_WINDOW_SLACK,
    MAX_ACTION_CALLS_PAGE_SIZE,
    MAX_HISTORY_WINDOW,
)
from ..queries.action_history import action_history_kept, async_action_calls
from .base import READ_ONLY_ANNOTATIONS, BaseTool
from .params import DATA_NOTICE, TEXT, paginate, paging, parse_time

_HOURS = int(DEFAULT_HISTORY_WINDOW.total_seconds() // 3600)


class GetActionHistoryTool(BaseTool):
    """Show which actions were called, who or what called them, and with what."""

    annotations = READ_ONLY_ANNOTATIONS
    name = "get_action_history"
    title = "Get which actions were called"
    description = (
        "List the actions called in Home Assistant, newest first, by anyone and "
        f"through any route, in a window of up to {MAX_HISTORY_WINDOW.days} days, by "
        f"default the last {_HOURS} hours. Each has its data, whether a user called "
        "it, and caused_by: what it ran in, such as an automation. "
        "through_these_tools marks the calls made with these tools, and context_id is "
        "the one in the result of a change. Use entity_id to find out what turned "
        "something on or off, or what an automation did. Home Assistant keeps this "
        "from 2026.11, for as long as the recorder keeps history. A call can name "
        "anything, so this needs nothing "
        f"to be hidden. {DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "start", description="Start of the window as an ISO 8601 time."
            ): TEXT,
            probatio.Optional(
                "end", description="End of the window as an ISO 8601 time."
            ): TEXT,
            probatio.Optional(
                "action",
                description="Only this action, such as light.turn_on, or domain.",
            ): TEXT,
            probatio.Optional(
                "entity_id",
                description=(
                    "Only calls on this entity, or made by it, such as an automation."
                ),
            ): TEXT,
            **paging(MAX_ACTION_CALLS_PAGE_SIZE),
        }
    )

    @classmethod
    @override
    def unavailable_because(
        cls, hass: HomeAssistant, runtime: RuntimeData
    ) -> str | None:
        """Return why this cannot work right now."""
        if not action_history_kept(hass):
            return "It needs Home Assistant 2026.11 or later, with the logbook."
        if runtime.writes.reading_restricted:
            return "An action call can name what is hidden, and something is."
        return None

    @override
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Return one page of the calls in the window."""
        if self._runtime.writes.reading_restricted:
            raise invalid_input("unavailable_while_hidden", what=self.name)
        end = parse_time(args["end"]) if "end" in args else dt_util.utcnow()
        start = (
            parse_time(args["start"])
            if "start" in args
            else end - DEFAULT_HISTORY_WINDOW
        )
        if start >= end:
            raise invalid_input("invalid_time_window")
        if end - start > MAX_HISTORY_WINDOW + HISTORY_WINDOW_SLACK:
            raise invalid_input(
                "time_window_too_long", days=str(MAX_HISTORY_WINDOW.days)
            )
        user_id = llm_context.context.user_id if llm_context.context else None
        calls = await async_action_calls(
            hass,
            user_id,
            start,
            end,
            action=args.get("action"),
            entity_id=args.get("entity_id"),
            own_contexts=self._runtime.journal.context_ids(),
        )
        return {"start": start, "end": end, **paginate(calls, args)}
