"""`POST /api/v1/otlp/v1/traces`: OpenTelemetry traces from programs outside the platform.

An OTLP/HTTP trace exporter pointed here (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) with a tenant API key
that has the `otlp` scope (`Authorization: Bearer pos_...`) shows its spans in Studio's Traces page next to
the platform's own. Both OTLP encodings are accepted: `application/x-protobuf` and `application/json`
(optionally `Content-Encoding: gzip`); gRPC and metrics/logs are not.

Order of refusals: 401 (no or unknown key), 403 (no `otlp` scope), 429 (per-key rate limit, with
`Retry-After`), 415 (another content type or encoding), 413 (body over 4 MiB, compressed or not, or
more than 1000 spans), 400 (not an `ExportTraceServiceRequest`). Spans that fail validation (ids,
timestamps) are dropped and counted: the answer is 200 with the `ExportTraceServiceResponse`, `{}` when
everything was stored, else `partialSuccess: {rejectedSpans, errorMessage}`. The tenant is the key's; a
`tenant` or `tenant.id` attribute is just an attribute. Attribute values are redacted (credential-like
keys and values) and bounded before they are stored. With `EAP_OTLP_FORWARD_ENDPOINT` set, the received
request is also forwarded unchanged to that collector URL (best effort, off by default).
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import time
import urllib.request
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status

from eap_api.deps import get_otlp_limiter
from eap_api.auth import AgUiPrincipal, otlp_principal
from eap_api.request_guard import read_capped_body, too_large_detail
from eap_api.service_deps.connections import OtlpIngestServiceDep
from eap_api.schemas import ErrorDetail
from eap_observability.otlp import (
    JSON,
    MAX_SPANS_PER_REQUEST,
    PROTOBUF,
    OtlpDecodeError,
    OtlpTooManySpans,
    decode_export,
    encode_response,
    media_type,
    prepare_spans,
)

logger = logging.getLogger(__name__)

MAX_OTLP_BODY_BYTES = 4 * 1024 * 1024
FORWARD_TIMEOUT_SECONDS = 5

router = APIRouter(
    tags=["otlp"],
    responses={
        401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 413: {"model": ErrorDetail},
        415: {"model": ErrorDetail}, 429: {"model": ErrorDetail},
    },
)

PrincipalDep = Annotated[AgUiPrincipal, Depends(otlp_principal)]


def _gunzip(body: bytes) -> bytes | None:
    """The decompressed body, or None past MAX_OTLP_BODY_BYTES (a compression bomb)."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as handle:
            data = handle.read(MAX_OTLP_BODY_BYTES + 1)
    except (OSError, EOFError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="body is not valid gzip") from None
    return None if len(data) > MAX_OTLP_BODY_BYTES else data


def _forward(endpoint: str, content_type: str, body: bytes) -> None:
    try:
        req = urllib.request.Request(endpoint, data=body, method="POST", headers={"Content-Type": content_type})
        with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT_SECONDS):  # noqa: S310 - operator-set URL
            pass
    except Exception as exc:  # best effort: the spans are already stored
        logger.warning("OTLP forward failed: %s", type(exc).__name__)


@router.post("/otlp/v1/traces")
async def ingest_traces(
    request: Request, principal: PrincipalDep, service: OtlpIngestServiceDep, background: BackgroundTasks
) -> Response:
    """Store the spans of an OTLP export request for the key's tenant and answer the OTLP response."""
    caller = principal.key_id or f"dev:{principal.tenant_id}"
    retry_after = get_otlp_limiter(request).acquire(caller)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="OTLP rate limit exceeded; retry later",
            headers={"Retry-After": str(max(int(retry_after) + 1, 1))},
        )
    content_type = request.headers.get("content-type")
    if media_type(content_type) not in (PROTOBUF, JSON):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="use Content-Type application/x-protobuf or application/json (gRPC is not supported)",
        )
    encoding = (request.headers.get("content-encoding") or "identity").strip().lower()
    if encoding not in ("identity", "gzip"):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Content-Encoding must be gzip or absent")
    body = await read_capped_body(request, MAX_OTLP_BODY_BYTES)
    if not isinstance(body, bytes):
        raise HTTPException(status_code=413, detail=too_large_detail(MAX_OTLP_BODY_BYTES))
    if encoding == "gzip":
        body = _gunzip(body)  # type: ignore[assignment]
        if body is None:
            raise HTTPException(status_code=413, detail=too_large_detail(MAX_OTLP_BODY_BYTES))
    try:
        raw = decode_export(content_type, body)
    except OtlpTooManySpans:
        raise HTTPException(
            status_code=413, detail=f"too many spans in one request (max {MAX_SPANS_PER_REQUEST})"
        ) from None
    except OtlpDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    prepared = prepare_spans(raw, time.time_ns())
    if prepared.spans:
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(service.store_spans, principal.tenant_id, principal.key_id, prepared.spans)
    endpoint = os.environ.get("EAP_OTLP_FORWARD_ENDPOINT", "").strip()
    if endpoint:
        background.add_task(_forward, endpoint, content_type or JSON, body)
    payload, out_type = encode_response(
        content_type, prepared.rejected,
        f"{prepared.rejected} span(s) rejected: {prepared.error}" if prepared.rejected else None,
    )
    return Response(content=payload, media_type=out_type, background=background)
