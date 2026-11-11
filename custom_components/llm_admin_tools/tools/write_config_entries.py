"""Tool that manages the config entries of integrations."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..policy.members import config_entry_members
from .params import TEXT
from .write_base import WRITE_PARAMS, WritePlan, WriteTool

# A reload leaves everything as it was, a moment later.
_STARTS_OR_STOPS = frozenset({"delete", "disable", "enable"})
_REPORTED = frozenset({"delete", "disable"})
# Neither changes what an entity does now. Anything else may, as turning off polling
# leaves an entity with an old value, so a new field is treated as doing so too.
_UPDATES_THAT_CHANGE_NOTHING_RUNNING = frozenset({"title", "pref_disable_new_entities"})


class ManageConfigEntryTool(WriteTool):
    """Reload, turn on or off, rename or delete a config entry."""

    name = "manage_config_entry"
    title = "Manage an integration entry"
    description = (
        "Reload, enable, disable, update or delete a config entry from "
        "list_integrations. update takes title, pref_disable_new_entities and "
        "pref_disable_polling in fields. delete and disable report in affects which "
        "entities and devices go and what uses them; use dry_run to see that first. "
        "Setting up new integrations is not possible, and neither is changing LLM "
        "Admin Tools, the MCP server or the Model Context Protocol integration."
    )
    subjects = ("config_entries",)
    parameters = probatio.Schema(
        {
            probatio.Required("entry_id", description="The entry ID."): TEXT,
            probatio.Required("operation", description="What to do."): probatio.In(
                ("reload", "enable", "disable", "update", "delete")
            ),
            probatio.Optional("fields", description="For update."): dict,
            **WRITE_PARAMS,
        }
    )

    @override
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check that the entry may be changed."""
        entry_id: str = args["entry_id"]
        operation: str = args["operation"]
        entry = self.writes.require_config_entry(entry_id)
        if operation in _STARTS_OR_STOPS or (
            operation == "update"
            and set(args.get("fields", {})) - _UPDATES_THAT_CHANGE_NOTHING_RUNNING
        ):
            self.writes.require_config_entry_members_writable(entry_id)
        summary = f"{operation} {entry.domain} entry {entry_id}"
        before = {
            "title": entry.title,
            "state": entry.state,
            "disabled_by": entry.disabled_by,
        }

        async def async_reload() -> dict[str, bool]:
            return {"loaded": await hass.config_entries.async_reload(entry_id)}

        async def async_delete() -> dict[str, Any]:
            return await hass.config_entries.async_remove(entry_id)

        apply: Callable[[], Awaitable[Any]]
        if operation == "reload":
            apply = async_reload
        elif operation == "delete":
            apply = async_delete
        elif operation == "update":
            apply = self.core_command(
                hass,
                llm_context,
                "config_entries/update",
                {**args.get("fields", {}), "entry_id": entry_id},
            )
        else:
            apply = self.core_command(
                hass,
                llm_context,
                "config_entries/disable",
                {
                    "entry_id": entry_id,
                    "disabled_by": "user" if operation == "disable" else None,
                },
            )
        return WritePlan(
            summary=summary,
            before=before,
            apply=apply,
            affects=(
                self.removal_report(
                    hass, llm_context, config_entry_members(hass, entry_id)
                )
                if operation in _REPORTED
                else None
            ),
        )
