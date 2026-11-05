"""Read how automations and scripts ran, from the traces Home Assistant keeps."""

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from ..core_commands import async_run_command
from ..errors import invalid_input
from ..policy.exposure import ExposurePolicy
from ..policy.limits import MAX_HEALTH_ITEMS
from .compact import without_empty_fields
from .definitions import component_of

TRACE_KINDS = ("automation", "script")
_FAILED = frozenset({"aborted", "error"})


@callback
def trace_item(
    hass: HomeAssistant, policy: ExposurePolicy, entity_id: str
) -> tuple[str, str]:
    """Return the kind and ID under which Home Assistant keeps traces of an entity."""
    kind = entity_id.partition(".")[0]
    if kind not in TRACE_KINDS:
        raise invalid_input("trace_kind", entity_id=entity_id)
    component = component_of(hass, kind)
    entity = component.get_entity(entity_id) if component is not None else None
    if entity is None or not policy.entity_allowed(entity_id):
        raise invalid_input("entity_not_found", entity_id=entity_id)
    if entity.unique_id is None:
        raise invalid_input("trace_needs_id", entity_id=entity_id)
    return kind, entity.unique_id


def _run(trace: dict[str, Any]) -> dict[str, Any]:
    """Return what a client needs to see how a run went."""
    return without_empty_fields(
        {
            "run_id": trace["run_id"],
            "start": trace["timestamp"]["start"],
            "finish": trace["timestamp"]["finish"],
            "state": trace["state"],
            "outcome": trace["script_execution"],
            "trigger": trace.get("trigger"),
            "last_step": trace["last_step"],
            "error": trace.get("error"),
        }
    )


async def async_list_runs(
    hass: HomeAssistant, user_id: str | None, kind: str, item_id: str
) -> list[dict[str, Any]]:
    """Return the runs Home Assistant still has, newest first."""
    traces = await async_run_command(
        hass, user_id, "trace/list", {"domain": kind, "item_id": item_id}
    )
    runs = [_run(trace) for trace in traces]
    return sorted(runs, key=lambda run: run["start"], reverse=True)


@callback
def started_run(
    hass: HomeAssistant, policy: ExposurePolicy, child: dict[str, str] | None
) -> dict[str, str] | None:
    """Return the run a step started, to ask for in turn, if the client may see it."""
    if child is None or child["domain"] not in TRACE_KINDS:
        return None
    component = component_of(hass, child["domain"])
    entity_ids = [
        entity.entity_id
        for entity in (component.entities if component is not None else ())
        if entity.unique_id == child["item_id"]
    ]
    if not entity_ids or not policy.entity_allowed(entity_ids[0]):
        return None
    return {"entity_id": entity_ids[0], "run_id": child["run_id"]}


def _step(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    step: dict[str, Any],
    *,
    variables: bool,
) -> dict[str, Any]:
    """Return what happened in a step, which can carry far more than that."""
    return without_empty_fields(
        {
            "path": step["path"],
            "result": step.get("result"),
            "error": step.get("error"),
            "started": started_run(hass, policy, step.get("child_id")),
            "variables": step.get("changed_variables") if variables else None,
        }
    )


async def async_get_run(
    hass: HomeAssistant,
    user_id: str | None,
    kind: str,
    item_id: str,
    run_id: str,
    *,
    policy: ExposurePolicy,
    variables: bool,
) -> dict[str, Any]:
    """Return each step of a run; get_definition has the configuration."""
    trace = await async_run_command(
        hass,
        user_id,
        "trace/get",
        {"domain": kind, "item_id": item_id, "run_id": run_id},
    )
    return {
        **_run(trace),
        "steps": [
            _step(hass, policy, step, variables=variables)
            for steps in trace["trace"].values()
            for step in steps
        ],
    }


async def async_failed_runs(hass: HomeAssistant, user_id: str | None) -> dict[str, Any]:
    """Return the newest runs that failed, and what could not be looked at.

    No runs that failed, and not being able to see them, are different answers. Core
    keeps the runs of what was deleted until it restarts, which nobody can act on.
    """
    failed: list[dict[str, Any]] = []
    not_checked: list[str] = []
    for kind in TRACE_KINDS:
        component = component_of(hass, kind)
        if component is None:
            continue
        entity_ids = {
            entity.unique_id: entity.entity_id for entity in component.entities
        }
        try:
            traces = await async_run_command(
                hass, user_id, "trace/list", {"domain": kind}
            )
        except HomeAssistantError:
            not_checked.append(kind)
            continue
        failed.extend(
            {"entity_id": entity_ids.get(trace["item_id"]), **_run(trace)}
            for trace in traces
            if trace["item_id"] in entity_ids
            and (trace["script_execution"] in _FAILED or "error" in trace)
        )
    newest_first = sorted(failed, key=lambda run: run["start"], reverse=True)
    return {
        "count": len(failed),
        "items": newest_first[:MAX_HEALTH_ITEMS],
        **({"not_checked": not_checked} if not_checked else {}),
    }
