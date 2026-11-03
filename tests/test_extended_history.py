"""Tests for asking for far more history, on purpose and with a reason."""

import asyncio
from datetime import timedelta
import json
import logging
import threading
import time
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.llm_admin_tools.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.llm_admin_tools.tools.extended_history import (
    GetExtendedHistoryTool,
)
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .conftest import CallTool, apply_options

_TOOL = "get_extended_history"
_REASON = "The summary hides a short spike I need to see."
_CHANGES = "homeassistant.components.recorder.history.state_changes_during_period"
_STATISTICS = "homeassistant.components.recorder.statistics.statistics_during_period"


def _days_ago(days: int) -> str:
    return (dt_util.utcnow() - timedelta(days=days)).isoformat()


async def _record(hass: HomeAssistant, entity_id: str, values: list[Any]) -> None:
    for value in values:
        hass.states.async_set(entity_id, str(value))
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def _import_statistics(hass: HomeAssistant, entity_id: str, hours: int) -> None:
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    async_import_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class=None,
            unit_of_measurement=None,
        ),
        [
            StatisticData(
                start=hour - timedelta(hours=index + 1), min=1.0, mean=2.0, max=3.0
            )
            for index in range(hours)
        ],
    )
    await async_wait_recording_done(hass)


@pytest.mark.parametrize("reason", [None, "because", 5])
async def test_a_reason_is_required(
    recorder_mock: Any, call_tool: CallTool, reason: Any
) -> None:
    """Without a stated reason, nothing is read."""
    args = {"reason": reason} if reason is not None else {}
    with patch(_CHANGES) as changes, pytest.raises(probatio.Invalid):
        await call_tool(_TOOL, entity_id="sensor.a", start=_days_ago(1), **args)
    changes.assert_not_called()


