"""SQLModel table models — all in the poller's own ``[polling]`` schema.

- ``PollJob``: the job registry the poller owns and reads (replaces reading any consumer's
  table). One row = poll this form → deliver to this destination with this config.
- ``ApiKey``: registered API keys, exchanged for JWTs at ``POST /auth/token``.
- ``SubmissionPollStatus``: per-(job, submission) runtime state; idempotency/retry/retention.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

_SCHEMA = "polling"


class SubmissionStatus(StrEnum):
    new = "new"
    delivered = "delivered"
    failed = "failed"
    dead_letter = "dead_letter"


class PollJob(SQLModel, table=True):
    """A registered polling job. Owned by the polling service; addressed by ``id``."""

    __tablename__ = "PollJob"
    __table_args__ = (
        sa.UniqueConstraint(
            "owner", "webformId", "destination_system", name="UQ_PollJob_owner_form_dest"
        ),
        {"schema": _SCHEMA},
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    # The registering consumer (= JWT `sub`); drives ownership-based authorization.
    owner: str = Field(sa_column=sa.Column(sa.String(255), nullable=False, index=True))
    source: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    webformId: str = Field(sa_column=sa.Column(sa.String(255), nullable=False, index=True))
    destination_system: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    # Opaque JSON (string) — only the destination adapter interprets it (caseType/caseData/...).
    destination_config: str = Field(
        default="{}", sa_column=sa.Column(sa.Text, nullable=False)
    )
    # Optional JSON (string) shaping the delivered payload: {"mode": raw|parsed|map, ...}.
    # NULL => raw passthrough. Interpreted by app.payload.build_workitem_payload.
    payload_mapping: str | None = Field(
        default=None, sa_column=sa.Column(sa.Text, nullable=True)
    )
    isActive: bool = Field(default=True, sa_column=sa.Column(sa.Boolean, nullable=False))
    erase_after: int | None = Field(
        default=None, sa_column=sa.Column(sa.Integer, nullable=True)
    )
    created_at: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime, nullable=True)
    )
    updated_at: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime, nullable=True)
    )


class ApiKey(SQLModel, table=True):
    """A registered API key (stored hashed). Exchanged for a JWT at /auth/token."""

    __tablename__ = "ApiKey"
    __table_args__ = {"schema": _SCHEMA}

    id: int | None = Field(default=None, primary_key=True)
    owner: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    # SHA-256 hex of the raw key; the raw key is never stored.
    key_hash: str = Field(sa_column=sa.Column(sa.String(64), nullable=False, unique=True))
    # Comma-separated scopes, e.g. "polling:read,polling:write" or "admin".
    scopes: str = Field(default="", sa_column=sa.Column(sa.String(255), nullable=False))
    isActive: bool = Field(default=True, sa_column=sa.Column(sa.Boolean, nullable=False))
    created_at: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime, nullable=True)
    )


class SubmissionPollStatus(SQLModel, table=True):
    """One row per (job, submission). Owns idempotency, retries, retention.

    The unique (job_id, submission_uuid) constraint is the dedup key — the same submission
    delivered to two destinations (two jobs) yields two independently-tracked rows.
    ``submission_serial`` is not part of any key; it exists purely so gaps can be spotted.
    """

    __tablename__ = "SubmissionPollStatus"
    __table_args__ = (
        sa.UniqueConstraint(
            "job_id", "submission_uuid", name="UQ_SubmissionPollStatus_job_uuid"
        ),
        {"schema": _SCHEMA},
    )

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(
        sa_column=sa.Column(
            sa.Integer,
            sa.ForeignKey(f"{_SCHEMA}.PollJob.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    # Denormalized for reporting/queries (the job's webformId at delivery time).
    os2formWebformId: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    submission_uuid: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    # The source's per-webform consecutive serial, for backward reconciliation: a hole in the
    # sequence means a submission was never listed to us. NULL for rows predating the column
    # and for sources that supply no usable numeric serial.
    submission_serial: int | None = Field(
        default=None, sa_column=sa.Column(sa.BigInteger, nullable=True)
    )
    status: str = Field(
        default=SubmissionStatus.new,
        sa_column=sa.Column(sa.String(20), nullable=False, index=True),
    )
    attempts: int = Field(default=0, sa_column=sa.Column(sa.Integer, nullable=False))
    last_error: str | None = Field(
        default=None, sa_column=sa.Column(sa.String(2000), nullable=True)
    )
    destination_reference: str | None = Field(
        default=None, sa_column=sa.Column(sa.String(255), nullable=True)
    )
    first_seen_at: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime, nullable=True)
    )
    delivered_at: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime, nullable=True)
    )
    erase_at: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime, nullable=True, index=True)
    )
