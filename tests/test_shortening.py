"""Tests for cutting long values short."""

from custom_components.llm_admin_tools.shortening import shorten_values


def test_short_values_are_untouched() -> None:
    """Nothing is cut or reported below the limit."""
    values = {"a": "text", "b": [1, 2], "c": {"d": 1}, "e": 1, "f": None}
    assert shorten_values(values, 100) == (values, {})


def test_text_is_cut_at_the_limit() -> None:
    """Text keeps its start, and the original size is reported."""
    assert shorten_values({"a": "x" * 20}, 10) == ({"a": "x" * 10}, {"a": 22})


def test_list_keeps_leading_items() -> None:
    """A list keeps whole items, from the start."""
    shortened, sizes = shorten_values({"a": ["aaaa", "bbbb", "cccc"]}, 13)
    assert shortened == {"a": ["aaaa", "bbbb"]}
    assert sizes == {"a": 24}


def test_mapping_keeps_leading_items() -> None:
    """A mapping keeps whole items, from the start."""
    shortened, _ = shorten_values({"a": {"k1": "aaaa", "k2": "bbbb"}}, 20)
    assert shortened == {"a": {"k1": "aaaa"}}


def test_list_that_only_its_punctuation_makes_long() -> None:
    """Items that fit by themselves are all kept, even if the whole did not fit."""
    shortened, sizes = shorten_values({"a": [1, 1, 1]}, 8)
    assert shortened == {"a": [1, 1, 1]}
    assert sizes == {"a": 9}
