from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from server.api.settings import ServerSettings
from server.application.query import (
    ForecastQueryService,
    QueryRequest,
    TimelineRequest,
)
from server.application.runtime import (
    create_analytics_repository_from_settings,
    create_artifact_repository_from_settings,
    create_documentation_source,
    create_model_source,
    create_query_service,
)
from server.application.snapshot_source import SnapshotStoreSource
from server.database.store import (
    SnapshotConflictError,
    SnapshotStoreProtocol,
    create_snapshot_store,
)
from smog_ai.observability.feedback import (
    LocalPromptFeedbackStore,
    PromptFeedbackRecord,
)
from smog_ai.observability.analytics import read_langfuse_quality_summary
from smog_ai.observability.own_store import OwnAnalyticsStore

logger = logging.getLogger("smog_ai.server")
settings = ServerSettings.from_env()
_artifact_repository = (
    create_artifact_repository_from_settings(settings) if settings.uses_object_store else None
)
store: SnapshotStoreProtocol = create_snapshot_store(
    data_dir=settings.data_dir,
    keep_versions=settings.keep_versions,
    database_url=settings.database_url,
    storage_backend=settings.storage_backend,
    artifact_repository=_artifact_repository,
)
query_service: ForecastQueryService = create_query_service(
    settings,
    SnapshotStoreSource(store),
    _artifact_repository,
)
documentation_source = create_documentation_source(settings, _artifact_repository)
model_source = create_model_source(settings, _artifact_repository)
feedback_store = LocalPromptFeedbackStore(settings.prompt_feedback_path)
_analytics_repository = (
    create_analytics_repository_from_settings(settings)
    if settings.own_analytics_enabled and settings.uses_separate_analytics_store
    else _artifact_repository
)
own_analytics_store = (
    OwnAnalyticsStore(
        _analytics_repository,
        private_prefix=settings.own_analytics_private_prefix,
        retention_days=settings.own_analytics_retention_days,
    )
    if settings.own_analytics_enabled and _analytics_repository is not None
    else None
)
_requests: dict[str, deque[float]] = defaultdict(deque)


class PromptFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    score: float = Field(ge=0.0, le=1.0)
    label: str | None = Field(default=None, max_length=120)
    comment: str | None = Field(default=None, max_length=2000)
    question: str | None = Field(default=None, max_length=2000)
    session_id: str | None = Field(default=None, max_length=200)
    user_id: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate()
    store.ping()
    if query_service.spatial_source is not None:
        query_service.spatial_source.ping()
    if own_analytics_store is not None:
        own_analytics_store.repository.ping()
    # Warm only the small spatial pointer/manifest. Surface payloads and the full
    # forecast snapshot stay lazy so startup does not transfer data no user asked for.
    try:
        if query_service.spatial_source is not None:
            query_service.spatial_source.latest_manifest()
    except Exception as exc:
        logger.warning("Query cache warm-up was incomplete: %s", exc)
    try:
        documentation_source.ping()
    except Exception as exc:
        logger.warning("Documentation source is not fully available: %s", exc)
    logger.info(
        "Air-quality API started version=%s commit=%s backend=%s nlp=%s observability=%s",
        settings.app_version,
        settings.commit_sha,
        store.backend_name,
        settings.nlp_provider,
        settings.observability_backend,
    )
    try:
        yield
    finally:
        # HF21_LANGFUSE_SCORE_CONTRACT_V1: export queued traces and scores.
        query_service.observability.flush()


app = FastAPI(
    title="GIOŚ/IMGW Air Quality Forecast API",
    description=(
        "Reads published forecasts through the storage Bridge and interprets natural-language "
        "questions such as: 'Wyjeżdżam jutro do Katowic, jakie będzie tam zanieczyszczenie?'."
    ),
    version=settings.app_version,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    started = time.perf_counter()
    try:
        response: Response = await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled API error request_id=%s path=%s", request_id, request.url.path
        )
        raise
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "request request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


def _client_address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", maxsplit=1)[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request) -> None:
    address = _client_address(request)
    now = time.monotonic()
    bucket = _requests[address]
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= settings.rate_limit_per_minute:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    bucket.append(now)


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
        )


