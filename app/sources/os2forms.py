"""OS2forms source adapter (Drupal ``webform_rest`` module).

Endpoints (base e.g. https://your-os2forms-host/da):
  - list:  GET /webform_rest/{webform_id}/submissions        -> identifiers
  - read:  GET /webform_rest/{webform_id}/submission/{uuid}   -> full submission

Auth: static ``api-key`` header on every request (read from config, never logged).
Retries: bounded, on 5xx/transport only (never 4xx).

NOTE(sample): the exact JSON shape of the two responses is not yet pinned. The parsing
below is deliberately defensive and marked ``TODO(sample)`` — one real submission JSON
will let us replace the heuristics with exact key access.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings
from app.exceptions import (
    SourceAuthError,
    SourceNotFoundError,
    SourceResponseError,
    SourceServerError,
)
from app.logging import get_logger
from app.sources.base import Attachment, SourceAdapter, Submission, SubmissionRef

log = get_logger(__name__)

_RETRY = retry(
    retry=retry_if_exception_type(SourceServerError),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, max=8),
    reraise=True,
)


class OS2formsSource(SourceAdapter):
    name = "os2forms"

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._settings.os2forms_base_url.rstrip("/"),
            timeout=self._settings.os2forms_timeout,
            verify=self._settings.os2forms_verify_ssl,
            headers={"api-key": self._settings.os2forms_api_key},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @_RETRY
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise SourceServerError(f"transport error for {path}: {exc}") from exc

        status = response.status_code
        log.info("os2forms.request", path=path, status=status)
        if status in (401, 403):
            raise SourceAuthError(f"auth failed for {path} ({status})")
        if status == 404:
            raise SourceNotFoundError(f"not found: {path}")
        if 400 <= status < 500:
            raise SourceResponseError(f"client error {status} for {path}")
        if status >= 500:
            raise SourceServerError(f"server error {status} for {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceResponseError(f"non-JSON response for {path}") from exc

    # --- SourceAdapter ---------------------------------------------------------

    def list_submissions(self, webform_id: str) -> list[SubmissionRef]:
        """List submission identifiers, following ?page pagination until a page is empty.

        Verified response shape:
            {"webform_id": "...",
             "submissions": {"<serial>": "<abs url .../submission/{uuid}>", ...}}
        The uuid is the last path segment of each URL (it is not a field).
        """
        refs: list[SubmissionRef] = []
        seen: set[str] = set()
        page = 0
        while True:
            payload = self._get(
                f"/webform_rest/{webform_id}/submissions", params={"page": page}
            )
            page_refs = self._parse_submissions_page(payload)
            new_on_page = 0
            for ref in page_refs:
                if ref.uuid in seen:
                    continue
                seen.add(ref.uuid)
                refs.append(ref)
                new_on_page += 1
            # Stop on an empty page, or when a page adds nothing new (guards a
            # non-paginated endpoint that returns the full set on every call).
            if new_on_page == 0:
                break
            page += 1
        log.info("os2forms.listed", webform_id=webform_id, count=len(refs))
        return refs

    def get_submission(self, webform_id: str, uuid: str) -> Submission:
        payload = self._get(f"/webform_rest/{webform_id}/submission/{uuid}")
        if not isinstance(payload, dict):
            raise SourceResponseError(f"submission {uuid} was not an object")
        data_node = payload.get("data")
        if not isinstance(data_node, dict):
            log.warning("os2forms.no_data_node", webform_id=webform_id, uuid=uuid)
            data_node = {}
        attachments = self._extract_attachments(data_node)
        # Pass form fields through as-is, minus the attachments sub-node (surfaced separately).
        case = {k: v for k, v in data_node.items() if k != "attachments"}
        metadata = self._extract_metadata(payload)
        return Submission(
            uuid=uuid, raw=payload, data=case, attachments=attachments, metadata=metadata
        )

    # --- Parsing ---------------------------------------------------------------

    @staticmethod
    def _parse_submissions_page(payload: Any) -> list[SubmissionRef]:
        """Parse one /submissions page into refs. uuid comes from each URL's tail."""
        if not isinstance(payload, dict):
            raise SourceResponseError("submissions response was not an object")
        subs = payload.get("submissions")
        if not subs:
            return []
        if isinstance(subs, dict):
            entries: list[tuple[str, Any]] = [(str(k), v) for k, v in subs.items()]
        elif isinstance(subs, list):
            entries = [(str(i), v) for i, v in enumerate(subs)]
        else:
            raise SourceResponseError("unexpected 'submissions' shape")

        refs: list[SubmissionRef] = []
        for serial, value in entries:
            url = value if isinstance(value, str) else None
            if isinstance(value, dict):
                url = value.get("url") or value.get("uri")
            if not isinstance(url, str):
                continue
            uuid = _uuid_from_url(url)
            if uuid:
                refs.append(SubmissionRef(uuid=uuid, serial=serial, url=url))
        return refs

    # --- Single-submission parsing (verified against a real response) ----------

    @staticmethod
    def _extract_attachments(data_node: dict[str, Any]) -> list[Attachment]:
        """Attachments live under data['attachments'] (absent when the form has no files).

        Verified shape: {"<field>": {"name": ..., "type": "pdf", "url": "https://.../file.pdf"}}
        A field may also hold a list of such objects (multi-file upload).
        """
        node = data_node.get("attachments")
        attachments: list[Attachment] = []
        if isinstance(node, dict):
            entries: list[tuple[str | None, Any]] = list(node.items())
        elif isinstance(node, list):
            entries = [(None, item) for item in node]
        else:
            return attachments
        for field_name, info in entries:
            attachments.extend(_to_attachments(info, field_name))
        return attachments

    @staticmethod
    def _extract_metadata(payload: dict[str, Any]) -> dict[str, Any]:
        """Pull useful submission metadata from the Drupal-style 'entity' field-arrays."""
        entity = payload.get("entity")
        if not isinstance(entity, dict):
            return {}

        def first(key: str) -> Any:
            seq = entity.get(key)
            if isinstance(seq, list) and seq and isinstance(seq[0], dict):
                return seq[0].get("value")
            return None

        return {
            "serial": first("serial"),
            "created": first("created"),
            "completed": first("completed"),
            "currentPage": first("current_page"),
        }


def _to_attachments(info: Any, fallback_name: str | None) -> list[Attachment]:
    """Convert one attachment entry (dict, list, or URL string) into Attachment objects."""
    if isinstance(info, list):
        out: list[Attachment] = []
        for item in info:
            out.extend(_to_attachments(item, fallback_name))
        return out
    if isinstance(info, str):
        if info.startswith(("http://", "https://")):
            return [Attachment(name=fallback_name or info.rsplit("/", 1)[-1], url=info)]
        return []
    if isinstance(info, dict):
        url = info.get("url") or info.get("uri")
        if not isinstance(url, str):
            return []
        name = info.get("name") or info.get("filename") or fallback_name or url.rsplit("/", 1)[-1]
        return [
            Attachment(
                name=str(name),
                url=url,
                mime=_str_or_none(info.get("type") or info.get("filemime") or info.get("mime")),
                size=_int_or_none(info.get("size") or info.get("filesize")),
            )
        ]
    return []


def _uuid_from_url(url: str) -> str | None:
    """The uuid is the last path segment of a submission URL (query/trailing-slash safe)."""
    tail = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
