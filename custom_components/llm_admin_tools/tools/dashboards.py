"""Tools that read dashboards."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..const import DEFAULT_DASHBOARD
from ..core_commands import async_run_command
from ..policy.limits import MAX_EXTENDED_RESULT_CHARACTERS
from .base import READ_ONLY_ANNOTATIONS, BaseTool
from .params import DATA_NOTICE, PAGING, TEXT, paginate


class ListDashboardsTool(BaseTool):
    """List the dashboards."""

    annotations = READ_ONLY_ANNOTATIONS
    name = "list_dashboards"
    title = "List dashboards"
    description = (
        "List the dashboards other than the default one, with their id and url_path."
    )
    parameters = probatio.Schema(PAGING)

    @override
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Return one page of dashboards."""
        user_id = llm_context.context.user_id if llm_context.context else None
        dashboards = await async_run_command(hass, user_id, "lovelace/dashboards/list")
        return paginate(dashboards, args)


class GetDashboardTool(BaseTool):
    """Get the configuration of one dashboard."""

    annotations = READ_ONLY_ANNOTATIONS
    max_result_characters = MAX_EXTENDED_RESULT_CHARACTERS
    name = "get_dashboard"
    title = "Get a dashboard"
    description = (
        "Return the configuration of one dashboard, which can be long. The cards are "
        f"text, and may name things that are otherwise hidden from you. {DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "url_path",
                description=(
                    f"From list_dashboards. Leave out, or {DEFAULT_DASHBOARD}, for the "
                    "default."
                ),
            ): TEXT,
        }
    )

    @override
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Return the configuration."""
        user_id = llm_context.context.user_id if llm_context.context else None
        url_path = args.get("url_path")
        config = await async_run_command(
            hass,
            user_id,
            "lovelace/config",
            {"url_path": None if url_path == DEFAULT_DASHBOARD else url_path},
        )
        return {"url_path": url_path, "config": config}