async def _read_limited_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise HTTPException(status_code=413, detail="Payload too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="Payload too large")
    return bytes(body)


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    try:
        store.ping()
        summary = store.audit_summary()
        spatial_manifest = (
            query_service.spatial_source.latest_manifest()
            if query_service.spatial_source is not None
            else None
        )
    except Exception as exc:
        logger.exception("Health check failed")
        raise HTTPException(status_code=503, detail=f"Storage unavailable: {exc}") from exc
    if spatial_manifest:
        # Serving v2 deliberately has no giant forecast snapshot/audit history.
        # A valid immutable release is the publication unit exposed by health.
        summary = {
            **summary,
            "publication_count": max(1, int(summary.get("publication_count", 0))),
            "latest_publication_id": spatial_manifest.get("release_id")
            or spatial_manifest.get("surface_set_id"),
            "latest_received_at": spatial_manifest.get("generated_at"),
        }
    return {
        "status": "ok",
        "service": "smog-ai-query-api",
        "customer": settings.customer_name,
        "version": settings.app_version,
        "commit_sha": settings.commit_sha,
        "nlp_provider": settings.nlp_provider,
        "nlp_model": settings.nlp_model,
        "observability_backend": settings.observability_backend,
        "uploads_enabled": settings.uploads_enabled,
        "prediction_mode": "published_station_forecasts_exact_point",
        "spatial_surface_set_id": (spatial_manifest or {}).get("surface_set_id"),
        "spatial_generated_at": (spatial_manifest or {}).get("generated_at"),
        "spatial_surface_count": len((spatial_manifest or {}).get("surfaces", [])),
        "serving_contract": (spatial_manifest or {}).get("contract"),
        "serving_release_id": (spatial_manifest or {}).get("release_id"),
        **summary,
    }


@app.get("/api/v1/ready")
def ready() -> dict[str, Any]:
    store.ping()
    manifest = (
        query_service.spatial_source.latest_manifest()
        if query_service.spatial_source is not None
        else None
    )
    return {
        "status": "ready",
        "spatial_ready": bool(manifest and manifest.get("surfaces")),
        "surface_set_id": (manifest or {}).get("surface_set_id"),
    }


@app.get("/api/v1/version")
def version() -> dict[str, str | None]:
    return {
        "version": settings.app_version,
        "commit_sha": settings.commit_sha,
        "environment": settings.environment,
    }


@app.post("/api/v1/snapshots", dependencies=[Depends(require_bearer)])
async def upload_snapshot(
    request: Request,
    x_publication_id: str = Header(alias="X-Publication-Id", min_length=1, max_length=160),
    x_checksum: str = Header(alias="X-Checksum", min_length=64, max_length=64),
) -> JSONResponse:
    if not settings.uploads_enabled:
        raise HTTPException(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            detail="HTTP uploads are disabled; publish through the configured object store.",
        )
    _check_rate_limit(request)
    body = await _read_limited_body(request, settings.max_upload_bytes)
    try:
        payload = store.decode_and_validate(body, x_checksum, x_publication_id)
        created, stored_reference = store.save(
            body,
            payload,
            remote_address=_client_address(request),
        )
    except SnapshotConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        status_code=201 if created else 200,
        content={
            "status": "created" if created else "duplicate",
            "publication_id": x_publication_id,
            "stored_as": stored_reference.name,
            "storage_backend": store.backend_name,
        },
    )


def _powershell_safe_json(value: Any) -> Any:
    """Remove case-only key collisions rejected by Windows PowerShell 5.1.

    JSON keys are case-sensitive, but ConvertFrom-Json in Windows PowerShell
    treats them case-insensitively. Public responses therefore keep one
    canonical spelling, preferring an all-lowercase key when both variants are
    present. Values and list ordering are otherwise unchanged.
    """
    if isinstance(value, list):
        return [_powershell_safe_json(item) for item in value]
    if not isinstance(value, dict):
        return value
    output: dict[str, Any] = {}
    selected: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        folded = key.casefold()
        existing = selected.get(folded)
        safe_value = _powershell_safe_json(raw_value)
        if existing is None:
            selected[folded] = key
            output[key] = safe_value
            continue
        if key == folded and existing != existing.casefold():
            output.pop(existing, None)
            selected[folded] = key
            output[key] = safe_value
    return output


