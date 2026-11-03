"""Tool that lists the repairs of Home Assistant, among them what Spook found."""

from collections.abc import Mapping
from typing import Any, override

import probatio

from homeassistant.core import HomeAssistant

from ..queries.repairs import (
    RepairQuery,
    async_describe_issues,
    spook_active,
    visible_issues,
)
from .base import ReadOnlyTool
from .params import DATA_NOTICE, PAGING, TEXT, paginate


class ListRepairsTool(ReadOnlyTool):
    """List what Home Assistant and its integrations report as needing repair."""

    name = "list_repairs"
    title = "List repairs"
    description = (
        "List the repairs, the most severe first, with the details each holds. If "
        "Spook is installed these include what it found, such as what names an unknown "
        "entity, with suggestions of what may have been meant: use domain spook for "
        f"those. Repairs that name something hidden are left out. {DATA_NOTICE}"
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "domain", description="Only those of this integration."
            ): TEXT,
            probatio.Optional(
                "include_ignored",
                description="Also those the owner chose to ignore.",
                default=False,
            ): bool,
            probatio.Optional(
                "include_descriptions",
                description="Also the explanation of each. This is long.",
                default=False,
            ): bool,
            **PAGING,
        }
    )

    @override
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return one page of repairs."""
        issues = visible_issues(
            hass,
            self.exposure,
            RepairQuery(
                domain=args.get("domain"), include_ignored=args["include_ignored"]
            ),
        )
        page = paginate(issues, args)
        return {
            **page,
            "items": await async_describe_issues(
                hass, page["items"], descriptions=args["include_descriptions"]
            ),
            "spook": spook_active(hass),
        }
