"""Data-access for [polling].[PollJob] — the poller's own job registry."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import PollJob
from app.state import utcnow


class DuplicateJobError(Exception):
    """A job with the same (owner, webformId, destination_system) already exists."""


class PollJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --- reads -----------------------------------------------------------------

    def get(self, job_id: int) -> PollJob | None:
        return self.session.get(PollJob, job_id)

    def list(
        self,
        *,
        owner: str | None = None,
        source: str | None = None,
        webform_id: str | None = None,
        destination_system: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[PollJob]]:
        """Return (total, rows). ``owner`` scopes the query for non-admin callers."""
        base = select(PollJob)
        if owner is not None:
            base = base.where(PollJob.owner == owner)
        if source is not None:
            base = base.where(PollJob.source == source)
        if webform_id is not None:
            base = base.where(PollJob.webformId == webform_id)
        if destination_system is not None:
            base = base.where(PollJob.destination_system == destination_system)
        if is_active is not None:
            base = base.where(PollJob.isActive == is_active)
        if search:
            like = f"%{search}%"
            base = base.where(
                PollJob.name.ilike(like)  # type: ignore[union-attr]
                | PollJob.webformId.ilike(like)  # type: ignore[union-attr]
                | PollJob.destination_system.ilike(like)  # type: ignore[union-attr]
            )
        total = self.session.exec(select(func.count()).select_from(base.subquery())).one()
        rows = self.session.exec(
            base.order_by(PollJob.id).offset(offset).limit(limit)  # type: ignore[arg-type]
        ).all()
        return total, list(rows)

    def load_active(self) -> list[PollJob]:
        """All active jobs, for the poll loop."""
        return list(
            self.session.exec(
                select(PollJob).where(PollJob.isActive == True)  # noqa: E712
            ).all()
        )

    # --- writes ----------------------------------------------------------------

    def create(self, data: dict[str, Any]) -> PollJob:
        now = utcnow()
        job = PollJob(**data, created_at=now, updated_at=now)
        self.session.add(job)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise DuplicateJobError(
                f"job already exists for ({job.owner}, {job.webformId}, "
                f"{job.destination_system})"
            ) from exc
        return job

    def update(self, job: PollJob, data: dict[str, Any]) -> PollJob:
        for key, value in data.items():
            setattr(job, key, value)
        job.updated_at = utcnow()
        self.session.add(job)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise DuplicateJobError("update would collide with an existing job") from exc
        return job

    def delete(self, job: PollJob) -> None:
        self.session.delete(job)
