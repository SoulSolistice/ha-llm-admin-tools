"""Tool that shows how an automation or script ran."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..errors import invalid_input
from ..models import RuntimeData
from ..queries.traces import async_get_run, async_list_runs, trace_item
from .base import READ_ONLY_ANNOTATIONS, BaseTool
from .params import DATA_NOTICE, PAGING, TEXT, paginate


class GetAutomationTraceTool(BaseTool):
    """Show how an automation or script ran, and where it stopped."""

    annotations = READ_ONLY_ANNOTATIONS
    name = "get_automation_trace"
    title = "Get how an automation or script ran"
    description = (
        "List the last runs of an automation or script, with what triggered each, how "
        "it ended and its last step. Use this to find out why one did or did not act. "
        "run_id returns each step of one run; a step that started a script names "
        "that run, to ask for in turn. Home Assistant keeps few runs, none "
        "across a change, and none for an automation without an ID. A trace holds "
        "values of whatever a run looked at, so this needs nothing to be hidden. "
        f"{DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Required(
                "entity_id", description="The automation or script."
            ): TEXT,
            probatio.Optional("run_id", description="The run to show in full."): TEXT,
            probatio.Optional(
                "include_variables",
                description=(
                    "With run_id: the variables each step changed. This is long."
                ),
                default=False,
            ): bool,
            **PAGING,
        }
    )

    @classmethod
    @override
    def unavailable_because(
        cls, hass: HomeAssistant, runtime: RuntimeData
    ) -> str | None:
        """Return why this cannot work right now."""
        if not runtime.writes.reading_restricted:
            return None
        return "A trace could show what is hidden, and something is."

    @override
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Return the runs, or the steps of one run."""
        if self.unavailable_because(hass, self._runtime) is not None:
            raise invalid_input("unavailable_while_hidden", what=self.name)
        entity_id: str = args["entity_id"]
        kind, item_id = trace_item(hass, self.exposure, entity_id)
        user_id = llm_context.context.user_id if llm_context.context else None
        if (run_id := args.get("run_id")) is not None:
            return {
                "entity_id": entity_id,
                **await async_get_run(
                    hass,
                    user_id,
                    kind,
                    item_id,
                    run_id,
                    policy=self.exposure,
                    variables=args["include_variables"],
                ),
            }
        runs: list[dict[str, Any]] = await async_list_runs(hass, user_id, kind, item_id)
        return {"entity_id": entity_id, **paginate(runs, args)}
