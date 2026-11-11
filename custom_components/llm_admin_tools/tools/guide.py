"""Which tool to use for what, as far as the owner and this installation allow it."""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from homeassistant.helpers import llm

from .action_history import GetActionHistoryTool
from .definitions import GetDefinitionTool, ListDefinitionsTool
from .entities import GetEntityTool, ListEntitiesTool
from .extended_history import GetExtendedHistoryTool
from .full_log import GetFullLogTool
from .health import GetHealthReportTool
from .history import GetEntityHistoryTool
from .log_summary import SummarizeLogTool
from .logs import GetSystemLogTool
from .references import FindReferencesTool
from .repairs import ListRepairsTool
from .services import FindServicesTool
from .traces import GetAutomationTraceTool
from .write_computed_helpers import WriteComputedHelperTool
from .write_config_entries import ManageConfigEntryTool
from .write_dashboards import WriteDashboardTool
from .write_definitions import WriteDefinitionTool
from .write_helpers import WriteHelperTool
from .write_registries import (
    UpdateDeviceTool,
    UpdateEntityTool,
    WriteRegistryItemTool,
)
from .write_services import CallServiceTool


@dataclass(slots=True, frozen=True)
class _Task:
    """Something a client may want to do, and the tools for it, the first one first."""

    goal: str
    tools: tuple[type[llm.Tool], ...]
    how: str | None = None
    works_when_limited: bool = True


_TASKS = (
    _Task(
        "See what is wrong",
        (GetHealthReportTool, ListRepairsTool, GetSystemLogTool),
    ),
    _Task(
        "Find out why an automation or script did or did not run",
        (ListDefinitionsTool, GetAutomationTraceTool, GetDefinitionTool),
    ),
    _Task(
        "Find what uses an entity or device, instead of reading every definition",
        (FindReferencesTool,),
    ),
    _Task(
        "Find what names an entity that does not exist",
        (FindReferencesTool,),
        "Leave out target.",
        works_when_limited=False,
    ),
    _Task(
        "Read the logs, the cheapest first",
        (GetSystemLogTool, SummarizeLogTool, GetFullLogTool),
    ),
    _Task(
        "Find an entity and see what it is doing",
        (ListEntitiesTool, GetEntityTool),
    ),
    _Task(
        "See how a value changed",
        (GetEntityHistoryTool, GetExtendedHistoryTool),
    ),
    _Task(
        "Find out what called an action, such as what turned a light on",
        (GetActionHistoryTool,),
    ),
    _Task(
        "Call a service",
        (FindServicesTool, CallServiceTool),
        "Use dry_run first.",
    ),
    _Task(
        "Change or remove something",
        (
            WriteDefinitionTool,
            UpdateEntityTool,
            UpdateDeviceTool,
            WriteRegistryItemTool,
            WriteHelperTool,
            WriteComputedHelperTool,
            WriteDashboardTool,
            ManageConfigEntryTool,
        ),
        "Use dry_run first: it says what would happen, and what a removal affects.",
    ),
)
_SPOOK_TASK = _Task(
    "See what Spook found",
    (ListRepairsTool,),
    "Use domain spook. find_references without a target adds them as spook_found.",
)


def task_guide(
    offered: Collection[str], limited: Collection[str], *, spook: bool
) -> list[dict[str, Any]]:
    """Return which tools to use for what, leaving out what cannot be used now."""
    guide = []
    for task in (*_TASKS, *((_SPOOK_TASK,) if spook else ())):
        tools = [tool.name for tool in task.tools if tool.name in offered]
        if not tools or (
            not task.works_when_limited and any(name in limited for name in tools)
        ):
            continue
        guide.append(
            {
                "goal": task.goal,
                "tools": tools,
                **({"how": task.how} if task.how else {}),
            }
        )
    return guide
