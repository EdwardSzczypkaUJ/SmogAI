from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from smog_ai.artifacts.datasets import export_operational_data
from smog_ai.collectors.gios import collect_gios
from smog_ai.collectors.imgw import collect_imgw
from smog_ai.collectors.imgw_archive import backfill_imgw_archive
from smog_ai.config import AppConfig
from smog_ai.database.models import RunStatus
from smog_ai.database.repository import add_collection_error, finish_run, start_run, update_run_stage
from smog_ai.documentation import publish_documentation
from smog_ai.domain import StageStats
from smog_ai.hourly.predictor import create_hourly_forecasts
from smog_ai.prediction.predictor import create_forecasts
from smog_ai.prediction.verifier import verify_forecasts
from smog_ai.processing.matching import match_stations
from smog_ai.processing.validation import validate_data
from smog_ai.publishing.publisher import retry_publications
from smog_ai.publishing.snapshot import build_snapshot_stage
from smog_ai.spatial.service import build_spatial_surfaces

logger = logging.getLogger(__name__)
StageFunction = Callable[[Session, AppConfig], StageStats]


def _object_store_stage(session: Session, config: AppConfig) -> StageStats:
    if not config.object_storage.enabled or not config.artifacts.export_after_collection:
        return StageStats(skipped=1, details={"reason": "object_store_export_disabled"})
    return export_operational_data(session, config)


def _documentation_stage(session: Session, config: AppConfig) -> StageStats:
    del session
    return publish_documentation(config)


def _prediction_stage(session: Session, config: AppConfig) -> StageStats:
    if config.hourly_forecasting.enabled:
        return create_hourly_forecasts(session, config)
    return create_forecasts(session, config)


PIPELINE_STAGES: list[tuple[str, StageFunction, bool]] = [
    ("collect_gios", collect_gios, False),
    ("collect_imgw", collect_imgw, False),
    # Archive backfill is opt-in per invocation. It is selected by first-run and
    # maintenance commands but deliberately excluded from the hourly default.
    ("backfill_imgw_archive", backfill_imgw_archive, False),
    ("validate", validate_data, False),
    ("match_stations", match_stations, False),
    # Mandatory cloud hand-off: all newly collected and matched operational data
    # is uploaded before any scheduled training task is allowed to run.
    ("export_object_store", _object_store_stage, False),
    ("verify", verify_forecasts, False),
    ("predict", _prediction_stage, False),
    ("build_spatial_surfaces", build_spatial_surfaces, False),
    ("publish_documentation", _documentation_stage, False),
    ("build_snapshot", build_snapshot_stage, False),
    ("publish", retry_publications, False),
]


def run_pipeline(
    engine: Engine,
    config: AppConfig,
    *,
    run_type: str = "hourly_pipeline",
    stage_names: Collection[str] | None = None,
) -> tuple[str, StageStats, dict[str, Any]]:
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory.begin() as session:
        run = start_run(session, run_type)
        run_id = run.run_id
    total = StageStats()
    stage_results: dict[str, Any] = {}
    selected_stages = [
        stage for stage in PIPELINE_STAGES if stage[0] != "backfill_imgw_archive"
    ]
    if stage_names is not None:
        requested = list(dict.fromkeys(stage_names))
        known = {name for name, _, _ in PIPELINE_STAGES}
        unknown = [name for name in requested if name not in known]
        if unknown:
            raise ValueError(f"Unknown pipeline stages: {', '.join(unknown)}")
        requested_set = set(requested)
        selected_stages = [stage for stage in PIPELINE_STAGES if stage[0] in requested_set]
    for stage_name, function, critical in selected_stages:
        with factory.begin() as session:
            update_run_stage(session, run_id, stage_name)
        try:
            with factory.begin() as session:
                if stage_name == "collect_gios":
                    stats = collect_gios(session, config, run_id=run_id)
                elif stage_name == "collect_imgw":
                    stats = collect_imgw(session, config, run_id=run_id)
                elif stage_name == "backfill_imgw_archive":
                    stats = backfill_imgw_archive(session, config, run_id=run_id)
                elif stage_name == "export_object_store":
                    stats = export_operational_data(session, config, run_id=run_id)
                else:
                    stats = function(session, config)
            total.merge(stats)
            stage_results[stage_name] = {"status": "success", **stats.as_dict()}
        except Exception as exc:
            logger.exception("Pipeline stage failed: %s", stage_name)
            total.errors += 1
            stage_results[stage_name] = {"status": "failed", "error": str(exc)}
            with factory.begin() as session:
                add_collection_error(
                    session,
                    run_id=run_id,
                    source=None,
                    stage=stage_name,
                    error=exc,
                    retryable=not critical,
                )
            if critical:
                break
    status = RunStatus.success.value if total.errors == 0 else RunStatus.partial_success.value
    with factory.begin() as session:
        finish_run(
            session,
            run_id,
            status=status,
            downloaded=total.downloaded,
            inserted=total.inserted,
            skipped=total.skipped,
            warnings=total.warnings,
            errors=total.errors,
            summary=stage_results,
        )
    return run_id, total, stage_results
