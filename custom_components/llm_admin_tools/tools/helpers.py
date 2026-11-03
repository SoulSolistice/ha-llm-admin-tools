"""Tool that lists helpers."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..core_commands import HELPER_KINDS
from ..queries.helpers import async_list_helpers
from .base import READ_ONLY_ANNOTATIONS, BaseTool
from .params import PAGING, paginate

HELPER_KIND = probatio.In(HELPER_KINDS)


class ListHelpersTool(BaseTool):
    """List the helpers of one kind that the UI can edit."""

    annotations = READ_ONLY_ANNOTATIONS
    name = "list_helpers"
    title = "List helpers"
    description = (
        "List the helpers of one kind that the UI can edit, with all their settings: "
        "counters, input helpers, persons, schedules, tags, timers and zones."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("kind", description="What to list."): HELPER_KIND,
            **PAGING,
        }
    )

    @override
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Return one page of helpers."""
        user_id = llm_context.context.user_id if llm_context.context else None
        helpers = await async_list_helpers(hass, self.exposure, user_id, args["kind"])
        return paginate(helpers, args)
