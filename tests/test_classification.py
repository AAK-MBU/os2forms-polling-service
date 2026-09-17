"""Unit tests for failure classification (app/exceptions.py::classify).

This is the decision that separates "try again next cycle" from "park it now" from "leave
every submission alone, the job itself is broken", so it is worth pinning down precisely.
"""

from __future__ import annotations

import pytest

from app.exceptions import (
    AdapterError,
    DestinationAuthError,
    DestinationConfigError,
    DestinationResponseError,
    DestinationServerError,
    Outcome,
    SourceAuthError,
    SourceNotFoundError,
    SourceResponseError,
    SourceServerError,
    classify,
)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # Transient — another cycle may well succeed.
        (SourceServerError("500"), Outcome.retry),
        (DestinationServerError("503"), Outcome.retry),
        # Terminal for this submission only.
        (SourceNotFoundError("gone"), Outcome.dead_letter),
        (SourceResponseError("unparseable"), Outcome.dead_letter),
        (DestinationResponseError("bad add response"), Outcome.dead_letter),
        # Fatal for the job: identical for every submission it owns.
        (DestinationAuthError("401"), Outcome.abort_job),
        (DestinationConfigError("no such queue"), Outcome.abort_job),
        # Fatal for every job on the webform.
        (SourceAuthError("403"), Outcome.abort_group),
    ],
)
def test_classify(exc: Exception, expected: Outcome) -> None:
    assert classify(exc) is expected


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("boom"), ValueError("nope"), AdapterError("unclassified")],
)
def test_unknown_errors_default_to_retry(exc: Exception) -> None:
    """Retrying an unexpected error is the recoverable mistake; dead-lettering it is not."""
    assert classify(exc) is Outcome.retry


def test_outcome_values_are_the_poll_completed_counters() -> None:
    """The enum values double as counter keys in the poll.completed log line."""
    assert Outcome.retry.value == "failed"
    assert {o.value for o in Outcome} == {"failed", "dead_letter", "abort_job", "abort_group"}
