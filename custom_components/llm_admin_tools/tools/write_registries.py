"""Tools that change entities, devices and what organizes them."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    category_registry as cr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
    llm,
)

from ..core_commands import REGISTRY_KINDS
from ..errors import invalid_input
from ..policy.members import members_of
from ..policy.writes import WritePolicy
from .params import TEXT
from .write_base import WRITE_PARAMS, WritePlan, WriteTool, change_summary

_OPERATION = probatio.In(("update", "delete"))
_NAME_OR_NONE = probatio.Any(str, None)
_USER_OR_NONE = probatio.Any("user", None)
# Core answers with all it knows, and that includes serial numbers, MAC addresses and
# the unique IDs of integrations, which often are one of those. Reading leaves them out.
_ENTITY_ANSWER_FIELDS = (
    "aliases",
    "area_id",
    "categories",
    "device_class",
    "device_id",
    "disabled_by",
    "entity_id",
    "hidden_by",
    "icon",
    "labels",
    "name",
)
_ENTITY_UPDATE_FIELDS = ("reload_delay", "require_restart")
_DEVICE_ANSWER_FIELDS = (
    "area_id",
    "disabled_by",
    "id",
    "labels",
    "manufacturer",
    "model",
    "name",
    "name_by_user",
)
_SUBJECTS = {
    "area": "areas",
    "floor": "floors",
    "label": "labels",
    "category": "categories",
}


def _check_new_links(policy: WritePolicy, fields: Mapping[str, Any]) -> None:
    """Raise if something would become part of what a client may not change."""
    if (area_id := fields.get("area_id")) is not None:
        policy.require_area(area_id)
    for label_id in fields.get("labels", ()):
        policy.require_label(label_id)


def _only(answer: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """Return those fields of an answer of core that a client should get."""
    return {name: answer[name] for name in names if name in answer}


def _changes(args: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """Return the fields a client stated; leaving one out leaves it as it is."""
    return {name: args[name] for name in names if name in args}


class UpdateEntityTool(WriteTool):
    """Change how an entity is registered."""

    name = "update_entity"
    title = "Update an entity"
    description = (
        "Change the registry entry of an entity: its name, icon, area, labels, "
        "aliases, entity ID, whether it is disabled or hidden. Only the fields you "
        "state change. delete removes the registry entry."
    )
    subjects = ("entities",)
    _FIELDS = (
        "aliases",
        "area_id",
        "categories",
        "device_class",
        "disabled_by",
        "hidden_by",
        "icon",
        "labels",
        "name",
        "new_entity_id",
    )
    parameters = probatio.Schema(
        {
            probatio.Required("entity_id", description="The entity ID."): TEXT,
            probatio.Optional(
                "operation", description="What to do.", default="update"
            ): _OPERATION,
            probatio.Optional("name"): _NAME_OR_NONE,
            probatio.Optional("icon"): _NAME_OR_NONE,
            probatio.Optional("area_id"): _NAME_OR_NONE,
            probatio.Optional("labels"): [str],
            probatio.Optional(
                "aliases", description="null stands for the name of the entity."
            ): [_NAME_OR_NONE],
            probatio.Optional(
                "categories", description="A category ID for each scope."
            ): dict,
            probatio.Optional("device_class"): _NAME_OR_NONE,
            probatio.Optional("new_entity_id"): str,
            probatio.Optional("disabled_by"): _USER_OR_NONE,
            probatio.Optional("hidden_by"): _USER_OR_NONE,
            **WRITE_PARAMS,
        }
    )

    @override
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check the entity and what it would be linked to."""
        entity_id = args["entity_id"].strip().lower()
        self.writes.require_existing_entity(entity_id)
        entry = er.async_get(hass).async_get(entity_id)
        if entry is None:
            raise invalid_input("entity_not_registered", entity_id=entity_id)
        before = {
            "entity_id": entry.entity_id,
            "name": entry.name,
            "icon": entry.icon,
            "area_id": entry.area_id,
            "labels": sorted(entry.labels),
            "aliases": [
                None if alias is er.COMPUTED_NAME else alias for alias in entry.aliases
            ],
            "categories": entry.categories,
            "device_class": entry.device_class,
            "disabled_by": entry.disabled_by,
            "hidden_by": entry.hidden_by,
        }
        if args["operation"] == "delete":
            return WritePlan(
                summary=f"delete entity {entity_id}",
                before=before,
                apply=self.core_command(
                    hass,
                    llm_context,
                    "config/entity_registry/remove",
                    {"entity_id": entity_id},
                ),
            )
        fields = _changes(args, self._FIELDS)
        _check_new_links(self.writes, fields)
        run = self.core_command(
            hass,
            llm_context,
            "config/entity_registry/update",
            {"entity_id": entity_id, **fields},
        )

        async def async_apply() -> dict[str, Any]:
            answer = await run()
            return {
                **_only(answer["entity_entry"], _ENTITY_ANSWER_FIELDS),
                **_only(answer, _ENTITY_UPDATE_FIELDS),
            }

        return WritePlan(
            summary=change_summary(f"update entity {entity_id}", fields),
            before=before,
            apply=async_apply,
        )


