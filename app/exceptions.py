"""Exception hierarchies for source and destination adapters.

The key distinction is *retryable* (5xx / transport — worth another poll cycle) vs
*terminal* (4xx / bad response — will not fix itself). The poller uses this to decide
whether to bump attempts toward the dead-letter threshold.
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base for all adapter errors."""


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
    """Retryable 5xx response or transport failure."""


# --- Destination (Automation Server, ...) --------------------------------------


class DestinationError(AdapterError):
    """Base for destination-adapter errors."""


class DestinationConfigError(DestinationError):
    """Job is misconfigured (e.g. unknown destination adapter or queue). Terminal."""


class DestinationAuthError(DestinationError):
    """401/403 from the destination. Terminal."""


class DestinationServerError(DestinationError):
    """Retryable 5xx response or transport failure from the destination."""


class DestinationResponseError(DestinationError):
    """Destination returned an unexpected response. Terminal."""


# Exceptions worth retrying on a later poll cycle (do NOT count hard against attempts
# in a way that dead-letters a merely-flaky dependency too fast — but we still bump).
RETRYABLE = (SourceServerError, DestinationServerError)
