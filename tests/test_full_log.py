"""Tests for reading the whole log, from a log file or from the Supervisor."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.policy import limits
from custom_components.llm_admin_tools.queries import full_log
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import SECRET_VALUE, CallTool, apply_options

_REASON = "The system log does not explain this."
_LOG = f"""\
2026-11-02 10:00:00.100 DEBUG (MainThread) [custom_components.boiler] heating up
2026-11-02 10:00:01.200 INFO (Thread-2 (_work)) [homeassistant.core] started
2026-11-02 10:00:02.300 WARNING (MainThread) [private.logger] kept out
Traceback of what is kept out
2026-11-02 10:00:03.400 ERROR (MainThread) [custom_components.boiler] it failed
Traceback (most recent call last):
ValueError: password={SECRET_VALUE}
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


def _supervisor(hass: HomeAssistant, **command: Any) -> AsyncMock:
    """Provide the client of the hassio integration."""
    send_command = AsyncMock(**command)
    hass.data["hassio"] = MagicMock(send_command=send_command)
    return send_command


async def _read(call_tool: CallTool, **args: Any) -> dict[str, Any]:
    """Read the log and return what a client gets."""
    result = await call_tool("get_full_log", reason=_REASON, **args)
    return dict(result.data)


@pytest.mark.usefixtures("log_file")
async def test_reads_records_newest_first(call_tool: CallTool) -> None:
    """Each record comes with its level, logger and the lines that continue it."""
    data = await _read(call_tool)
    assert data["total_count"] == 4
    newest = data["items"][0]
    assert newest["level"] == "ERROR"
    assert newest["logger"] == "custom_components.boiler"
    assert newest["message"].startswith("it failed\nTraceback")
    assert data["items"][2]["logger"] == "homeassistant.core"
    assert data["items"][3]["message"] == "heating up"
    assert data["older_records_left_out"] is False
    assert data["covers_from"].startswith("2026-11-02")


@pytest.mark.usefixtures("log_file")
async def test_secrets_stay_out(call_tool: CallTool) -> None:
    """A log goes through the same redaction as everything else."""
    assert SECRET_VALUE not in json.dumps(await _read(call_tool))


@pytest.mark.usefixtures("log_file")
async def test_filters(call_tool: CallTool) -> None:
    """Level, logger, text and time each narrow the result."""
    assert (await _read(call_tool, min_level="ERROR"))["total_count"] == 1
    assert (await _read(call_tool, logger="homeassistant"))["total_count"] == 1
    assert (await _read(call_tool, search="HEATING"))["total_count"] == 1
    assert (await _read(call_tool, search="traceback"))["total_count"] == 2
    first = await _read(call_tool, end="2026-11-02T10:00:00.500")
    assert [item["message"] for item in first["items"]] == ["heating up"]
    last = await _read(call_tool, start="2026-11-02T10:00:03")
    assert [item["level"] for item in last["items"]] == ["ERROR"]


@pytest.mark.usefixtures("log_file")
async def test_paging(call_tool: CallTool) -> None:
    """A long result is read a page at a time."""
    page = await _read(call_tool, limit=3)
    assert len(page["items"]) == 3
    assert page["next_offset"] == 3
    assert len((await _read(call_tool, limit=3, offset=3))["items"]) == 1


@pytest.mark.usefixtures("log_file")
async def test_a_window_must_have_a_length(call_tool: CallTool) -> None:
    """An end before the start cannot be answered."""
    with pytest.raises(ServiceValidationError) as err:
        await _read(call_tool, start="2026-11-02T11:00:00", end="2026-11-02T10:00:00")
    assert err.value.translation_key == "invalid_time_window"


async def test_a_hidden_logger_stays_hidden(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    log_file: Path,
    call_tool: CallTool,
) -> None:
    """A hidden logger is left out with the lines that continue its records."""
    await apply_options(hass, init_integration, excluded_loggers=["private"])
    dumped = json.dumps(await _read(call_tool))
    assert "kept out" not in dumped
    assert "private.logger" not in dumped


async def test_search_finds_nothing_that_is_withheld(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    log_file: Path,
    call_tool: CallTool,
) -> None:
    """A search looks at what a client would see, so it cannot recover anything."""
    assert (await _read(call_tool, search=SECRET_VALUE))["total_count"] == 0
    await apply_options(hass, init_integration, extra_redacted_text=["heating up"])
    assert (await _read(call_tool, search="heating"))["total_count"] == 0


@pytest.mark.usefixtures("init_integration")
async def test_supervisor_log(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Without a log file, the Supervisor is asked, and colors are removed."""
    send_command = _supervisor(
        hass,
        return_value=(
            "\x1b[31m2026-11-02 10:00:00.100 ERROR (MainThread) "
            "[core.part] broke\x1b[0m\n"
        ),
    )
    data = await _read(call_tool)
    assert data["items"][0]["level"] == "ERROR"
    assert data["items"][0]["message"] == "broke"
    assert send_command.await_args.args == ("/core/logs",)
    assert send_command.await_args.kwargs["return_text"] is True
    assert send_command.await_args.kwargs["params"] == {
        "lines": limits.MAX_FULL_LOG_LINES
    }


@pytest.mark.usefixtures("init_integration")
async def test_supervisor_log_ignores_what_came_before_a_record(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Lines before the first record belong to something that was not read."""
    _supervisor(
        hass,
        return_value=(
            "[11:27:24] INFO: starting\n"
            "2026-99-99 10:00:00.100 ERROR (MainThread) [core.part] no such date\n"
            "2026-11-02 10:00:00.100 CUSTOM (MainThread) [core.part] broke\n"
            "[11:27:25] INFO: continuing\n"
        ),
    )
    data = await _read(call_tool, min_level="CRITICAL")
    # A level that is not known is not known to be below the minimum either.
    assert [item["level"] for item in data["items"]] == ["CUSTOM"]
    assert data["items"][0]["message"] == "broke\n[11:27:25] INFO: continuing"


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    "error", [RuntimeError("no"), UnicodeDecodeError("utf-8", b"x", 0, 1, "no")]
)
async def test_supervisor_failure(
    hass: HomeAssistant, call_tool: CallTool, error: Exception
) -> None:
    """A Supervisor that does not answer is a clear error."""
    _supervisor(hass, side_effect=error)
    with pytest.raises(HomeAssistantError) as err:
        await _read(call_tool)
    assert err.value.translation_key == "full_log_unreadable"


