"""Automation Server destination adapter (odense-rpa/automation-server).

Robots consume work-queue items. Wire contract (verified against the Automation_Hub
rpa-management + journalizing clients):
  - auth:    Authorization: Bearer <api_key>
  - prefix:  /api
  - resolve: GET  /api/workqueues/by_name/{name}                 -> {id, name, ...}
  - guard:   GET  /api/workqueues/{id}/by_reference/{reference}  -> item | 404
  - add:     POST /api/workqueues/{id}/add  {"data": {...}, "reference": "<uuid>"}

Setting reference = OS2forms submission uuid gives idempotency + end-to-end traceability.
"""

from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings
from app.destinations.base import DestinationAdapter
from app.exceptions import (
    DestinationAuthError,
    DestinationConfigError,
    DestinationResponseError,
    DestinationServerError,
)
from app.logging import get_logger

log = get_logger(__name__)

_RETRY = retry(
    retry=retry_if_exception_type(DestinationServerError),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, max=8),
    reraise=True,
)


class AutomationServerDestination(DestinationAdapter):
    """Delivers submissions as work-queue items to a named Automation Server queue."""

    adapter = "automation_server"

    def __init__(
        self,
        queue_name: str,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ):
        self._settings = settings or get_settings()
        if not queue_name:
            raise DestinationConfigError(
                "Automation Server destination requires a queue name, "
                "e.g. destination_system='automation_server:MyQueue'"
            )
        self._queue_name = queue_name
        self._prefix = self._settings.automation_server_api_prefix.rstrip("/")
        self._queue_id: str | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._settings.automation_server_url.rstrip("/"),
            timeout=self._settings.automation_server_timeout,
            verify=self._settings.automation_server_verify_ssl,
            headers={"Authorization": f"Bearer {self._settings.automation_server_api_key}"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- HTTP helpers ----------------------------------------------------------

    @_RETRY
    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._client.request(method, f"{self._prefix}{path}", **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise DestinationServerError(f"transport error for {path}: {exc}") from exc
        status = response.status_code
        if status in (401, 403):
            raise DestinationAuthError(f"auth failed for {path} ({status})")
        if status >= 500:
            raise DestinationServerError(f"server error {status} for {path}")
        return response

    def _resolve_queue_id(self) -> str:
        if self._queue_id is not None:
            return self._queue_id
        response = self._request("GET", f"/workqueues/by_name/{self._queue_name}")
        if response.status_code == 404:
            raise DestinationConfigError(f"workqueue not found: {self._queue_name!r}")
        if response.status_code >= 400:
            raise DestinationResponseError(
                f"resolve queue {self._queue_name!r} failed ({response.status_code})"
            )
        body = response.json()
        queue_id = body.get("id") if isinstance(body, dict) else None
        if queue_id is None:
            raise DestinationResponseError(f"queue {self._queue_name!r} response had no id")
        self._queue_id = str(queue_id)
        return self._queue_id

    def _already_delivered(self, queue_id: str, reference: str) -> bool:
        """Second-line idempotency guard: does an item with this reference exist?"""
        response = self._request(
            "GET", f"/workqueues/{queue_id}/by_reference/{reference}"
        )
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            # Non-fatal: fall through to add (the poller's state table is the primary guard).
            log.warning(
                "automation_server.by_reference_unexpected",
                status=response.status_code,
                reference=reference,
            )
            return False
        body = response.json()
        return bool(body)

    # --- DestinationAdapter ----------------------------------------------------

    def deliver(self, payload: dict[str, object], reference: str) -> str:
        queue_id = self._resolve_queue_id()
        if self._already_delivered(queue_id, reference):
            log.info(
                "automation_server.already_present",
                queue=self._queue_name,
                reference=reference,
            )
            return reference
        response = self._request(
            "POST",
            f"/workqueues/{queue_id}/add",
            json={"data": payload, "reference": reference},
        )
        if response.status_code >= 400:
            raise DestinationResponseError(
                f"add to queue {self._queue_name!r} failed ({response.status_code})"
            )
        item = response.json()
        item_ref = item.get("reference") if isinstance(item, dict) else None
        item_id = item.get("id") if isinstance(item, dict) else None
        log.info(
            "automation_server.added",
            queue=self._queue_name,
            reference=reference,
            item_id=item_id,
        )
        return str(item_ref or item_id or reference)
