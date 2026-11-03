"""Report what a removal would take away, and what makes use of that."""

from typing import Any

from homeassistant.core import HomeAssistant

from ..policy.exposure import ExposurePolicy
from ..policy.limits import MAX_REPORT_ITEMS
from ..policy.members import Members
from .references import HOW_FOUND, async_read_sources, find_uses, names_of_members


def _some(items: list[Any]) -> dict[str, Any]:
    """Return as many as fit in a report, and how many there are."""
    return {"count": len(items), "items": items[:MAX_REPORT_ITEMS]}


async def async_removal_report(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    user_id: str | None,
    members: Members,
    *,
    read_definitions: bool,
    read_dashboards: bool,
) -> dict[str, Any]:
    """Return what would go, and what names it.

    This informs and does not refuse, so what cannot be read is stated, not raised.
    Everything in ``members`` has passed the check that it may be changed, and so seen.
    """
    sources = await async_read_sources(
        hass,
        policy,
        user_id,
        read_definitions=read_definitions,
        read_dashboards=read_dashboards,
    )
    used_by = await hass.async_add_executor_job(
        find_uses, sources, names_of_members(members)
    )
    return {
        "entities": _some(members.entity_ids),
        "devices": _some(
            sorted(
                (
                    {"id": device.id, "name": device.name_by_user or device.name}
                    for device in members.devices
                ),
                key=lambda device: device["id"],
            )
        ),
        "used_by": _some(used_by),
        **({"not_checked": sources.not_checked} if sources.not_checked else {}),
        "how_found": HOW_FOUND,
    }
