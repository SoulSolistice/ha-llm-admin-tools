"""Tool that finds what uses an entity or device, or names what does not exist."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..errors import invalid_input
from ..models import RuntimeData
from ..policy.exposure import ExposurePolicy
from ..policy.limits import MAX_TARGETS_PER_CALL
from ..queries.references import (
    HOW_FOUND,
    Sources,
    async_find_missing,
    async_find_target,
    async_read_sources,
)
from ..queries.repairs import repairs_naming, spook_active, spook_unknown_references
from .base import READ_ONLY_ANNOTATIONS, BaseTool
from .dashboards import GetDashboardTool
from .definitions import GetDefinitionTool
from .params import DATA_NOTICE, PAGING, TEXT, paginate


def readable_sources(exposure: ExposurePolicy) -> dict[str, bool]:
    """Return what the owner lets a client read, and so what may be looked in."""
    return {
        "read_definitions": exposure.tool_enabled(GetDefinitionTool.name),
        "read_dashboards": exposure.tool_enabled(GetDashboardTool.name),
    }


class FindReferencesTool(BaseTool):
    """Find what names an entity or device, or something that does not exist."""

    annotations = READ_ONLY_ANNOTATIONS
    name = "find_references"
    title = "Find what uses something"
    description = (
        "List the automations, scripts, scenes and dashboards that name an entity or "
        "device, instead of reading them all. With targets, each row says which of "
        f"up to {MAX_TARGETS_PER_CALL} it names. Leave both out to list those that "
        "name an entity that does not exist, and on the first page what Spook found if "
        "installed; "
        f"that needs nothing to be hidden. {DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Exclusive(
                "target", "target", description="An entity ID or device ID."
            ): TEXT,
            probatio.Exclusive(
                "targets", "target", description="Several of them instead."
            ): probatio.All([TEXT], probatio.Length(min=1, max=MAX_TARGETS_PER_CALL)),
            **PAGING,
        }
    )

    @classmethod
    @override
    def limited_because(cls, runtime: RuntimeData) -> str | None:
        """Return what this cannot do right now."""
        if not runtime.writes.reading_restricted:
            return None
        return "It needs a target while something is hidden."

    async def _async_missing(
        self, hass: HomeAssistant, sources: Sources, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return what names something that does not exist."""
        # A hidden entity would have to look as if it does not exist, and be reported
        # as gone, which could lead to a working automation being taken apart.
        if self.limited_because(self._runtime) is not None:
            raise invalid_input(
                "unavailable_while_hidden", what=f"{self.name} without a target"
            )
        rows = await async_find_missing(hass, sources, self._runtime.redactor.redact)
        # Spook only adds to this, with what it can see and this integration cannot.
        # That is long and the same on each page, so it comes with the first.
        if not spook_active(hass) or args["offset"]:
            return {"rows": rows}
        return {
            "rows": rows,
            "spook_found": spook_unknown_references(hass, self.exposure),
        }

    @override
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Return one page of what uses the target, or of what names nothing."""
        sources = await async_read_sources(
            hass,
            self.exposure,
            llm_context.context.user_id if llm_context.context else None,
            **readable_sources(self.exposure),
        )
        targets: list[str] | None = (
            [args["target"]] if "target" in args else args.get("targets")
        )
        if targets is None:
            found = await self._async_missing(hass, sources, args)
            rows = found.pop("rows")
        else:
            redact = self._runtime.redactor.redact
            rows, names = [], set()
            for target in dict.fromkeys(targets):
                found_rows, found_names = await async_find_target(
                    hass, self.exposure, sources, target, redact
                )
                names |= found_names
                rows += (
                    found_rows
                    if "target" in args
                    else [{"target": target, **row} for row in found_rows]
                )
            found = {
                **(
                    {"target": args["target"]}
                    if "target" in args
                    else {"targets": targets}
                ),
                "repairs": repairs_naming(hass, self.exposure, names, redact),
            }
        return {
            **paginate(rows, args),
            **found,
            **({"not_checked": sources.not_checked} if sources.not_checked else {}),
            "how_found": HOW_FOUND,
        }
