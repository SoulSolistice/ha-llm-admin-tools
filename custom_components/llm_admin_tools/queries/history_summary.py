"""Reduce what the recorder returned for one entity to what a client can use."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from ..policy.limits import MAX_HISTORY_SUMMARY_POINTS
from .compact import without_empty_fields

SOURCE_STATES = "states"
SOURCE_STATISTICS = "statistics"

_NO_VALUE_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})

# A time leaves here rounded to a microsecond, while the recorder compares the exact
# time it stored. Asked to continue from a rounded time, it could return the same
# change again, and forever if that change was all a client asked for.
_ROUNDING_MARGIN = timedelta(microseconds=1)


def _as_number(state: str) -> float | None:
    """Return a state as a finite number, if it is one."""
    try:
        number = float(state)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def changes_since(states: list[State], start: datetime) -> int:
    """Return how many states changed within a window, unlike the one before it."""
    since = start.timestamp()
    return sum(1 for state in states if state.last_changed_timestamp > since)


def covered_until(state: State) -> datetime:
    """Return the time to continue from, for changes newer than a state."""
    return state.last_changed + _ROUNDING_MARGIN


def _points(states: list[State]) -> list[dict[str, Any]]:
    """Return states as what they were and when they became that."""
    return [
        {"state": state.state, "last_changed": state.last_changed} for state in states
    ]


def _split_at_start(
    states: list[State], start: datetime
) -> tuple[dict[str, str], list[State]]:
    """Return the value held when a window began, and the changes within it.

    The recorder adds that value to the changes, usually with the start of the window
    as its time, when it did not change then. A client should not take it for a change.
    """
    since = start.timestamp()
    changes = [state for state in states if state.last_changed_timestamp > since]
    held = states[-len(changes) - 1] if len(states) > len(changes) else None
    return ({"state_at_start": held.state} if held else {}), changes


def newest_points(
    states: list[State], start: datetime, count: int, *, reached_start: bool
) -> dict[str, Any]:
    """Return the newest changes, and whether older ones exist that are left out.

    Without having reached the start of the time window, older ones may exist, and
    the client can continue from there.
    """
    at_start, changes = _split_at_start(states, start)
    kept = changes[-count:]
    complete = reached_start and len(changes) <= count
    reaches_back_to: datetime | None = None if complete else start
    if kept:
        reaches_back_to = kept[0].last_changed - _ROUNDING_MARGIN
    return {
        "points": _points(kept),
        "older_changes_left_out": not complete,
        "covered_from": reaches_back_to,
        **(at_start if complete else {}),
    }


def oldest_points(states: list[State], start: datetime, limit: int) -> dict[str, Any]:
    """Return the oldest changes of a window, and where to continue if more exist."""
    at_start, changes = _split_at_start(states, start)
    kept = changes[:limit]
    more = len(changes) > limit
    return {
        "points": _points(kept),
        "newer_changes_left_out": more,
        "covered_until": covered_until(kept[-1]) if more else None,
        **at_start,
    }


def is_numeric(states: list[State]) -> bool:
    """Return if an entity only has numbers, apart from having no value at times."""
    numbers = [_as_number(state.state) is not None for state in states]
    return any(numbers) and all(
        is_number or state.state in _NO_VALUE_STATES
        for is_number, state in zip(numbers, states, strict=True)
    )


@dataclass(slots=True)
class _Bucket:
    """What happened in one part of the time window."""

    changes: int = 0
    seconds: float = 0
    weighted_sum: float = 0
    no_value_seconds: float = 0
    minimum: float | None = None
    maximum: float | None = None
    last: float | None = None

    def add(self, value: float | None, seconds: float) -> None:
        """Record that a value was held for a time."""
        if value is None:
            self.no_value_seconds += seconds
            return
        self.seconds += seconds
        self.weighted_sum += value * seconds
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.last = value

    def as_dict(self, start: datetime) -> dict[str, Any]:
        """Return the facts of the bucket, leaving out those that do not apply."""
        result: dict[str, Any] = {"start": start, "changes": self.changes}
        if self.last is not None:
            result |= {
                "min": self.minimum,
                # Weighted by time: a value held for an hour counts for more than a
                # burst of values within a second. Those can have no time at all.
                "mean": round(self.weighted_sum / self.seconds, 3)
                if self.seconds
                else self.last,
                "max": self.maximum,
                "last": self.last,
            }
        if self.no_value_seconds:
            result["no_value_seconds"] = round(self.no_value_seconds)
        return result


def states_summary(
    states: list[State], start: datetime, end: datetime
) -> dict[str, Any]:
    """Return min, mean, max and last per part of the time window, from raw states."""
    first = start.timestamp()
    # Nothing can have happened yet in a window that ends in the future.
    last = max(min(end, dt_util.utcnow()).timestamp(), first + 1)
    count = MAX_HISTORY_SUMMARY_POINTS
    width = (last - first) / count
    buckets = [_Bucket() for _ in range(count)]

    def index_at(timestamp: float) -> int:
        return min(int((timestamp - first) / width), count - 1)

    following = [state.last_changed_timestamp for state in states[1:]]
    for state, next_change in zip(states, [*following, last], strict=True):
        value = _as_number(state.state)
        held_from = max(state.last_changed_timestamp, first)
        held_until = min(next_change, last)
        # The state from before the window is not a change within it.
        if state.last_changed_timestamp > first:
            buckets[index_at(held_from)].changes += 1
        for index in range(index_at(held_from), index_at(held_until) + 1):
            until = min(first + (index + 1) * width, held_until)
            buckets[index].add(value, max(until - held_from, 0))
            held_from = max(until, held_from)
            # A value that ends on the edge of a bucket has no part in the next one.
            if held_from >= held_until:
                break

    return {
        "source": SOURCE_STATES,
        "bucket_seconds": round(width),
        "buckets": [
            bucket.as_dict(dt_util.utc_from_timestamp(first + index * width))
            for index, bucket in enumerate(buckets)
            if bucket.changes or bucket.last is not None or bucket.no_value_seconds
        ],
    }


def _present(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    """Return the values of a statistic in rows that have one."""
    return [row[key] for row in rows if row.get(key) is not None]


def _statistics_bucket(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return one bucket from statistics rows of the same length of time.

    Measurements have a min, mean and max. Meters have a state and a change.
    """
    minimums = _present(rows, "min")
    means = _present(rows, "mean")
    maximums = _present(rows, "max")
    changes = _present(rows, "change")
    return without_empty_fields(
        {
            "start": dt_util.utc_from_timestamp(rows[0]["start"]),
            "min": min(minimums) if minimums else None,
            "mean": round(sum(means) / len(means), 3) if means else None,
            "max": max(maximums) if maximums else None,
            "last": rows[-1].get("state"),
            "change": round(sum(changes), 3) if changes else None,
        }
    )


def statistics_rows(rows: Sequence[Mapping[str, Any]], period: str) -> dict[str, Any]:
    """Return one bucket for every row of statistics."""
    return {
        "source": SOURCE_STATISTICS,
        "period": period,
        "buckets": [_statistics_bucket([row]) for row in rows],
        "covered_from": dt_util.utc_from_timestamp(rows[0]["start"]) if rows else None,
        "covered_until": dt_util.utc_from_timestamp(rows[-1]["end"]) if rows else None,
    }


def statistics_summary(
    rows: Sequence[Mapping[str, Any]], row_seconds: int
) -> dict[str, Any]:
    """Return buckets of whole statistics rows, however many of them there are."""
    per_bucket = math.ceil(len(rows) / MAX_HISTORY_SUMMARY_POINTS)
    return {
        "source": SOURCE_STATISTICS,
        "bucket_seconds": per_bucket * row_seconds,
        "buckets": [
            _statistics_bucket(rows[index : index + per_bucket])
            for index in range(0, len(rows), per_bucket)
        ],
        "covered_from": dt_util.utc_from_timestamp(rows[0]["start"]),
        "covered_until": dt_util.utc_from_timestamp(rows[-1]["end"]),
    }
