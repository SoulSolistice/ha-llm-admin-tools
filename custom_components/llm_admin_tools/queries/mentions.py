"""Find the IDs that a piece of configuration or text names."""

from collections.abc import Collection, Iterator, Mapping
import re
from typing import Any

from homeassistant.helpers.template import is_template_string

from ..serialization import to_json_safe

# Each word, with the word after a dot: sensor.one has to be found in
# states.sensor.one.state, and not in sensor.one_more.
_MENTION = re.compile(r"(?<!\w)(?=(\w+)(\.\w+)?)")


def texts_in(value: Any, *, except_text_under: Collection[str] = ()) -> Iterator[str]:
    """Return all text in a configuration, which entity IDs can be the keys of.

    The keys in except_text_under only leave out text directly under them: an older
    configuration has its triggers and actions in lists, under the same names. A
    template there stays, as it can read anything.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            if not (
                isinstance(item, str)
                and key in except_text_under
                and not is_template_string(item)
            ):
                yield from texts_in(item, except_text_under=except_text_under)
    elif isinstance(value, list):
        for item in value:
            yield from texts_in(item, except_text_under=except_text_under)


def mentions(config: Any) -> set[str]:
    """Return what a configuration could be naming, in lower case."""
    found: set[str] = set()
    for text in texts_in(to_json_safe(config)):
        for match in _MENTION.finditer(text.lower()):
            found.add(match[1])
            if match[2]:
                found.add(match[1] + match[2])
    return found
