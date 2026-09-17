"""Data-access helpers for [polling].[SubmissionPollStatus].

All state mutations for a submission go through here so idempotency, retry counting, and
the dead-letter transition live in one place. State is keyed by (job_id, submission_uuid).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import SubmissionPollStatus, SubmissionStatus

_MAX_ERROR_LEN = 2000


def utcnow() -> datetime:
    """Naive UTC, to match the DATETIME2 columns (stored without offset)."""
    return datetime.now(UTC).replace(tzinfo=None)


class StateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, job_id: int, uuid: str) -> SubmissionPollStatus | None:
        stmt = select(SubmissionPollStatus).where(
            SubmissionPollStatus.job_id == job_id,
            SubmissionPollStatus.submission_uuid == uuid,
        )
        return self.session.exec(stmt).first()

    def get_or_create(self, job_id: int, webform_id: str, uuid: str) -> SubmissionPollStatus:
        existing = self.get(job_id, uuid)
        if existing is not None:
            return existing
        state = SubmissionPollStatus(
            job_id=job_id,
            os2formWebformId=webform_id,
            submission_uuid=uuid,
            status=SubmissionStatus.new,
            attempts=0,
            first_seen_at=utcnow(),
        )
        self.session.add(state)
        self.session.flush()
        return state

    def mark_delivered(
        self, state: SubmissionPollStatus, destination_reference: str, erase_at: datetime | None
    ) -> None:
        state.status = SubmissionStatus.delivered
        state.destination_reference = destination_reference
        state.delivered_at = utcnow()
        state.erase_at = erase_at
        state.last_error = None
        self.session.add(state)

    def mark_failure(self, state: SubmissionPollStatus, error: str, max_attempts: int) -> None:
        """Record a *retryable* failure; dead-letter once the attempt budget is spent."""
        state.attempts += 1
        state.last_error = error[:_MAX_ERROR_LEN]
        state.status = (
            SubmissionStatus.dead_letter
            if state.attempts >= max_attempts
            else SubmissionStatus.failed
        )
        self.session.add(state)

    def mark_dead_letter(self, state: SubmissionPollStatus, error: str) -> None:
        """Park a submission immediately, bypassing the attempt budget.

        For failures that cannot fix themselves (submission deleted at the source, response
        that will not parse): spending MAX_ATTEMPTS poll cycles on them only delays the point
        at which someone sees the problem.
        """
        state.attempts += 1
        state.last_error = error[:_MAX_ERROR_LEN]
        state.status = SubmissionStatus.dead_letter
        self.session.add(state)

    def sweep_expired(self, now: datetime | None = None) -> int:
        """Delete state rows whose retention window has passed. Returns rows removed."""
        now = now or utcnow()
        stmt = select(SubmissionPollStatus).where(
            SubmissionPollStatus.erase_at.is_not(None),  # type: ignore[union-attr]
            SubmissionPollStatus.erase_at < now,
        )
        rows = self.session.exec(stmt).all()
        for row in rows:
            self.session.delete(row)
        return len(rows)

    # --- API read/ops -----------------------------------------------------------

    def status_counts(self, job_id: int) -> dict[str, int]:
        """Return {status: count} for a job (missing statuses default to 0)."""
        stmt = (
            select(SubmissionPollStatus.status, func.count())
            .where(SubmissionPollStatus.job_id == job_id)
            .group_by(SubmissionPollStatus.status)
        )
        counts = {s.value: 0 for s in SubmissionStatus}
        for status, count in self.session.exec(stmt).all():
            counts[status] = count
        return counts

    def last_delivered_at(self, job_id: int) -> datetime | None:
        stmt = select(func.max(SubmissionPollStatus.delivered_at)).where(
            SubmissionPollStatus.job_id == job_id
        )
        return self.session.exec(stmt).one()

    def list_submissions(
        self, job_id: int, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[int, list[SubmissionPollStatus]]:
        """Return (total, rows) of submission-state rows for a job, newest first."""
        base = select(SubmissionPollStatus).where(SubmissionPollStatus.job_id == job_id)
        if status:
            base = base.where(SubmissionPollStatus.status == status)
        total = self.session.exec(
            select(func.count()).select_from(base.subquery())
        ).one()
        rows = self.session.exec(
            base.order_by(SubmissionPollStatus.first_seen_at.desc())  # type: ignore[union-attr]
            .offset(offset)
            .limit(limit)
        ).all()
        return total, list(rows)

    def reset_for_retry(self, job_id: int, uuid: str) -> SubmissionPollStatus | None:
        """Reset a failed/dead_letter row to `new` so the next cycle retries it."""
        state = self.get(job_id, uuid)
        if state is None:
            return None
        state.status = SubmissionStatus.new
        state.attempts = 0
        state.last_error = None
        self.session.add(state)
        return state
