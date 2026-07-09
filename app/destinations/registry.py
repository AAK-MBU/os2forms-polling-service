"""Destination-adapter registry.

The Metadata.destination_system value is ``adapter[:target]`` — the part before the first
colon selects the adapter; the remainder is adapter-specific (for Automation Server, the
work-queue name). Examples:
    'automation_server:IntakeQueue'  -> AutomationServerDestination(queue_name='IntakeQueue')
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings
from app.destinations.automation_server import AutomationServerDestination
from app.destinations.base import DestinationAdapter
from app.exceptions import DestinationConfigError

# Factory per adapter key: (target, settings) -> adapter.
_DESTINATIONS: dict[str, Callable[[str, Settings], DestinationAdapter]] = {
    AutomationServerDestination.adapter: lambda target, settings: AutomationServerDestination(
        queue_name=target, settings=settings
    ),
}


def parse_destination(destination_system: str) -> tuple[str, str]:
    """Split 'adapter:target' into (adapter_key, target)."""
    raw = (destination_system or "").strip()
    if not raw:
        raise DestinationConfigError("destination_system is empty")
    adapter, _, target = raw.partition(":")
    return adapter.strip().lower(), target.strip()


def build_destination(destination_system: str, settings: Settings) -> DestinationAdapter:
    """Instantiate the destination adapter for a job's ``destination_system`` value."""
    adapter_key, target = parse_destination(destination_system)
    factory = _DESTINATIONS.get(adapter_key)
    if factory is None:
        raise DestinationConfigError(f"unknown destination adapter: {adapter_key!r}")
    return factory(target, settings)


def is_known_destination(destination_system: str) -> bool:
    """True if ``destination_system`` parses and names a registered destination adapter."""
    try:
        adapter_key, _ = parse_destination(destination_system)
    except DestinationConfigError:
        return False
    return adapter_key in _DESTINATIONS
