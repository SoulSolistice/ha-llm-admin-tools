"""Tests for how what the recorder returned for one entity is reduced."""

from datetime import datetime, timedelta
from typing import Any

from custom_components.llm_admin_tools.policy.limits import (
    MAX_HISTORY_SUMMARY_POINTS,
)
from custom_components.llm_admin_tools.queries.history_summary import (
    is_numeric,
    newest_points,
    oldest_points,
    states_summary,
    statistics_summary,
)
from homeassistant.core import State
from homeassistant.util import dt as dt_util

_END = dt_util.utcnow().replace(microsecond=0) - timedelta(hours=1)
_START = _END - timedelta(seconds=MAX_HISTORY_SUMMARY_POINTS * 10)
_MANY = MAX_HISTORY_SUMMARY_POINTS + 1
_EARLIER = _START - timedelta(hours=1)


def _state(value: Any, at: datetime) -> State:
    return State("sensor.a", str(value), last_changed=at)


def _summary(states: list[State]) -> dict[str, Any]:
    return states_summary(states, _START, _END)


def _alternating(first: float, second: float) -> list[State]:
    """Return many changes, all within the first second of the first bucket."""
    return [
        _state(
            first if index % 2 else second,
            _START + timedelta(seconds=(index + 1) / (_MANY + 1)),
        )
        for index in range(_MANY)
    ]


def test_newest_points_reaching_the_start() -> None:
    """Everything fits, and nothing older exists."""
    states = [_state(index, _START + timedelta(seconds=index)) for index in range(3)]
    result = newest_points(states, _EARLIER, 3, reached_start=True)
    assert [point["state"] for point in result["points"]] == ["0", "1", "2"]
    assert result["older_changes_left_out"] is False
    assert result["covered_from"] < states[0].last_changed


def test_newest_points_keeps_the_newest() -> None:
    """What does not fit is left out from the old end, and that is reported."""
    states = [_state(index, _START + timedelta(seconds=index)) for index in range(5)]
    result = newest_points(states, _EARLIER, 2, reached_start=True)
    assert [point["state"] for point in result["points"]] == ["3", "4"]
    assert result["older_changes_left_out"] is True
    assert states[2].last_changed < result["covered_from"] < states[3].last_changed


def test_newest_points_before_reaching_the_start() -> None:
    """Without having looked further back, older ones may exist."""
    states = [_state(1, _START)]
    assert newest_points(states, _EARLIER, 5, reached_start=False)[
        "older_changes_left_out"
    ]


def test_times_to_continue_from_are_past_the_change() -> None:
    """Rounded on its way out, such a time must still not reach the change itself."""
    states = [_state(index, _START + timedelta(seconds=index)) for index in range(3)]
    older = newest_points(states, _EARLIER, 2, reached_start=True)
    assert states[0].last_changed < older["covered_from"] < states[1].last_changed
    newer = oldest_points(states, _EARLIER, 2)
    assert states[1].last_changed < newer["covered_until"] < states[2].last_changed


def test_value_at_the_start_is_not_a_change() -> None:
    """The value held when the window began is told apart from the changes."""
    states = [_state("old", _START), _state("new", _START + timedelta(seconds=1))]
    newest = newest_points(states, _START, 5, reached_start=True)
    assert [point["state"] for point in newest["points"]] == ["new"]
    assert newest["state_at_start"] == "old"
    oldest = oldest_points(states, _START, 5)
    assert [point["state"] for point in oldest["points"]] == ["new"]
    assert oldest["state_at_start"] == "old"


def test_value_at_the_start_of_a_window_without_changes() -> None:
    """An entity that did not change still has a value, and nothing older is implied."""
    result = newest_points([_state("old", _START)], _START, 5, reached_start=True)
    assert result == {
        "points": [],
        "older_changes_left_out": False,
        "covered_from": None,
        "state_at_start": "old",
    }


def test_value_at_the_start_is_left_out_when_changes_are() -> None:
    """Between that value and the points there are changes the client has not seen."""
    states = [
        _state("old", _START),
        *(_state(index, _START + timedelta(seconds=index + 1)) for index in range(3)),
    ]
    assert "state_at_start" not in newest_points(states, _START, 2, reached_start=True)


def test_reading_back_less_far_than_the_window_can_be_continued() -> None:
    """With no changes found and more to read, the client continues from there."""
    result = newest_points([_state("old", _START)], _START, 5, reached_start=False)
    assert result == {
        "points": [],
        "older_changes_left_out": True,
        "covered_from": _START,
    }


def test_newest_points_of_nothing() -> None:
    """Nothing recorded is an empty result."""
    assert newest_points([], _START, 1, reached_start=True) == {
        "points": [],
        "older_changes_left_out": False,
        "covered_from": None,
    }


def test_numeric_means_only_numbers_and_times_without_a_value() -> None:
    """Only states that can go into a mean make an entity numeric."""
    at = _START
    assert is_numeric([_state(1, at), _state("unavailable", at)])
    assert not is_numeric([_state(1, at), _state("on", at)])
    assert not is_numeric([_state("unknown", at), _state("unavailable", at)])
    assert not is_numeric([_state("nan", at), _state("inf", at)])


