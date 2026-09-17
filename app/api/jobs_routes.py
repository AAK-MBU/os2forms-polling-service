"""PollJob CRUD + per-job observability endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.api.deps import get_session, require_read, require_write
from app.api.schemas import (
    JobStatus,
    PaginatedPollJobs,
    PaginatedSubmissions,
    PollJobCreate,
    PollJobItem,
    PollJobUpdate,
    SerialGaps,
    SubmissionStateItem,
)
from app.auth import Principal
from app.destinations.registry import is_known_destination
from app.jobs import DuplicateJobError, PollJob, PollJobRepository
from app.sources.registry import is_known_source
from app.state import StateRepository

router = APIRouter(prefix="/polling/jobs", tags=["jobs"])


# --- helpers -------------------------------------------------------------------


def _to_item(job: PollJob) -> PollJobItem:
    try:
        config = json.loads(job.destination_config) if job.destination_config else {}
    except json.JSONDecodeError:
        config = {}
    mapping: dict | None = None
    if job.payload_mapping:
        try:
            loaded = json.loads(job.payload_mapping)
            mapping = loaded if isinstance(loaded, dict) else None
        except json.JSONDecodeError:
            mapping = None
    return PollJobItem(
        id=job.id,
        name=job.name,
        owner=job.owner,
        source=job.source,
        webformId=job.webformId,
        destination_system=job.destination_system,
        destination_config=config if isinstance(config, dict) else {},
        payload_mapping=mapping,
        isActive=job.isActive,
        erase_after=job.erase_after,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _owned_job(session: Session, job_id: int, principal: Principal) -> PollJob:
    job = PollJobRepository(session).get(job_id)
    if job is None or not principal.can_manage(job.owner):
        # 404 (not 403) so callers can't probe for jobs they don't own.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job


def _validate_routing(source: str, destination_system: str) -> None:
    if not is_known_source(source):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown source adapter: {source!r}",
        )
    if not is_known_destination(destination_system):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown or malformed destination_system: {destination_system!r}",
        )


# --- CRUD ----------------------------------------------------------------------


@router.post("", response_model=PollJobItem, status_code=201, summary="Register a job")
async def create_job(
    body: PollJobCreate,
    principal: Principal = Depends(require_write),
    session: Session = Depends(get_session),
) -> PollJobItem:
    owner = principal.sub
    if body.owner and body.owner != principal.sub:
        if not principal.is_admin:
            raise HTTPException(status_code=403, detail="cannot create jobs for another owner")
        owner = body.owner
    _validate_routing(body.source, body.destination_system)
    data = {
        "name": body.name,
        "owner": owner,
        "source": body.source,
        "webformId": body.webformId,
        "destination_system": body.destination_system,
        "destination_config": json.dumps(body.destination_config),
        "payload_mapping": (
            json.dumps(body.payload_mapping) if body.payload_mapping is not None else None
        ),
        "isActive": body.isActive,
        "erase_after": body.erase_after,
    }
    try:
        job = PollJobRepository(session).create(data)
    except DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_item(job)


@router.get("", response_model=PaginatedPollJobs, summary="List jobs")
async def list_jobs(
    principal: Principal = Depends(require_read),
    session: Session = Depends(get_session),
    owner: str | None = Query(None, description="Admin-only filter; ignored for non-admins"),
    source: str | None = Query(None),
    webformId: str | None = Query(None),
    destination_system: str | None = Query(None),
    isActive: bool | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> PaginatedPollJobs:
    owner_filter = owner if principal.is_admin else principal.sub
    total, rows = PollJobRepository(session).list(
        owner=owner_filter,
        source=source,
        webform_id=webformId,
        destination_system=destination_system,
        is_active=isActive,
        search=search,
        limit=limit,
        offset=offset,
    )
    next_offset = offset + limit if offset + limit < total else None
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    return PaginatedPollJobs(
        total=total,
        limit=limit,
        offset=offset,
        next_offset=next_offset,
        prev_offset=prev_offset,
        items=[_to_item(j) for j in rows],
    )


@router.get("/{job_id}", response_model=PollJobItem, summary="Get a job")
async def get_job(
    job_id: int,
    principal: Principal = Depends(require_read),
    session: Session = Depends(get_session),
) -> PollJobItem:
    return _to_item(_owned_job(session, job_id, principal))


@router.put("/{job_id}", response_model=PollJobItem, summary="Replace a job")
async def replace_job(
    job_id: int,
    body: PollJobCreate,
    principal: Principal = Depends(require_write),
    session: Session = Depends(get_session),
) -> PollJobItem:
    job = _owned_job(session, job_id, principal)
    _validate_routing(body.source, body.destination_system)
    data = {
        "name": body.name,
        "source": body.source,
        "webformId": body.webformId,
        "destination_system": body.destination_system,
        "destination_config": json.dumps(body.destination_config),
        "payload_mapping": (
            json.dumps(body.payload_mapping) if body.payload_mapping is not None else None
        ),
        "isActive": body.isActive,
        "erase_after": body.erase_after,
    }
    if body.owner and principal.is_admin:
        data["owner"] = body.owner
    try:
        job = PollJobRepository(session).update(job, data)
    except DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_item(job)


@router.patch("/{job_id}", response_model=PollJobItem, summary="Update a job (partial)")
async def update_job(
    job_id: int,
    body: PollJobUpdate,
    principal: Principal = Depends(require_write),
    session: Session = Depends(get_session),
) -> PollJobItem:
    job = _owned_job(session, job_id, principal)
    data = body.model_dump(exclude_unset=True)
    if "destination_config" in data and data["destination_config"] is not None:
        data["destination_config"] = json.dumps(data["destination_config"])
    # payload_mapping: an explicit null clears it back to raw passthrough.
    if "payload_mapping" in data:
        data["payload_mapping"] = (
            json.dumps(data["payload_mapping"])
            if data["payload_mapping"] is not None
            else None
        )
    # Validate routing against the post-update values.
    new_source = data.get("source", job.source)
    new_dest = data.get("destination_system", job.destination_system)
    if "source" in data or "destination_system" in data:
        _validate_routing(new_source, new_dest)
    if not data:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        job = PollJobRepository(session).update(job, data)
    except DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_item(job)


@router.delete("/{job_id}", status_code=204, summary="Delete a job (cascades its state)")
async def delete_job(
    job_id: int,
    principal: Principal = Depends(require_write),
    session: Session = Depends(get_session),
) -> None:
    job = _owned_job(session, job_id, principal)
    PollJobRepository(session).delete(job)


# --- Observability -------------------------------------------------------------


@router.get("/{job_id}/status", response_model=JobStatus, summary="Poll health for a job")
async def job_status(
    job_id: int,
    principal: Principal = Depends(require_read),
    session: Session = Depends(get_session),
) -> JobStatus:
    job = _owned_job(session, job_id, principal)
    repo = StateRepository(session)
    return JobStatus(
        job_id=job.id,
        webformId=job.webformId,
        isActive=job.isActive,
        counts=repo.status_counts(job.id),
        last_delivered_at=repo.last_delivered_at(job.id),
    )


@router.get(
    "/{job_id}/submissions",
    response_model=PaginatedSubmissions,
    summary="Per-submission poll state for a job",
)
async def job_submissions(
    job_id: int,
    principal: Principal = Depends(require_read),
    session: Session = Depends(get_session),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> PaginatedSubmissions:
    job = _owned_job(session, job_id, principal)
    total, rows = StateRepository(session).list_submissions(
        job.id, status=status_filter, limit=limit, offset=offset
    )
    return PaginatedSubmissions(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            SubmissionStateItem(
                submission_uuid=r.submission_uuid,
                submission_serial=r.submission_serial,
                status=r.status,
                attempts=r.attempts,
                last_error=r.last_error,
                destination_reference=r.destination_reference,
                first_seen_at=r.first_seen_at,
                delivered_at=r.delivered_at,
                erase_at=r.erase_at,
            )
            for r in rows
        ],
    )


@router.get(
    "/{job_id}/gaps",
    response_model=SerialGaps,
    summary="Missing serials for a job (backward reconciliation)",
)
async def job_serial_gaps(
    job_id: int,
    principal: Principal = Depends(require_read),
    session: Session = Depends(get_session),
    limit: int = Query(1000, ge=1, le=10000, description="Max missing serials to enumerate"),
) -> SerialGaps:
    """Report holes in this job's sequence of OS2forms serials.

    Serials are consecutive per webform, so a hole means the source **never listed that
    submission to us** — the only loss that leaves no state row behind to notice. A submission
    that was listed but failed to deliver has a row and a serial, so it is not a hole; look at
    `/status` for those.

    Three things make a hole legitimate rather than alarming, so treat the result as a prompt
    to check, not as proof of loss:

    - the sequence is anchored at this job's own lowest serial, but submissions that arrived
      before the job was registered are simply not this job's to have;
    - a submission deleted in OS2forms leaves a permanent, legitimate hole;
    - a job currently aborting (see the `job.fatal` log event) records nothing, so its
      un-processed submissions read as holes until it recovers — transient, and self-healing;
    - `unknown_serial` counts rows the analysis cannot place at all.
    """
    job = _owned_job(session, job_id, principal)
    result = StateRepository(session).serial_reconciliation(job.id, limit=limit)
    return SerialGaps(
        job_id=job.id,
        webformId=job.webformId,
        first_serial=result.first_serial,
        last_serial=result.last_serial,
        seen=result.seen,
        unknown_serial=result.unknown_serial,
        missing_count=result.missing_count,
        missing=result.missing,
        truncated=result.truncated,
    )


@router.post(
    "/{job_id}/submissions/{uuid}/retry",
    status_code=202,
    summary="Reset a failed/dead-letter submission to retry next cycle",
)
async def retry_submission(
    job_id: int,
    uuid: str,
    principal: Principal = Depends(require_write),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    job = _owned_job(session, job_id, principal)
    state = StateRepository(session).reset_for_retry(job.id, uuid)
    if state is None:
        raise HTTPException(status_code=404, detail="submission not found for this job")
    return {"status": "queued_for_retry", "submission_uuid": uuid}
