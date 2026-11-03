"""Read helpers, which core only offers through websocket commands."""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..core_commands import async_run_command
from ..policy.exposure import ExposurePolicy

_TAG = "tag"


def helper_entity_id(hass: HomeAssistant, kind: str, item_id: str) -> str | None:
    """Return the entity of a helper, if it has one."""
    return er.async_get(hass).async_get_entity_id(kind, kind, item_id)


async def async_read_helpers(
    hass: HomeAssistant, user_id: str | None, kind: str
) -> list[dict[str, Any]]:
    """Return the helpers of a kind that core keeps in storage."""
    result = await async_run_command(hass, user_id, f"{kind}/list")
    # Persons answer with those from storage and those from YAML, which are read-only.
    helpers: list[dict[str, Any]] = result["storage"] if kind == "person" else result
    return helpers


async def async_list_helpers(
    hass: HomeAssistant, policy: ExposurePolicy, user_id: str | None, kind: str
) -> list[dict[str, Any]]:
    """Return the helpers of a kind that may be seen, with what they refer to."""
    summaries = []
    if kind in policy.settings.excluded_domains:
        return []
    for item in await async_read_helpers(hass, user_id, kind):
        entity_id = helper_entity_id(hass, kind, item["id"])
        if entity_id is not None and not policy.entity_allowed(entity_id):
            continue
        summary = {**item, "entity_id": entity_id}
        if "device_trackers" in item:
            summary["device_trackers"] = [
                tracker
                for tracker in item["device_trackers"]
                if policy.entity_allowed(tracker)
            ]
        summaries.append(summary)
    summaries.sort(key=lambda summary: summary["id"])
    if kind == _TAG:
        # Whoever knows the ID of a tag can act as if it was scanned.
        for summary in summaries:
            del summary["id"]
    return summaries
