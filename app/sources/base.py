"""Source-adapter interface.

A source adapter knows how to (1) list submission identifiers for a job and (2) fetch a
single full submission. Keeping listing and fetching separate lets the poller fetch only
submissions it has not already delivered (see PollingService), which is why the OS2forms
list endpoint returning identifiers-only is a good fit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SubmissionRef:
    """A lightweight submission identifier from a list call.

    ``uuid`` is parsed from the submission URL; ``serial`` is the OS2forms sid/serial the
    list keys on; ``url`` is the absolute link to fetch the full submission.
    """

    uuid: str
    serial: str | None = None
    url: str | None = None


@dataclass
class Attachment:
    """A document attachment, passed through to the destination as a URL."""

    name: str
    url: str
    mime: str | None = None
    size: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "url": self.url, "mime": self.mime, "size": self.size}


@dataclass
class Submission:
    """A full submission.

    ``raw`` is the source's response for the submission, verbatim — delivered as-is when the
    destination payload is untransformed. ``data``/``attachments``/``metadata`` are the parsed
    views (kept for adapters that want a structured payload).
    """

    uuid: str
    raw: dict[str, object] = field(default_factory=dict)
    data: dict[str, object] = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


class SourceAdapter(ABC):
    """Interface every source adapter implements."""

    @abstractmethod
    def list_submissions(self, webform_id: str) -> list[SubmissionRef]:
        """Return submission identifiers for a webform (all states; poller dedups)."""

    @abstractmethod
    def get_submission(self, webform_id: str, uuid: str) -> Submission:
        """Fetch a single full submission by uuid."""

    def close(self) -> None:  # noqa: B027 - optional override, no-op by default
        """Release any held resources (HTTP clients, etc.)."""
