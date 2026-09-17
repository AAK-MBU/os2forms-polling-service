"""Unit tests for the adapter retry policy (app/exceptions.py).

These are deliberately pure-function tests: the poller's own paths need a SQL Server (the
models are bound to the [polling] schema), so the policy that decides *whether* to retry is
where unit tests actually pay off.
"""

from __future__ import annotations

import pytest

from app.exceptions import (
    DestinationServerError,
    SourceServerError,
    parse_retry_after,
    retry_wait,
)


class _Outcome:
    def __init__(self, exc: BaseException | None) -> None:
        self._exc = exc
        self.failed = exc is not None

    def exception(self) -> BaseException | None:
        return self._exc


class _State:
    """Minimal stand-in for tenacity's RetryCallState."""

    def __init__(self, exc: BaseException | None = None, attempt: int = 1) -> None:
        self.outcome = _Outcome(exc) if exc is not None else None
        self.attempt_number = attempt


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("12", 12.0),
        ("2.5", 2.5),
        ("0", 0.0),
        (None, None),
        ("", None),
        ("-5", None),
        # The HTTP-date form is valid but unused by both dependencies: ignored, not half-parsed.
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_retry_wait_honours_server_hint() -> None:
    exc = SourceServerError("throttled 429", retry_after=7)
    assert retry_wait(_State(exc)) == 7.0


def test_retry_wait_caps_a_long_hint() -> None:
    """A large hint must not stall the poll loop; the submission retries next cycle instead."""
    exc = DestinationServerError("throttled 429", retry_after=3600)
    assert retry_wait(_State(exc)) == 30.0


def test_retry_wait_falls_back_to_backoff_without_a_hint() -> None:
    plain = retry_wait(_State(SourceServerError("server error 500")))
    assert plain == pytest.approx(0.5)  # wait_exponential(multiplier=0.5) at attempt 1
    assert retry_wait(_State(SourceServerError("server error 500"), attempt=3)) > plain


def test_retry_wait_handles_a_missing_outcome() -> None:
    assert retry_wait(_State()) == pytest.approx(0.5)
