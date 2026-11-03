"""Cut long JSON values short, reporting what was cut."""

import json
from typing import Any


def _size(value: Any) -> int:
    """Return the number of characters a JSON value takes up."""
    return len(json.dumps(value, ensure_ascii=False))


def _shorten(value: Any, max_characters: int) -> Any:
    """Return the start of a text, or the leading items of a list or mapping."""
    if isinstance(value, str):
        return value[:max_characters]
    is_mapping = isinstance(value, dict)
    kept = []
    used = 0
    for item in value.items() if is_mapping else value:
        used += _size(item)
        if used > max_characters:
            break
        kept.append(item)
    return dict(kept) if is_mapping else kept


def shorten_values(
    values: dict[str, Any], max_characters: int
) -> tuple[dict[str, Any], dict[str, int]]:
    """Return values with long ones cut, and the original size of those.

    Only text, lists and mappings can be long.
    """
    shortened = {}
    original_sizes = {}
    for key, value in values.items():
        size = _size(value)
        if size > max_characters:
            shortened[key] = _shorten(value, max_characters)
            original_sizes[key] = size
        else:
            shortened[key] = value
    return shortened, original_sizes
