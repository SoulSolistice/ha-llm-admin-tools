"""Read the history of entities from the recorder, without reading more than needed.

What Home Assistant offers decides how:

- Only ``state_changes_during_period`` has a limit, and it applies to the oldest
  changes of a time window. The newest are found by reading back in growing steps.
- ``get_last_state_changes`` has to scan the states table, and warns against it.
- ``get_significant_states`` always reads the whole window, which for a busy sensor
  over 7 days is hundreds of thousands of states.

All of this runs in the recorder's executor.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from ..errors import read_failed
from ..policy.exposure import ExposurePolicy
from ..policy.limits import (
    MAX_HISTORY_POINTS_PER_ENTITY,
    MAX_HISTORY_POINTS_PER_REQUEST,
    MAX_HISTORY_STATES_READ_PER_LOOKBACK,
    MAX_HISTORY_STATES_READ_PER_REQUEST,
    MAX_HISTORY_SUMMARY_POINTS,
    MIN_HISTORY_STATISTICS_ROWS,
)
from .entities import entity_exists
from .history_summary import (
    changes_since,
    covered_until,
    is_numeric,
    newest_points,
    states_summary,
    statistics_summary,
)

_LOOKBACK_STEPS = (timedelta(minutes=5), timedelta(hours=1), timedelta(days=1))

_STATISTICS_TYPES: set[Any] = {"change", "max", "mean", "min", "state"}

type StatisticsPeriod = Literal["5minute", "hour", "day", "week", "month"]


def recorder_running(hass: HomeAssistant) -> bool:
    """Return if there is a recorder to read history from."""
    return "recorder" in hass.config.components


def require_recorder(hass: HomeAssistant) -> None:
    """Raise if there is no recorder to read history from."""
    if not recorder_running(hass):
        raise read_failed("recorder_unavailable")


def read_changes(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime, limit: int
) -> list[State]:
    """Return the state before the window, and its oldest changes up to a limit."""
    # The recorder and its database driver are optional, so import only when used.
    from homeassistant.components.recorder.history import (  # noqa: PLC0415
        state_changes_during_period,
    )

    return state_changes_during_period(
        hass, start, end, entity_id, no_attributes=True, limit=limit
    ).get(entity_id, [])


def read_statistics(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime,
    period: StatisticsPeriod,
) -> Sequence[Mapping[str, Any]]:
    """Return the statistics of an entity; there are none without a state class."""
    # The recorder and its database driver are optional, so import only when used.
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        statistics_during_period,
    )

    return statistics_during_period(
        hass, start, end, {entity_id}, period, None, _STATISTICS_TYPES
    ).get(entity_id, [])


def _read_newest_changes(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime,
    count: int,
    *,
    max_states: int,
) -> dict[str, Any]:
    """Return the newest changes of a window, reading back from its end in steps.

    Most entities are either busy, and done after the first step, or quiet, and read
    few states at every step. Should a step find too many to read, then what the step
    before it found has to do.
    """
    max_states = min(max_states, MAX_HISTORY_STATES_READ_PER_LOOKBACK)
    found: list[State] = []
    found_since = start
    reached_start = False
    shorter = [end - step for step in _LOOKBACK_STEPS if end - step > start]
    for since in (*shorter, start):
        states = read_changes(hass, entity_id, since, end, max_states + 1)
        changes = changes_since(states, since)
        if changes > max_states:
            if not found:
                # All that can be read of the shortest step are its oldest changes.
                states = states[:-1]
                return {
                    **newest_points(states, since, count, reached_start=False),
                    "newer_changes_left_out": True,
                    "covered_until": covered_until(states[-1]),
                }
            break
        found, found_since = states, since
        reached_start = since == start
        if changes > count:
            break
    return newest_points(found, found_since, count, reached_start=reached_start)


def _read_statistics_summary(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> dict[str, Any] | None:
    """Return a summary from statistics, if they say enough about the window.

    Statistics are already calculated, so this reads thousands of times fewer rows
    than the states behind them. Only sensors with a state class have them. Five-minute
    statistics are removed with old states, while hourly ones are kept.
    """
    keep_days = get_instance(hass).keep_days
    hourly = end - start >= timedelta(
        hours=MAX_HISTORY_SUMMARY_POINTS
    ) or start < dt_util.utcnow() - timedelta(days=keep_days)
    rows = read_statistics(hass, entity_id, start, end, "hour" if hourly else "5minute")
    if len(rows) < MIN_HISTORY_STATISTICS_ROWS:
        return None
    return statistics_summary(rows, 3600 if hourly else 300)


def _read_states_summary(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime, max_states: int
) -> dict[str, Any]:
    """Return a summary from states, of as much of the window as may be read."""
    states = read_changes(hass, entity_id, start, end, max_states + 1)
    changes = changes_since(states, start)
    if changes <= max_states:
        return {**states_summary(states, start, end), "changes_read": changes}
    states = states[:-1]
    return {
        **states_summary(states, start, states[-1].last_changed),
        "changes_read": max_states,
        "newer_changes_left_out": True,
        "covered_until": covered_until(states[-1]),
    }


def _read_summary(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime, max_states: int
) -> dict[str, Any]:
    """Return the cheapest answer that represents the whole window.

    A first read finds out if the entity is busy at all, and costs almost nothing.
    """
    limit = MAX_HISTORY_SUMMARY_POINTS
    states = read_changes(hass, entity_id, start, end, limit + 1)
    if changes_since(states, start) <= limit:
        return newest_points(states, start, limit, reached_start=True)
    if not is_numeric(states):
        return _read_newest_changes(
            hass, entity_id, start, end, limit, max_states=max_states
        )
    return _read_statistics_summary(
        hass, entity_id, start, end
    ) or _read_states_summary(hass, entity_id, start, end, max_states)


def _read_history(
    hass: HomeAssistant,
    entity_ids: list[str],
    start: datetime,
    end: datetime,
    limit: int | None,
) -> Mapping[str, dict[str, Any]]:
    """Return the history of entities; runs in the recorder's executor."""
    max_states = MAX_HISTORY_STATES_READ_PER_REQUEST // len(entity_ids)
    if limit is None:
        return {
            entity_id: _read_summary(hass, entity_id, start, end, max_states)
            for entity_id in entity_ids
        }
    count = min(
        limit,
        MAX_HISTORY_POINTS_PER_ENTITY,
        MAX_HISTORY_POINTS_PER_REQUEST // len(entity_ids),
    )
    return {
        entity_id: _read_newest_changes(
            hass, entity_id, start, end, count, max_states=max_states
        )
        for entity_id in entity_ids
    }


async def async_get_history(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    entity_ids: list[str],
    start: datetime,
    end: datetime,
    *,
    limit: int | None,
) -> dict[str, Any]:
    """Return the history of entities in a time window.

    A hidden entity gets the same answer as one that does not exist.
    """
    require_recorder(hass)
    visible = [
        entity_id for entity_id in entity_ids if policy.entity_allowed(entity_id)
    ]
    histories = (
        await get_instance(hass).async_add_executor_job(
            _read_history, hass, visible, start, end, limit
        )
        if visible
        else {}
    )
    return {
        entity_id: {
            "exists": entity_id in visible and entity_exists(hass, entity_id),
            **histories.get(entity_id, newest_points([], start, 1, reached_start=True)),
        }
        for entity_id in entity_ids
    }
