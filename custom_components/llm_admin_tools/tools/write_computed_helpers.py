"""Tool that creates, changes and deletes helpers computed from other entities.

These are set up through the forms of their integration, as the UI does.
"""

from collections.abc import Mapping
from contextlib import suppress
from typing import Any, override

import probatio

from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.data_entry_flow import (
    FlowManager,
    FlowResultType,
    InvalidData,
    UnknownFlow,
    UnknownHandler,
)
from homeassistant.helpers import entity_registry as er, llm

from ..errors import invalid_input
from ..policy.members import config_entry_members
from .params import TEXT
from .write_base import WRITE_PARAMS, WritePlan, WriteTool, change_summary

# Each reads other entities only, with no template, member or code that could get
# around what may be seen. A template, a group or history_stats could.
COMPUTED_HELPER_KINDS = (
    "derivative",
    "filter",
    "integration",
    "min_max",
    "statistics",
    "threshold",
    "trend",
    "utility_meter",
)
# What the forms of config and options flows answer, which differ only in their types.
_Result = Mapping[str, Any]
# Statistics, filter and trend ask in two or three forms; more means something else.
_MAX_STEPS = 5
_UNKNOWN_ENTITIES = (
    "These entities do not exist yet, so it computes nothing until they do: "
    "{entities}. Check for a typo."
)


def _named_entities(value: Any) -> list[str]:
    """Return what in a field is an entity ID, as a selector would take it."""
    items = value if isinstance(value, list) else [value]
    return [
        item.strip().lower()
        for item in items
        if isinstance(item, str) and valid_entity_id(item.strip().lower())
    ]


def _keys(result: _Result) -> list[str]:
    """Return the fields a form asks for."""
    schema = result.get("data_schema")
    return [str(key) for key in schema.schema] if schema is not None else []


