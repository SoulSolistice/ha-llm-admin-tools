"""Tool that groups the log by what it says, instead of returning all of it."""

import asyncio
from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant

from ..errors import invalid_input
from ..models import RuntimeData
from ..queries.full_log import async_read_full_log
from ..queries.log_summary import summarize_records
from ..queries.logs import LOG_LEVELS
from .base import ReadOnlyTool
from .full_log import LOG_FILTERS, GetFullLogTool, full_log_query
from .params import DATA_NOTICE, PAGING, paginate


class SummarizeLogTool(ReadOnlyTool):
    """Group the records of the log, so that what repeats is read once."""

    name = "summarize_log"
    title = "Summarize the log"
    description = (
        "Group the records of the whole log by what they say, with how many there are "
        "and when they first and last came, what repeats most first. This costs far "
        "less than get_full_log, so try it first. A traceback comes with its last "
        f"line. covers_from is how far back the log was read. {DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "min_level",
                description="The lowest level to count.",
                default="WARNING",
            ): probatio.In(LOG_LEVELS),
            **LOG_FILTERS,
            **PAGING,
        }
    )

    @classmethod
    @override
    def unavailable_because(
        cls, hass: HomeAssistant, runtime: RuntimeData
    ) -> str | None:
        """Return why this cannot work right now."""
        if not runtime.exposure.tool_enabled(GetFullLogTool.name):
            return "The owner turned off get_full_log, which reads the same log."
        return GetFullLogTool.unavailable_because(hass, runtime)

    def __init__(self, runtime: RuntimeData) -> None:
        """Initialize the tool."""
        super().__init__(runtime)
        # The log is read whole each time, however many a client asks for at once.
        self._one_at_a_time = asyncio.Lock()

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of groups."""
        # An owner who turned that off does not expect the same text to come from here.
        if not self.exposure.tool_enabled(GetFullLogTool.name):
            raise invalid_input("summary_needs_full_log")
        query = full_log_query(args)
        scrub = self._runtime.redactor.scrub_text
        async with self._one_at_a_time:
            log = await async_read_full_log(hass, self.exposure, query, scrub=scrub)
        groups = await hass.async_add_executor_job(
            summarize_records, log["records"], scrub
        )
        return {
            **paginate(groups, args),
            "records_counted": len(log["records"]),
            "covers_from": log["covers_from"],
            "older_records_left_out": log["older_records_left_out"],
        }
