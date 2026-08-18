from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import typer
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from smog_ai.air_parameters import (
    WEATHER_TARGETS,
    create_air_parameter_registry,
)
from smog_ai.artifacts.datasets import (
    create_artifact_repository,
    export_hourly_training_frames,
    export_operational_data,
    materialize_hourly_training_frames_from_store,
    materialize_training_frames_from_store,
)
from smog_ai.collectors.gios import GiosCollector, collect_gios
from smog_ai.collectors.gios_history import (
    ALL_VOIVODESHIPS,
    HistoryImportOptions,
    backfill_gios_history,
    gios_history_status,
)
from smog_ai.collectors.imgw import collect_imgw
from smog_ai.collectors.imgw_archive import backfill_imgw_archive
from smog_ai.config import AppConfig, load_config
from smog_ai.data_flow import create_training_data_bridge, data_flow_status
from smog_ai.database.engine import create_db_engine, init_database, session_scope
from smog_ai.database.models import (
    AirMeasurement,
    AirSensor,
    ModelVersion,
    WeatherMeasurement,
)
from smog_ai.documentation import load_documentation_bundle, publish_documentation
from smog_ai.domain import StageStats
from smog_ai.errors import ConfigurationError, DatabaseError, LockUnavailable
from smog_ai.features.builder import build_training_frame
from smog_ai.hourly.audit import audit_latest_hourly_serving_contract
from smog_ai.hourly.predictor import create_hourly_forecasts
from smog_ai.hourly.drift import hourly_drift_status
from smog_ai.hourly.incremental import update_hourly_residual_correctors
from smog_ai.hourly.training_policy import (
    create_training_set_policy,
    resolve_training_profile,
)
from smog_ai.hourly.recovery import (
    audit_hourly_model_artifacts,
    recover_hourly_models_from_object_store,
)
from smog_ai.hourly.resume import (
    RESUME_STAGE_DEFAULT_SECONDS,
    RESUME_STAGE_WEIGHTS,
    resume_hourly_after_failure,
)
from smog_ai.hourly.trainer import create_hourly_model_registry, train_hourly_models
from smog_ai.locking import ProcessLease
from smog_ai.logging_config import configure_logging
from smog_ai.monitoring.backup import create_backup
from smog_ai.monitoring.health import run_healthcheck
from smog_ai.mlops.comparison import export_model_comparison
from smog_ai.mlops.publish import (
    PUBLISH_CONFIRMATION,
    publish_approved_hourly_models,
)
from smog_ai.parameter_catalog import build_weather_parameter_catalog
from smog_ai.pipeline import run_pipeline
from smog_ai.prediction.predictor import create_forecasts
from smog_ai.prediction.verifier import verify_forecasts
from smog_ai.progress import (
    FIRST_RUN_STAGE_DEFAULT_SECONDS,
    FIRST_RUN_STAGE_WEIGHTS,
    ProgressReporter,
    format_progress_text,
    read_progress,
)
from smog_ai.processing.backfill import backfill_gios
from smog_ai.processing.matching import match_stations
from smog_ai.processing.validation import validate_data
from smog_ai.publishing.publisher import retry_publications
from smog_ai.publishing.serving_release import (
    RETENTION_CONFIRMATION,
    inspect_local_serving_release,
    prune_remote_serving_releases,
    publish_local_serving_release,
)
from smog_ai.publishing.snapshot import build_snapshot_stage
from smog_ai.reports.summary import build_report
from smog_ai.reports.freshness import build_freshness_report, write_freshness_report
from smog_ai.range_backfill import (
    RANGE_BACKFILL_STAGE_DEFAULT_SECONDS,
    RANGE_BACKFILL_STAGE_WEIGHTS,
    CoverageAuditor,
    RangeAwareBackfillService,
    resolve_requested_scope,
    run_range_aware_backfill,
)
from smog_ai.spatial.service import build_spatial_surfaces, validate_latest_spatial_surfaces
from smog_ai.training.trainer import train_models
from smog_ai.time_utils import utc_now
from smog_ai.training_snapshot import (
    create_snapshot_engine,
    create_training_snapshot_bridge,
)
from smog_ai.training_delta import (
    CONFIRMATION as TRAINING_DELTA_CONFIRMATION,
    build_delta,
    create_layered_sqlalchemy_engine,
    fast_preflight_candidate,
    layered_candidate_provenance,
    plan_delta,
    verify_candidate as verify_layered_candidate,
)
from smog_ai.training_compaction import (
    COMPACTION_CONFIRMATION as TRAINING_COMPACTION_CONFIRMATION,
    ROLLBACK_CONFIRMATION as TRAINING_COMPACTION_ROLLBACK_CONFIRMATION,
    apply_compaction,
    plan_compaction,
    rollback_compaction,
    verify_compaction,
)

app = typer.Typer(no_args_is_help=True, help="GIOŚ/IMGW Forecast Suite — lokalny pipeline i MLOps.")
EXIT_SUCCESS = 0
EXIT_GENERAL = 1
EXIT_CONFIG = 2
EXIT_DATABASE = 3
EXIT_PARTIAL = 4
EXIT_LOCKED = 5
EXIT_PUBLICATION = 6


def _select_digitalocean_spaces_destination(config: AppConfig) -> dict[str, str]:
    """Select SPACES_* destination even when local runtime overrides are active."""

    values = {
        "bucket": os.getenv("SPACES_BUCKET") or config.object_storage.bucket,
        "region": os.getenv("SPACES_REGION") or config.object_storage.region,
        "endpoint_url": os.getenv("SPACES_ENDPOINT_URL") or config.object_storage.endpoint_url,
        "prefix": os.getenv("SPACES_PREFIX") or config.object_storage.prefix,
    }
    missing = [name for name in ("bucket", "region", "endpoint_url", "prefix") if not values[name]]
    if missing:
        raise ConfigurationError(
            "DigitalOcean destination is incomplete. Missing: " + ", ".join(missing)
        )
    if not str(values["endpoint_url"]).lower().startswith("https://"):
        raise ConfigurationError("SPACES_ENDPOINT_URL must use HTTPS.")
    config.object_storage.backend = "spaces"
    config.object_storage.bucket = str(values["bucket"])
    config.object_storage.region = str(values["region"])
    config.object_storage.endpoint_url = str(values["endpoint_url"]).rstrip("/")
    config.object_storage.prefix = str(values["prefix"]).strip("/ ")
    return {
        "backend": "spaces",
        "bucket": config.object_storage.bucket,
        "region": config.object_storage.region,
        "endpoint": config.object_storage.endpoint_url,
        "prefix": config.object_storage.prefix,
    }


def _runtime(config_path: Path | None, env_path: Path | None, task: str) -> tuple[AppConfig, Engine]:
    config = load_config(config_path, env_path)
    configure_logging(config.paths.logs_dir, task_name=task)
    engine = create_db_engine(config)
    init_database(engine)
    return config, engine


def _render_cli_value(
    value: Any,
    *,
    as_json: bool = True,
    encoding: str | None = None,
) -> str:
    """Render CLI output without crashing on legacy Windows code pages.

    Windows PowerShell 5.1 often exposes cp1250 to a redirected Python
    process.  Symbols used by scientific units (for example superscript 3 in
    ``mg/m³``) are not representable in cp1250.  JSON remains lossless when
    such characters are escaped, so fall back to ``ensure_ascii=True`` only
    when the active stream encoding cannot represent the Unicode payload.
    """

    if isinstance(value, StageStats):
        value = value.as_dict()

    if not as_json:
        rendered = str(value)
        selected_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            rendered.encode(selected_encoding)
        except (LookupError, UnicodeEncodeError):
            return rendered.encode("ascii", "backslashreplace").decode("ascii")
        return rendered

    rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    selected_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        rendered.encode(selected_encoding)
    except (LookupError, UnicodeEncodeError):
        rendered = json.dumps(value, ensure_ascii=True, indent=2, default=str)
    return rendered


def _emit(value: Any, as_json: bool = True) -> None:
    typer.echo(_render_cli_value(value, as_json=as_json))


def _single_stage(
    name: str,
    function: Callable[[Session, AppConfig], Any],
    config_path: Path | None,
    env_path: Path | None,
) -> None:
    try:
        config, engine = _runtime(config_path, env_path, name)
        with session_scope(engine) as session:
            result = function(session, config)
        _emit(result)
        if isinstance(result, StageStats) and result.errors:
            raise typer.Exit(EXIT_PARTIAL)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG) from exc
    except DatabaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_DATABASE) from exc


def _single_stage_with_progress(
    name: str,
    stage: str,
    function: Callable[[Session, AppConfig, ProgressReporter | None], Any],
    config_path: Path | None,
    env_path: Path | None,
    *,
    default_seconds: float,
) -> None:
    reporter: ProgressReporter | None = None
    try:
        config, engine = _runtime(config_path, env_path, name)
        reporter = ProgressReporter(
            config.paths.logs_dir,
            run_type=name,
            stage_weights={stage: 1.0},
            stage_default_seconds={stage: default_seconds},
        ).start(task=f"starting {name}")
        with session_scope(engine) as session:
            result = function(session, config, reporter)
        if isinstance(result, StageStats) and result.errors:
            reporter.finish("partial_success", detail=result.as_dict())
        else:
            reporter.finish("success", detail=(result.as_dict() if isinstance(result, StageStats) else {}))
        _emit(result)
        if isinstance(result, StageStats) and result.errors:
            raise typer.Exit(EXIT_PARTIAL)
    except typer.Exit:
        raise
    except ConfigurationError as exc:
        if reporter is not None:
            reporter.fail(exc)
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG) from exc
    except DatabaseError as exc:
        if reporter is not None:
            reporter.fail(exc)
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_DATABASE) from exc
    except KeyboardInterrupt as exc:
        if reporter is not None:
            reporter.fail("cancelled by user", status="cancelled")
        raise typer.Exit(130) from exc
    except Exception as exc:
        if reporter is not None:
            reporter.fail(exc)
        raise


def _parse_history_pollutants(
    value: str,
    config: AppConfig,
) -> tuple[str, ...]:
    registry = create_air_parameter_registry(config)
    raw = value.strip()
    if not raw or raw.upper() in {"ALL", "WSZYSTKIE"}:
        selected = registry.historical_codes
        if not selected:
            raise typer.BadParameter(
                "No air parameters have historical_backfill enabled"
            )
        return selected

    # Protect decimal comma in PM2,5 before splitting a list.
    prepared = re.sub(r"(?i)PM2\s*,\s*5", "PM2.5", value)
    tokens = [
        item.strip()
        for item in prepared.replace(";", ",").split(",")
        if item.strip()
    ]
    try:
        normalized = registry.normalise_many(tokens, require_configured=True)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    disabled = [
        code
        for code in normalized
        if not registry.require(code).historical_backfill
    ]
    if disabled:
        raise typer.BadParameter(
            "Historical backfill disabled for: " + ", ".join(disabled)
        )
    return normalized


