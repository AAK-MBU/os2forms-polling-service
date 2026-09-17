"""Pydantic request/response schemas for the management API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

_VALID_PAYLOAD_MODES = {"raw", "parsed", "map"}


def validate_payload_mapping(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate a payload_mapping object so bad configs are rejected at registration.

    Shape: {"mode": "raw"|"parsed"|"map", ...}. For "map", "fields" is a non-empty object
    of ``name -> path-string`` or ``name -> {"path": str, "default"?: any}``.
    """
    if value is None:
        return None
    mode = value.get("mode", "raw")
    if mode not in _VALID_PAYLOAD_MODES:
        raise ValueError(f"mode must be one of {sorted(_VALID_PAYLOAD_MODES)}")
    if mode == "map":
        fields = value.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("map mode requires a non-empty 'fields' object")
        for name, spec in fields.items():
            if isinstance(spec, str):
                if not spec:
                    raise ValueError(f"field {name!r}: path must be a non-empty string")
            elif isinstance(spec, dict):
                path = spec.get("path")
                if not isinstance(path, str) or not path:
                    raise ValueError(f"field {name!r}: 'path' must be a non-empty string")
            else:
                raise ValueError(
                    f"field {name!r}: must be a path string or a {{path, default?}} object"
                )
    return value


# --- Auth ----------------------------------------------------------------------


class TokenRequest(BaseModel):
    api_key: str = Field(..., description="A registered API key")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# --- Jobs ----------------------------------------------------------------------


class PollJobCreate(BaseModel):
    name: str = Field(..., max_length=255)
    source: str = Field(..., max_length=255, description="Source-adapter key, e.g. 'os2forms'")
    webformId: str = Field(..., max_length=255)
    destination_system: str = Field(
        ..., max_length=255, description="'adapter:target', e.g. 'automation_server:IntakeQueue'"
    )
    destination_config: dict[str, Any] = Field(
        default_factory=dict, description="Opaque per-job JSON for the destination adapter"
    )
    payload_mapping: dict[str, Any] | None = Field(
        default=None,
        description="Optional payload shaping: {mode: raw|parsed|map, ...}. NULL => raw.",
    )
    isActive: bool = True
    erase_after: int | None = None
    # Admin-only: create a job on behalf of another owner. Ignored for non-admin callers.
    owner: str | None = Field(default=None, max_length=255)

    @field_validator("payload_mapping")
    @classmethod
    def _check_payload_mapping(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_payload_mapping(v)


class PollJobUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default=None, max_length=255)
    webformId: str | None = Field(default=None, max_length=255)
    destination_system: str | None = Field(default=None, max_length=255)
    destination_config: dict[str, Any] | None = None
    payload_mapping: dict[str, Any] | None = None
    isActive: bool | None = None
    erase_after: int | None = None

    @field_validator("payload_mapping")
    @classmethod
    def _check_payload_mapping(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_payload_mapping(v)


class PollJobItem(BaseModel):
    id: int
    name: str
    owner: str
    source: str
    webformId: str
    destination_system: str
    destination_config: dict[str, Any]
    payload_mapping: dict[str, Any] | None = None
    isActive: bool
    erase_after: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PaginatedPollJobs(BaseModel):
    total: int
    limit: int
    offset: int
    next_offset: int | None = None
    prev_offset: int | None = None
    items: list[PollJobItem]


# --- Status / submissions ------------------------------------------------------


class JobStatus(BaseModel):
    job_id: int
    webformId: str
    isActive: bool
    counts: dict[str, int]
    last_delivered_at: datetime | None = None


class SubmissionStateItem(BaseModel):
    submission_uuid: str
    submission_serial: int | None = None
    status: str
    attempts: int
    last_error: str | None = None
    destination_reference: str | None = None
    first_seen_at: datetime | None = None
    delivered_at: datetime | None = None
    erase_at: datetime | None = None


class PaginatedSubmissions(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[SubmissionStateItem]


class SerialGaps(BaseModel):
    """Holes in a job's serial sequence — submissions the source never listed to us."""

    job_id: int
    webformId: str
    first_serial: int | None = Field(
        default=None, description="Lowest serial this job has on record"
    )
    last_serial: int | None = Field(
        default=None, description="Highest serial this job has on record"
    )
    seen: int = Field(description="Distinct serials on record between first and last")
    unknown_serial: int = Field(
        description="Rows with no serial (predating the column, or non-numeric at the source). "
        "A blind spot: these are not counted as present or missing."
    )
    missing_count: int = Field(description="Total holes between first_serial and last_serial")
    missing: list[int] = Field(description="The missing serials, ascending, up to `limit`")
    truncated: bool = Field(description="True when missing_count exceeds the returned list")
