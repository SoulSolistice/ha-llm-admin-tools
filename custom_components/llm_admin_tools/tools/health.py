"""Tool that gathers what is wrong right now."""

from collections.abc import Callable, Mapping
from inspect import isawaitable
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..models import RuntimeData
from ..policy.limits import MAX_HEALTH_ITEMS, MAX_REPORT_USES
from ..queries.health import (
    async_repairs_summary,
    failing_integrations,
    recurring_log_records,
    unavailable_entities,
)
from ..queries.logs import system_log_available
from ..queries.references import async_find_missing, async_read_sources
from ..queries.traces import async_failed_runs
from .base import READ_ONLY_ANNOTATIONS, BaseTool
from .entities import ListEntitiesTool
from .integrations import ListIntegrationsTool
from .logs import GetSystemLogTool
from .params import DATA_NOTICE
from .references import FindReferencesTool, readable_sources
from .repairs import ListRepairsTool
from .traces import GetAutomationTraceTool


class GetHealthReportTool(BaseTool):
    """Report what is wrong, in one call that fits in little context."""

    annotations = READ_ONLY_ANNOTATIONS
    name = "get_health_report"
    title = "Get a health report"
    description = (
        "Report what is wrong right now: repairs, integrations that failed, "
        "unavailable entities by integration, what the system log counted most, "
        "automation runs that failed, and what names an entity that does not exist. "
        "Each is short, so use this first when asked what is wrong, then "
        "list_repairs, get_system_log, get_automation_trace or find_references for "
        "more. not_checked, on its own or in a part, says what could not be read, "
        f"so do not take that as being fine. {DATA_NOTICE}"
    )
    parameters = probatio.Schema({})

    @classmethod
    @override
    def limited_because(cls, runtime: RuntimeData) -> str | None:
        """Return what this cannot do right now."""
        if not runtime.writes.reading_restricted:
            return None
        return (
            "It leaves out failed runs and missing references, as something is hidden."
        )

    async def _async_missing_references(
        self, hass: HomeAssistant, user_id: str | None
    ) -> dict[str, Any]:
        """Return what names an entity that does not exist."""
        sources = await async_read_sources(
            hass, self.exposure, user_id, **readable_sources(self.exposure)
        )
        rows = await async_find_missing(
            hass,
            sources,
            self._runtime.redactor.redact,
            max_names=MAX_REPORT_USES,
        )
        return {
            "count": len(rows),
            "items": rows[:MAX_HEALTH_ITEMS],
            **({"not_checked": sources.not_checked} if sources.not_checked else {}),
        }

    @override
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Return the report, reading only what the owner lets a client read."""
        enabled = self.exposure.tool_enabled
        hidden = self._runtime.writes.reading_restricted
        user_id = llm_context.context.user_id if llm_context.context else None
        scrub = self._runtime.redactor.scrub_text
        sections: dict[str, tuple[bool, Callable[[], Any]]] = {
            "repairs": (
                enabled(ListRepairsTool.name),
                lambda: async_repairs_summary(hass, self.exposure),
            ),
            "failing_integrations": (
                enabled(ListIntegrationsTool.name),
                lambda: failing_integrations(hass, self.exposure),
            ),
            "unavailable_entities": (
                enabled(ListEntitiesTool.name),
                lambda: unavailable_entities(hass, self.exposure),
            ),
            "recurring_log_records": (
                enabled(GetSystemLogTool.name) and system_log_available(hass),
                lambda: recurring_log_records(hass, self.exposure, scrub),
            ),
            "failed_runs": (
                enabled(GetAutomationTraceTool.name) and not hidden,
                lambda: async_failed_runs(hass, user_id),
            ),
            "missing_references": (
                enabled(FindReferencesTool.name) and not hidden,
                lambda: self._async_missing_references(hass, user_id),
            ),
        }
        report: dict[str, Any] = {}
        not_checked: list[str] = []
        for section, (available, read) in sections.items():
            if not available:
                not_checked.append(section)
                continue
            value = read()
            report[section] = await value if isawaitable(value) else value
        return {**report, **({"not_checked": not_checked} if not_checked else {})}
