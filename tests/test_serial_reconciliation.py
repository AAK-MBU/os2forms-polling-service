"""Unit tests for serial-based backward reconciliation.

Serials are consecutive per webform, so a hole in the serials we hold rows for means the
source never listed that submission to us. These cover the two pure pieces: turning a source
serial into something storable, and finding the holes.
"""

from __future__ import annotations

import pytest

from app.sources.base import SubmissionRef
from app.state import walk_missing

# --- SubmissionRef.numeric_serial ----------------------------------------------


@pytest.mark.parametrize(
    ("serial", "expected"),
    [("41", 41), ("0", 0), (None, None), ("", None), ("abc", None), ("12.5", None)],
)
def test_numeric_serial(serial: str | None, expected: int | None) -> None:
    assert SubmissionRef(uuid="u", serial=serial).numeric_serial == expected


# --- walk_missing ---------------------------------------------------------------


def test_no_holes_in_a_dense_sequence() -> None:
    assert walk_missing([1, 2, 3, 4], limit=10) == (0, [])


def test_single_hole() -> None:
    assert walk_missing([1, 2, 4, 5], limit=10) == (1, [3])


def test_several_holes_of_different_widths() -> None:
    total, missing = walk_missing([10, 13, 14, 20], limit=100)
    assert total == 7
    assert missing == [11, 12, 15, 16, 17, 18, 19]


def test_limit_truncates_the_list_but_not_the_count() -> None:
    """The count must stay honest even when the enumeration is capped."""
    total, missing = walk_missing([1, 100], limit=5)
    assert total == 98
    assert missing == [2, 3, 4, 5, 6]


def test_limit_is_respected_across_multiple_holes() -> None:
    total, missing = walk_missing([1, 5, 20], limit=4)
    assert total == 17
    assert len(missing) == 4
    assert missing == [2, 3, 4, 6]


@pytest.mark.parametrize("serials", [[], [7]])
def test_too_few_serials_to_have_a_hole(serials: list[int]) -> None:
    assert walk_missing(serials, limit=10) == (0, [])


def test_a_large_jump_does_not_materialize_the_whole_range() -> None:
    """A job registered years into a form's life must not build a million-entry list."""
    total, missing = walk_missing([1, 1_000_000], limit=3)
    assert total == 999_998
    assert missing == [2, 3, 4]
