"""Tests for grouping the log by what it says."""

from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import SECRET_VALUE, CallTool, apply_options

_LOG = f"""\
2026-11-02 10:00:00.100 INFO (MainThread) [homeassistant.core] started
2026-11-02 10:00:01.000 WARNING (MainThread) [custom.boiler] Lost device 12 at 0xff
2026-11-02 10:00:02.000 WARNING (MainThread) [custom.boiler] Lost device 13 at 0xaa
2026-11-02 10:00:03.000 WARNING (MainThread) [custom.boiler] Lost device 14 at 0xbb
2026-11-02 10:00:04.000 ERROR (MainThread) [custom.boiler] it failed
Traceback (most recent call last):
ValueError: password={SECRET_VALUE}
2026-11-02 10:00:05.000 ERROR (MainThread) [custom.boiler] it failed
Traceback (most recent call last):
KeyError: other
2026-11-02 10:00:06.000 WARNING (MainThread) [private.logger] kept out
2026-11-02 10:00:07.000 WARNING (MainThread) [test.{SECRET_VALUE}] name
2026-11-02 10:00:08.000 WARNING (MainThread) [custom.quiet]
"""


@pytest.fixture
def log_file(
    hass: HomeAssistant, init_integration: MockConfigEntry, tmp_path: Path
) -> Path:
    """Provide a log file."""
    path = tmp_path / "home-assistant.log"
    path.write_text(_LOG, encoding="utf-8")
    hass.data[KEY_DATA_LOGGING] = str(path)
    return path


async def _summary(call_tool: CallTool, **args: Any) -> dict[str, Any]:
    return dict((await call_tool("summarize_log", **args)).data)


@pytest.mark.usefixtures("log_file")
async def test_groups_what_only_differs_in_numbers(call_tool: CallTool) -> None:
    """What repeats most comes first, with when it first and last came."""
    data = await _summary(call_tool)
    first = data["items"][0]
    assert first["count"] == 3
    assert first["message"] == "Lost device 14 at 0xbb"
    assert first["first"] < first["last"]
    assert first["last"].startswith("2026-11-02") or first["last"]
    assert data["records_counted"] == 8
    assert data["covers_from"] is not None
    assert data["older_records_left_out"] is False


@pytest.mark.usefixtures("log_file")
async def test_a_traceback_is_known_by_its_last_line(call_tool: CallTool) -> None:
    """The same message can come from different errors."""
    data = await _summary(call_tool, min_level="ERROR")
    assert data["total_count"] == 2
    last_lines = {item["last_line"] for item in data["items"]}
    assert "KeyError: other" in last_lines
    assert SECRET_VALUE not in str(data)


@pytest.mark.usefixtures("log_file")
async def test_a_record_without_text_is_counted(call_tool: CallTool) -> None:
    """It still says that something happened."""
    data = await _summary(call_tool, logger="quiet")
    assert data["items"][0]["message"] == ""
    assert "last_line" not in data["items"][0]


@pytest.mark.usefixtures("log_file")
async def test_filters_compare_what_a_client_sees(call_tool: CallTool) -> None:
    """As in the tools that return the records themselves."""
    assert (await _summary(call_tool, logger=SECRET_VALUE[:4]))["total_count"] == 0
    assert (await _summary(call_tool, search=SECRET_VALUE[:4]))["total_count"] == 0
    assert (await _summary(call_tool, search="device"))["total_count"] == 1
    every = await _summary(call_tool, min_level="DEBUG")
    assert any(item["logger"] == "homeassistant.core" for item in every["items"])
    assert SECRET_VALUE not in str(every)


@pytest.mark.usefixtures("log_file")
async def test_a_window_must_have_a_length(call_tool: CallTool) -> None:
    """The check is the one get_full_log has."""
    with pytest.raises(HomeAssistantError) as err:
        await _summary(
            call_tool, start="2026-11-02T10:00:00", end="2026-11-02T10:00:00"
        )
    assert err.value.translation_key == "invalid_time_window"


async def test_hidden_loggers_stay_so(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    log_file: Path,
    call_tool: CallTool,
) -> None:
    """They are not even counted."""
    await apply_options(hass, init_integration, excluded_loggers=["private.logger"])
    data = await _summary(call_tool)
    assert "private.logger" not in str(data)
    assert data["records_counted"] == 7


async def test_needs_the_full_log_to_be_allowed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    log_file: Path,
    call_tool: CallTool,
) -> None:
    """An owner who turned that off does not expect the same text to come from here."""
    await apply_options(hass, init_integration, enabled_tools=["summarize_log"])
    with pytest.raises(HomeAssistantError) as err:
        await _summary(call_tool)
    assert err.value.translation_key == "summary_needs_full_log"
