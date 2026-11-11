"""Tool that creates, changes and deletes helpers and persons."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..errors import invalid_input
from ..policy.definition_guard import reaches_outside
from ..queries.helpers import async_read_helpers, helper_entity_id
from .helpers import HELPER_KIND
from .params import TEXT
from .write_base import WRITE_PARAMS, WritePlan, WriteTool, change_summary

# Core restores the last value of an input helper, unless initial says otherwise.
_INITIAL = (
    "initial sets the value of this {kind} at every start of Home Assistant, undoing "
    "any change made since. Leave it out to keep the last value, and set the value "
    "with its service."
)


class WriteHelperTool(WriteTool):
    """Change the helpers that the UI can edit."""

    name = "write_helper"
    title = "Write a helper"
    description = (
        "Create, change or delete a helper that the UI can edit: a counter, input "
        "helper, person, schedule, tag, timer or zone. fields are those the UI offers "
        "for it, as list_helpers shows them. When changing one, state all of them. To "
        "set its value, call its service afterwards, such as input_number.set_value. "
        "A person cannot be linked to a user account, and its picture must be within "
        "Home Assistant. list_helpers withholds the ID of "
        "a tag, so only one whose ID you know can be changed."
    )
    subjects = ("helpers", "persons")
    parameters = probatio.Schema(
        {
            probatio.Required("kind", description="What to write."): HELPER_KIND,
            probatio.Required("operation", description="What to do."): probatio.In(
                ("create", "update", "delete")
            ),
            probatio.Optional(
                "item_id", description="Its id. Required unless creating."
            ): TEXT,
            probatio.Optional(
                "fields", description="Its settings. Required unless deleting."
            ): dict,
            **WRITE_PARAMS,
        }
    )

    @override
    def subject_of(self, args: Mapping[str, Any]) -> str:
        """Return the subject of the kind being written."""
        return "persons" if args["kind"] == "person" else "helpers"

    def _check_trackers(
        self, fields: dict[str, Any], before: dict[str, Any] | None, operation: str
    ) -> dict[str, Any] | None:
        """Keep a person's trackers intact, and return what the client may see of it.

        Core clears them if the field is left out. Those the client cannot see, and so
        cannot name, stay whatever it states.
        """
        current: list[str] = before["device_trackers"] if before else []
        if "device_trackers" in fields:
            for entity_id in fields["device_trackers"]:
                self.writes.require_visible_entity(entity_id)
            fields["device_trackers"] = [
                *fields["device_trackers"],
                *(t for t in current if not self.exposure.entity_allowed(t)),
            ]
        elif operation == "update":
            fields["device_trackers"] = current
        return None if before is None else self._as_visible(before)

    def _as_visible(self, person: dict[str, Any]) -> dict[str, Any]:
        """Return a person without the trackers the client may not see."""
        trackers = person["device_trackers"]
        visible = [t for t in trackers if self.exposure.entity_allowed(t)]
        return {**person, "device_trackers": visible}

    @override
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check the helper and what it would refer to."""
        kind: str = args["kind"]
        operation: str = args["operation"]
        self.writes.require_domain(kind)
        fields: dict[str, Any] = dict(args.get("fields", {}))
        if "user_id" in fields:
            raise invalid_input("person_user_refused")
        # A dashboard that shows the person has every browser load it.
        if isinstance(picture := fields.get("picture"), str) and reaches_outside(
            picture
        ):
            raise invalid_input("picture_link_refused")
        user_id = llm_context.context.user_id if llm_context.context else None
        before = None
        target = ""
        stated = dict(fields)
        if operation != "create":
            if "item_id" not in args:
                raise invalid_input("registry_item_id_required")
            item_id: str = args["item_id"]
            target = f" {item_id}"
            if (entity_id := helper_entity_id(hass, kind, item_id)) is not None:
                if not self.exposure.entity_allowed(entity_id):
                    raise invalid_input("helper_not_found", item_id=item_id)
                self.writes.require_entity(entity_id)
            before = next(
                (
                    item
                    for item in await async_read_helpers(hass, user_id, kind)
                    if item["id"] == item_id
                ),
                None,
            )
            if before is None:
                raise invalid_input("helper_not_found", item_id=item_id)
            fields[f"{kind}_id"] = item_id
        if kind == "person":
            before = self._check_trackers(fields, before, operation)
        run = self.core_command(hass, llm_context, f"{kind}/{operation}", fields)

        async def async_apply() -> Any:
            result = await run()
            is_person = kind == "person" and operation != "delete"
            return self._as_visible(result) if is_person else result

        return WritePlan(
            summary=change_summary(f"{operation} {kind}{target}", stated),
            before=before,
            apply=async_apply,
            warnings=(
                (_INITIAL.format(kind=kind),)
                if kind.startswith("input_") and "initial" in fields
                else ()
            ),
        )
