"""Read the whole log of Home Assistant, including its INFO and DEBUG records."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import os
import re
from typing import Any

from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from ..errors import read_failed
from ..policy.exposure import ExposurePolicy
from ..policy.limits import (
    FULL_LOG_TIMEOUT,
    MAX_FULL_LOG_CHARACTERS,
    MAX_FULL_LOG_LINES,
)
from .logs import LOG_LEVELS

# The key the hassio integration stores its client under. With a Supervisor there is
# no log file, and this is how Home Assistant itself gets at the log.
_DATA_SUPERVISOR = "hassio"
_SUPERVISOR_LOG_COMMAND = "/core/logs"

_COLOR = re.compile(r"\x1b\[[0-9;]*m")
# A thread can have brackets in its name, such as Thread-2 (_work).
_RECORD_START = re.compile(
    r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)"
    r" ([A-Z]+) \(.{0,200}?\) \[([^\]]+)\] ?(.*)$"
)


@dataclass(frozen=True, kw_only=True)
class FullLogQuery:
    """Which records to return."""

    min_level: str
    logger: str | None
    search: str | None
    start: datetime | None
    end: datetime | None


@callback
def full_log_available(hass: HomeAssistant) -> bool:
    """Return if there is a log file, or a Supervisor that keeps the log instead."""
    return KEY_DATA_LOGGING in hass.data or _DATA_SUPERVISOR in hass.data


def _last_lines(text: str, cut_already: bool) -> tuple[list[str], bool]:
    """Return the last lines of a text, and if any before those were left out."""
    cut = cut_already or len(text) > MAX_FULL_LOG_CHARACTERS
    lines = text[-MAX_FULL_LOG_CHARACTERS:].splitlines()
    if cut:
        # Where the text was cut is most likely inside a line.
        lines = lines[1:]
    return lines[-MAX_FULL_LOG_LINES:], cut or len(lines) >= MAX_FULL_LOG_LINES


def _read_log_file(path: str) -> tuple[list[str], bool]:
    """Return the end of a log file, which can be far larger than what is read."""
    try:
        with open(path, "rb") as log_file:
            size = log_file.seek(0, os.SEEK_END)
            log_file.seek(max(0, size - MAX_FULL_LOG_CHARACTERS))
            data = log_file.read(MAX_FULL_LOG_CHARACTERS)
    except OSError as err:
        raise read_failed("full_log_unreadable") from err
    return _last_lines(
        data.decode("utf-8", errors="replace"), size > MAX_FULL_LOG_CHARACTERS
    )


async def _async_read_lines(hass: HomeAssistant) -> tuple[list[str], bool]:
    """Return the last lines of the log, and if any before those were left out."""
    if (path := hass.data.get(KEY_DATA_LOGGING)) is not None:
        return await hass.async_add_executor_job(_read_log_file, path)
    if (supervisor := hass.data.get(_DATA_SUPERVISOR)) is None:
        raise read_failed("full_log_unavailable")
    try:
        text: str = await supervisor.send_command(
            _SUPERVISOR_LOG_COMMAND,
            method="get",
            return_text=True,
            params={"lines": MAX_FULL_LOG_LINES},
            timeout=FULL_LOG_TIMEOUT,
        )
    # The hassio integration raises a RuntimeError, and undecodable text a ValueError.
    except (RuntimeError, ValueError) as err:
        raise read_failed("full_log_unreadable") from err
    return await hass.async_add_executor_job(_last_lines, text, False)


def _parse_time(text: str) -> datetime | None:
    """Return a time from the log, or None if it only looks like one."""
    try:
        return dt_util.parse_datetime(text)
    except ValueError:
        return None


def _parse_records(lines: list[str]) -> list[dict[str, Any]]:
    """Return records, oldest first, each with the lines that continue it.

    A log file holds no more than text, so a message that imitates the start of a
    record cannot be told from one.
    """
    records: list[dict[str, Any]] = []
    for raw_line in lines:
        line = _COLOR.sub("", raw_line)
        match = _RECORD_START.match(line)
        if match and (time := _parse_time(match[1])) is not None:
            records.append(
                {
                    "timestamp": dt_util.as_utc(time),
                    "level": match[2],
                    "logger": match[3],
                    "message": match[4],
                }
            )
        # Lines from before the first record belong to something that was not read.
        elif records:
            records[-1]["message"] += f"\n{line}"
    return records


def _matches(
    record: dict[str, Any],
    logger_allowed: Callable[[str], bool],
    query: FullLogQuery,
    scrub: Callable[[str], str],
) -> bool:
    """Return if a record may be shown and was asked for.

    The text and the logger are compared as a client would see them: comparing what is
    withheld would let a client recover it, one character at a time.
    """
    level = record["level"]
    # A level that is not one of the usual ones is not known to be below the minimum.
    return (
        (
            level not in LOG_LEVELS
            or LOG_LEVELS.index(level) >= LOG_LEVELS.index(query.min_level)
        )
        and logger_allowed(record["logger"])
        and (query.logger is None or query.logger in scrub(record["logger"]))
        and (query.start is None or record["timestamp"] >= query.start)
        and (query.end is None or record["timestamp"] <= query.end)
        and (
            query.search is None
            or query.search.casefold() in scrub(record["message"]).casefold()
        )
    )


def _select_records(
    lines: list[str],
    older_left_out: bool,
    logger_allowed: Callable[[str], bool],
    query: FullLogQuery,
    scrub: Callable[[str], str],
) -> dict[str, Any]:
    """Return the matching records, newest first, and how far back the log was read."""
    records = _parse_records(lines)
    return {
        "records": [
            record
            for record in reversed(records)
            if _matches(record, logger_allowed, query, scrub)
        ],
        "covers_from": records[0]["timestamp"] if records else None,
        "older_records_left_out": older_left_out,
    }


async def async_read_full_log(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    query: FullLogQuery,
    *,
    scrub: Callable[[str], str],
) -> dict[str, Any]:
    """Return the records of the log that match a query."""
    lines, older_left_out = await _async_read_lines(hass)
    return await hass.async_add_executor_job(
        partial(
            _select_records,
            lines,
            older_left_out,
            policy.logger_filter(),
            query,
            scrub,
        )
    )
