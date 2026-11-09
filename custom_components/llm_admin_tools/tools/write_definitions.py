"""Tool that creates, changes and deletes automations, scripts and scenes."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..actions.definitions import EDITABLE_KINDS
from ..errors import invalid_input
from ..policy.definition_guard import check_definition
from .params import TEXT
from .write_base import WRITE_PARAMS, WritePlan, WriteTool

_SUBJECTS = {"automation": "automations", "script": "scripts", "scene": "scenes"}
_UNKNOWN_ENTITIES = (
    "These entities do not exist yet, so it does nothing with them until they do: "
    "{entities}. Check for a typo."
)


class WriteDefinitionTool(WriteTool):
    """Change what the UI edits for automations, scripts and scenes."""

    name = "write_definition"
    title = "Write an automation, script or scene"
    description = (
        "Create, replace or delete an automation, script or scene the UI can edit. An "
        "automation runs by itself once created, so everything it does must be allowed "
        "for you too: each service on the allowed list, and each entity one you may "
        "change. Blueprints are refused, and so are templates unless "
        "get_capabilities says writes.templates_allowed. id is the unique_id from "
        "list_definitions, or for a new script its object ID. config is the whole "
        "definition, not only what changes. The result gives the state it loaded "
        "with, or null if it did not load."
    )
    subjects = tuple(_SUBJECTS.values())
    parameters = probatio.Schema(
        {
            probatio.Required("kind", description="What to write."): probatio.In(
                EDITABLE_KINDS
            ),
            probatio.Required("operation", description="What to do."): probatio.In(
                ("create", "update", "delete")
            ),
            probatio.Optional(
                "id", description="Required unless creating an automation or scene."
            ): TEXT,
            probatio.Optional("config", description="Required unless deleting."): dict,
            **WRITE_PARAMS,
        }
    )

    @override
    def subject_of(self, args: Mapping[str, Any]) -> str:
        """Return the subject of the kind being written."""
        return _SUBJECTS[args["kind"]]

    @override
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check that the definition may be changed, and that core accepts the new."""
        kind: str = args["kind"]
        operation: str = args["operation"]
        editor = self._runtime.definitions
        self.writes.require_domain(kind)
        editor.require_available(kind)
        if operation != "create" and "id" not in args:
            raise invalid_input("definition_id_required")
        key = editor.resolve_key(kind, args.get("id"))
        if (entity_id := editor.entity_id(kind, key)) is not None:
            if not self.exposure.entity_allowed(entity_id):
                # Its ID is taken, which is all a client may learn of it.
                raise invalid_input(
                    "definition_exists"
                    if operation == "create"
                    else "definition_not_found",
                    definition_id=key,
                )
            self.writes.require_entity(entity_id)

        before = await editor.async_get(kind, key)
        if operation == "create" and before is not None:
            raise invalid_input("definition_exists", definition_id=key)
        if operation != "create" and before is None:
            raise invalid_input("definition_not_found", definition_id=key)

        # A dry run makes up an ID of its own, which the change would not get.
        new_id = operation == "create" and "id" not in args
        summary = (
            f"create {kind} with a new ID" if new_id else f"{operation} {kind} {key}"
        )
        context = llm_context.context
        if operation == "delete":

            async def async_delete() -> None:
                await editor.async_delete(kind, key, context)

            return WritePlan(summary=summary, before=before, apply=async_delete)

        if "config" not in args:
            raise invalid_input("definition_config_required")
        config: dict[str, Any] = args["config"]
        unknown = check_definition(self.writes, config)
        await editor.async_validate(kind, key, config)

        async def async_put() -> dict[str, Any]:
            entity_id = await editor.async_put(kind, key, config, context)
            # Whether it loaded and is on, without another call to find out.
            state = (
                hass.states.get(entity_id)
                if entity_id is not None and self.exposure.entity_allowed(entity_id)
                else None
            )
            return {
                "id": key,
                "entity_id": entity_id,
                "state": state.state if state is not None else None,
            }

        return WritePlan(
            summary=summary,
            before=before,
            apply=async_put,
            warnings=(
                (_UNKNOWN_ENTITIES.format(entities=", ".join(unknown)),)
                if unknown
                else ()
            ),
        )
