"""The poll loop.

Each cycle:
  1. load active PollJobs from [polling].[PollJob];
  2. group them by (source, webformId) so each form is listed from the source only once;
  3. for each job in a group, deliver any submissions not yet delivered *for that job*.

Design notes:
  - Each submission×job is processed in its own DB transaction, so one bad submission or job
    never rolls back the others.
  - A submission already delivered/dead_letter for a job is skipped; a failed one is retried
    on later cycles until it succeeds or hits MAX_ATTEMPTS (then dead_letter).
  - Delivered/dead_letter rows are kept permanently: they are the dedup key AND the audit
    record. The source re-lists every submission every poll, so deleting a row would re-poll it.
  - The loop survives transient errors; it runs until ``stop()`` (called by the app lifespan).
"""

from __future__ import annotations

import threading
from collections import defaultdict

from app.config import Settings, get_settings
from app.db import bootstrap_state_table, session_scope
from app.destinations.base import DestinationAdapter
from app.destinations.registry import build_destination
from app.exceptions import AdapterError, DestinationConfigError
from app.jobs import PollJobRepository
from app.logging import get_logger
from app.models import PollJob, SubmissionStatus
from app.payload import build_workitem_payload
from app.sources.base import SourceAdapter
from app.sources.registry import build_source
from app.state import StateRepository

log = get_logger(__name__)

_TERMINAL_STATES = {SubmissionStatus.delivered, SubmissionStatus.dead_letter}


class PollingService:
    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._stop = threading.Event()

    # --- lifecycle -------------------------------------------------------------

    def run_forever(self) -> None:
        if self._settings.bootstrap_state_table:
            bootstrap_state_table(self._settings)
        interval = self._settings.poll_interval_seconds
        log.info("poller.started", interval_seconds=interval, app_env=self._settings.app_env)
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - loop must survive transient failures
                log.warning("poller.cycle_error", error=type(exc).__name__, detail=str(exc))
            self._stop.wait(timeout=interval)
        log.info("poller.stopped")

    def stop(self) -> None:
        self._stop.set()

    # --- one cycle -------------------------------------------------------------

    def poll_once(self) -> dict[str, int]:
        groups = self._load_active_groups()
        totals = {"jobs": 0, "new": 0, "delivered": 0, "failed": 0, "skipped": 0}
        for (source, webform_id), jobs in groups.items():
            totals["jobs"] += len(jobs)
            outcome = self._process_group(source, webform_id, jobs)
            for key, value in outcome.items():
                totals[key] = totals.get(key, 0) + value
        log.info("poll.completed", **totals)
        return totals

    def _load_active_groups(self) -> dict[tuple[str, str], list[PollJob]]:
        """Active jobs grouped by (source, webformId) so each form is listed once."""
        with session_scope() as session:
            jobs = PollJobRepository(session).load_active()
        groups: dict[tuple[str, str], list[PollJob]] = defaultdict(list)
        for job in jobs:
            groups[(job.source, job.webformId)].append(job)
        return groups

    # --- per (source, webform) group -------------------------------------------

    def _process_group(
        self, source_key: str, webform_id: str, jobs: list[PollJob]
    ) -> dict[str, int]:
        counts = {"new": 0, "delivered": 0, "failed": 0, "skipped": 0}
        try:
            source: SourceAdapter = build_source(source_key, self._settings)
        except DestinationConfigError as exc:
            log.warning("group.bad_source", source=source_key, detail=str(exc))
            return counts
        try:
            refs = source.list_submissions(webform_id)
        except AdapterError as exc:
            log.warning("group.list_failed", webform_id=webform_id, detail=str(exc))
            source.close()
            return counts

        try:
            for job in jobs:
                if self._stop.is_set():
                    break
                self._process_job(job, source, refs, counts)
        finally:
            source.close()
        return counts

    def _process_job(self, job, source, refs, counts: dict[str, int]) -> None:
        try:
            destination: DestinationAdapter = build_destination(
                job.destination_system, self._settings
            )
        except DestinationConfigError as exc:
            log.warning("job.misconfigured", job_id=job.id, detail=str(exc))
            return
        try:
            for ref in refs:
                if self._stop.is_set():
                    break
                outcome = self._process_submission(job, source, destination, ref.uuid)
                counts[outcome] = counts.get(outcome, 0) + 1
        finally:
            destination.close()

    def _process_submission(self, job, source, destination, uuid: str) -> str:
        """Process one submission for one job in its own transaction. Returns an outcome key."""
        with session_scope() as session:
            repo = StateRepository(session)
            state = repo.get(job.id, uuid)
            if state is not None and state.status in _TERMINAL_STATES:
                return "skipped"

            state = repo.get_or_create(job.id, job.webformId, uuid)
            is_new = state.status == SubmissionStatus.new and state.attempts == 0

            try:
                submission = source.get_submission(job.webformId, uuid)
                payload = build_workitem_payload(job, submission)
                dref = destination.deliver(payload, uuid)
                # Keep delivered rows permanently: they are the dedup key AND the audit record.
                # The source lists every submission on every poll, so deleting a delivered row
                # would make it look "new" again and re-poll forever.
                repo.mark_delivered(state, dref, erase_at=None)
                log.info("submission.delivered", job_id=job.id, webform_id=job.webformId, uuid=uuid)
                return "delivered"
            except Exception as exc:  # noqa: BLE001 - record failure, keep looping
                repo.mark_failure(state, str(exc), self._settings.max_attempts)
                log.warning(
                    "submission.failed",
                    job_id=job.id,
                    uuid=uuid,
                    attempts=state.attempts,
                    status=state.status,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                return "new" if is_new else "failed"
