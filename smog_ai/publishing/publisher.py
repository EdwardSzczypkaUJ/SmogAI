from __future__ import annotations

import gzip
import logging
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.config import AppConfig
from smog_ai.database.models import OutboxStatus
from smog_ai.database.repository import due_outbox_query, register_published_snapshot, set_application_state
from smog_ai.domain import StageStats
from smog_ai.publishing.schema import SnapshotPayload
from smog_ai.time_utils import utc_now

logger = logging.getLogger(__name__)


def _endpoint(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/snapshots") else f"{base}/snapshots"


def _backoff(config: AppConfig, attempts: int) -> int:
    return min(
        config.publication.backoff_max_seconds,
        config.publication.backoff_base_seconds * (2 ** max(0, attempts - 1)),
    )


def _publish_http(
    *,
    compressed: bytes,
    publication_id: str,
    checksum: str,
    config: AppConfig,
) -> None:
    token = config.publication.token()
    if not token:
        raise RuntimeError(f"Missing environment variable {config.publication.api_token_env}")
    with httpx.Client(timeout=config.publication.timeout_seconds, follow_redirects=False) as client:
        response = client.post(
            _endpoint(config.publication.api_url),
            content=compressed,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/gzip",
                "X-Publication-Id": publication_id,
                "X-Checksum": checksum,
            },
        )
        response.raise_for_status()


def _publish_object_store(
    *,
    compressed: bytes,
    payload: SnapshotPayload,
    checksum: str,
    config: AppConfig,
) -> dict[str, str | int]:
    repository = create_artifact_repository(config)
    stored = repository.publish_snapshot(
        compressed=compressed,
        publication_id=payload.metadata.publication_id,
        checksum=checksum,
        metadata=payload.metadata.model_dump(mode="json"),
    )
    return {
        "object_key": stored.key,
        "transport_checksum": stored.checksum,
        "size": stored.size,
        "backend": repository.store.backend_name,
    }


def retry_publications(session: Session, config: AppConfig, *, limit: int = 20) -> StageStats:
    stats = StageStats()
    if not config.publication.enabled:
        stats.skipped = 1
        stats.details = {"reason": "publication_disabled"}
        return stats
    rows = session.scalars(due_outbox_query().limit(limit)).all()
    successful_details: list[dict[str, object]] = []
    for row in rows:
        path = Path(row.payload_path)
        row.status = OutboxStatus.sending.value
        row.attempt_count += 1
        row.last_attempt_at = utc_now()
        try:
            if not path.exists():
                raise FileNotFoundError(f"Snapshot payload missing: {path}")
            compressed = path.read_bytes()
            payload = SnapshotPayload.model_validate_json(gzip.decompress(compressed))
            transport = config.publication.transport
            details: dict[str, object] = {
                "publication_id": row.publication_id,
                "transport": transport,
            }
            if transport in {"object_store", "both"}:
                details["object_store"] = _publish_object_store(
                    compressed=compressed,
                    payload=payload,
                    checksum=row.checksum,
                    config=config,
                )
            if transport in {"http", "both"}:
                _publish_http(
                    compressed=compressed,
                    publication_id=row.publication_id,
                    checksum=row.checksum,
                    config=config,
                )
                details["http"] = {"endpoint": _endpoint(config.publication.api_url), "status": "ok"}
            row.status = OutboxStatus.published.value
            row.published_at = utc_now()
            row.last_error = None
            row.next_attempt_at = None
            register_published_snapshot(
                session,
                publication_id=row.publication_id,
                schema_version=payload.metadata.schema_version,
                generated_at=payload.metadata.generated_at,
                data_start=payload.metadata.data_start,
                data_end=payload.metadata.data_end,
                model_version=payload.metadata.model_version,
                record_count=payload.metadata.record_count,
                checksum=row.checksum,
                source_host_id=payload.metadata.source_host_id,
                payload_path=str(path),
            ).published_at = utc_now()
            stats.inserted += 1
            successful_details.append(details)
        except Exception as exc:
            logger.warning("Snapshot publication failed: %s", exc)
            row.last_error = str(exc)[:4000]
            if row.attempt_count >= config.publication.dead_letter_after_attempts:
                row.status = OutboxStatus.dead_letter.value
                row.next_attempt_at = None
            else:
                row.status = OutboxStatus.failed.value
                row.next_attempt_at = utc_now() + timedelta(seconds=_backoff(config, row.attempt_count))
            stats.errors += 1
    if stats.inserted:
        set_application_state(session, "last_publication_at", utc_now().isoformat())
    stats.downloaded = len(rows)
    stats.details = {
        "transport": config.publication.transport,
        "published": successful_details,
    }
    return stats