def _parse_hourly_targets(
    value: str | None,
    config: AppConfig,
) -> list[str]:
    if not value:
        return list(config.hourly_forecasting.targets)
    registry = create_air_parameter_registry(config)
    tokens = [
        item.strip()
        for item in value.replace(";", ",").split(",")
        if item.strip()
    ]
    output: list[str] = []
    for token in tokens:
        if token in WEATHER_TARGETS:
            canonical = token
        else:
            canonical = registry.resolve(token)
            definition = registry.get(canonical)
            if definition is None or not definition.forecast_target:
                raise typer.BadParameter(
                    f"Parameter {token!r} is not enabled as a forecast target"
                )
        if canonical not in config.hourly_forecasting.targets:
            raise typer.BadParameter(
                f"Target {canonical!r} is not present in hourly_forecasting.targets"
            )
        if canonical not in output:
            output.append(canonical)
    if not output:
        raise typer.BadParameter("Target list cannot be empty")
    return output


def _parse_history_voivodeships(value: str) -> tuple[str, ...]:
    raw = value.strip()
    if not raw or raw.upper() in {"ALL", "WSZYSTKIE", "POLSKA"}:
        return ALL_VOIVODESHIPS
    tokens = [
        " ".join(item.strip().upper().split())
        for item in raw.replace(";", ",").split(",")
        if item.strip()
    ]
    invalid = [item for item in tokens if item not in ALL_VOIVODESHIPS]
    if invalid:
        raise typer.BadParameter(
            "Nieobsługiwane województwa: "
            + ", ".join(invalid)
            + ". Użyj nazw wielkimi literami albo --voivodeships ALL."
        )
    return tuple(dict.fromkeys(tokens))


COMMON_CONFIG = typer.Option(None, "--config", help="Ścieżka do config.yaml")
COMMON_ENV = typer.Option(None, "--env-file", help="Ścieżka do pliku .env")