async def test_empty_log(hass: HomeAssistant, call_tool: CallTool) -> None:
    """A log without records has no start."""
    _supervisor(hass, return_value="")
    data = await _read(call_tool)
    assert data["total_count"] == 0
    assert data["covers_from"] is None


@pytest.mark.usefixtures("init_integration")
async def test_no_log_at_all(call_tool: CallTool) -> None:
    """Without a log file or a Supervisor, the tool says so."""
    with pytest.raises(HomeAssistantError) as err:
        await _read(call_tool)
    assert err.value.translation_key == "full_log_unavailable"


async def test_unreadable_file(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    tmp_path: Path,
    call_tool: CallTool,
) -> None:
    """A log file that cannot be opened is a clear error."""
    hass.data[KEY_DATA_LOGGING] = str(tmp_path / "missing.log")
    with pytest.raises(HomeAssistantError) as err:
        await _read(call_tool)
    assert err.value.translation_key == "full_log_unreadable"


async def test_a_large_file_is_read_from_its_end(
    log_file: Path, call_tool: CallTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the end is read, starting at a whole line, and the client is told."""
    monkeypatch.setattr(full_log, "MAX_FULL_LOG_CHARACTERS", 100)
    log_file.write_text(
        "".join(
            f"2026-11-02 10:00:0{i}.000 INFO (MainThread) [core.part] number {i}\n"
            for i in range(9)
        ),
        encoding="utf-8",
    )
    data = await _read(call_tool)
    assert data["older_records_left_out"] is True
    assert [item["message"] for item in data["items"]] == ["number 8"]


async def test_many_lines_are_cut_to_the_last(
    log_file: Path, call_tool: CallTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log of more lines than allowed is cut, and the client is told."""
    monkeypatch.setattr(full_log, "MAX_FULL_LOG_LINES", 2)
    data = await _read(call_tool)
    assert data["older_records_left_out"] is True
    # The last two lines continue a record whose start was not read.
    assert data["total_count"] == 0


@pytest.mark.usefixtures("init_integration")
async def test_a_long_record_is_cut_in_the_middle(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Both ends of a traceback say something, and the cut is reported."""
    long_message = f"start{'x' * limits.MAX_LOG_RECORD_CHARACTERS}end"
    _supervisor(
        hass,
        return_value=(
            f"2026-11-02 10:00:00.100 ERROR (MainThread) [core.part] {long_message}\n"
        ),
    )
    item = (await _read(call_tool))["items"][0]
    assert item["message"].startswith("start")
    assert item["message"].endswith("end")
    assert item["characters_left_out"] == len(long_message) - (
        limits.MAX_LOG_RECORD_CHARACTERS
    )


async def test_needs_a_reason(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Without a reason nothing is read."""
    send_command = _supervisor(hass, return_value="")
    with pytest.raises(Exception, match="reason"):
        await call_tool("get_full_log")
    send_command.assert_not_awaited()


async def test_offered_by_the_report(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    log_file: Path,
    call_tool: CallTool,
) -> None:
    """The report says it can be asked for, until the owner turns it off."""
    report = (await call_tool("get_capabilities")).data
    assert report["data_sources"]["full_log"] is True
    assert "get_full_log" in [offer["tool"] for offer in report["on_request"]]

    await apply_options(hass, init_integration, enabled_tools=["get_capabilities"])
    report = (await call_tool("get_capabilities")).data
    assert report["on_request"] == []


async def test_a_hidden_automation_or_script_hides_the_logger_named_after_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    log_file: Path,
    call_tool: CallTool,
) -> None:
    """Its name is in the logger and the message, also when it no longer exists."""
    with log_file.open("a", encoding="utf-8") as log:
        for name in ("automation.quiet", "script.quiet", "script.quiet_gone"):
            log.write(
                "2026-11-02 10:00:04.500 ERROR (MainThread) "
                f"[homeassistant.components.{name}] {name}: it failed\n"
            )
        log.write(
            "2026-11-02 10:00:04.600 ERROR (MainThread) "
            "[homeassistant.components.automation.open] open: it failed\n"
        )
    hass.states.async_set("automation.quiet", "on")
    hass.states.async_set("script.quiet", "off")
    hass.states.async_set("automation.open", "on")
    await apply_options(
        hass,
        init_integration,
        excluded_entities=["automation.quiet", "script.quiet", "script.quiet_gone"],
    )
    for answer in (
        await _read(call_tool),
        (await call_tool("summarize_log")).data,
    ):
        dumped = json.dumps(answer)
        assert "quiet" not in dumped
        assert "automation.open" in dumped