@app.post("/api/v1/query/preview")
def preview_natural_language_query(payload: QueryRequest, request: Request) -> dict[str, Any]:
    """Interpret location, time and parameters without calculating forecasts."""
    _check_rate_limit(request)
    try:
        result = query_service.preview(payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Natural-language query preview failed")
        raise HTTPException(
            status_code=503,
            detail=f"Query preview unavailable: {exc}",
        ) from exc
    return _powershell_safe_json(result)


@app.post("/api/v1/query")
def natural_language_query(payload: QueryRequest, request: Request) -> dict[str, Any]:
    """Interpret a textbox query and read a locally precomputed forecast surface."""
    _check_rate_limit(request)
    try:
        # The heavy multi-hour profile is loaded through /timeline after the
        # exact-hour response is already visible in the dashboard.
        result = query_service.ask(payload, include_timeline=False)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Natural-language query failed")
        raise HTTPException(status_code=503, detail=f"Query service unavailable: {exc}") from exc
    finally:
        if settings.observability_flush_on_request:
            query_service.observability.flush()
    response_payload = result.model_dump(mode="json")
    if own_analytics_store is not None:
        try:
            own_analytics_store.save_interaction(response_payload)
        except Exception:
            logger.exception("Private interaction event could not be stored")
    return _powershell_safe_json(response_payload)


@app.post("/api/v1/timeline")
def spatial_timeline(payload: TimelineRequest, request: Request) -> dict[str, Any]:
    """Load a daily point profile lazily and concurrently.

    This endpoint is intentionally separate from /query so a textbox answer and
    selected map can be rendered without waiting for dozens of surface objects.
    """

    _check_rate_limit(request)
    try:
        result = query_service.timeline(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Spatial timeline query failed")
        raise HTTPException(
            status_code=503, detail=f"Timeline service unavailable: {exc}"
        ) from exc
    return _powershell_safe_json(result.model_dump(mode="json"))


@app.get("/api/v1/spatial/manifest")
def spatial_manifest() -> JSONResponse:
    if query_service.spatial_source is None:
        raise HTTPException(status_code=404, detail="Spatial surfaces are disabled")
    payload = query_service.spatial_source.latest_manifest()
    if payload is None:
        raise HTTPException(status_code=404, detail="No spatial surface manifest has been published")
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "public, max-age=30, stale-while-revalidate=120"},
    )


@app.get("/api/v1/spatial/boundary")
def spatial_boundary() -> JSONResponse:
    if query_service.spatial_source is None:
        raise HTTPException(status_code=404, detail="Spatial surfaces are disabled")
    payload = query_service.spatial_source.boundary()
    if payload is None:
        raise HTTPException(status_code=404, detail="Poland boundary is unavailable")
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/v1/spatial/surface")
def spatial_surface(
    parameter: str = Query(
        default="PM10",
        pattern=r"^[A-Za-z0-9_.-]{1,64}$",
    ),
    horizon_hours: int | None = Query(default=None, ge=1, le=168),
    target_time: str | None = Query(default=None),
) -> JSONResponse:
    if query_service.spatial_source is None:
        raise HTTPException(status_code=404, detail="Spatial surfaces are disabled")
    parsed_target: datetime | None = None
    if target_time:
        try:
            parsed_target = datetime.fromisoformat(target_time.replace("Z", "+00:00"))
            if parsed_target.tzinfo is None:
                parsed_target = parsed_target.replace(tzinfo=UTC)
            parsed_target = parsed_target.astimezone(UTC)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid target_time") from exc
    manifest = query_service.spatial_source.latest_manifest() or {}
    published_parameters = {
        str(value) for value in manifest.get("parameters", []) if value
    }
    if published_parameters and parameter not in published_parameters:
        raise HTTPException(
            status_code=404,
            detail=f"Parameter {parameter!r} is not present in the active serving release",
        )
    payload = query_service.spatial_source.surface(
        parameter=parameter,
        horizon_hours=horizon_hours,
        target_time=parsed_target,
        exact_target_time=bool(parsed_target and manifest.get("exact_target_time_available")),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="Requested spatial surface is unavailable")
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "public, max-age=60, stale-while-revalidate=300"},
    )


