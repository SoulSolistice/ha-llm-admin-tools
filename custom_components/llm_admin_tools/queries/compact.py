"""Keep the items of long lists short."""

from typing import Any


def without_empty_fields(summary: dict[str, Any]) -> dict[str, Any]:
    """Return a list item without the fields that are not set.

    Most are unset for most items, and repeated across hundreds of them.
    """
    return {key: value for key, value in summary.items() if value is not None}
