"""Tool that changes dashboards."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from ..const import DEFAULT_DASHBOARD
from ..core_commands import async_run_command
from ..errors import invalid_input
from ..policy.definition_guard import check_dashboard
from .params import TEXT
from .write_base import WRITE_PARAMS, WritePlan, WriteTool, change_summary

_CONFIG_OPERATIONS = {
    "save_config": "lovelace/config/save",
    "delete_config": "lovelace/config/delete",
}
_DASHBOARD_OPERATIONS = {
    "create": "lovelace/dashboards/create",
    "update": "lovelace/dashboards/update",
    "delete": "lovelace/dashboards/delete",
}


class WriteDashboardTool(WriteTool):
    """Change dashboards and what is on them."""

    name = "write_dashboard"
    title = "Write a dashboard"
    description = (
        "save_config replaces all that is on a dashboard, delete_config empties it. "
        "create, update and delete act on the dashboard itself: for create, fields are "
        "url_path, title, and optionally icon, show_in_sidebar and require_admin. "
        "Dashboard resources cannot be changed, and neither can YAML dashboards. A "
        "dashboard cannot link to anything outside Home Assistant, buttons can only "
        "call what call_service can, and it cannot hold templates unless "
        "get_capabilities says writes.templates_allowed."
    )
    subjects = ("dashboards",)
    parameters = probatio.Schema(
        {
            probatio.Required("operation", description="What to do."): probatio.In(
                (*_CONFIG_OPERATIONS, *_DASHBOARD_OPERATIONS)
            ),
            probatio.Optional(
                "url_path",
                description=(
                    "For save_config and delete_config, from list_dashboards. "
                    f"{DEFAULT_DASHBOARD} stands for the default dashboard."
                ),
            ): TEXT,
            probatio.Optional(
                "dashboard_id",
                description="The id from list_dashboards, for update and delete.",
            ): TEXT,
            probatio.Optional("config", description="For save_config."): dict,
            probatio.Optional("fields", description="For create and update."): dict,
            **WRITE_PARAMS,
        }
    )

    async def _async_current_config(
        self, hass: HomeAssistant, user_id: str | None, url_path: str | None
    ) -> Any:
        """Return what is on a dashboard, if anything has been saved yet."""
        try:
            return await async_run_command(
                hass, user_id, "lovelace/config", {"url_path": url_path}
            )
        except HomeAssistantError:
            return None

    async def _async_dashboards(
        self, hass: HomeAssistant, user_id: str | None
    ) -> list[dict[str, Any]]:
        """Return the dashboards that can be changed; the default is not among them."""
        dashboards: list[dict[str, Any]] = await async_run_command(
            hass, user_id, "lovelace/dashboards/list"
        )
        return dashboards

    @override
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check that the request says what it needs to, about what exists."""
        operation: str = args["operation"]
        user_id = llm_context.context.user_id if llm_context.context else None
        if operation in _CONFIG_OPERATIONS:
            # Leaving this out would replace the default one, so that has to be stated.
            if "url_path" not in args:
                raise invalid_input("dashboard_path_required", marker=DEFAULT_DASHBOARD)
            stated_path: str = args["url_path"]
            url_path = None if stated_path == DEFAULT_DASHBOARD else stated_path
            fields: dict[str, Any] = {"url_path": url_path}
            if operation == "save_config":
                if "config" not in args:
                    raise invalid_input("dashboard_config_required")
                check_dashboard(self.writes, args["config"])
                fields["config"] = args["config"]
            apply = self.core_command(
                hass, llm_context, _CONFIG_OPERATIONS[operation], fields
            )
            if url_path is not None and not any(
                dashboard["url_path"] == url_path
                for dashboard in await self._async_dashboards(hass, user_id)
            ):
                raise invalid_input("dashboard_not_found", dashboard=url_path)
            return WritePlan(
                summary=f"{operation} of dashboard {url_path or DEFAULT_DASHBOARD}",
                before=await self._async_current_config(hass, user_id, url_path),
                apply=apply,
            )
        fields = dict(args.get("fields", {}))
        summary = change_summary(f"{operation} dashboard", fields)
        before = None
        if operation != "create":
            if "dashboard_id" not in args:
                raise invalid_input("registry_item_id_required")
            dashboard_id: str = args["dashboard_id"]
            summary = change_summary(f"{operation} dashboard {dashboard_id}", fields)
            fields["dashboard_id"] = dashboard_id
            before = next(
                (
                    dashboard
                    for dashboard in await self._async_dashboards(hass, user_id)
                    if dashboard["id"] == dashboard_id
                ),
                None,
            )
            if before is None:
                raise invalid_input("dashboard_not_found", dashboard=dashboard_id)
        return WritePlan(
            summary=summary,
            before=before,
            apply=self.core_command(
                hass, llm_context, _DASHBOARD_OPERATIONS[operation], fields
            ),
        )