class WriteComputedHelperTool(WriteTool):
    """Set up, change or delete a helper that the UI makes with a form."""

    name = "write_computed_helper"
    title = "Write a helper computed from other entities"
    description = (
        "Create, change or delete a helper computed from other entities: "
        f"{', '.join(COMPUTED_HELPER_KINDS)}. fields are those its form in the UI "
        "asks for, by their keys, such as source and time_window of a derivative; "
        "for a form in several steps, give the fields of all of them. Each entity it "
        "reads must be one you may see. To change or delete one, give the entry_id "
        "from list_integrations, and for a change only the fields that change. "
        "dry_run checks what it names and the fields of its first form."
    )
    subjects = ("helpers",)
    parameters = probatio.Schema(
        {
            probatio.Required("operation", description="What to do."): probatio.In(
                ("create", "update", "delete")
            ),
            probatio.Optional("kind", description="What to create."): probatio.In(
                COMPUTED_HELPER_KINDS
            ),
            probatio.Optional(
                "entry_id", description="Its entry. Required unless creating."
            ): TEXT,
            probatio.Optional(
                "fields", description="Its settings. Required unless deleting."
            ): dict,
            **WRITE_PARAMS,
        }
    )

    def _check_fields(self, fields: Mapping[str, Any]) -> list[str]:
        """Raise unless each entity named may be seen; return those not there."""
        unknown = []
        for value in fields.values():
            for entity_id in _named_entities(value):
                self.writes.require_visible_entity(entity_id)
                if not self.writes.entity_exists(entity_id):
                    unknown.append(entity_id)
        return sorted(set(unknown))

    def _require_entry(self, args: Mapping[str, Any]) -> ConfigEntry:
        """Return the entry of a helper of these kinds that may be changed."""
        if "entry_id" not in args:
            raise invalid_input("registry_item_id_required")
        entry = self.writes.require_config_entry(args["entry_id"])
        if entry.domain not in COMPUTED_HELPER_KINDS:
            raise invalid_input("computed_helper_kind", kind=entry.domain)
        self.writes.require_config_entry_members_writable(entry.entry_id)
        return entry

    @override
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check the helper and what it would read."""
        operation: str = args["operation"]
        fields: dict[str, Any] = dict(args.get("fields", {}))
        if operation == "delete":
            return self._plan_delete(hass, llm_context, self._require_entry(args))
        if "fields" not in args:
            raise invalid_input("computed_helper_fields_required")
        unknown = self._check_fields(fields)
        warnings = (
            (_UNKNOWN_ENTITIES.format(entities=", ".join(unknown)),) if unknown else ()
        )
        if operation == "update":
            entry = self._require_entry(args)
            # A form takes all its fields, of which the client states what changes.
            stated = {**entry.options, **fields}

            async def async_update() -> dict[str, Any]:
                manager = hass.config_entries.options
                result = await manager.async_init(entry.entry_id)
                await _async_finish(manager, result, stated)
                return _report(hass, entry)

            return WritePlan(
                summary=change_summary(
                    f"update {entry.domain} entry {entry.entry_id}", fields
                ),
                before=dict(entry.options),
                apply=async_update,
                warnings=warnings,
            )

        if "kind" not in args:
            raise invalid_input("computed_helper_kind_required")
        kind: str = args["kind"]
        manager = hass.config_entries.flow
        # The first form is checked now, so a dry run says what it would refuse.
        first = await _async_start(manager, kind)
        try:
            _require_form(first)
            schema = first.get("data_schema") or probatio.Schema({})
            schema({key: fields[key] for key in _keys(first) if key in fields})
        except probatio.Invalid as err:
            raise invalid_input(
                "computed_helper_fields_invalid", error=str(err)
            ) from err
        finally:
            _abort(manager, first["flow_id"])

        async def async_create() -> dict[str, Any]:
            result = await _async_start(manager, kind)
            done = await _async_finish(manager, result, fields)
            entry: ConfigEntry = done["result"]
            return _report(hass, entry)

        return WritePlan(
            summary=change_summary(f"create {kind}", fields),
            apply=async_create,
            warnings=warnings,
        )

    def _plan_delete(
        self, hass: HomeAssistant, llm_context: llm.LLMContext, entry: ConfigEntry
    ) -> WritePlan:
        """Return how to delete a helper, and what that affects."""

        async def async_delete() -> dict[str, Any]:
            return await hass.config_entries.async_remove(entry.entry_id)

        return WritePlan(
            summary=f"delete {entry.domain} entry {entry.entry_id}",
            before=dict(entry.options),
            apply=async_delete,
            affects=self.removal_report(
                hass, llm_context, config_entry_members(hass, entry.entry_id)
            ),
        )


async def _async_start(manager: FlowManager[Any, Any], kind: str) -> _Result:
    """Return the first form of the integration of a kind."""
    try:
        result: _Result = await manager.async_init(
            kind, context={"source": SOURCE_USER}
        )
    except UnknownHandler as err:
        raise invalid_input("computed_helper_refused", reason="not available") from err
    return result


def _abort(manager: FlowManager[Any, Any], flow_id: str) -> None:
    """Close a form left open, which an integration may already have closed."""
    with suppress(UnknownFlow):
        manager.async_abort(flow_id)


def _require_form(result: _Result) -> None:
    """Raise unless the integration asks for fields, as it does for a helper."""
    if result["type"] is FlowResultType.ABORT:
        raise invalid_input("computed_helper_refused", reason=str(result["reason"]))
    if result["type"] is not FlowResultType.FORM:
        raise invalid_input("computed_helper_refused", reason=str(result["type"]))


async def _async_finish(
    manager: FlowManager[Any, Any], result: _Result, fields: Mapping[str, Any]
) -> _Result:
    """Fill in each form with the fields it asks for, until the helper is made."""
    flow_id = result["flow_id"]
    try:
        for _ in range(_MAX_STEPS):
            if result["type"] is FlowResultType.CREATE_ENTRY:
                return result
            _require_form(result)
            if errors := result.get("errors"):
                raise invalid_input("computed_helper_fields_invalid", error=str(errors))
            asked = {key: fields[key] for key in _keys(result) if key in fields}
            try:
                result = await manager.async_configure(flow_id, asked)
            except InvalidData as err:
                raise invalid_input(
                    "computed_helper_fields_invalid", error=str(err.schema_errors)
                ) from err
        raise invalid_input("computed_helper_refused", reason="too many forms")
    except Exception:
        _abort(manager, flow_id)
        raise


def _report(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return the entry with the entities it made and their states."""
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "entities": {
            item.entity_id: state.state
            if (state := hass.states.get(item.entity_id))
            else None
            for item in entities
        },
    }