@app.command("init-db")
def init_db(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    _, engine = _runtime(config, env_file, "init-db")
    _emit({"status": "ok", "database": str(engine.url)})


@app.command("collect-gios")
def command_collect_gios(
    parameters: str | None = typer.Option(
        None,
        "--parameters",
        help=(
            "Comma-separated configured air parameters. Omitted = roles "
            "with collect_current=true; ALL uses the same configured role."
        ),
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg = load_config(config, env_file)
    registry = create_air_parameter_registry(cfg)
    selected: tuple[str, ...] | None = None
    if parameters and parameters.strip().upper() not in {"ALL", "WSZYSTKIE"}:
        try:
            selected = registry.normalise_many(
                [
                    item.strip()
                    for item in parameters.replace(";", ",").split(",")
                    if item.strip()
                ]
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        disabled = [
            code for code in selected if not registry.require(code).collect_current
        ]
        if disabled:
            raise typer.BadParameter(
                "Current collection disabled for: " + ", ".join(disabled)
            )

    def stage(
        session: Session,
        stage_config: AppConfig,
        reporter: ProgressReporter | None,
    ) -> StageStats:
        return collect_gios(
            session,
            stage_config,
            parameters=selected,
            progress=reporter,
        )

    _single_stage_with_progress(
        "collect-gios",
        "collection",
        stage,
        config,
        env_file,
        default_seconds=900.0,
    )


@app.command("probe-gios")
def command_probe_gios(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Check the live GIOŚ JSON-LD contract without changing the database."""
    cfg = load_config(config, env_file)
    configure_logging(cfg.paths.logs_dir, task_name="probe-gios")
    collector = GiosCollector(cfg)
    try:
        _emit(collector.probe())
    finally:
        collector.close()


@app.command("collect-imgw")
def command_collect_imgw(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    _single_stage("collect-imgw", collect_imgw, config, env_file)


@app.command("backfill-imgw-archive")
def command_backfill_imgw_archive(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Import official monthly terminowe/SYNOP archives idempotently."""
    _single_stage(
        "backfill-imgw-archive",
        backfill_imgw_archive,
        config,
        env_file,
    )


@app.command("backfill-gios-history")
def command_backfill_gios_history(
    start_year: int = typer.Option(
        2022,
        "--start-year",
        min=2000,
        help="Pierwszy rok historii GIOŚ (włącznie).",
    ),
    end_year: int = typer.Option(
        datetime.now(UTC).year,
        "--end-year",
        min=2000,
        help="Ostatni rok historii GIOŚ (włącznie).",
    ),
    source: str = typer.Option(
        "auto",
        "--source",
        help="auto, prepared (roczne ZIP-y) albo api (API rok/województwo).",
    ),
    pollutants: str = typer.Option(
        "ALL",
        "--pollutants",
        help="Lista skonfigurowanych parametrów albo ALL.",
    ),
    voivodeships: str = typer.Option(
        "ALL",
        "--voivodeships",
        help="ALL albo lista województw rozdzielona przecinkami.",
    ),
    request_interval_seconds: float = typer.Option(
        31.0,
        "--request-interval-seconds",
        min=30.0,
        help="Odstęp dla API archiwalnego; oficjalny limit to 2 żądania/min.",
    ),
    page_size: int = typer.Option(
        500,
        "--page-size",
        min=1,
        max=500,
    ),
    max_pages_per_combination: int = typer.Option(
        0,
        "--max-pages-per-combination",
        min=0,
        help="0 = bez limitu; wartość dodatnia służy do krótkiego pilota API.",
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--no-resume",
        help="Wznawiaj na podstawie cache i state.json.",
    ),
    refresh_cache: bool = typer.Option(
        False,
        "--refresh-cache",
        help="Pobierz ponownie pliki/strony mimo istniejącego cache.",
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help="Opcjonalny katalog cache; domyślnie runtime/tmp/gios-history-cache.",
    ),
    cache_mode: str | None = typer.Option(
        None,
        "--cache-mode",
        help=(
            "local, object_store albo hybrid. Brak wartości używa "
            "data_flow.history_cache_mode z config.yaml."
        ),
    ),
    cache_prefix: str | None = typer.Option(
        None,
        "--cache-prefix",
        help=(
            "Prefiks cache w ObjectStore/Spaces; brak wartości używa "
            "data_flow.history_cache_prefix."
        ),
    ),
    insert_batch_size: int = typer.Option(
        20_000,
        "--insert-batch-size",
        min=100,
        max=100_000,
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Import configured official historical hourly air parameters from GIOŚ.

    ``source=auto`` uses prepared national annual ZIP files where available
    (currently through 2024) and the official rate-limited annual API for newer
    years. The operation is idempotent, cached and resumable.
    """

    normalized_source = source.strip().lower()
    if normalized_source not in {"auto", "prepared", "api"}:
        raise typer.BadParameter("--source musi mieć wartość auto, prepared albo api")
    normalized_cache_mode = (
        cache_mode.strip().lower()
        if cache_mode is not None
        else None
    )
    if normalized_cache_mode not in {
        None,
        "local",
        "object_store",
        "hybrid",
    }:
        raise typer.BadParameter(
            "--cache-mode musi mieć wartość local, object_store albo hybrid"
        )
    cfg, engine = _runtime(config, env_file, "backfill-gios-history")
    options = HistoryImportOptions(
        start_year=start_year,
        end_year=end_year,
        source=normalized_source,  # type: ignore[arg-type]
        pollutants=_parse_history_pollutants(pollutants, cfg),
        voivodeships=_parse_history_voivodeships(voivodeships),
        request_interval_seconds=request_interval_seconds,
        page_size=page_size,
        max_pages_per_combination=max_pages_per_combination,
        resume=resume,
        refresh_cache=refresh_cache,
        cache_dir=cache_dir,
        cache_mode=normalized_cache_mode,  # type: ignore[arg-type]
        cache_prefix=cache_prefix,
        insert_batch_size=insert_batch_size,
    )
    try:
        with ProcessLease(engine, cfg, "gios-history-backfill"):
            with session_scope(engine) as session:
                result = backfill_gios_history(session, cfg, options)
        _emit(result)
        if result.errors:
            raise typer.Exit(EXIT_PARTIAL)
    except LockUnavailable as exc:
        _emit({"status": "skipped_locked", "message": str(exc)})
        raise typer.Exit(EXIT_LOCKED) from exc


@app.command("gios-history-status")
def command_gios_history_status(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Show configured air-parameter history coverage and one-year gates."""
    cfg, engine = _runtime(config, env_file, "gios-history-status")
    with session_scope(engine) as session:
        status = gios_history_status(session, cfg)
    _emit(
        {
            "status": "ok",
            "database": str(engine.url),
            "parameters": status,
            "all_parameters_have_365_days": all(
                bool(item.get("production_training_ready"))
                for item in status.values()
            ),
        }
    )



@app.command("data-range-audit")
def command_data_range_audit(
    start: str | None = typer.Option(None, "--start"),
    end: str | None = typer.Option(None, "--end"),
    parameters: str | None = typer.Option(
        None,
        "--parameters",
        help="Brak = parametry z audit-package, a bez audit-package wszystkie obsługiwane.",
    ),
    audit_package: Path | None = typer.Option(
        None,
        "--audit-package",
        help="ZIP/katalog/JSON z wcześniejszego audytu; zakres jest zawsze weryfikowany ponownie w SQLite.",
    ),
    minimum_air_stations: int | None = typer.Option(
        None,
        "--minimum-air-stations",
        min=1,
        help="Brak = próg z audit-package, a bez pakietu 1.",
    ),
    minimum_weather_stations: int | None = typer.Option(
        None,
        "--minimum-weather-stations",
        min=1,
        help="Brak = próg z audit-package, a bez pakietu 1.",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Recompute source-level coverage with precipitation's six-hour cadence."""

    cfg, engine = _runtime(config, env_file, "data-range-audit")
    requested, air, weather, metadata = resolve_requested_scope(
        cfg,
        start=start,
        end=end,
        parameters=parameters,
        audit_package=audit_package,
    )
    effective_air_stations = int(
        minimum_air_stations
        or metadata.get("audit_minimum_air_stations")
        or 1
    )
    effective_weather_stations = int(
        minimum_weather_stations
        or metadata.get("audit_minimum_weather_stations")
        or 1
    )
    with session_scope(engine) as session:
        auditor = CoverageAuditor(
            session,
            display_timezone=cfg.display_timezone,
            precipitation_cadence_hours=(
                cfg.imgw_archive.precipitation_accumulation_period_hours
            ),
        )
        report = auditor.audit(
            requested,
            air_parameters=air,
            weather_parameters=weather,
            minimum_air_stations=effective_air_stations,
            minimum_weather_stations=effective_weather_stations,
        )
    _emit(
        {
            "status": "ok",
            "scope": {
                **metadata,
                "effective_minimum_air_stations": effective_air_stations,
                "effective_minimum_weather_stations": effective_weather_stations,
            },
            "coverage": report.to_dict(),
        }
    )


@app.command("plan-missing-ranges")
def command_plan_missing_ranges(
    start: str | None = typer.Option(None, "--start"),
    end: str | None = typer.Option(None, "--end"),
    parameters: str | None = typer.Option(
        None,
        "--parameters",
        help="Brak = parametry z audit-package, a bez audit-package wszystkie obsługiwane.",
    ),
    audit_package: Path | None = typer.Option(None, "--audit-package"),
    minimum_air_stations: int | None = typer.Option(
        None,
        "--minimum-air-stations",
        min=1,
    ),
    minimum_weather_stations: int | None = typer.Option(
        None,
        "--minimum-weather-stations",
        min=1,
    ),
    cache_mode: str | None = typer.Option(
        None,
        "--cache-mode",
        help="local, object_store lub hybrid; brak używa DataFlow Bridge z config.yaml.",
    ),
    include_isolated_gaps: bool = typer.Option(
        False,
        "--include-isolated-gaps/--ignore-isolated-gaps",
    ),
    minimum_historical_gap_hours: int = typer.Option(
        2,
        "--minimum-historical-gap-hours",
        min=1,
        max=168,
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Build a source-aware plan without downloading or modifying data."""

    cfg, engine = _runtime(config, env_file, "plan-missing-ranges")
    requested, air, weather, metadata = resolve_requested_scope(
        cfg,
        start=start,
        end=end,
        parameters=parameters,
        audit_package=audit_package,
    )
    effective_air_stations = int(
        minimum_air_stations
        or metadata.get("audit_minimum_air_stations")
        or 1
    )
    effective_weather_stations = int(
        minimum_weather_stations
        or metadata.get("audit_minimum_weather_stations")
        or 1
    )
    with session_scope(engine) as session:
        service = RangeAwareBackfillService(
            session,
            cfg,
            cache_mode=cache_mode,
            minimum_air_stations=effective_air_stations,
            minimum_weather_stations=effective_weather_stations,
            include_isolated_gaps=include_isolated_gaps,
            minimum_historical_gap_hours=minimum_historical_gap_hours,
        )
        coverage = service.audit(
            requested,
            air_parameters=air,
            weather_parameters=weather,
        )
        plan = service.build_plan(coverage)
    _emit(
        {
            "status": "ok",
            "scope": {
                **metadata,
                "effective_minimum_air_stations": effective_air_stations,
                "effective_minimum_weather_stations": effective_weather_stations,
            },
            "plan": plan.to_dict(),
        }
    )


@app.command("fill-missing-ranges")
def command_fill_missing_ranges(
    start: str | None = typer.Option(None, "--start"),
    end: str | None = typer.Option(None, "--end"),
    parameters: str | None = typer.Option(
        None,
        "--parameters",
        help="Brak = parametry z audit-package, a bez audit-package wszystkie obsługiwane.",
    ),
    audit_package: Path | None = typer.Option(
        None,
        "--audit-package",
        help="ZIP/katalog/JSON z zakresem. Pokrycie jest sprawdzane ponownie przed każdą akcją.",
    ),
    minimum_air_stations: int | None = typer.Option(
        None,
        "--minimum-air-stations",
        min=1,
        help="Brak = próg z audit-package, a bez pakietu 1.",
    ),
    minimum_weather_stations: int | None = typer.Option(
        None,
        "--minimum-weather-stations",
        min=1,
        help="Brak = próg z audit-package, a bez pakietu 1.",
    ),
    cache_mode: str | None = typer.Option(
        None,
        "--cache-mode",
        help="local, object_store lub hybrid.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    include_isolated_gaps: bool = typer.Option(
        False,
        "--include-isolated-gaps/--ignore-isolated-gaps",
    ),
    minimum_historical_gap_hours: int = typer.Option(
        2,
        "--minimum-historical-gap-hours",
        min=1,
        max=168,
    ),
    max_no_progress_attempts: int = typer.Option(
        2,
        "--max-no-progress-attempts",
        min=1,
        max=10,
    ),
    max_actions: int = typer.Option(
        0,
        "--max-actions",
        min=0,
        help="0 = cały plan; wartość dodatnia służy do pilota.",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Fill only fresh SQLite gaps using the configured cache/storage Bridge."""

    reporter: ProgressReporter | None = None
    try:
        cfg, engine = _runtime(config, env_file, "fill-missing-ranges")
        normalized_cache = (
            cache_mode.strip().lower() if cache_mode is not None else None
        )
        if normalized_cache not in {None, "local", "object_store", "hybrid"}:
            raise typer.BadParameter(
                "--cache-mode musi mieć wartość local, object_store albo hybrid"
            )
        requested, air, weather, metadata = resolve_requested_scope(
            cfg,
            start=start,
            end=end,
            parameters=parameters,
            audit_package=audit_package,
        )
        effective_air_stations = int(
            minimum_air_stations
            or metadata.get("audit_minimum_air_stations")
            or 1
        )
        effective_weather_stations = int(
            minimum_weather_stations
            or metadata.get("audit_minimum_weather_stations")
            or 1
        )
        metadata = {
            **metadata,
            "effective_minimum_air_stations": effective_air_stations,
            "effective_minimum_weather_stations": effective_weather_stations,
        }
        reporter = ProgressReporter(
            cfg.paths.logs_dir,
            run_type="range-backfill",
            stage_weights=RANGE_BACKFILL_STAGE_WEIGHTS,
            stage_default_seconds=RANGE_BACKFILL_STAGE_DEFAULT_SECONDS,
        ).start(task="starting range-aware backfill")
        with ProcessLease(engine, cfg, "range-aware-backfill"):
            with session_scope(engine) as session:
                result = run_range_aware_backfill(
                    session,
                    cfg,
                    requested=requested,
                    air_parameters=air,
                    weather_parameters=weather,
                    cache_mode=normalized_cache,
                    dry_run=dry_run,
                    include_isolated_gaps=include_isolated_gaps,
                    minimum_historical_gap_hours=minimum_historical_gap_hours,
                    max_no_progress_attempts=max_no_progress_attempts,
                    max_actions=max_actions,
                    minimum_air_stations=effective_air_stations,
                    minimum_weather_stations=effective_weather_stations,
                    progress=reporter,
                )
        final_status = str(result.get("status") or "success")
        reporter.finish(final_status, detail={"scope": metadata, **result})
        _emit({"scope": metadata, **result})
        if final_status == "partial_success":
            raise typer.Exit(EXIT_PARTIAL)
    except typer.Exit:
        raise
    except LockUnavailable as exc:
        if reporter is not None:
            reporter.fail(str(exc), status="skipped_locked")
        _emit({"status": "skipped_locked", "message": str(exc)})
        raise typer.Exit(EXIT_LOCKED) from exc
    except KeyboardInterrupt as exc:
        if reporter is not None:
            reporter.fail("cancelled by user", status="cancelled")
        raise typer.Exit(130) from exc
    except Exception as exc:
        if reporter is not None:
            reporter.fail(exc)
        raise


@app.command("collect-all")
def command_collect_all(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    def both(session: Session, cfg: AppConfig) -> StageStats:
        return collect_gios(session, cfg).merge(collect_imgw(session, cfg))
    _single_stage("collect-all", both, config, env_file)


@app.command("validate")
def command_validate(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    _single_stage("validate", validate_data, config, env_file)


@app.command("match-stations")
def command_match(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    _single_stage("match-stations", match_stations, config, env_file)


@app.command("build-features")
def command_features(
    parameter: str = typer.Option("PM10"),
    horizon: int = typer.Option(24),
    output: Path | None = typer.Option(None),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, engine = _runtime(config, env_file, "build-features")
    with session_scope(engine) as session:
        frame = build_training_frame(session, parameter=parameter, horizon_hours=horizon, max_days=cfg.training.max_training_days)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(output, index=False)
    _emit({"rows": len(frame), "columns": list(frame.columns), "output": str(output) if output else None})


@app.command("list-model-methods")
def command_list_model_methods(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """List built-in and externally discovered model providers."""
    cfg = load_config(config, env_file)
    registry = create_hourly_model_registry(cfg)
    _emit({
        "entry_point_group": cfg.model_platform.entry_point_group,
        "providers": registry.describe(),
        "plugin_modules": cfg.model_platform.plugin_modules,
    })


@app.command("publish-documentation")
def command_publish_documentation(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg = load_config(config, env_file)
    configure_logging(cfg.paths.logs_dir, task_name="publish-documentation")
    _emit(publish_documentation(cfg))


@app.command("documentation-preflight")
def command_documentation_preflight(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Resolve every documentation source before expensive model work starts."""
    cfg = load_config(config, env_file)
    configure_logging(cfg.paths.logs_dir, task_name="documentation-preflight")
    bundle = load_documentation_bundle(cfg)
    _emit({"status": "ok", "metadata": bundle.metadata})


@app.command("audit-hourly-models")
def command_audit_hourly_models(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Inspect DB, Spaces versioned artifacts and local joblib files without changes."""
    cfg, engine = _runtime(config, env_file, "audit-hourly-models")
    with session_scope(engine) as session:
        result = audit_hourly_model_artifacts(session, cfg)
    _emit(result)
    if not bool(result.get("all_targets_recoverable")):
        raise typer.Exit(EXIT_PARTIAL)


@app.command("recover-hourly-models")
def command_recover_hourly_models(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Recover models from pointers, versioned Spaces objects or local joblib files."""
    _single_stage(
        "recover-hourly-models",
        recover_hourly_models_from_object_store,
        config,
        env_file,
    )


@app.command("resume-hourly-after-failure")
def command_resume_hourly_after_failure(
    retrain_if_missing: bool = typer.Option(
        False,
        "--retrain-if-missing/--no-retrain-if-missing",
        help=(
            "Jeżeli nie ma kompletu lokalnych/wersjonowanych artefaktów, "
            "powtórz wyłącznie budowę ramek, trening i etapy downstream."
        ),
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Resume after late failure; recover models first and avoid collection/backfill."""

    reporter: ProgressReporter | None = None
    try:
        cfg, engine = _runtime(config, env_file, "resume-hourly-after-failure")
        reporter = ProgressReporter(
            cfg.paths.logs_dir,
            run_type="resume-hourly",
            stage_weights=RESUME_STAGE_WEIGHTS,
            stage_default_seconds=RESUME_STAGE_DEFAULT_SECONDS,
        ).start(task="starting hourly recovery/resume")
        with ProcessLease(engine, cfg, "resume-hourly-after-failure"):
            result = resume_hourly_after_failure(
                engine,
                cfg,
                retrain_if_missing=retrain_if_missing,
                progress=reporter,
            )
        status = "partial_success" if result.errors else "success"
        reporter.finish(status, detail=result.as_dict())
        _emit(result)
        if result.errors:
            raise typer.Exit(EXIT_PARTIAL)
    except typer.Exit:
        raise
    except LockUnavailable as exc:
        if reporter is not None:
            reporter.fail(str(exc), status="skipped_locked")
        _emit({"status": "skipped_locked", "message": str(exc)})
        raise typer.Exit(EXIT_LOCKED) from exc
    except KeyboardInterrupt as exc:
        if reporter is not None:
            reporter.fail("cancelled by user", status="cancelled")
        raise typer.Exit(130) from exc
    except Exception as exc:
        if reporter is not None:
            reporter.fail(exc)
        raise


def _parameter_catalog_payload(cfg: AppConfig, engine: Engine) -> dict[str, Any]:
    registry = create_air_parameter_registry(cfg)
    with session_scope(engine) as session:
        sensor_rows = session.execute(
            select(
                AirSensor.parameter_code,
                func.count(AirSensor.id),
            ).group_by(AirSensor.parameter_code)
        ).all()
        sensor_counts = {
            str(code): int(count or 0) for code, count in sensor_rows if code
        }

        measurement_rows = session.execute(
            select(
                AirMeasurement.parameter,
                func.count(AirMeasurement.id),
                func.min(AirMeasurement.measurement_time),
                func.max(AirMeasurement.measurement_time),
                func.count(func.distinct(AirMeasurement.air_station_id)),
                func.count(func.distinct(AirMeasurement.measurement_time)),
            ).group_by(AirMeasurement.parameter)
        ).all()
        measurement_stats = {
            str(parameter): {
                "rows": int(count or 0),
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "stations": int(stations or 0),
                "unique_hours": int(hours or 0),
            }
            for parameter, count, start, end, stations, hours in measurement_rows
            if parameter
        }

        model_rows = session.scalars(
            select(ModelVersion).where(
                ModelVersion.forecast_horizon == 0,
                ModelVersion.active.is_(True),
            )
        ).all()
        active_models = {
            row.parameter: {
                "provider": row.algorithm,
                "version": row.semantic_version,
                "metrics": row.metrics_json or {},
            }
            for row in model_rows
        }

        weather_parameters = build_weather_parameter_catalog(
            session,
            cfg,
            active_models=active_models,
        )

    configured = registry.to_dict()
    parameters: dict[str, Any] = {}
    for code, definition in configured["parameters"].items():
        parameters[code] = {
            **definition,
            "source": "GIOS",
            "sensor_count": sensor_counts.get(code, 0),
            "measurements": {
                "rows": 0,
                "start": None,
                "end": None,
                "stations": 0,
                "unique_hours": 0,
                **measurement_stats.get(code, {}),
            },
            "active_model": active_models.get(code),
        }

    unknown_sensors = {
        code: count
        for code, count in sensor_counts.items()
        if code not in parameters
    }
    unknown_measurements = {
        code: values
        for code, values in measurement_stats.items()
        if code not in parameters
    }
    return {
        "status": "ok",
        "unknown_sensor_policy": registry.unknown_policy,
        "roles": configured["roles"],
        "parameters": parameters,
        "weather_parameters": weather_parameters,
        "hourly_targets": list(cfg.hourly_forecasting.targets),
        "spatial_targets": list(cfg.hourly_forecasting.spatial_targets),
        "unconfigured_sensor_catalog": unknown_sensors,
        "unconfigured_measurements": unknown_measurements,
    }


@app.command("parameter-catalog")
def command_parameter_catalog(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Show air pollutants, IMGW weather variables, roles, coverage and models."""

    cfg, engine = _runtime(config, env_file, "parameter-catalog")
    _emit(_parameter_catalog_payload(cfg, engine))


@app.command("air-parameter-catalog")
def command_air_parameter_catalog(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Backward-compatible alias for the unified parameter catalog."""

    cfg, engine = _runtime(config, env_file, "air-parameter-catalog")
    _emit(_parameter_catalog_payload(cfg, engine))


@app.command("data-flow-status")
def command_data_flow_status(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Show the effective local/ObjectStore Bridge configuration."""
    cfg = load_config(config, env_file)
    _emit(data_flow_status(cfg))


@app.command("build-hourly-features")
def command_build_hourly_features(
    source: str = typer.Option(
        "auto",
        help="auto, database albo object_store",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="quick/full; brak oznacza profil domyślny",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    if source not in {"auto", "database", "object_store"}:
        raise typer.BadParameter(
            "source must be auto, database or object_store"
        )
    if profile is not None and profile not in {"quick", "full"}:
        raise typer.BadParameter("profile must be quick or full")

    def stage(session: Session, cfg: AppConfig) -> StageStats:
        if profile is not None:
            cfg.hourly_forecasting.training_policy.default_profile = profile
        if source == "auto":
            bridge = create_training_data_bridge(cfg)
            return bridge.prepare(session, cfg)
        return export_hourly_training_frames(
            session,
            cfg,
            source=source,  # type: ignore[arg-type]
            profile_name=profile,
        )

    _single_stage("build-hourly-features", stage, config, env_file)



@app.command("create-training-snapshot")
def command_create_training_snapshot(
    profile: str = typer.Option(
        "quick",
        "--profile",
        help="Profil snapshotu: quick albo full.",
    ),
    targets: str | None = typer.Option(
        None,
        "--targets",
        help="Opcjonalna lista celów zapisywana w manifeście datasetu.",
    ),
    mirror_manifest: bool = typer.Option(
        True,
        "--mirror-manifest/--no-mirror-manifest",
        help="Opublikuj mały manifest datasetu do ObjectStore/Spaces.",
    ),
    training_start: datetime | None = typer.Option(
        None,
        "--training-start",
        help="Włączny początek danych treningowych w ISO 8601 z timezone.",
    ),
    training_end: datetime | None = typer.Option(
        None,
        "--training-end",
        help="Wyłączny koniec danych treningowych w ISO 8601 z timezone.",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Create an immutable SQLite dataset while live ingestion may continue."""

    selected = profile.strip().lower()
    if selected not in {"quick", "full"}:
        raise typer.BadParameter("profile must be quick or full")

    reporter: ProgressReporter | None = None
    try:
        cfg, engine = _runtime(config, env_file, "create-training-snapshot")
        if not cfg.training_snapshot.enabled:
            raise ConfigurationError("training_snapshot.enabled is false")
        selected_targets = _parse_hourly_targets(targets, cfg)
        reporter = ProgressReporter(
            cfg.paths.logs_dir,
            run_type=f"create-training-snapshot-{selected}",
            stage_weights={"snapshot": 1.0},
            stage_default_seconds={"snapshot": 300.0},
        ).start(task="creating immutable training dataset")
        with ProcessLease(
            engine,
            cfg,
            "training-snapshot-create",
            heartbeat_enabled=False,
        ):
            snapshot = create_training_snapshot_bridge(cfg).create(
                profile=selected,
                targets=selected_targets,
                progress=reporter,
                mirror_manifest=mirror_manifest,
                training_start=training_start,
                training_end=training_end,
            )
        reporter.finish("success", detail=snapshot.as_dict())
        _emit({"status": "ok", "training_snapshot": snapshot.as_dict()})
    except typer.Exit:
        raise
    except LockUnavailable as exc:
        if reporter is not None:
            reporter.fail(str(exc), status="skipped_locked")
        _emit({"status": "skipped_locked", "message": str(exc)})
        raise typer.Exit(EXIT_LOCKED) from exc
    except KeyboardInterrupt as exc:
        if reporter is not None:
            reporter.fail("cancelled by user", status="cancelled")
        raise typer.Exit(130) from exc
    except Exception as exc:
        if reporter is not None:
            reporter.fail(exc)
        raise


@app.command("training-snapshot-status")
def command_training_snapshot_status(
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Opcjonalnie ogranicz wynik do profilu quick albo full.",
    ),
    verify_checksum: bool = typer.Option(
        False,
        "--verify-checksum/--no-verify-checksum",
        help="Przelicz SHA-256 plików baz danych (wolniejsze).",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """List immutable training datasets and their exact provenance."""

    cfg = load_config(config, env_file)
    bridge = create_training_snapshot_bridge(cfg)
    selected_profile = profile.strip().lower() if profile else None
    if selected_profile not in {None, "quick", "full"}:
        raise typer.BadParameter("profile must be quick or full")

    rows: list[dict[str, Any]] = []
    for snapshot in bridge.list(profile=selected_profile):
        item = snapshot.as_dict()
        item["database_exists"] = snapshot.database_path.exists()
        item["validation"] = None
        if snapshot.database_path.exists():
            try:
                item["validation"] = bridge.validate(
                    snapshot,
                    verify_checksum=verify_checksum,
                )
            except Exception as exc:
                item["validation"] = {
                    "valid": False,
                    "error": str(exc),
                }
        rows.append(item)

    latest: dict[str, Any] = {}
    for name in ("quick", "full"):
        if selected_profile is not None and selected_profile != name:
            continue
        try:
            latest[name] = bridge.latest(name).as_dict()
        except FileNotFoundError:
            latest[name] = None

    _emit(
        {
            "status": "ok",
            "root_dir": str(cfg.training_snapshot.root_dir),
            "default_selector": cfg.training_snapshot.default_selector,
            "latest": latest,
            "snapshots": rows,
        }
    )


@app.command("training-delta-plan")
def command_training_delta_plan(
    profile: str = typer.Option("quick", "--profile"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Plan the next incremental training layer without modifying data."""

    cfg = load_config(config, env_file)
    runtime_root = cfg.paths.data_dir.expanduser().resolve().parent
    payload = plan_delta(runtime_root=runtime_root, profile=profile)
    _emit(payload)
    if payload.get("compaction_due"):
        raise typer.Exit(EXIT_CONFIG)


@app.command("training-delta-build")
def command_training_delta_build(
    profile: str = typer.Option("quick", "--profile"),
    confirmation: str = typer.Option(..., "--confirmation"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Atomically build a delta; never change the production snapshot pointer."""

    cfg = load_config(config, env_file)
    runtime_root = cfg.paths.data_dir.expanduser().resolve().parent
    payload = build_delta(
        runtime_root=runtime_root,
        profile=profile,
        confirmation=confirmation,
    )
    _emit(payload)


@app.command("training-delta-verify")
def command_training_delta_verify(
    profile: str = typer.Option("quick", "--profile"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Verify base + deltas before model training."""

    cfg = load_config(config, env_file)
    runtime_root = cfg.paths.data_dir.expanduser().resolve().parent
    payload = verify_layered_candidate(
        runtime_root=runtime_root,
        profile=profile,
    )
    _emit(payload)
    if payload.get("candidate_ready_for_training_integration") is not True:
        raise typer.Exit(EXIT_CONFIG)


@app.command("training-delta-preflight")
def command_training_delta_preflight(
    profile: str = typer.Option("quick", "--profile"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Quickly verify delta hashes and the live change-journal boundary."""

    cfg = load_config(config, env_file)
    runtime_root = cfg.paths.data_dir.expanduser().resolve().parent
    payload = fast_preflight_candidate(
        runtime_root=runtime_root,
        profile=profile,
    )
    _emit(payload)
    if payload.get("status") != "ready":
        raise typer.Exit(EXIT_CONFIG)


@app.command("training-compaction-plan")
def command_training_compaction_plan(
    profile: str = typer.Option("quick", "--profile"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Plan base + delta compaction without modifying any file."""

    cfg = load_config(config, env_file)
    runtime_root = cfg.paths.data_dir.expanduser().resolve().parent
    payload = plan_compaction(runtime_root=runtime_root, profile=profile)
    _emit(payload)
    if payload.get("status") != "ready":
        raise typer.Exit(EXIT_CONFIG)


@app.command("training-compaction-apply")
def command_training_compaction_apply(
    profile: str = typer.Option("quick", "--profile"),
    confirmation: str = typer.Option(..., "--confirmation"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Build, verify and atomically activate a compacted snapshot."""

    cfg = load_config(config, env_file)
    runtime_root = cfg.paths.data_dir.expanduser().resolve().parent
    _emit(
        apply_compaction(
            runtime_root=runtime_root,
            profile=profile,
            confirmation=confirmation,
        )
    )


@app.command("training-compaction-verify")
def command_training_compaction_verify(
    profile: str = typer.Option("quick", "--profile"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Verify the active compacted generation and retained recovery assets."""

    cfg = load_config(config, env_file)
    runtime_root = cfg.paths.data_dir.expanduser().resolve().parent
    payload = verify_compaction(runtime_root=runtime_root, profile=profile)
    _emit(payload)
    if payload.get("status") != "ok":
        raise typer.Exit(EXIT_CONFIG)


@app.command("training-compaction-rollback")
def command_training_compaction_rollback(
    profile: str = typer.Option("quick", "--profile"),
    confirmation: str = typer.Option(..., "--confirmation"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Restore the previous pointer if no post-compaction changes exist."""

    cfg = load_config(config, env_file)
    runtime_root = cfg.paths.data_dir.expanduser().resolve().parent
    _emit(
        rollback_compaction(
            runtime_root=runtime_root,
            profile=profile,
            confirmation=confirmation,
        )
    )


def _run_snapshot_training(
    *,
    selected: str,
    targets: str | None,
    snapshot_selector: str,
    candidate_only: bool,
    config: Path | None,
    env_file: Path | None,
) -> None:
    reporter: ProgressReporter | None = None
    snapshot_engine: Engine | None = None
    try:
        cfg, live_engine = _runtime(
            config,
            env_file,
            f"snapshot-train-hourly-{selected}",
        )
        cfg.hourly_forecasting.targets = _parse_hourly_targets(targets, cfg)
        if candidate_only:
            # Candidate-only is a local registry experiment.  It must never
            # upload a model, publish a pointer or create a remote MLflow run.
            cfg.object_storage.enabled = False
            cfg.artifacts.upload_models = False
            cfg.mlflow.enabled = False
            cfg.mlflow.strict = False
            cfg.mlflow.publish_comparison_to_object_storage = False
        selector = (snapshot_selector or cfg.training_snapshot.default_selector).strip()
        if not selector:
            selector = cfg.training_snapshot.default_selector
        if selected not in {"quick", "full"}:
            raise typer.BadParameter("profile must be quick or full")
        if not cfg.training_snapshot.enabled and selector != "live":
            raise ConfigurationError(
                "training_snapshot.enabled is false; use --snapshot live only for diagnostics"
            )

        reporter = ProgressReporter(
            cfg.paths.logs_dir,
            run_type=f"snapshot-train-hourly-{selected}",
            stage_weights={"snapshot": 0.10, "training": 0.90},
            stage_default_seconds={
                "snapshot": 300.0,
                "training": 1_800.0 if selected == "quick" else 7_200.0,
            },
        ).start(task="preparing immutable training dataset")

        bridge = create_training_snapshot_bridge(cfg)
        layered = selector.lower() == "layered"
        snapshot = None if layered else bridge.resolve(selected, selector)
        snapshot_created = False

        layered_provenance: dict[str, Any] | None = None
        if layered:
            runtime_root = cfg.paths.data_dir.expanduser().resolve().parent
            verification = verify_layered_candidate(
                runtime_root=runtime_root,
                profile=selected,
            )
            if verification.get("candidate_ready_for_training_integration") is not True:
                raise RuntimeError(
                    "Layered training candidate failed verification: "
                    + json.dumps(verification, ensure_ascii=False, default=str)
                )
            layered_provenance = layered_candidate_provenance(
                runtime_root=runtime_root,
                profile=selected,
            )

        # ProcessLease normally renews its row in process_locks.  That is a
        # database write, so it must not run while sqlite3_backup() copies the
        # same live database.  The quiet lease keeps the OS mutex and owner row
        # but starts no heartbeat thread during the copy.
        if not layered and selector != "live" and snapshot is None:
            with ProcessLease(
                live_engine,
                cfg,
                "training-snapshot-create",
                heartbeat_enabled=False,
            ):
                snapshot = bridge.create(
                    profile=selected,
                    targets=cfg.hourly_forecasting.targets,
                    progress=reporter,
                )
                snapshot_created = True

        # Normal renewable locking starts only after the immutable copy exists.
        # Its writes can no longer restart the SQLite backup.
        with ProcessLease(live_engine, cfg, "snapshot-hourly-training"):
            if layered:
                if layered_provenance is None:  # pragma: no cover
                    raise RuntimeError("Layered candidate provenance was not resolved")
                reporter.complete_stage(
                    "snapshot",
                    task=(
                        "using verified layered dataset "
                        f"{layered_provenance['dataset_id']}"
                    ),
                    detail=layered_provenance,
                )
                cfg.training.input_source = "database"
                snapshot_engine = create_layered_sqlalchemy_engine(
                    runtime_root=cfg.paths.data_dir.expanduser().resolve().parent,
                    profile=selected,
                )
                with session_scope(live_engine) as live_session:
                    with session_scope(snapshot_engine) as training_session:
                        result = train_hourly_models(
                            live_session,
                            cfg,
                            reporter,
                            profile_name=selected,
                            training_session=training_session,
                            dataset_provenance=layered_provenance,
                            commit_live_metadata=True,
                            activation_policy=(
                                "candidate_only"
                                if candidate_only
                                else "quality_gated"
                            ),
                        )
                snapshot_payload = layered_provenance
            elif selector == "live":
                reporter.complete_stage(
                    "snapshot",
                    task="diagnostic live-database training selected",
                    detail={"selector": "live", "immutable": False},
                )
                with session_scope(live_engine) as live_session:
                    result = train_hourly_models(
                        live_session,
                        cfg,
                        reporter,
                        profile_name=selected,
                        dataset_provenance={
                            "dataset_id": "live",
                            "profile": selected,
                            "immutable": False,
                            "database_path": str(cfg.paths.database_path),
                        },
                        activation_policy=(
                            "candidate_only" if candidate_only else "quality_gated"
                        ),
                    )
                snapshot_payload: dict[str, Any] | None = None
            else:
                if snapshot is None:  # pragma: no cover - defensive invariant
                    raise RuntimeError("Immutable training snapshot was not resolved")
                if not snapshot_created:
                    reporter.complete_stage(
                        "snapshot",
                        task=f"using immutable dataset {snapshot.dataset_id}",
                        detail=snapshot.as_dict(),
                    )

                cfg.training.input_source = "database"
                snapshot_engine = create_snapshot_engine(snapshot.database_path)
                with session_scope(live_engine) as live_session:
                    with session_scope(snapshot_engine) as training_session:
                        result = train_hourly_models(
                            live_session,
                            cfg,
                            reporter,
                            profile_name=selected,
                            training_session=training_session,
                            dataset_provenance=snapshot.as_dict(),
                            commit_live_metadata=True,
                            activation_policy=(
                                "candidate_only"
                                if candidate_only
                                else "quality_gated"
                            ),
                        )
                bridge.cleanup(profile=selected)
                snapshot_payload = snapshot.as_dict()

        status = "partial_success" if result.errors else "success"
        detail = result.as_dict()
        detail["training_snapshot"] = snapshot_payload
        reporter.finish(status, detail=detail)
        _emit(result)
        if result.errors:
            raise typer.Exit(EXIT_PARTIAL)
    except typer.Exit:
        raise
    except LockUnavailable as exc:
        if reporter is not None:
            reporter.fail(str(exc), status="skipped_locked")
        _emit({"status": "skipped_locked", "message": str(exc)})
        raise typer.Exit(EXIT_LOCKED) from exc
    except KeyboardInterrupt as exc:
        if reporter is not None:
            reporter.fail("cancelled by user", status="cancelled")
        raise typer.Exit(130) from exc
    except Exception as exc:
        if reporter is not None:
            reporter.fail(exc)
        raise
    finally:
        if snapshot_engine is not None:
            snapshot_engine.dispose()


@app.command("snapshot-train-hourly")
def command_snapshot_train_hourly(
    profile: str = typer.Option(
        "quick",
        "--profile",
        help="Profil treningu: quick albo full.",
    ),
    targets: str | None = typer.Option(
        None,
        "--targets",
        help="Opcjonalna lista celów; pozostałe aktywne modele nie są dezaktywowane.",
    ),
    snapshot: str = typer.Option(
        "auto",
        "--snapshot",
        help="auto, latest, layered, live albo konkretny dataset_id.",
    ),
    candidate_only: bool = typer.Option(
        False,
        "--candidate-only/--quality-gated-activation",
        help=(
            "Zapisz kandydatow i klasyfikacje, ale nie zmieniaj aktywnych modeli."
        ),
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Train on an immutable SQLite snapshot while ingestion may continue."""

    selected = profile.strip().lower()
    _run_snapshot_training(
        selected=selected,
        targets=targets,
        snapshot_selector=snapshot,
        candidate_only=candidate_only,
        config=config,
        env_file=env_file,
    )


@app.command("train-hourly")
def command_train_hourly(
    profile: str = typer.Option(
        "quick",
        "--profile",
        help="Profil treningu: quick albo full",
    ),
    targets: str | None = typer.Option(
        None,
        "--targets",
        help="Opcjonalna lista skonfigurowanych celów tego przebiegu.",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    selected = profile.strip().lower()
    if selected not in {"quick", "full"}:
        raise typer.BadParameter("profile must be quick or full")

    def stage(
        session: Session,
        cfg: AppConfig,
        reporter: ProgressReporter | None,
    ) -> StageStats:
        cfg.hourly_forecasting.targets = _parse_hourly_targets(targets, cfg)
        return train_hourly_models(
            session,
            cfg,
            reporter,
            profile_name=selected,
        )

    _single_stage_with_progress(
        f"train-hourly-{selected}",
        "training",
        stage,
        config,
        env_file,
        default_seconds=1_800.0 if selected == "quick" else 7_200.0,
    )


@app.command("quick-retrain")
def command_quick_retrain(
    snapshot: str = typer.Option(
        "auto",
        "--snapshot",
        help="auto, latest, live albo dataset_id.",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    _run_snapshot_training(
        selected="quick",
        targets=None,
        snapshot_selector=snapshot,
        candidate_only=False,
        config=config,
        env_file=env_file,
    )


@app.command("full-retrain")
def command_full_retrain(
    snapshot: str = typer.Option(
        "auto",
        "--snapshot",
        help="auto, latest, live albo dataset_id.",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    _run_snapshot_training(
        selected="full",
        targets=None,
        snapshot_selector=snapshot,
        candidate_only=False,
        config=config,
        env_file=env_file,
    )


@app.command("training-policy-status")
def command_training_policy_status(
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="quick/full; brak oznacza profil domyślny",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg = load_config(config, env_file)
    resolved = resolve_training_profile(cfg, profile)
    policy = create_training_set_policy(cfg)
    _emit(
        {
            "strategy": policy.name,
            "default_profile": cfg.hourly_forecasting.training_policy.default_profile,
            "resolved_profile": {
                "name": resolved.name,
                "maximum_training_days_by_target": (
                    resolved.maximum_training_days_by_target
                ),
                "maximum_rows_per_target": resolved.maximum_rows_per_target,
                "validation_max_rows": resolved.validation_max_rows,
                "always_keep_recent_days": resolved.always_keep_recent_days,
                "horizon_bucket_edges": list(resolved.horizon_bucket_edges),
                "samples_per_horizon_bucket": (
                    resolved.samples_per_horizon_bucket
                ),
                "maximum_horizons_per_origin": resolved.horizons_per_origin,
                "cross_fit_folds": resolved.cross_fit_folds,
                "algorithms": {
                    target: list(names)
                    for target, names in resolved.algorithms.items()
                },
                "fit_quantiles": resolved.fit_quantiles,
                "max_wall_time_seconds": resolved.max_wall_time_seconds,
                "rare_event_quantile": resolved.rare_event_quantile,
                "recency_half_life_days": resolved.recency_half_life_days,
            },
        }
    )


@app.command("update-hourly-residuals")
def command_update_hourly_residuals(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    _single_stage_with_progress(
        "update-hourly-residuals",
        "incremental_update",
        update_hourly_residual_correctors,
        config,
        env_file,
        default_seconds=300.0,
    )


@app.command("hourly-drift-status")
def command_hourly_drift_status(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, engine = _runtime(config, env_file, "hourly-drift-status")
    with session_scope(engine) as session:
        payload = hourly_drift_status(session, cfg)
    _emit(payload)


@app.command("predict-hourly")
def command_predict_hourly(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    _single_stage_with_progress(
        "predict-hourly",
        "prediction",
        create_hourly_forecasts,
        config,
        env_file,
        default_seconds=600.0,
    )


@app.command("hourly-readiness")
def command_hourly_readiness(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, engine = _runtime(config, env_file, "hourly-readiness")
    with session_scope(engine) as session:
        from sqlalchemy import func, select
        from smog_ai.database.models import AirMeasurement, WeatherMeasurement, ModelVersion
        resolved_profile = resolve_training_profile(cfg)
        payload = {
            "enabled": cfg.hourly_forecasting.enabled,
            "horizons_hours": cfg.hourly_forecasting.horizons_hours,
            "targets": cfg.hourly_forecasting.targets,
            "training_policy": {
                "strategy": create_training_set_policy(cfg).name,
                "default_profile": resolved_profile.name,
                "maximum_rows_per_target": resolved_profile.maximum_rows_per_target,
                "maximum_horizons_per_origin": resolved_profile.horizons_per_origin,
                "max_wall_time_seconds": resolved_profile.max_wall_time_seconds,
            },
            "incremental_residual_enabled": (
                cfg.hourly_forecasting.incremental_residual.enabled
            ),
            "drift": hourly_drift_status(session, cfg),
            "air_measurements": int(session.scalar(select(func.count(AirMeasurement.id))) or 0),
            "weather_measurements": int(session.scalar(select(func.count(WeatherMeasurement.id))) or 0),
            "active_hourly_models": [
                {
                    "target": row.parameter,
                    "provider": row.algorithm,
                    "version": row.semantic_version,
                    "bootstrap": bool((row.metrics_json or {}).get("bootstrap")),
                }
                for row in session.scalars(
                    select(ModelVersion).where(
                        ModelVersion.forecast_horizon == 0,
                        ModelVersion.active.is_(True),
                    )
                ).all()
            ],
        }
    _emit(payload)


@app.command("progress")
def command_progress(
    run_type: str = typer.Option("first-run", help="Typ przebiegu, np. first-run albo train-hourly"),
    watch: bool = typer.Option(False, "--watch", help="Odświeżaj aż do zakończenia"),
    refresh_seconds: float = typer.Option(5.0, min=1.0, max=300.0),
    as_json: bool = typer.Option(False, "--json", help="Zwróć pełny stan JSON"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Show durable model/pipeline progress and ETA."""

    cfg = load_config(config, env_file)
    while True:
        payload = read_progress(cfg.paths.logs_dir, run_type=run_type)
        if payload is None:
            typer.echo(
                json.dumps(
                    {
                        "status": "not_found",
                        "run_type": run_type,
                        "progress_dir": str(cfg.paths.logs_dir / "progress"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                if as_json
                else f"Brak stanu progress dla {run_type} w {cfg.paths.logs_dir / 'progress'}"
            )
            if not watch:
                return
        else:
            typer.echo(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str)
                if as_json
                else format_progress_text(payload)
            )
            if not watch or payload.get("status") not in {"created", "running"}:
                return
        time.sleep(refresh_seconds)


@app.command("train")
def command_train(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    cfg = load_config(config, env_file)
    if cfg.hourly_forecasting.enabled:
        selected = cfg.hourly_forecasting.training_policy.default_profile

        def stage(
            session: Session,
            loaded: AppConfig,
            reporter: ProgressReporter | None,
        ) -> StageStats:
            return train_hourly_models(
                session,
                loaded,
                reporter,
                profile_name=selected,
            )

        _single_stage_with_progress(
            f"train-hourly-{selected}",
            "training",
            stage,
            config,
            env_file,
            default_seconds=(1_800.0 if selected == "quick" else 7_200.0),
        )
    else:
        _single_stage("train", train_models, config, env_file)


@app.command("predict")
def command_predict(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    cfg = load_config(config, env_file)
    _single_stage(
        "predict-hourly" if cfg.hourly_forecasting.enabled else "predict",
        create_hourly_forecasts if cfg.hourly_forecasting.enabled else create_forecasts,
        config,
        env_file,
    )


@app.command("verify")
def command_verify(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    _single_stage("verify", verify_forecasts, config, env_file)


@app.command("build-spatial-surfaces")
def command_build_spatial_surfaces(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Locally interpolate station forecasts over Poland and publish ready map artifacts."""
    _single_stage("build-spatial-surfaces", build_spatial_surfaces, config, env_file)


@app.command("validate-spatial-surfaces")
def command_validate_spatial_surfaces(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    _single_stage(
        "validate-spatial-surfaces",
        validate_latest_spatial_surfaces,
        config,
        env_file,
    )


@app.command("publish-spatial-surfaces")
def command_publish_spatial_surfaces(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Idempotent alias: build locally and publish the resulting immutable surfaces."""
    _single_stage("publish-spatial-surfaces", build_spatial_surfaces, config, env_file)


@app.command("build-snapshot")
def command_snapshot(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    _single_stage_with_progress(
        "build-snapshot",
        "snapshot",
        build_snapshot_stage,
        config,
        env_file,
        default_seconds=600.0,
    )


@app.command("publish")
def command_publish(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    _single_stage("publish", retry_publications, config, env_file)


@app.command("retry-publications")
def command_retry(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    _single_stage("retry-publications", retry_publications, config, env_file)


@app.command("backfill")
def command_backfill(
    lookback_days: int = typer.Option(7, min=1, max=31),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    def stage(session: Session, cfg: AppConfig) -> StageStats:
        return backfill_gios(session, cfg, lookback_days=lookback_days)
    _single_stage("backfill", stage, config, env_file)


@app.command("report")
def command_report(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    cfg, engine = _runtime(config, env_file, "report")
    with session_scope(engine) as session:
        _emit(build_report(session, cfg))


@app.command("data-freshness-report")
def command_data_freshness_report(
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        help="Katalog JSON/HTML; domyślnie <RuntimeRoot>/reports/freshness.",
    ),
    fail_on_stale: bool = typer.Option(
        False,
        "--fail-on-stale/--no-fail-on-stale",
        help="Zwróć kod częściowy 4 dla stale/missing.",
    ),
    threshold_hours: float | None = typer.Option(
        None,
        "--threshold-hours",
        min=0.1,
        help="Opcjonalny koniec przedziału fresh (domyślnie 14 h).",
    ),
    stale_threshold_hours: float | None = typer.Option(
        None,
        "--stale-threshold-hours",
        min=0.1,
        help="Opcjonalny początek stale/block (domyślnie ponad 22 h).",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Report measurement age and station coverage for every collected parameter."""

    cfg, engine = _runtime(config, env_file, "data-freshness-report")
    if threshold_hours is not None:
        cfg.operations.freshness_hours = threshold_hours
    if stale_threshold_hours is not None:
        cfg.operations.freshness_stale_hours = stale_threshold_hours
    if cfg.operations.freshness_stale_hours <= cfg.operations.freshness_hours:
        raise typer.BadParameter(
            "--stale-threshold-hours must be greater than --threshold-hours"
        )
    destination = output_dir or (cfg.paths.logs_dir.parent / "reports" / "freshness")
    with session_scope(engine) as session:
        report = build_freshness_report(session, cfg)
    report["files"] = write_freshness_report(report, destination)
    _emit(report)
    if fail_on_stale and report["overall_status"] in {"stale", "missing"}:
        raise typer.Exit(EXIT_PARTIAL)


@app.command("pipeline")
def command_pipeline(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    try:
        cfg, engine = _runtime(config, env_file, "pipeline")
        with ProcessLease(engine, cfg, "hourly-pipeline"):
            run_id, stats, stages = run_pipeline(engine, cfg)
        _emit({"run_id": run_id, "stats": stats.as_dict(), "stages": stages})
        if stats.errors:
            raise typer.Exit(EXIT_PARTIAL)
    except LockUnavailable as exc:
        _emit({"status": "skipped_locked", "message": str(exc)})
        raise typer.Exit(EXIT_LOCKED) from exc
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG) from exc
    except DatabaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_DATABASE) from exc


@app.command("healthcheck")
def command_healthcheck(
    as_json: bool = typer.Option(False, "--json"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, engine = _runtime(config, env_file, "healthcheck")
    with session_scope(engine) as session:
        result = run_healthcheck(session, engine, cfg)
    _emit(result.as_dict(), as_json=True)
    if not result.ok:
        raise typer.Exit(EXIT_GENERAL)


@app.command("storage-init")
def command_storage_init(
    create_if_missing: bool = typer.Option(
        False,
        "--create-if-missing/--no-create-if-missing",
        help="Utwórz Space/bucket, jeżeli nie istnieje i klucz ma takie uprawnienie.",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, _ = _runtime(config, env_file, "storage-init")
    repository = create_artifact_repository(cfg)
    created = repository.store.ensure_container(create_if_missing=create_if_missing)
    repository.ping()
    _emit({
        "status": "created" if created else "ready",
        "backend": repository.store.backend_name,
        "bucket": cfg.object_storage.bucket,
        "prefix": cfg.object_storage.prefix,
    })


@app.command("storage-health")
def command_storage_health(
    digitalocean_destination: bool = typer.Option(
        False,
        "--digitalocean-destination/--configured-destination",
        help="Odczytaj Spaces z SPACES_* zamiast lokalnego Object Store.",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg = load_config(config, env_file)
    if digitalocean_destination:
        _select_digitalocean_spaces_destination(cfg)
    configure_logging(cfg.paths.logs_dir, task_name="storage-health")
    repository = create_artifact_repository(cfg)
    repository.ping()
    latest_raw = None
    latest_forecast = None
    latest_spatial = None
    documentation = None
    try:
        latest_raw = repository.get_json(repository.layout.latest_raw_manifest)
    except Exception:
        pass
    try:
        latest_forecast = repository.get_json(repository.layout.latest_forecast_pointer)
    except Exception:
        pass
    try:
        latest_spatial = repository.get_json(repository.layout.latest_spatial_pointer)
    except Exception:
        pass
    try:
        documentation = repository.get_json(repository.layout.documentation_manifest)
    except Exception:
        pass
    _emit({
        "status": "ok",
        "backend": repository.store.backend_name,
        "latest_raw": latest_raw,
        "latest_forecast": latest_forecast,
        "latest_spatial": latest_spatial,
        "documentation": documentation,
    })


@app.command("upload-operational-data")
def command_upload_operational_data(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    _single_stage("upload-operational-data", export_operational_data, config, env_file)


@app.command("publish-serving-release")
def command_publish_serving_release(
    source_root: Path = typer.Option(
        ...,
        "--source-root",
        help="Lokalny katalog Object Store zawierający serving/latest.json.",
    ),
    digitalocean_destination: bool = typer.Option(
        False,
        "--digitalocean-destination/--configured-destination",
        help="Wymuś Spaces i wartości SPACES_* zamiast lokalnych SMOG_AI_*.",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Publish the verified local Serving v2 release to configured Spaces/S3."""

    try:
        cfg = load_config(config, env_file)
        if digitalocean_destination:
            _select_digitalocean_spaces_destination(cfg)
        configure_logging(cfg.paths.logs_dir, task_name="publish-serving-release")
        result = publish_local_serving_release(cfg, source_root)
        _emit(result)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG) from exc


@app.command("digitalocean-serving-preflight")
def command_digitalocean_serving_preflight(
    source_root: Path = typer.Option(
        ...,
        "--source-root",
        help="Lokalny Object Store zawierający zweryfikowane serving/latest.json.",
    ),
    output: Path | None = typer.Option(None, "--output", help="Opcjonalny raport JSON."),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Verify the exact release, Spaces destination and estimated upload delta."""

    cfg = load_config(config, env_file)
    _select_digitalocean_spaces_destination(cfg)
    configure_logging(cfg.paths.logs_dir, task_name="digitalocean-serving-preflight")
    result = inspect_local_serving_release(cfg, source_root, check_destination=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    _emit(result)
    if result["forbidden_payloads_present"] or not result["compressed_assets_only"]:
        problems = [
            *result.get("forbidden_objects", []),
            *result.get("uncompressed_objects", []),
        ]
        raise RuntimeError(
            "Serving release contains forbidden or uncompressed payloads: "
            + ", ".join(dict.fromkeys(str(item) for item in problems))
        )


@app.command("prune-serving-releases")
def command_prune_serving_releases(
    keep: int = typer.Option(3, "--keep", min=1),
    confirmation: str = typer.Option(
        "",
        "--confirmation",
        help=f"Bezpiecznik zapisu: {RETENTION_CONFIRMATION}",
    ),
    digitalocean_destination: bool = typer.Option(
        False,
        "--digitalocean-destination/--configured-destination",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Keep recent Serving v2 releases without touching pointer or static assets."""

    cfg = load_config(config, env_file)
    if digitalocean_destination:
        _select_digitalocean_spaces_destination(cfg)
    configure_logging(cfg.paths.logs_dir, task_name="prune-serving-releases")
    _emit(
        prune_remote_serving_releases(
            cfg,
            keep=keep,
            confirmation=confirmation,
        )
    )


@app.command("prepare-training-data")
def command_prepare_training_data(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    def stage(session: Session, cfg: AppConfig) -> StageStats:
        bridge = create_training_data_bridge(cfg)
        return bridge.prepare(session, cfg)

    _single_stage("prepare-training-data", stage, config, env_file)


@app.command("first-run")
def command_first_run(
    train: bool = typer.Option(True, "--train/--no-train"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    """Perform API -> Spaces -> local ML -> ready artifacts exactly once."""

    cfg, engine = _runtime(config, env_file, "first-run")
    data_bridge = create_training_data_bridge(cfg)
    data_bridge.configure_training(cfg)
    # Documentation is a required release artifact, but it must never invalidate
    # hours of model work. Resolve all sources before the expensive pipeline starts.
    documentation_preflight = (
        load_documentation_bundle(cfg).metadata
        if cfg.documentation.enabled
        else {"status": "disabled"}
    )
    progress = ProgressReporter(
        cfg.paths.logs_dir,
        run_type="first-run",
        stage_weights=FIRST_RUN_STAGE_WEIGHTS,
        stage_default_seconds=FIRST_RUN_STAGE_DEFAULT_SECONDS,
    ).start(task="initializing first-run")
    total = StageStats()
    details: dict[str, Any] = {
        "progress": {
            "current": str(progress.current_path),
            "run": str(progress.run_path),
            "events": str(progress.event_path),
        },
        "documentation_preflight": documentation_preflight,
        "data_flow": data_bridge.describe(cfg),
    }
    prerequisite_failures: list[str] = []
    collection_stages_list = [
        "collect_gios",
        "collect_imgw",
    ]
    if cfg.imgw_archive.enabled and cfg.imgw_archive.run_on_first_run:
        collection_stages_list.append("backfill_imgw_archive")
    collection_stages_list.extend(
        [
            "validate",
            "match_stations",
        ]
    )
    if (
        data_bridge.requires_operational_export
        or cfg.data_flow.mirror_operational_to_object_store
    ):
        collection_stages_list.append("export_object_store")
    collection_stages = tuple(collection_stages_list)

    try:
        with ProcessLease(engine, cfg, "first-run"):
            progress.update(
                "collection",
                0.0,
                task=(
                    "collect GIOŚ/IMGW, validate and prepare selected data flow"
                ),
                detail={"pipeline_stages": list(collection_stages)},
                force=True,
            )
            run_id, pipeline_stats, stages = run_pipeline(
                engine,
                cfg,
                run_type="first_run_collection",
                stage_names=collection_stages,
            )
            total.merge(pipeline_stats)
            details["collection_pipeline"] = {"run_id": run_id, "stages": stages}
            progress.complete_stage(
                "collection",
                task="collection pipeline completed",
                detail={"run_id": run_id, "stats": pipeline_stats.as_dict()},
            )

            mandatory_names = ["collect_gios", "collect_imgw"]
            if data_bridge.requires_operational_export:
                mandatory_names.append("export_object_store")
            for name in mandatory_names:
                if stages.get(name, {}).get("status") != "success":
                    prerequisite_failures.append(f"{name}_failed")
            artifact = (
                stages.get("export_object_store", {})
                .get("details", {})
                .get("artifact", {})
            )
            if (
                data_bridge.requires_operational_export
                and artifact
                and not bool(artifact.get("complete", False))
            ):
                missing = artifact.get("missing_required") or []
                prerequisite_failures.append(
                    "incomplete_operational_bundle:"
                    + ",".join(str(item) for item in missing)
                )

            if train and not prerequisite_failures:
                # Transaction boundary 1: model work. Once this block exits,
                # training, activation, forecasts and spatial metadata are committed.
                # A later documentation or publication failure cannot roll them back.
                with session_scope(engine) as session:
                    progress.update(
                        "training_data",
                        0.0,
                        task=(
                            "prepare h1–h48 training data via "
                            f"{data_bridge.mode}"
                        ),
                        detail={
                            "targets": cfg.hourly_forecasting.targets,
                            "maximum_rows_per_target": (
                                cfg.hourly_forecasting.maximum_training_rows_per_target
                            ),
                        },
                        force=True,
                    )
                    prepared = data_bridge.prepare(
                        session,
                        cfg,
                        progress=progress,
                    )
                    total.merge(prepared)
                    details["training_data"] = prepared.as_dict()
                    progress.complete_stage(
                        "training_data",
                        task="training frames completed",
                        detail=prepared.as_dict(),
                    )

                    data_bridge.configure_training(cfg)
                    if cfg.hourly_forecasting.enabled:
                        trained = train_hourly_models(session, cfg, progress=progress)
                    else:
                        progress.update(
                            "training",
                            0.0,
                            task="train legacy discrete-horizon models",
                            force=True,
                        )
                        trained = train_models(session, cfg)
                        progress.complete_stage(
                            "training",
                            task="legacy model training completed",
                            detail=trained.as_dict(),
                        )
                    total.merge(trained)
                    details["training"] = trained.as_dict()

                    if cfg.hourly_forecasting.enabled:
                        predicted = create_hourly_forecasts(
                            session,
                            cfg,
                            progress=progress,
                        )
                    else:
                        progress.update(
                            "prediction",
                            0.0,
                            task="create forecasts",
                            force=True,
                        )
                        predicted = create_forecasts(session, cfg)
                        progress.complete_stage(
                            "prediction",
                            task="forecast creation completed",
                            detail=predicted.as_dict(),
                        )
                    total.merge(predicted)
                    details["prediction"] = predicted.as_dict()

                    spatial = build_spatial_surfaces(
                        session,
                        cfg,
                        progress=progress,
                    )
                    total.merge(spatial)
                    details["spatial"] = spatial.as_dict()

                # Documentation has no database dependency. Its failure is recorded
                # as partial success, while already committed model work is preserved.
                progress.update(
                    "documentation",
                    0.0,
                    task="publish technical and mathematical documentation",
                    force=True,
                )
                try:
                    documentation = publish_documentation(cfg)
                except Exception as exc:
                    total.errors += 1
                    details["documentation"] = {
                        "status": "failed",
                        "error": str(exc),
                        "model_work_preserved": True,
                    }
                    progress.complete_stage(
                        "documentation",
                        task="documentation failed; model work preserved",
                        detail=details["documentation"],
                    )
                else:
                    total.merge(documentation)
                    details["documentation"] = documentation.as_dict()
                    progress.complete_stage(
                        "documentation",
                        task="documentation published",
                        detail=documentation.as_dict(),
                    )

                # Transaction boundary 2: snapshot and outbox publication.
                with session_scope(engine) as session:
                    progress.update(
                        "snapshot",
                        0.0,
                        task="build dashboard snapshot",
                        force=True,
                    )
                    snapshot_stats = build_snapshot_stage(session, cfg)
                    total.merge(snapshot_stats)
                    details["snapshot"] = snapshot_stats.as_dict()
                    progress.complete_stage(
                        "snapshot",
                        task="dashboard snapshot completed",
                        detail=snapshot_stats.as_dict(),
                    )

                    progress.update(
                        "publication",
                        0.0,
                        task="publish or retry outbox",
                        force=True,
                    )
                    if snapshot_stats.inserted > 0:
                        published = retry_publications(session, cfg)
                    else:
                        published = StageStats(
                            skipped=1,
                            details={"reason": "snapshot_not_created"},
                        )
                    total.merge(published)
                    details["publication"] = published.as_dict()
                    progress.complete_stage(
                        "publication",
                        task="publication stage completed",
                        detail=published.as_dict(),
                    )
            elif train:
                total.warnings += 1
                details["training"] = {
                    "status": "skipped",
                    "reason": "mandatory_collection_prerequisite_failed",
                    "prerequisite_failures": prerequisite_failures,
                    "message": (
                        "Nie uruchomiono treningu ani publikacji, ponieważ nie powstał "
                        "kompletny pakiet danych GIOŚ/IMGW."
                    ),
                }
                for stage in (
                    "training_data",
                    "training",
                    "prediction",
                    "spatial",
                    "documentation",
                    "snapshot",
                    "publication",
                ):
                    progress.complete_stage(
                        stage,
                        task=f"{stage} skipped",
                        detail={"prerequisite_failures": prerequisite_failures},
                    )
            else:
                details["training"] = {"status": "disabled_by_option"}
                for stage in (
                    "training_data",
                    "training",
                    "prediction",
                    "spatial",
                    "documentation",
                    "snapshot",
                    "publication",
                ):
                    progress.complete_stage(
                        stage,
                        task=f"{stage} disabled by option",
                        detail={"train": False},
                    )

        final_status = (
            "partial_success"
            if total.errors or prerequisite_failures
            else "success"
        )
        progress.finish(
            final_status,
            detail={
                "stats": total.as_dict(),
                "prerequisite_failures": prerequisite_failures,
            },
        )
        _emit({"stats": total.as_dict(), "details": details})
        if total.errors or prerequisite_failures:
            raise typer.Exit(EXIT_PARTIAL)
    except typer.Exit:
        raise
    except KeyboardInterrupt as exc:
        progress.fail("cancelled by user", status="cancelled")
        raise typer.Exit(130) from exc
    except Exception as exc:
        progress.fail(exc)
        raise


@app.command("backup")
def command_backup(
    tier: str = typer.Option("daily", help="daily, weekly lub monthly"),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    if tier not in {"daily", "weekly", "monthly"}:
        raise typer.BadParameter("tier must be daily, weekly or monthly")
    cfg, _ = _runtime(config, env_file, "backup")
    _emit(create_backup(cfg, tier))


@app.command("daily-maintenance")
def command_daily_maintenance(config: Path | None = COMMON_CONFIG, env_file: Path | None = COMMON_ENV) -> None:
    cfg, engine = _runtime(config, env_file, "daily-maintenance")
    total = StageStats()
    with ProcessLease(engine, cfg, "daily-maintenance"):
        with session_scope(engine) as session:
            try:
                total.merge(backfill_gios(session, cfg, lookback_days=7))
            except Exception as exc:
                total.errors += 1
                total.details["backfill_error"] = str(exc)
            if cfg.imgw_archive.enabled:
                try:
                    total.merge(backfill_imgw_archive(session, cfg))
                except Exception as exc:
                    total.errors += 1
                    total.details["imgw_archive_backfill_error"] = str(exc)
            total.merge(validate_data(session, cfg))
            if cfg.hourly_forecasting.enabled:
                total.merge(verify_forecasts(session, cfg))
                total.merge(update_hourly_residual_correctors(session, cfg))
                total.details["hourly_drift"] = hourly_drift_status(session, cfg)
            total.merge(retry_publications(session, cfg))
            report = build_report(session, cfg)
        backup = create_backup(cfg, "daily")
    _emit({"stats": total.as_dict(), "report": report, "backup": backup})
    if total.errors:
        raise typer.Exit(EXIT_PARTIAL)


@app.command("weekly-maintenance")
def command_weekly_maintenance(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, engine = _runtime(config, env_file, "weekly-maintenance")
    cfg.hourly_forecasting.training_policy.default_profile = "quick"
    bridge = create_training_data_bridge(cfg)
    bridge.configure_training(cfg)

    with ProcessLease(engine, cfg, "weekly-training"):
        with session_scope(engine) as session:
            stats = StageStats()
            export_required = (
                bridge.requires_operational_export
                or cfg.data_flow.mirror_operational_to_object_store
            )
            exported: StageStats | None = None

            if export_required:
                exported = export_operational_data(session, cfg)
                stats.merge(exported)

            if bridge.requires_operational_export:
                artifact = (
                    (exported.details if exported is not None else {})
                    .get("artifact", {})
                )
                if not bool(artifact.get("complete", False)):
                    stats.warnings += 1
                    stats.details["training_skipped"] = {
                        "reason": "incomplete_operational_bundle",
                        "missing_required": artifact.get(
                            "missing_required",
                            [],
                        ),
                    }
                    _emit(stats)
                    raise typer.Exit(EXIT_PARTIAL)

            stats.merge(bridge.prepare(session, cfg))
            bridge.configure_training(cfg)
            stats.merge(
                train_hourly_models(
                    session,
                    cfg,
                    profile_name="quick",
                )
                if cfg.hourly_forecasting.enabled
                else train_models(session, cfg)
            )
            stats.merge(
                create_hourly_forecasts(session, cfg)
                if cfg.hourly_forecasting.enabled
                else create_forecasts(session, cfg)
            )
            stats.merge(build_spatial_surfaces(session, cfg))
            stats.merge(publish_documentation(cfg))
            snapshot_stats = build_snapshot_stage(session, cfg)
            stats.merge(snapshot_stats)
            if snapshot_stats.inserted > 0:
                stats.merge(retry_publications(session, cfg))

            stats.details["data_flow"] = bridge.describe(cfg)

    _emit(stats)
    if stats.errors:
        raise typer.Exit(EXIT_PARTIAL)


@app.command("audit-hourly-serving-contract")
def command_audit_hourly_serving_contract(
    output: Path | None = typer.Option(None, "--output"),
    allow_experimental_targets: str | None = typer.Option(
        None,
        "--allow-experimental-targets",
        help=(
            "Opcjonalna jawna lista celów dopuszczonych mimo miękkiej bramki "
            "jakości. Domyślnie wszystkie aktywne cele są publikowane i "
            "oznaczane jako eksperymentalne. Podaj 'none', aby je wyłączyć."
        ),
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, engine = _runtime(config, env_file, "audit-hourly-serving-contract")
    if output is None:
        output = (
            cfg.paths.data_dir.parent
            / "reports"
            / "stage2-stage3"
            / f"hourly-serving-contract-{utc_now():%Y%m%dT%H%M%SZ}.json"
        )
    with session_scope(engine) as session:
        result = audit_latest_hourly_serving_contract(
            session,
            cfg,
            output=output,
            allow_experimental_targets=allow_experimental_targets,
        )
    _emit(result)
    if not result.get("passed"):
        raise typer.Exit(EXIT_PARTIAL)


@app.command("mlflow-status")
def command_mlflow_status(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg = load_config(config, env_file)
    try:
        import mlflow  # type: ignore

        installed = True
        version = getattr(mlflow, "__version__", None)
    except ImportError:
        installed = False
        version = None
    _emit(
        {
            "status": "ok",
            "enabled": cfg.mlflow.enabled,
            "strict": cfg.mlflow.strict,
            "installed": installed,
            "version": version,
            "tracking_uri": cfg.mlflow.tracking_uri or None,
            "experiment_name": cfg.mlflow.experiment_name,
            "comparison_path": str(cfg.mlflow.comparison_path),
            "publish_comparison_to_object_storage": (
                cfg.mlflow.publish_comparison_to_object_storage
            ),
            "ui_url": cfg.mlflow.ui_url,
        }
    )


@app.command("export-model-comparison")
def command_export_model_comparison(
    publish: bool = typer.Option(
        False,
        "--publish/--no-publish",
        help=(
            "Jawnie opublikuj artefakt porownania do ObjectStore. "
            "Domyslnie zapis jest wylacznie lokalny."
        ),
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, engine = _runtime(config, env_file, "export-model-comparison")
    with session_scope(engine) as session:
        result = export_model_comparison(session, cfg, publish=publish)
    _emit(result)


@app.command("publish-approved-models")
def command_publish_approved_models(
    targets: str = typer.Option(
        ...,
        "--targets",
        help="Comma-separated active model targets approved for publication.",
    ),
    confirmation: str = typer.Option(
        ...,
        "--confirmation",
        help=(
            "Exact safety phrase required: " + PUBLISH_CONFIRMATION
        ),
    ),
    publish_comparison: bool = typer.Option(
        True,
        "--publish-comparison/--no-publish-comparison",
    ),
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, engine = _runtime(config, env_file, "publish-approved-models")
    selected = [
        value.strip()
        for value in targets.replace(";", ",").split(",")
        if value.strip()
    ]
    with session_scope(engine) as session:
        result = publish_approved_hourly_models(
            session,
            cfg,
            targets=selected,
            confirmation=confirmation,
            publish_comparison=publish_comparison,
        )
        session.commit()
    _emit(result)


@app.command("monthly-maintenance")
def command_monthly_maintenance(
    config: Path | None = COMMON_CONFIG,
    env_file: Path | None = COMMON_ENV,
) -> None:
    cfg, engine = _runtime(config, env_file, "monthly-maintenance")
    cfg.hourly_forecasting.training_policy.default_profile = "full"
    bridge = create_training_data_bridge(cfg)
    bridge.configure_training(cfg)
    with ProcessLease(engine, cfg, "monthly-maintenance"):
        with session_scope(engine) as session:
            stats = StageStats()
            export_required = (
                bridge.requires_operational_export
                or cfg.data_flow.mirror_operational_to_object_store
            )
            exported: StageStats | None = None
            if export_required:
                exported = export_operational_data(session, cfg)
                stats.merge(exported)
            if bridge.requires_operational_export:
                artifact = (
                    (exported.details if exported is not None else {})
                    .get("artifact", {})
                )
                if not bool(artifact.get("complete", False)):
                    stats.warnings += 1
                    stats.details["training_skipped"] = {
                        "reason": "incomplete_operational_bundle",
                        "missing_required": artifact.get("missing_required", []),
                    }
                else:
                    stats.merge(bridge.prepare(session, cfg))
            else:
                stats.merge(bridge.prepare(session, cfg))

            if "training_skipped" not in stats.details:
                stats.merge(
                    train_hourly_models(
                        session,
                        cfg,
                        profile_name="full",
                    )
                    if cfg.hourly_forecasting.enabled
                    else train_models(session, cfg)
                )
                stats.merge(
                    create_hourly_forecasts(session, cfg)
                    if cfg.hourly_forecasting.enabled
                    else create_forecasts(session, cfg)
                )
                stats.merge(build_spatial_surfaces(session, cfg))
                stats.merge(publish_documentation(cfg))
                snapshot_stats = build_snapshot_stage(session, cfg)
                stats.merge(snapshot_stats)
                if snapshot_stats.inserted > 0:
                    stats.merge(retry_publications(session, cfg))

            stats.details["data_flow"] = bridge.describe(cfg)
            stats.details["hourly_drift"] = (
                hourly_drift_status(session, cfg)
                if cfg.hourly_forecasting.enabled
                else None
            )
            report = build_report(session, cfg)
        backup = create_backup(cfg, "monthly")
    _emit({"stats": stats.as_dict(), "report": report, "backup": backup})
    if stats.errors:
        raise typer.Exit(EXIT_PARTIAL)


if __name__ == "__main__":
    app()
