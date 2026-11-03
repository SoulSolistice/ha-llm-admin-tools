"""Tests for how history is read, through the tool and a real recorder."""

from datetime import timedelta
import json
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.llm_admin_tools.policy.limits import (
    MAX_HISTORY_SUMMARY_POINTS,
)
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .conftest import CallTool

_HISTORY = "custom_components.llm_admin_tools.queries.history"
_STATISTICS = "homeassistant.components.recorder.statistics.statistics_during_period"
_CHANGES = "homeassistant.components.recorder.history.state_changes_during_period"
_BUSY = MAX_HISTORY_SUMMARY_POINTS + 5


async def _record(hass: HomeAssistant, entity_id: str, values: list[Any]) -> None:
    """Record states as a real integration would."""
    for value in values:
        hass.states.async_set(entity_id, str(value))
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def test_quiet_entity_returns_its_changes_exactly(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A few changes need no summary, and neither statistics nor a long read."""
    await _record(hass, "sensor.a", [1, 2])
    with patch(_STATISTICS) as statistics:
        result = await call_tool("get_entity_history", entity_ids=["sensor.a"])
    statistics.assert_not_called()
    entity = result.data["entities"]["sensor.a"]
    assert [point["state"] for point in entity["points"]] == ["1", "2"]
    assert entity["older_changes_left_out"] is False


async def test_repeated_state_is_not_a_change(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """An update of the attributes alone does not repeat the state."""
    hass.states.async_set("sensor.a", "home", {"x": 1})
    hass.states.async_set("sensor.a", "home", {"x": 2})
    hass.states.async_set("sensor.a", "away", {"x": 2})
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    result = await call_tool("get_entity_history", entity_ids=["sensor.a"])
    points = result.data["entities"]["sensor.a"]["points"]
    assert [point["state"] for point in points] == ["home", "away"]


@pytest.mark.parametrize("args", [{}, {"limit": 5}], ids=["summary", "limit"])
async def test_value_held_when_the_window_began(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool, args: dict[str, int]
) -> None:
    """That value is not presented as a change, with a time it did not change at."""
    await _record(hass, "sensor.a", [1])
    start = dt_util.utcnow().isoformat()
    await _record(hass, "sensor.b", [1])

    quiet = await call_tool(
        "get_entity_history", entity_ids=["sensor.a"], start=start, **args
    )
    entity = quiet.data["entities"]["sensor.a"]
    assert entity["points"] == []
    assert entity["state_at_start"] == "1"
    assert entity["older_changes_left_out"] is False

    await _record(hass, "sensor.a", [2])
    changed = await call_tool(
        "get_entity_history", entity_ids=["sensor.a"], start=start, **args
    )
    entity = changed.data["entities"]["sensor.a"]
    assert [point["state"] for point in entity["points"]] == ["2"]
    assert entity["state_at_start"] == "1"


async def test_busy_number_without_statistics_uses_states(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A sensor without a state class has only its states to summarize."""
    await _record(hass, "sensor.a", list(range(_BUSY)))
    result = await call_tool("get_entity_history", entity_ids=["sensor.a"])
    entity = result.data["entities"]["sensor.a"]
    assert entity["source"] == "states"
    assert entity["changes_read"] == _BUSY
    assert entity["buckets"][-1]["last"] == _BUSY - 1
    assert "points" not in entity


async def test_reading_states_stops_at_a_limit(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A summary from states says how far it got, and it can be continued from there."""
    await _record(hass, "sensor.a", list(range(_BUSY)))
    with patch(f"{_HISTORY}.MAX_HISTORY_STATES_READ_PER_REQUEST", 102):
        result = await call_tool("get_entity_history", entity_ids=["sensor.a"])
    entity = result.data["entities"]["sensor.a"]
    assert entity["changes_read"] == 102
    assert entity["newer_changes_left_out"] is True
    assert entity["buckets"][-1]["last"] == 101
    assert entity["covered_until"]


async def test_many_entities_share_the_limit_on_reading_states(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """The limit is per request, so that more entities do not read more in total."""
    await _record(hass, "sensor.a", list(range(_BUSY)))
    with patch(f"{_HISTORY}.MAX_HISTORY_STATES_READ_PER_REQUEST", 204):
        result = await call_tool(
            "get_entity_history", entity_ids=["sensor.a", "sensor.b"]
        )
    assert result.data["entities"]["sensor.a"]["changes_read"] == 102


async def test_busy_text_returns_its_newest_changes(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """States that are not numbers cannot be summarized, so they stay changes."""
    await _record(hass, "sensor.a", [f"mode{index}" for index in range(_BUSY)])
    result = await call_tool("get_entity_history", entity_ids=["sensor.a"])
    entity = result.data["entities"]["sensor.a"]
    assert "buckets" not in entity
    assert len(entity["points"]) == MAX_HISTORY_SUMMARY_POINTS
    assert entity["points"][-1]["state"] == f"mode{_BUSY - 1}"
    assert entity["older_changes_left_out"] is True


async def test_limit_returns_the_newest_values(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A limit gets exact values, the newest, and says that older ones exist."""
    await _record(hass, "sensor.a", list(range(_BUSY)))
    result = await call_tool("get_entity_history", entity_ids=["sensor.a"], limit=3)
    entity = result.data["entities"]["sensor.a"]
    assert [point["state"] for point in entity["points"]] == [
        str(_BUSY - 3),
        str(_BUSY - 2),
        str(_BUSY - 1),
    ]
    assert entity["older_changes_left_out"] is True
    assert entity["covered_from"]


async def test_limit_can_page_back_through_a_window(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Older values come by asking for those before the oldest so far."""
    await _record(hass, "sensor.a", [1])
    await _record(hass, "sensor.a", [2])
    await _record(hass, "sensor.a", [3])
    newest = await call_tool("get_entity_history", entity_ids=["sensor.a"], limit=1)
    entity = newest.data["entities"]["sensor.a"]
    assert [point["state"] for point in entity["points"]] == ["3"]

    seen = ["3"]
    end = entity["covered_from"]
    while len(seen) < 3:
        older = await call_tool(
            "get_entity_history", entity_ids=["sensor.a"], limit=1, end=end
        )
        entity = older.data["entities"]["sensor.a"]
        seen.append(entity["points"][0]["state"])
        end = entity["covered_from"]
    assert seen == ["3", "2", "1"]


async def test_limit_on_a_quiet_entity_looks_back_all_the_way(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """With fewer values than asked for, every step is taken and nothing is left out."""
    await _record(hass, "sensor.a", [1, 2])
    result = await call_tool("get_entity_history", entity_ids=["sensor.a"], limit=10)
    entity = result.data["entities"]["sensor.a"]
    assert [point["state"] for point in entity["points"]] == ["1", "2"]
    assert entity["older_changes_left_out"] is False


async def test_limit_is_shared_by_many_entities(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """The largest request allowed still fits in a result."""
    now = dt_util.utcnow()
    entity_ids = [f"sensor.s{index}" for index in range(10)]

    def changes(_hass: HomeAssistant, _start: Any, _end: Any, entity_id: str, **kwargs):
        return {
            entity_id: [
                State(
                    entity_id, str(index), last_changed=now - timedelta(seconds=index)
                )
                for index in range(1000, 0, -1)
            ]
        }

    with patch(_CHANGES, side_effect=changes):
        result = await call_tool(
            "get_entity_history", entity_ids=entity_ids, limit=1000
        )
    for entity_id in entity_ids:
        assert len(result.data["entities"][entity_id]["points"]) == 200
    assert len(json.dumps(result.data)) < 200_000


async def test_lookback_stops_at_too_many_states_to_read(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """What the step before found has to do, and may leave out older values."""
    now = dt_util.utcnow()
    few = [State("sensor.a", "1", last_changed=now - timedelta(seconds=1))]
    many = [
        State("sensor.a", str(index), last_changed=now - timedelta(minutes=10))
        for index in range(5)
    ]
    with (
        patch(f"{_HISTORY}.MAX_HISTORY_STATES_READ_PER_LOOKBACK", 2),
        patch(_CHANGES, side_effect=[{"sensor.a": few}, {"sensor.a": many}]),
    ):
        result = await call_tool("get_entity_history", entity_ids=["sensor.a"], limit=2)
    entity = result.data["entities"]["sensor.a"]
    assert [point["state"] for point in entity["points"]] == ["1"]
    assert entity["older_changes_left_out"] is True


async def test_lookback_with_too_many_states_at_the_first_step(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Something comes back even from an entity too busy for any step."""
    now = dt_util.utcnow()
    many = [
        State("sensor.a", str(index), last_changed=now - timedelta(seconds=9 - index))
        for index in range(5)
    ]
    with (
        patch(f"{_HISTORY}.MAX_HISTORY_STATES_READ_PER_LOOKBACK", 2),
        patch(_CHANGES, return_value={"sensor.a": many}),
    ):
        result = await call_tool("get_entity_history", entity_ids=["sensor.a"], limit=2)
    entity = result.data["entities"]["sensor.a"]
    assert len(entity["points"]) == 2
    assert entity["older_changes_left_out"] is True


def _rows(count: int, **values: float) -> list[dict[str, float]]:
    now = dt_util.utcnow().timestamp()
    return [
        {"start": now - (count - index) * 300, "end": now - (count - index - 1) * 300}
        | values
        for index in range(count)
    ]


async def test_recent_window_uses_five_minute_statistics(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A day has enough statistics of five minutes, without reading all states."""
    await _record(hass, "sensor.a", list(range(_BUSY)))
    with patch(
        _STATISTICS, return_value={"sensor.a": _rows(288, min=1, mean=2, max=3)}
    ) as statistics:
        result = await call_tool("get_entity_history", entity_ids=["sensor.a"])
    assert statistics.call_args.args[4] == "5minute"
    entity = result.data["entities"]["sensor.a"]
    assert entity["source"] == "statistics"
    assert entity["bucket_seconds"] == 900
    assert entity["buckets"][0]["mean"] == 2


async def test_long_window_uses_hourly_statistics(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Hourly statistics are enough for a week, and far fewer rows."""
    await _record(hass, "sensor.a", list(range(_BUSY)))
    now = dt_util.utcnow()
    with patch(
        _STATISTICS, return_value={"sensor.a": _rows(168, mean=2)}
    ) as statistics:
        await call_tool(
            "get_entity_history",
            entity_ids=["sensor.a"],
            start=(now - timedelta(days=7)).isoformat(),
            end=now.isoformat(),
        )
    assert statistics.call_args.args[4] == "hour"


async def test_old_window_uses_hourly_statistics(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Five-minute statistics no longer exist for a time older than the states kept."""
    await _record(hass, "sensor.a", list(range(_BUSY)))
    now = dt_util.utcnow()
    with (
        patch.object(recorder_mock, "keep_days", 1),
        patch(_CHANGES) as changes,
        patch(_STATISTICS, return_value={"sensor.a": _rows(24, mean=2)}) as statistics,
    ):
        changes.return_value = {
            "sensor.a": [
                State(
                    "sensor.a",
                    str(index),
                    last_changed=now - timedelta(days=3) + timedelta(seconds=index),
                )
                for index in range(_BUSY)
            ]
        }
        await call_tool(
            "get_entity_history",
            entity_ids=["sensor.a"],
            start=(now - timedelta(days=4)).isoformat(),
            end=(now - timedelta(days=3)).isoformat(),
        )
    assert statistics.call_args.args[4] == "hour"


async def test_too_few_statistics_fall_back_to_states(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A short window has a handful of rows, which say less than its states."""
    await _record(hass, "sensor.a", list(range(_BUSY)))
    with patch(_STATISTICS, return_value={"sensor.a": _rows(3, mean=2)}):
        result = await call_tool("get_entity_history", entity_ids=["sensor.a"])
    assert result.data["entities"]["sensor.a"]["source"] == "states"


async def test_real_statistics_are_used(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """What the recorder really returns has the fields that the summary relies on."""
    await _record(hass, "sensor.a", list(range(_BUSY)))
    now = dt_util.utcnow()
    hour = now.replace(minute=0, second=0, microsecond=0)
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.ARITHMETIC,
        has_sum=False,
        name=None,
        source="recorder",
        statistic_id="sensor.a",
        unit_class=None,
        unit_of_measurement=None,
    )
    async_import_statistics(
        hass,
        metadata,
        [
            StatisticData(
                start=hour - timedelta(hours=index + 1),
                min=1.0,
                mean=2.0,
                max=3.0,
            )
            for index in range(30)
        ],
    )
    await async_wait_recording_done(hass)

    result = await call_tool(
        "get_entity_history",
        entity_ids=["sensor.a"],
        start=(now - timedelta(days=6)).isoformat(),
        end=now.isoformat(),
    )
    entity = result.data["entities"]["sensor.a"]
    assert entity["source"] == "statistics"
    assert entity["bucket_seconds"] == 3600
    assert len(entity["buckets"]) == 30
    bucket = entity["buckets"][0]
    assert (bucket["min"], bucket["mean"], bucket["max"]) == (1, 2, 3)
    assert entity["covered_until"] == (hour.isoformat())
