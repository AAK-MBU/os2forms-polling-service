"""Exception hierarchies for source and destination adapters.

The key distinction is *retryable* (5xx / transport / throttling — worth another poll cycle) vs
*terminal* (4xx / bad response — will not fix itself). The poller uses this to decide
whether to bump attempts toward the dead-letter threshold.
"""

from __future__ import annotations

from enum import StrEnum

from tenacity import RetryCallState, wait_exponential

# Upper bound on an honoured Retry-After, so a large (or hostile) hint cannot stall the poll
# loop: the submission is simply retried on a later cycle instead.
_MAX_RETRY_AFTER_SECONDS = 30.0


class AdapterError(Exception):
    """Base for all adapter errors.

    ``retry_after`` carries a server-supplied wait hint (the ``Retry-After`` header on a 429
    or 503). Only the retryable subclasses ever set it; ``_retry_wait`` honours it.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# --- Source (OS2forms) ---------------------------------------------------------


class SourceError(AdapterError):
    """Base for source-adapter errors."""


class SourceAuthError(SourceError):
    """401/403 — API key missing, invalid, or insufficient. Terminal."""


class SourceNotFoundError(SourceError):
    """404 — webform or submission does not exist. Terminal."""


class SourceResponseError(SourceError):
    """Response could not be parsed into the expected shape. Terminal."""


class SourceServerError(SourceError):
    """Retryable: 5xx, transport failure, or throttling (408/429)."""


# --- Destination (Automation Server, ...) --------------------------------------


class DestinationError(AdapterError):
    """Base for destination-adapter errors."""


class DestinationConfigError(DestinationError):
    """Job is misconfigured (e.g. unknown destination adapter or queue). Terminal."""


class DestinationAuthError(DestinationError):
    """401/403 from the destination. Terminal."""


class DestinationServerError(DestinationError):
    """Retryable: 5xx, transport failure, or throttling (408/429) from the destination."""


class DestinationResponseError(DestinationError):
    """Destination returned an unexpected response. Terminal."""


# --- Classification: what the poller should do with a failure -------------------


class Outcome(StrEnum):
    """What a failure means for the poll loop. Values double as ``poll.completed`` counters."""

    # Transient: bump attempts and try again on a later cycle.
    retry = "failed"
    # This submission will never succeed: park it now instead of burning MAX_ATTEMPTS cycles.
    dead_letter = "dead_letter"
    # The job is misconfigured or its destination rejects us — nothing to do with this
    # submission. Abandon the job for this cycle without touching any attempt counter.
    abort_job = "abort_job"
    # The source itself rejects us, which breaks every job on this webform.
    abort_group = "abort_group"


# Exceptions worth retrying on a later poll cycle (do NOT count hard against attempts
# in a way that dead-letters a merely-flaky dependency too fast — but we still bump).
RETRYABLE = (SourceServerError, DestinationServerError)

# Terminal for one submission: this submission is gone or unparseable, but the job is fine.
TERMINAL_FOR_SUBMISSION = (SourceNotFoundError, SourceResponseError, DestinationResponseError)

# Fatal for the whole job: a bad destination credential, an unknown queue, or an invalid
# payload_mapping fails identically for every submission. Dead-lettering each one would park
# the entire backlog on a single misconfiguration and force a manual retry per submission.
FATAL_FOR_JOB = (DestinationAuthError, DestinationConfigError)

# Fatal for every job on the webform: the source credential is bad or has been revoked.
FATAL_FOR_GROUP = (SourceAuthError,)


def classify(exc: BaseException) -> Outcome:
    """Map a failure to the poller's reaction.

    Anything unrecognized is treated as retryable on purpose: an unexpected exception is more
    likely a transient bug or dependency hiccup than proof that a submission is undeliverable,
    and retrying is the recoverable mistake of the two.
    """
    if isinstance(exc, FATAL_FOR_GROUP):
        return Outcome.abort_group
    if isinstance(exc, FATAL_FOR_JOB):
        return Outcome.abort_job
    if isinstance(exc, TERMINAL_FOR_SUBMISSION):
        return Outcome.dead_letter
    return Outcome.retry


# --- Retry policy shared by the adapters ---------------------------------------

_BACKOFF = wait_exponential(multiplier=0.5, max=8)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header expressed in delta-seconds.

    The HTTP-date form is valid but not produced by either dependency, so it is ignored
    rather than half-parsed — falling back to exponential backoff is safe either way.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def retry_wait(retry_state: RetryCallState) -> float:
    """Tenacity wait: honour a server ``Retry-After`` hint, else exponential backoff."""
    outcome = retry_state.outcome
    if outcome is not None and outcome.failed:
        hint = getattr(outcome.exception(), "retry_after", None)
        if hint is not None:
            return min(float(hint), _MAX_RETRY_AFTER_SECONDS)
    return _BACKOFF(retry_state)
