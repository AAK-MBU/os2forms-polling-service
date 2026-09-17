"""Data-access helpers for [polling].[SubmissionPollStatus].

All state mutations for a submission go through here so idempotency, retry counting, and
the dead-letter transition live in one place. State is keyed by (job_id, submission_uuid).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import distinct, func
from sqlmodel import Session, select

from app.models import SubmissionPollStatus, SubmissionStatus

_MAX_ERROR_LEN = 2000


@dataclass(frozen=True)
class SerialReconciliation:
    """The result of checking a job's stored serials for holes.

    A hole means the source never listed that submission to us at all. A submission that was
    listed but failed to deliver has a row (and a serial), so it is *not* a hole — it is
    already visible through the job's status counts.
    """

    first_serial: int | None
    last_serial: int | None
    seen: int
    # Rows carrying no serial: written before the column existed, or a non-numeric source
    # serial. They are a blind spot in the analysis, so they are reported rather than hidden.
    unknown_serial: int
    missing_count: int
    missing: list[int]
    truncated: bool


def utcnow() -> datetime:
    """Naive UTC, to match the DATETIME2 columns (stored without offset)."""
    return datetime.now(UTC).replace(tzinfo=None)


def walk_missing(serials: list[int], limit: int) -> tuple[int, list[int]]:
    """Find holes in an ascending list of serials. Returns (total missing, first `limit`).

    Walks consecutive pairs rather than materializing ``range(first, last)``: a job whose
    serials jump — a form that ran for years before this job was registered — would otherwise
    build an enormous list to describe a handful of real holes.
    """
    missing: list[int] = []
    total = 0
    for previous, current in zip(serials, serials[1:], strict=False):
        hole = current - previous - 1
        if hole <= 0:
            continue
        total += hole
        if len(missing) < limit:
            room = limit - len(missing)
            missing.extend(range(previous + 1, min(current, previous + 1 + room)))
    return total, missing


class StateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, job_id: int, uuid: str) -> SubmissionPollStatus | None:
        stmt = select(SubmissionPollStatus).where(
            SubmissionPollStatus.job_id == job_id,
            SubmissionPollStatus.submission_uuid == uuid,
        )
        return self.session.exec(stmt).first()

    def get_or_create(
        self, job_id: int, webform_id: str, uuid: str, serial: int | None = None
    ) -> SubmissionPollStatus:
        existing = self.get(job_id, uuid)
        if existing is not None:
            self.backfill_serial(existing, serial)
            return existing
        state = SubmissionPollStatus(
            job_id=job_id,
            os2formWebformId=webform_id,
            submission_uuid=uuid,
            submission_serial=serial,
            status=SubmissionStatus.new,
            attempts=0,
            first_seen_at=utcnow(),
        )
        self.session.add(state)
        self.session.flush()
        return state

    def backfill_serial(self, state: SubmissionPollStatus, serial: int | None) -> bool:
        """Fill in a serial on a row written before the column existed. Returns True if it wrote.

        Costs one UPDATE per historical row, once — after which the guard short-circuits on
        every later cycle. Without it, reconciliation could only ever look forward from the
        deployment of this column.
        """
        if serial is None or state.submission_serial is not None:
            return False
        state.submission_serial = serial
        self.session.add(state)
        return True

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

    # --- Serial reconciliation --------------------------------------------------

    def serial_gap_count(self, job_id: int) -> int:
        """Cheap gap count: (max - min + 1) - distinct serials. 0 when the sequence is dense.

        One aggregate, covered by IX_SubmissionPollStatus_job_serial — cheap enough to run
        once per job per poll cycle so a gap is noticed without anyone calling the API.
        """
        low, high, count = self.session.exec(
            select(
                func.min(SubmissionPollStatus.submission_serial),
                func.max(SubmissionPollStatus.submission_serial),
                func.count(distinct(SubmissionPollStatus.submission_serial)),  # type: ignore[arg-type]
            ).where(SubmissionPollStatus.job_id == job_id)
        ).one()
        if low is None or high is None:
            return 0
        return (high - low + 1) - count

    def serial_reconciliation(self, job_id: int, *, limit: int = 1000) -> SerialReconciliation:
        """Enumerate missing serials between the lowest and highest this job has on record.

        Anchored at the job's *own* lowest serial, not the webform's: a job registered after
        the form went live legitimately starts mid-sequence.
        """
        unknown = self.session.exec(
            select(func.count())
            .select_from(SubmissionPollStatus)
            .where(
                SubmissionPollStatus.job_id == job_id,
                SubmissionPollStatus.submission_serial.is_(None),  # type: ignore[union-attr]
            )
        ).one()
        rows = self.session.exec(
            select(SubmissionPollStatus.submission_serial)
            .where(
                SubmissionPollStatus.job_id == job_id,
                SubmissionPollStatus.submission_serial.is_not(None),  # type: ignore[union-attr]
            )
            .distinct()
            .order_by(SubmissionPollStatus.submission_serial)  # type: ignore[arg-type]
        ).all()
        # The NULL filter is in the query; re-stating it here keeps the list typed as ints.
        serials = [s for s in rows if s is not None]
        if not serials:
            return SerialReconciliation(None, None, 0, unknown, 0, [], False)

        missing_count, missing = walk_missing(serials, limit)
        return SerialReconciliation(
            first_serial=serials[0],
            last_serial=serials[-1],
            seen=len(serials),
            unknown_serial=unknown,
            missing_count=missing_count,
            missing=missing,
            truncated=missing_count > len(missing),
        )

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