class UpdateDeviceTool(WriteTool):
    """Change how a device is registered."""

    name = "update_device"
    title = "Update a device"
    description = (
        "Change the registry entry of a device: its name, area, labels, whether it is "
        "disabled. Only the fields you state change. delete removes the device, if its "
        "integration allows that. delete and disabling report in affects which "
        "entities and devices go and what uses them; use dry_run to see that first."
    )
    subjects = ("devices",)
    _FIELDS = ("area_id", "disabled_by", "labels", "name_by_user")
    parameters = probatio.Schema(
        {
            probatio.Required("device_id", description="The device ID."): TEXT,
            probatio.Optional(
                "operation", description="What to do.", default="update"
            ): _OPERATION,
            probatio.Optional("name_by_user"): _NAME_OR_NONE,
            probatio.Optional("area_id"): _NAME_OR_NONE,
            probatio.Optional("labels"): [str],
            probatio.Optional("disabled_by"): _USER_OR_NONE,
            **WRITE_PARAMS,
        }
    )

    @override
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check the device and what it would be linked to."""
        device_id: str = args["device_id"]
        device = self.writes.require_device(device_id)
        self.writes.require_device_members_writable(device)
        before = {
            "name_by_user": device.name_by_user,
            "area_id": device.area_id,
            "labels": sorted(device.labels),
            "disabled_by": device.disabled_by,
        }
        if args["operation"] == "delete":
            return WritePlan(
                summary=f"delete device {device_id}",
                before=before,
                apply=self.core_command(
                    hass,
                    llm_context,
                    "config/device_registry/remove",
                    {"device_id": device_id},
                ),
                affects=self.removal_report(
                    hass, llm_context, members_of(hass, [], [device])
                ),
            )
        fields = _changes(args, self._FIELDS)
        _check_new_links(self.writes, fields)
        run = self.core_command(
            hass,
            llm_context,
            "config/device_registry/update",
            {"device_id": device_id, **fields},
        )

        async def async_apply() -> dict[str, Any]:
            return _only(await run(), _DEVICE_ANSWER_FIELDS)

        return WritePlan(
            summary=change_summary(f"update device {device_id}", fields),
            before=before,
            apply=async_apply,
            affects=(
                self.removal_report(hass, llm_context, members_of(hass, [], [device]))
                if fields.get("disabled_by")
                else None
            ),
        )


def _registry_item(
    hass: HomeAssistant, kind: str, item_id: str, scope: str | None
) -> dict[str, Any] | None:
    """Return what is known of an item, as the client can have seen it."""
    if kind == "area":
        area = ar.async_get(hass).async_get_area(item_id)
        return {"name": area.name, "floor_id": area.floor_id} if area else None
    if kind == "floor":
        floor = fr.async_get(hass).async_get_floor(item_id)
        return {"name": floor.name, "level": floor.level} if floor else None
    if kind == "label":
        label = lr.async_get(hass).async_get_label(item_id)
        return {"name": label.name, "color": label.color} if label else None
    category = cr.async_get(hass).async_get_category(
        scope=scope or "", category_id=item_id
    )
    return {"name": category.name, "icon": category.icon} if category else None


class WriteRegistryItemTool(WriteTool):
    """Create, change or delete an area, floor, label or category."""

    name = "write_registry_item"
    title = "Write an area, floor, label or category"
    description = (
        "Create, change or delete an area, floor, label or category. fields are those "
        "Home Assistant takes for it: name, icon, aliases, floor_id, labels, level, "
        "color or description. Only the fields you state change."
    )
    subjects = tuple(_SUBJECTS.values())
    parameters = probatio.Schema(
        {
            probatio.Required("kind", description="What to write."): probatio.In(
                REGISTRY_KINDS
            ),
            probatio.Required("operation", description="What to do."): probatio.In(
                ("create", "update", "delete")
            ),
            probatio.Optional(
                "item_id", description="Its ID. Required unless creating."
            ): TEXT,
            probatio.Optional(
                "scope", description="For a category, such as automation."
            ): TEXT,
            probatio.Optional("fields", description="What to set."): dict,
            **WRITE_PARAMS,
        }
    )

    @override
    def subject_of(self, args: Mapping[str, Any]) -> str:
        """Return the subject of the kind being written."""
        return _SUBJECTS[args["kind"]]

    def _check_item(self, kind: str, item_id: str, *, deleting: bool) -> None:
        """Raise unless an existing item may be changed."""
        if kind == "area":
            self.writes.require_area(item_id)
            if deleting:
                self.writes.require_area_deletable(item_id)
        elif kind == "floor":
            self.writes.require_floor(item_id)
        elif kind == "label":
            self.writes.require_label(item_id)
            if deleting:
                self.writes.require_label_deletable(item_id)

    def _check_fields(self, fields: Mapping[str, Any]) -> None:
        """Raise if an item would come to refer to what may not be seen."""
        for key in ("humidity_entity_id", "temperature_entity_id"):
            if (entity_id := fields.get(key)) is not None:
                self.writes.require_visible_entity(entity_id)
        for label_id in fields.get("labels", ()):
            self.writes.require_label(label_id)

    @override
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check the item and what it would refer to."""
        kind: str = args["kind"]
        operation: str = args["operation"]
        fields: dict[str, Any] = dict(args.get("fields", {}))
        if "scope" in args:
            fields["scope"] = args["scope"]
        self._check_fields(fields)
        summary = change_summary(f"{operation} {kind}", fields)
        before = None
        command = f"config/{kind}_registry/{operation}"
        if operation == "create":
            apply = self.core_command(hass, llm_context, command, fields)
        else:
            if "item_id" not in args:
                raise invalid_input("registry_item_id_required")
            item_id: str = args["item_id"]
            self._check_item(kind, item_id, deleting=operation == "delete")
            summary = change_summary(f"{operation} {kind} {item_id}", fields)
            fields[f"{kind}_id"] = item_id
            # Core's schema goes first, for a category that is missing its scope.
            apply = self.core_command(hass, llm_context, command, fields)
            before = _registry_item(hass, kind, item_id, fields.get("scope"))
            if before is None:
                raise invalid_input("registry_item_not_found", item_id=item_id)
        return WritePlan(summary=summary, before=before, apply=apply)
