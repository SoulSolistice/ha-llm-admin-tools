"""Tool that calls a service."""

from collections.abc import Mapping
from functools import partial
from typing import Any, override

import probatio
from probatio.humanize import humanize_error

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..actions.services import async_call_service
from ..errors import invalid_input
from ..policy.definition_guard import check_service_data
from ..policy.limits import MAX_SERVICE_ENTITIES
from .params import TEXT, require_valid_entity_id
from .write_base import WRITE_ANNOTATIONS, WRITE_PARAMS, WritePlan, WriteTool


class CallServiceTool(WriteTool):
    """Call a service the owner allows, on entities that may be changed."""

    name = "call_service"
    title = "Call a service"
    description = (
        "Call a service the owner allows, on entities named one by one. Areas, devices "
        "and all entities cannot be targeted. find_services lists what is allowed, "
        "with its fields. The result gives the states before the call, and after it "
        "once it finished; a device may report its new state a moment later. If "
        "finished is false, the service is still running and may yet take effect."
    )
    subjects = ("services",)
    # What a service does is up to it, and many reach outside: a notification, speech,
    # a cloud service.
    annotations = llm.ToolAnnotations(
        read_only=WRITE_ANNOTATIONS.read_only,
        destructive=WRITE_ANNOTATIONS.destructive,
        idempotent=WRITE_ANNOTATIONS.idempotent,
        open_world=True,
    )
    parameters = probatio.Schema(
        {
            probatio.Required("domain", description="For example light."): TEXT,
            probatio.Required("service", description="For example turn_on."): TEXT,
            probatio.Optional(
                "entity_ids", description="The entities to act on."
            ): probatio.All([str], probatio.Length(min=1, max=MAX_SERVICE_ENTITIES)),
            probatio.Optional(
                "data", description="The fields of the service, without any target."
            ): dict,
            **WRITE_PARAMS,
        }
    )

    @override
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check the service and everything it would act on."""
        # Core treats a name in capitals as the same service.
        domain = args["domain"].strip().lower()
        service = args["service"].strip().lower()
        self.writes.require_service(domain, service)
        if not hass.services.has_service(
            domain, service
        ) or not self.writes.service_visible(domain, service):
            raise invalid_input("service_not_found", service=f"{domain}.{service}")
        self.writes.require_service_target(domain, service)

        data: dict[str, Any] = args.get("data", {})
        check_service_data(self.writes, data)
        entity_ids = [
            require_valid_entity_id(entity_id.strip().lower())
            for entity_id in args.get("entity_ids", [])
        ]
        for entity_id in entity_ids:
            self.writes.require_existing_entity(entity_id)
            self.writes.require_target(entity_id)
        states = {
            entity_id: state.state
            for entity_id in entity_ids
            if (state := hass.states.get(entity_id)) is not None
        }
        call_data = {**data, **({ATTR_ENTITY_ID: entity_ids} if entity_ids else {})}
        schema = hass.services.async_services_for_domain(domain)[service].schema
        if schema is not None:
            try:
                schema(call_data)
            except probatio.Invalid as err:
                raise invalid_input(
                    "service_data_invalid",
                    service=f"{domain}.{service}",
                    message=humanize_error(call_data, err),
                ) from err
        targets = f" on {', '.join(entity_ids)}" if entity_ids else ""
        call = partial(
            async_call_service, hass, domain, service, call_data, llm_context.context
        )

        async def async_apply() -> dict[str, Any]:
            result = await call()
            if not result["finished"] or not entity_ids:
                return result
            after = {
                entity_id: state.state
                for entity_id in entity_ids
                if (state := hass.states.get(entity_id)) is not None
            }
            return {**result, "after": after}

        return WritePlan(
            summary=f"{domain}.{service}{targets}",
            before=states or None,
            apply=async_apply,
        )