@app.get("/api/v1/spatial/places")
def spatial_places() -> list[dict[str, Any]]:
    if query_service.spatial_source is None:
        return []
    return query_service.spatial_source.places()


@app.get("/api/v1/places/search")
def search_places(q: str = Query(min_length=2, max_length=120)) -> list[dict[str, Any]]:
    resolver = query_service.place_resolver
    if resolver is None:
        return []
    normalized = q.casefold()
    matches = [name for name in resolver.candidates if normalized in name.casefold()]
    if not matches:
        try:
            place = resolver.resolve(q)
            matches = [place.name]
        except ValueError:
            return []
    result: list[dict[str, Any]] = []
    for name in matches[:20]:
        try:
            place = resolver.resolve(name)
        except ValueError:
            continue
        result.append({
            "name": place.name,
            "latitude": place.latitude,
            "longitude": place.longitude,
            "source": place.source,
        })
    return result


@app.get("/api/v1/models")
def active_models() -> dict[str, Any]:
    try:
        models = model_source.active_models()
    except Exception as exc:
        logger.exception("Active-model metadata unavailable")
        raise HTTPException(status_code=503, detail=f"Model metadata unavailable: {exc}") from exc
    return {
        "schema_version": "1.0",
        "forecast_mode": "horizon-conditioned-hourly",
        "backend": model_source.backend_name,
        "models": models,
    }


@app.get("/api/v1/models/compare")
def compare_models() -> dict[str, Any]:
    try:
        payload = model_source.comparison()
    except Exception as exc:
        logger.exception("Model-comparison metadata unavailable")
        raise HTTPException(
            status_code=503,
            detail=f"Model comparison unavailable: {exc}",
        ) from exc
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail="Model-comparison artifact has not been published",
        )
    output = dict(payload)
    output.setdefault("mlflow_ui_url", settings.mlflow_ui_url)
    return output


@app.post("/api/v1/feedback")
def submit_prompt_feedback(payload: PromptFeedbackRequest) -> dict[str, Any]:
    if not settings.prompt_feedback_enabled:
        raise HTTPException(status_code=404, detail="Prompt feedback is disabled")
    record = PromptFeedbackRecord(
        trace_id=payload.trace_id,
        request_id=payload.request_id,
        score=payload.score,
        label=payload.label,
        comment=payload.comment,
        question=payload.question,
        session_id=payload.session_id,
        user_id=payload.user_id,
        metadata={
            "prompt_template_version": settings.prompt_template_version,
            **payload.metadata,
        },
    )
    local_result = feedback_store.append(record)
    own_result: dict[str, Any] | None = None
    if own_analytics_store is not None:
        try:
            own_result = own_analytics_store.save_feedback(
                feedback_id=record.feedback_id,
                trace_id=payload.trace_id,
                request_id=payload.request_id,
                score=payload.score,
                label=payload.label,
                comment=payload.comment,
                question=payload.question,
                components=dict(payload.metadata.get("component_scores") or {}),
            )
        except Exception as exc:
            logger.exception("Private feedback event could not be stored")
            own_result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    remote_result = query_service.observability.score(
        trace_id=payload.trace_id,
        name="answer_quality",
        value=payload.score,
        comment=payload.comment,
        metadata={
            "label": payload.label,
            "request_id": payload.request_id,
            "prompt_template_version": settings.prompt_template_version,
            **payload.metadata,
        },
    )
    if settings.observability_flush_on_request:
        query_service.observability.flush()
    return {
        "status": "ok",
        "feedback_id": record.feedback_id,
        "local": local_result,
        "own_analytics": own_result,
        "observability": remote_result,
    }


@app.get("/api/v1/feedback/summary")
def prompt_feedback_summary() -> dict[str, Any]:
    if not settings.prompt_feedback_enabled:
        raise HTTPException(status_code=404, detail="Prompt feedback is disabled")
    return feedback_store.summary()