def test_states_summary_covers_the_whole_window() -> None:
    """The summary reaches across the whole window, not only the newest part."""
    states = [
        _state(index, _START + timedelta(seconds=index * 5 + 1)) for index in range(200)
    ]
    result = _summary(states)
    assert result["source"] == "states"
    assert result["bucket_seconds"] == 10
    buckets = result["buckets"]
    assert len(buckets) == MAX_HISTORY_SUMMARY_POINTS
    assert buckets[0]["start"] == _START
    assert buckets[0]["changes"] == 2
    assert (buckets[0]["min"], buckets[0]["max"], buckets[0]["last"]) == (0, 1, 1)
    assert buckets[-1]["last"] == 199
    assert sum(bucket["changes"] for bucket in buckets) == 200


def test_mean_is_weighted_by_time() -> None:
    """A value held for long counts for more than a burst of other values."""
    states = [
        *_alternating(100, 100.5),
        # Holds until the end of the first bucket, after the burst of the first second.
        _state(0, _START + timedelta(seconds=2)),
    ]
    first = _summary(states)["buckets"][0]
    assert first["max"] == 100.5
    assert first["min"] == 0
    assert first["mean"] < 25


def test_value_is_carried_through_quiet_buckets() -> None:
    """A bucket without a change still reports the value that held throughout."""
    buckets = _summary(_alternating(1, 2))["buckets"]
    quiet = buckets[50]
    assert quiet["changes"] == 0
    assert quiet["min"] == quiet["max"] == quiet["mean"] == quiet["last"]


def test_state_from_before_the_window_is_not_a_change() -> None:
    """The value at the start of the window counts for its time, but changed earlier."""
    states = [_state(5, _START - timedelta(hours=1)), *_alternating(1, 2)]
    result = _summary(states)
    assert sum(bucket["changes"] for bucket in result["buckets"]) == _MANY


def test_states_replaced_before_the_window_have_no_time_in_it() -> None:
    """A state replaced before the window starts has no part in any bucket."""
    states = [
        _state(99, _START - timedelta(hours=2)),
        _state(5, _START - timedelta(hours=1)),
        *_alternating(1, 2),
    ]
    first = _summary(states)["buckets"][0]
    assert first["max"] == 5
    assert first["changes"] == _MANY


def test_changes_at_the_same_moment_have_a_mean() -> None:
    """Values held for no time at all still give a bucket a mean."""
    states = [
        *_alternating(1, 2),
        _state(7, _END - timedelta(seconds=5)),
        _state(8, _END),
        _state(9, _END),
    ]
    assert _summary(states)["buckets"][-1]["max"] == 9


def test_time_without_a_value_is_reported() -> None:
    """Being unavailable is neither hidden nor counted as a number."""
    states = [
        *_alternating(1, 2),
        _state("unavailable", _START + timedelta(seconds=20)),
    ]
    buckets = _summary(states)["buckets"]
    assert buckets[50]["no_value_seconds"] == 10
    assert "mean" not in buckets[50]
    # The value before ended on the edge of this bucket, so it has no part in it.
    assert buckets[2]["changes"] == 1
    assert "last" not in buckets[2]
    assert "no_value_seconds" not in buckets[1]


def test_window_reaching_into_the_future_ends_now() -> None:
    """Buckets only cover time that has passed."""
    now = dt_util.utcnow()
    start = now - timedelta(seconds=1000)
    states = [_state(index, start + timedelta(seconds=index)) for index in range(_MANY)]
    result = states_summary(states, start, now + timedelta(days=1))
    assert result["bucket_seconds"] == 10


def _row(index: int, **values: float | None) -> dict[str, float | None]:
    start = _START.timestamp() + index * 300
    return {"start": start, "end": start + 300, **values}


def test_statistics_of_a_measurement() -> None:
    """Rows go into buckets whole: the lowest low, highest high and average of means."""
    rows = [_row(index, min=index, mean=index + 1, max=index + 2) for index in range(4)]
    result = statistics_summary(rows, 300)
    assert result["source"] == "statistics"
    assert result["bucket_seconds"] == 300
    assert result["buckets"][0] == {
        "start": dt_util.utc_from_timestamp(rows[0]["start"]),
        "min": 0,
        "mean": 1,
        "max": 2,
    }
    assert result["covered_from"] == dt_util.utc_from_timestamp(rows[0]["start"])
    assert result["covered_until"] == dt_util.utc_from_timestamp(rows[3]["end"])


def test_statistics_merge_into_the_number_of_buckets() -> None:
    """More rows than buckets merge, and no row is lost at the end."""
    rows = [
        _row(index, min=index, mean=index, max=index) for index in range(_MANY * 2 + 1)
    ]
    result = statistics_summary(rows, 300)
    buckets = result["buckets"]
    assert len(buckets) <= MAX_HISTORY_SUMMARY_POINTS
    assert result["bucket_seconds"] == 900
    assert (buckets[0]["min"], buckets[0]["mean"], buckets[0]["max"]) == (0, 1, 2)
    assert buckets[-1]["max"] == _MANY * 2


def test_statistics_of_a_meter() -> None:
    """A meter has no mean, but a reading and how much it went up."""
    rows = [_row(index, state=100 + index, change=1) for index in range(2)]
    assert statistics_summary(rows, 300)["buckets"][1] == {
        "start": dt_util.utc_from_timestamp(rows[1]["start"]),
        "last": 101,
        "change": 1,
    }


def test_statistics_that_are_partly_empty() -> None:
    """A row without values does not end up in a mean."""
    rows = [_row(0, mean=4), _row(1, mean=None)]
    result = statistics_summary(rows, 3600)
    assert len(result["buckets"]) == 2
    assert result["buckets"][0]["mean"] == 4
    assert "mean" not in result["buckets"][1]