async def test_changes_page_forward_through_a_long_window(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A window beyond the usual limit reads forward, and says where to continue."""
    await _record(hass, "sensor.a", [0, 1, 2, 3, 4])
    first = await call_tool(
        _TOOL, reason=_REASON, entity_id="sensor.a", start=_days_ago(30), limit=2
    )
    assert first.data["exists"] is True
    assert [point["state"] for point in first.data["points"]] == ["0", "1"]
    assert first.data["newer_changes_left_out"] is True

    second = await call_tool(
        _TOOL,
        reason=_REASON,
        entity_id="sensor.a",
        start=first.data["covered_until"],
        limit=10,
    )
    assert [point["state"] for point in second.data["points"]] == ["2", "3", "4"]
    assert second.data["state_at_start"] == "1"
    assert second.data["newer_changes_left_out"] is False
    assert second.data["covered_until"] is None


async def test_changes_page_forward_one_by_one(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Continuing never returns the same change, or a client would never finish."""
    await _record(hass, "sensor.a", [0])
    await _record(hass, "sensor.a", [1])
    await _record(hass, "sensor.a", [2])
    seen: list[str] = []
    start = _days_ago(1)
    for _ in range(3):
        page = await call_tool(
            _TOOL, reason=_REASON, entity_id="sensor.a", start=start, limit=1
        )
        seen += [point["state"] for point in page.data["points"]]
        if not page.data["newer_changes_left_out"]:
            break
        start = page.data["covered_until"]
    assert seen == ["0", "1", "2"]


async def test_more_than_the_usual_limit_can_be_returned(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Results can be larger than other tools may return, up to their own limit."""
    now = dt_util.utcnow()
    states = [
        State("sensor.a", "x" * 100, last_changed=now - timedelta(seconds=index + 1))
        for index in range(3000, 0, -1)
    ]
    args = {
        "reason": _REASON,
        "entity_id": "sensor.a",
        "start": _days_ago(1),
        "limit": 5000,
    }
    with patch(_CHANGES, return_value={"sensor.a": states}):
        result = await call_tool(_TOOL, **args)
        assert len(json.dumps(result.data)) > 200_000
        with (
            patch.object(GetExtendedHistoryTool, "max_result_characters", 200_000),
            pytest.raises(HomeAssistantError),
        ):
            await call_tool(_TOOL, **args)


async def test_statistics_reach_as_far_as_they_exist(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Every row comes back, with what really is in the statistics."""
    await _import_statistics(hass, "sensor.a", 3)
    result = await call_tool(
        _TOOL,
        reason=_REASON,
        entity_id="sensor.a",
        start=_days_ago(100),
        values="statistics",
    )
    assert result.data["source"] == "statistics"
    assert result.data["period"] == "hour"
    assert len(result.data["buckets"]) == 3
    bucket = result.data["buckets"][0]
    assert (bucket["min"], bucket["mean"], bucket["max"]) == (1, 2, 3)
    assert result.data["covered_from"] == bucket["start"]
    assert result.data["covered_until"]


async def test_statistics_of_a_shorter_period_are_a_shorter_window(
    recorder_mock: Any, call_tool: CallTool
) -> None:
    """A window that could hold more rows than allowed is refused, saying how long."""
    with (
        patch(_STATISTICS) as statistics,
        pytest.raises(ServiceValidationError) as error,
    ):
        await call_tool(
            _TOOL,
            reason=_REASON,
            entity_id="sensor.a",
            start=_days_ago(18),
            values="statistics",
            period="5minute",
        )
    statistics.assert_not_called()
    assert error.value.translation_placeholders == {"days": "17"}


async def test_no_statistics_is_an_empty_answer(
    recorder_mock: Any, call_tool: CallTool
) -> None:
    """A sensor without a state class has no rows, which is not an error."""
    result = await call_tool(
        _TOOL,
        reason=_REASON,
        entity_id="sensor.a",
        start=_days_ago(1),
        values="statistics",
    )
    assert result.data["buckets"] == []
    assert result.data["covered_from"] is None


@pytest.mark.parametrize(
    "extra", [{}, {"values": "statistics"}], ids=["changes", "statistics"]
)
async def test_hidden_entity_looks_like_one_that_does_not_exist(
    recorder_mock: Any,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    extra: dict[str, str],
) -> None:
    """Asking on purpose is no way around the settings."""
    await _record(hass, "sensor.hidden", [1, 2])
    await apply_options(hass, init_integration, excluded_entities=["sensor.hidden"])
    start = _days_ago(1)

    async def ask(entity_id: str) -> dict[str, Any]:
        result = await call_tool(
            _TOOL, reason=_REASON, entity_id=entity_id, start=start, **extra
        )
        answer = dict(result.data)
        del answer["entity_id"], answer["end"]
        return answer

    with patch(_CHANGES) as changes, patch(_STATISTICS) as statistics:
        hidden = await ask("sensor.hidden")
    changes.assert_not_called()
    statistics.assert_not_called()
    assert hidden["exists"] is False
    assert hidden == await ask("sensor.never_existed")


async def test_values_stay_withheld(
    recorder_mock: Any,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
) -> None:
    """Asking on purpose is no way around redaction."""
    await apply_options(hass, init_integration, extra_redacted_text=["topsecretvalue"])
    await _record(hass, "sensor.a", ["topsecretvalue"])
    result = await call_tool(
        _TOOL, reason=_REASON, entity_id="sensor.a", start=_days_ago(1)
    )
    assert "topsecretvalue" not in json.dumps(result.data)


async def test_reason_is_recorded_for_the_owner(
    recorder_mock: Any,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The owner can read why, on one line and without credentials."""
    with caplog.at_level(logging.INFO, "custom_components.llm_admin_tools"):
        await call_tool(
            _TOOL,
            reason="Need more\nfor password=hunter2 today",
            entity_id="sensor.a",
            start=_days_ago(1),
        )
    expected = "Need more for password=**REDACTED** today"
    calls = init_integration.runtime_data.audit.as_diagnostics()
    assert calls[-1]["tool"] == _TOOL
    assert calls[-1]["reason"] == expected
    assert f"reason: {expected}" in caplog.text
    assert "hunter2" not in caplog.text


async def test_other_tools_record_no_reason(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """Only a stated reason is kept."""
    await call_tool("list_areas")
    calls = (await async_get_config_entry_diagnostics(hass, init_integration))[
        "recent_calls"
    ]
    assert "reason" not in calls[-1]


async def test_only_one_runs_at_a_time(recorder_mock: Any, call_tool: CallTool) -> None:
    """However many a client sends, the database serves them one after another."""
    guard = threading.Lock()
    running = 0
    most = 0

    def changes(*_args: Any, **_kwargs: Any) -> dict[str, list[State]]:
        nonlocal running, most
        with guard:
            running += 1
            most = max(most, running)
        time.sleep(0.02)
        with guard:
            running -= 1
        return {}

    with patch(_CHANGES, side_effect=changes):
        await asyncio.gather(
            *(
                call_tool(
                    _TOOL, reason=_REASON, entity_id="sensor.a", start=_days_ago(1)
                )
                for _ in range(3)
            )
        )
    assert most == 1


async def test_a_client_giving_up_does_not_let_the_next_one_in(
    hass: HomeAssistant, recorder_mock: Any, call_tool: CallTool
) -> None:
    """What runs in the executor goes on, so the next one has to wait for it."""
    started = threading.Event()
    release = threading.Event()

    def changes(*_args: Any, **_kwargs: Any) -> dict[str, list[State]]:
        started.set()
        release.wait(5)
        return {}

    with patch(_CHANGES, side_effect=changes):
        first = asyncio.ensure_future(
            call_tool(_TOOL, reason=_REASON, entity_id="sensor.a", start=_days_ago(1))
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.sleep(0.05)
        assert not first.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first


@pytest.mark.parametrize(
    ("args", "key"),
    [
        (
            {"entity_id": "not an id", "start": "2026-01-01T00:00:00"},
            "invalid_entity_id",
        ),
        ({"entity_id": "sensor.a", "start": "soon"}, "invalid_time"),
        (
            {
                "entity_id": "sensor.a",
                "start": "2026-01-02T00:00:00",
                "end": "2026-01-01T00:00:00",
            },
            "invalid_time_window",
        ),
    ],
)
async def test_invalid_requests_are_refused(
    recorder_mock: Any, call_tool: CallTool, args: dict[str, str], key: str
) -> None:
    """A request that cannot be answered says why."""
    with pytest.raises(ServiceValidationError) as error:
        await call_tool(_TOOL, reason=_REASON, **args)
    assert error.value.translation_key == key


@pytest.mark.parametrize("values", ["changes", "statistics"])
async def test_without_a_recorder(call_tool: CallTool, values: str) -> None:
    """Without a recorder there is no history, which is said."""
    with pytest.raises(HomeAssistantError) as error:
        await call_tool(
            _TOOL,
            reason=_REASON,
            entity_id="sensor.a",
            start=_days_ago(1),
            values=values,
        )
    assert error.value.translation_key == "recorder_unavailable"
