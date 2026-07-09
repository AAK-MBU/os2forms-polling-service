"""Source-adapter registry, keyed by the Metadata.source value."""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings
from app.exceptions import DestinationConfigError
from app.sources.base import SourceAdapter
from app.sources.os2forms import OS2formsSource

# Factory per source key. Add new sources here.
_SOURCES: dict[str, Callable[[Settings], SourceAdapter]] = {
    OS2formsSource.name: lambda settings: OS2formsSource(settings),
}


def build_source(source: str, settings: Settings) -> SourceAdapter:
    """Instantiate the source adapter for a job's ``source`` value."""
    key = (source or "").strip().lower()
    factory = _SOURCES.get(key)
    if factory is None:
        raise DestinationConfigError(f"unknown source adapter: {source!r}")
    return factory(settings)


def is_known_source(source: str) -> bool:
    """True if ``source`` names a registered source adapter."""
    return (source or "").strip().lower() in _SOURCES
