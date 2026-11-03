"""Read much more history of one entity, for a request that says it needs that.

A hidden entity gets the same answer as one that does not exist.
"""

from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance

from ..policy.exposure import ExposurePolicy
from .entities import entity_exists
from .history import StatisticsPeriod, read_changes, read_statistics, require_recorder
from .history_summary import oldest_points, statistics_rows

# A month is taken at its longest, so that a window cannot hold more rows than counted.
STATISTICS_PERIOD_SECONDS: dict[StatisticsPeriod, int] = {
    "5minute": 300,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2678400,
}


def _read_oldest_points(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime, limit: int
) -> dict[str, Any]:
    """Return changes from the start of a window; runs in the recorder's executor."""
    states = read_changes(hass, entity_id, start, end, limit + 1)
    return oldest_points(states, start, limit)


def _read_statistics_rows(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime,
    period: StatisticsPeriod,
) -> dict[str, Any]:
    """Return one row per period; runs in the recorder's executor."""
    rows = read_statistics(hass, entity_id, start, end, period)
    return statistics_rows(rows, period)


async def async_get_extended_changes(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    entity_id: str,
    start: datetime,
    end: datetime,
    *,
    limit: int,
) -> dict[str, Any]:
    """Return up to a limit of exact changes, oldest first from the start."""
    require_recorder(hass)
    allowed = policy.entity_allowed(entity_id)
    return {
        "exists": allowed and entity_exists(hass, entity_id),
        **(
            await get_instance(hass).async_add_executor_job(
                _read_oldest_points, hass, entity_id, start, end, limit
            )
            if allowed
            else oldest_points([], start, limit)
        ),
    }


async def async_get_extended_statistics(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    entity_id: str,
    start: datetime,
    end: datetime,
    *,
    period: StatisticsPeriod,
) -> dict[str, Any]:
    """Return one row of statistics per period of a window."""
    require_recorder(hass)
    allowed = policy.entity_allowed(entity_id)
    return {
        "exists": allowed and entity_exists(hass, entity_id),
        **(
            await get_instance(hass).async_add_executor_job(
                _read_statistics_rows, hass, entity_id, start, end, period
            )
            if allowed
            else statistics_rows([], period)
        ),
    }