@app.get("/api/v1/quality/overview")
def quality_overview() -> dict[str, Any]:
    """Return own ObjectStore analytics; external observability is secondary."""
    local = feedback_store.summary()
    own = own_analytics_store.summary() if own_analytics_store is not None else {
        "status": "disabled", "source": "own_object_store", "count": 0,
        "average_score": None, "positive_fraction": None, "daily": [],
    }
    remote = (
        read_langfuse_quality_summary()
        if settings.observability_backend == "langfuse"
        else {
            "status": "disabled", "backend": settings.observability_backend,
            "count": 0, "average_score": None, "positive_fraction": None,
            "daily": [], "recent": [],
        }
    )
    return {
        "schema_version": "1.0",
        "backend": settings.observability_backend,
        "score_name": "answer_quality",
        "primary": own,
        "remote": remote,
        "local": local,
    }


@app.get("/api/v1/docs/manifest")
def documentation_manifest() -> dict[str, Any]:
    payload = documentation_source.manifest()
    if payload is None:
        raise HTTPException(status_code=404, detail="Documentation manifest is unavailable")
    return payload


@app.get("/api/v1/docs/processing", response_class=PlainTextResponse)
def documentation_processing() -> PlainTextResponse:
    try:
        text = documentation_source.processing_markdown()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/api/v1/docs/processing/source", response_class=PlainTextResponse)
def documentation_processing_source() -> PlainTextResponse:
    try:
        text = documentation_source.processing_latex()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        text,
        media_type="application/x-tex; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=300",
            "Content-Disposition": (
                'attachment; filename="smog-ai-przetwarzanie-techniczne.tex"'
            ),
        },
    )


@app.get("/api/v1/docs/mathematics", response_class=PlainTextResponse)
def documentation_mathematics() -> PlainTextResponse:
    try:
        text = documentation_source.mathematics_markdown()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/api/v1/docs/model-plugins", response_class=PlainTextResponse)
def documentation_model_plugins() -> PlainTextResponse:
    try:
        text = documentation_source.model_plugin_markdown()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/api/v1/docs/hf20", response_class=PlainTextResponse)
def documentation_hf20() -> PlainTextResponse:
    try:
        text = documentation_source.hf20_markdown()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/api/v1/docs/hf20/source", response_class=PlainTextResponse)
def documentation_hf20_source() -> PlainTextResponse:
    try:
        text = documentation_source.hf20_latex()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        text,
        media_type="application/x-tex; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=300",
            "Content-Disposition": 'attachment; filename="smog-ai-hf20-time-contract-mlops.tex"',
        },
    )


@app.get("/api/v1/docs/mathematics/source", response_class=PlainTextResponse)
def documentation_mathematics_source() -> PlainTextResponse:
    try:
        text = documentation_source.mathematics_latex()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        text,
        media_type="application/x-tex; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=300",
            "Content-Disposition": 'attachment; filename="smog-ai-model-matematyczny.tex"',
        },
    )


@app.get("/api/v1/snapshots/latest")
def latest_snapshot() -> JSONResponse:
    payload = store.latest_payload()
    if payload is None:
        raise HTTPException(status_code=404, detail="No snapshot has been published")
    checksum = str(payload.get("metadata", {}).get("checksum", ""))
    headers = {"Cache-Control": "public, max-age=30, stale-while-revalidate=60"}
    if checksum:
        headers["ETag"] = f'"{checksum}"'
    return JSONResponse(content=payload, headers=headers)


@app.get("/api/v1/snapshots/history")
def snapshot_history(limit: int = Query(default=20, ge=1, le=200)) -> list[dict[str, Any]]:
    return store.history(limit=limit)


@app.get("/api/v1/stations")
def stations() -> list[dict[str, Any]]:
    payload = store.latest_payload()
    return [] if payload is None else payload.get("stations", [])


@app.get("/api/v1/forecasts")
def forecasts() -> list[dict[str, Any]]:
    payload = store.latest_payload()
    return [] if payload is None else payload.get("forecasts", [])


@app.get("/api/v1/metrics")
def metrics() -> list[dict[str, Any]]:
    payload = store.latest_payload()
    return [] if payload is None else payload.get("metrics", [])


def run() -> None:
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "server.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
