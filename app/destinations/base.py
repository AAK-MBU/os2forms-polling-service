"""Destination-adapter interface.

``deliver`` must be idempotent with respect to ``reference``: called twice with the same
reference it must not create a duplicate. Adapters achieve this however their target
allows (Automation Server: a by-reference lookup before add). The poller also guards with
its own state table, so this is the second line of defence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class DestinationAdapter(ABC):
    @abstractmethod
    def deliver(self, payload: dict[str, object], reference: str) -> str:
        """Deliver one submission. Returns a destination-side reference/id for logging."""

    def close(self) -> None:  # noqa: B027 - optional override, no-op by default
        """Release any held resources (HTTP clients, etc.)."""
