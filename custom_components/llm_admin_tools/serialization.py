"""Conversion of Home Assistant values to JSON primitives."""

from collections.abc import Mapping
from datetime import date, time, timedelta
from enum import Enum
from typing import Any

from .policy.redaction import REDACTED

_MAX_DEPTH = 32


def to_json_safe(value: Any, depth: int = 0) -> Any:
    """Recursively convert a value; the MCP server encodes with plain json.dumps.

    State attributes and raw configuration hold datetimes, enums, sets and YAML
    node classes. Anything unknown becomes its string form. What is nested too deep is
    withheld, as in string form redaction could no longer tell what its keys are.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        return to_json_safe(value.value, depth + 1)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if depth >= _MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        return {str(key): to_json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json_safe(item, depth + 1) for item in value]
    return str(value)
