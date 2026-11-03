"""The tools that get_capabilities reports on, which is why it is not one of them."""

from .base import BaseTool
from .dashboards import GetDashboardTool, ListDashboardsTool
from .definitions import GetDefinitionTool, ListDefinitionsTool
from .entities import GetEntityTool, ListEntitiesTool
from .extended_history import GetExtendedHistoryTool
from .full_log import GetFullLogTool
from .health import GetHealthReportTool
from .helpers import ListHelpersTool
from .history import GetEntityHistoryTool
from .integrations import ListIntegrationsTool
from .log_summary import SummarizeLogTool
from .logs import GetSystemLogTool
from .references import FindReferencesTool
from .registries import ListAreasTool, ListDevicesTool, ListRegistryItemsTool
from .repairs import ListRepairsTool
from .services import FindServicesTool
from .system import GetSystemInfoTool
from .traces import GetAutomationTraceTool
from .write_base import WriteTool
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

READ_TOOL_CLASSES: tuple[type[BaseTool], ...] = (
    GetSystemInfoTool,
    GetHealthReportTool,
    ListRepairsTool,
    ListEntitiesTool,
    GetEntityTool,
    GetEntityHistoryTool,
    GetExtendedHistoryTool,
    ListDevicesTool,
    ListAreasTool,
    ListRegistryItemsTool,
    ListIntegrationsTool,
    ListDefinitionsTool,
    GetDefinitionTool,
    GetAutomationTraceTool,
    FindReferencesTool,
    ListHelpersTool,
    ListDashboardsTool,
    GetDashboardTool,
    GetSystemLogTool,
    SummarizeLogTool,
    GetFullLogTool,
)

# Apart from the tools above, as those are all on until the owner chooses otherwise.
WRITE_TOOL_CLASSES: tuple[type[WriteTool], ...] = (
    CallServiceTool,
    WriteDefinitionTool,
    UpdateEntityTool,
    UpdateDeviceTool,
    WriteRegistryItemTool,
    WriteHelperTool,
    WriteDashboardTool,
    ManageConfigEntryTool,
)

# Offered once the owner allows one of their subjects, and never otherwise.
TOOL_CLASSES_FOR_CHANGES: tuple[type[WriteTool] | type[FindServicesTool], ...] = (
    *WRITE_TOOL_CLASSES,
    FindServicesTool,
)
