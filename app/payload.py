"""Build the destination work-item payload from a submission.

A job's optional ``payload_mapping`` (a JSON string on ``PollJob``) chooses how the source
submission is shaped for the destination:

  - mode ``raw`` (default, or column NULL): the source response verbatim (``submission.raw``).
    This is the pre-existing behavior, so jobs with no mapping are unchanged.
  - mode ``parsed``: the adapter's structured view — ``{data, attachments, metadata}``.
  - mode ``map``: a custom object built by pulling values out of ``submission.raw`` by dotted
    path (e.g. ``"entity.serial.0.value"``; numeric segments index into lists). A path that
    resolves to nothing omits its key, unless the field declares a ``default``. Set
    ``includeAttachments: true`` to append the normalized attachment list.

Paths resolve against the raw submission, so the mapping is source-agnostic. The mapping is
validated when the job is registered (see app.api.schemas); the guards here are defensive.
"""

from __future__ import annotations

import json
from typing import Any

from app.exceptions import DestinationConfigError
from app.models import PollJob
from app.sources.base import Submission

# Sentinel distinguishing "path resolved to a real None" from "path was absent".
_MISSING = object()


def build_workitem_payload(job: PollJob, submission: Submission) -> dict[str, Any]:
    """Shape the submission for delivery per the job's payload_mapping (default: raw)."""
    config = _load_mapping(job)
    mode = config.get("mode", "raw")
    if mode == "raw":
        return dict(submission.raw)
    if mode == "parsed":
        return _parsed(submission)
    if mode == "map":
        return _apply_map(config, submission)
    raise DestinationConfigError(f"unknown payload_mapping mode: {mode!r}")


def _load_mapping(job: PollJob) -> dict[str, Any]:
    raw = job.payload_mapping
    if not raw:
        return {"mode": "raw"}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DestinationConfigError(f"payload_mapping is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DestinationConfigError("payload_mapping must be a JSON object")
    return parsed


def _parsed(submission: Submission) -> dict[str, Any]:
    return {
        "data": dict(submission.data),
        "attachments": [a.as_dict() for a in submission.attachments],
        "metadata": dict(submission.metadata),
    }


def _apply_map(config: dict[str, Any], submission: Submission) -> dict[str, Any]:
    fields = config.get("fields")
    if not isinstance(fields, dict):
        raise DestinationConfigError("payload_mapping.fields must be a JSON object")
    out: dict[str, Any] = {}
    for key, spec in fields.items():
        path, default = _field_spec(key, spec)
        value = _resolve_path(submission.raw, path)
        if value is not _MISSING:
            out[key] = value
        elif default is not _MISSING:
            out[key] = default
        # else: path missing and no default -> omit the key
    if config.get("includeAttachments"):
        out["attachments"] = [a.as_dict() for a in submission.attachments]
    return out


def _field_spec(key: str, spec: Any) -> tuple[str, Any]:
    """A field is a path string, or ``{"path": str, "default"?: any}``."""
    if isinstance(spec, str):
        return spec, _MISSING
    if isinstance(spec, dict):
        path = spec.get("path")
        if not isinstance(path, str) or not path:
            raise DestinationConfigError(f"field {key!r} needs a non-empty string 'path'")
        return path, spec.get("default", _MISSING)
    raise DestinationConfigError(
        f"field {key!r} must be a path string or a {{path, default?}} object"
    )


def _resolve_path(root: Any, path: str) -> Any:
    """Walk a dotted path from ``root``; numeric segments index lists.

    Returns ``_MISSING`` if any segment is absent (missing key, non-numeric index into a
    list, out-of-range index, or descending into a scalar).
    """
    current: Any = root
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list):
            if not segment.isdigit():
                return _MISSING
            idx = int(segment)
            if idx >= len(current):
                return _MISSING
            current = current[idx]
        else:
            return _MISSING
    return current
