from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from smog_ai.air_parameters import create_air_parameter_registry
from smog_ai.config import AppConfig
from smog_ai.database.repository import get_application_state, set_application_state
from smog_ai.domain import StageStats
from smog_ai.progress import ProgressReporter, WeightedStageProgress
from smog_ai.range_backfill.audit import (
    DEFAULT_AIR_PARAMETERS,
    DEFAULT_WEATHER_PARAMETERS,
    CoverageAuditor,
    load_latest_audit_from_path,
    parse_datetime_bound,
    requested_range_from_audit_payload,
    save_coverage_report,
)
from smog_ai.range_backfill.contracts import (
    BackfillAction,
    BackfillExecutionResult,
    BackfillPlan,
    CoverageReport,
    TimeInterval,
)
from smog_ai.range_backfill.planner import BackfillPlanner
from smog_ai.range_backfill.providers import create_backfill_provider_registry

logger = logging.getLogger(__name__)
RANGE_BACKFILL_STAGE_WEIGHTS = {
    "audit": 5.0,
    "plan": 5.0,
    "execute": 85.0,
    "verify": 5.0,
}
RANGE_BACKFILL_STAGE_DEFAULT_SECONDS = {
    "audit": 120.0,
    "plan": 10.0,
    "execute": 7_200.0,
    "verify": 120.0,
}
ATTEMPT_STATE_KEY = "range_backfill_attempts_v1"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _action_bound(action: BackfillAction) -> TimeInterval:
    return TimeInterval(
        min(item.start for item in action.intervals),
        max(item.end for item in action.intervals),
    )


def _intersection_hours(left: TimeInterval, right: TimeInterval) -> float:
    intersection = left.intersection(right)
    return intersection.hours if intersection is not None else 0.0


def _remaining_missing_hours(
    report: CoverageReport,
    action: BackfillAction,
) -> float:
    total = 0.0
    for parameter in action.parameters:
        dataset = report.find(action.dataset, parameter)
        if dataset is None:
            continue
        for missing in dataset.missing_intervals:
            for requested in action.intervals:
                total += _intersection_hours(missing, requested)
    return total


def _action_expected_hours(action: BackfillAction) -> float:
    return max(
        1.0,
        sum(item.hours for item in action.intervals) * max(1, len(action.parameters)),
    )


def _action_coverage(report: CoverageReport, action: BackfillAction) -> float:
    expected = _action_expected_hours(action)
    missing = _remaining_missing_hours(report, action)
    return max(0.0, min(1.0, 1.0 - missing / expected))


def _parameters_from_text(
    config: AppConfig,
    value: str | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    registry = create_air_parameter_registry(config)
    if not value or value.strip().upper() in {"ALL", "WSZYSTKIE"}:
        air = tuple(
            dict.fromkeys(
                (*registry.collection_codes, *registry.historical_codes)
            )
        )
        return air or DEFAULT_AIR_PARAMETERS, DEFAULT_WEATHER_PARAMETERS
    tokens = [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]
    air: list[str] = []
    weather: list[str] = []
    for token in tokens:
        normalized = registry.resolve(token)
        if normalized != "UNKNOWN" and registry.contains(normalized):
            if normalized not in air:
                air.append(normalized)
            continue
        original = token.strip()
        if original in DEFAULT_WEATHER_PARAMETERS:
            if original not in weather:
                weather.append(original)
            continue
        raise ValueError(f"Unsupported range-backfill parameter: {token!r}")
    return tuple(air), tuple(weather)


def resolve_requested_scope(
    config: AppConfig,
    *,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    parameters: str | None = None,
    audit_package: Path | None = None,
    default_lookback_days: int = 365,
) -> tuple[TimeInterval, tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    now = datetime.now(UTC)
    metadata: dict[str, Any] = {}
    if audit_package is not None:
        payload, source = load_latest_audit_from_path(audit_package)
        interval, audit_air, audit_weather = requested_range_from_audit_payload(payload)
        thresholds = payload.get("thresholds")
        threshold_metadata: dict[str, Any] = {}
        if isinstance(thresholds, Mapping):
            air_threshold = thresholds.get("minimum_air_stations_per_hour")
            weather_threshold = thresholds.get(
                "minimum_weather_stations_per_hour"
            )
            if air_threshold is not None:
                threshold_metadata["audit_minimum_air_stations"] = int(
                    air_threshold
                )
            if weather_threshold is not None:
                threshold_metadata["audit_minimum_weather_stations"] = int(
                    weather_threshold
                )
        metadata.update(
            {
                "audit_package": str(audit_package),
                "audit_source": source,
                "audit_generated_at": payload.get("generated_at_utc"),
                **threshold_metadata,
            }
        )
        requested = TimeInterval(interval.start, min(interval.end, now))
        if parameters:
            air, weather = _parameters_from_text(config, parameters)
        else:
            air, weather = audit_air, audit_weather
        return requested, air, weather, metadata

    lower = parse_datetime_bound(
        start,
        display_timezone=config.display_timezone,
        default=now - timedelta(days=default_lookback_days),
    )
    upper = parse_datetime_bound(
        end,
        display_timezone=config.display_timezone,
        default=now,
        end_date_inclusive=True,
    )
    upper = min(upper, now)
    if upper <= lower:
        raise ValueError("Requested end must be later than start")
    air, weather = _parameters_from_text(config, parameters)
    return TimeInterval(lower, upper), air, weather, metadata


class RangeAwareBackfillService:
    """Audit -> plan -> fill -> re-audit orchestration.

    SQLite is the source of truth for completion. Cache/state markers accelerate
    transport but can never override the fresh coverage audit.
    """

    def __init__(
        self,
        session: Session,
        config: AppConfig,
        *,
        progress: ProgressReporter | None = None,
        cache_mode: str | None = None,
        include_isolated_gaps: bool = False,
        minimum_historical_gap_hours: int = 2,
        max_no_progress_attempts: int = 2,
        minimum_air_stations: int = 1,
        minimum_weather_stations: int = 1,
    ) -> None:
        self.session = session
        self.config = config
        self.progress = progress
        self.cache_mode = cache_mode or config.data_flow.history_cache_mode
        self.include_isolated_gaps = include_isolated_gaps
        self.minimum_historical_gap_hours = minimum_historical_gap_hours
        self.max_no_progress_attempts = max(1, max_no_progress_attempts)
        self.minimum_air_stations = max(1, int(minimum_air_stations))
        self.minimum_weather_stations = max(
            1,
            int(minimum_weather_stations),
        )
        self.auditor = CoverageAuditor(
            session,
            display_timezone=config.display_timezone,
            precipitation_cadence_hours=(
                config.imgw_archive.precipitation_accumulation_period_hours
            ),
        )
        self.registry = create_backfill_provider_registry()
        self.report_root = config.paths.logs_dir / "range-backfill"
        self.report_root.mkdir(parents=True, exist_ok=True)

    def audit(
        self,
        requested: TimeInterval,
        *,
        air_parameters: Sequence[str],
        weather_parameters: Sequence[str],
    ) -> CoverageReport:
        return self.auditor.audit(
            requested,
            air_parameters=air_parameters,
            weather_parameters=weather_parameters,
            minimum_air_stations=self.minimum_air_stations,
            minimum_weather_stations=self.minimum_weather_stations,
        )

    def build_plan(self, report: CoverageReport) -> BackfillPlan:
        planner = BackfillPlanner(
            cache_mode=self.cache_mode,
            include_isolated_gaps=self.include_isolated_gaps,
            minimum_historical_gap_hours=self.minimum_historical_gap_hours,
        )
        return planner.plan(report)

    def _audit_action(self, action: BackfillAction) -> CoverageReport:
        bound = _action_bound(action)
        return self.auditor.audit(
            bound,
            air_parameters=(action.parameters if action.dataset == "air" else ()),
            weather_parameters=(
                action.parameters if action.dataset == "weather" else ()
            ),
            minimum_air_stations=self.minimum_air_stations,
            minimum_weather_stations=self.minimum_weather_stations,
        )

    def _load_attempts(self) -> dict[str, Any]:
        value = get_application_state(self.session, ATTEMPT_STATE_KEY, {})
        return dict(value) if isinstance(value, Mapping) else {}

    def _save_attempts(self, value: Mapping[str, Any]) -> None:
        set_application_state(self.session, ATTEMPT_STATE_KEY, dict(value))
        self.session.commit()

    def execute(
        self,
        plan: BackfillPlan,
        *,
        dry_run: bool = False,
        max_actions: int = 0,
    ) -> dict[str, Any]:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        plan_path = self.report_root / f"range-backfill-plan-{stamp}.json"
        _write_json(plan_path, plan.to_dict())

        if dry_run:
            if self.progress is not None:
                self.progress.complete_stage(
                    "execute",
                    task="dry-run: no source action executed",
                    detail={
                        "actions": len(plan.actions),
                        "plan_path": str(plan_path),
                    },
                )
            return {
                "status": "dry_run",
                "plan_path": str(plan_path),
                "plan": plan.to_dict(),
            }

        attempts = self._load_attempts()
        actions = list(plan.actions)
        if max_actions > 0:
            actions = actions[:max_actions]
        total_weight = sum(max(0.001, item.weight) for item in actions) or 1.0
        weighted = WeightedStageProgress(
            self.progress,
            stage="execute",
            total_weight=total_weight,
        )
        results: list[BackfillExecutionResult] = []

        for index, action in enumerate(actions, start=1):
            signature = action.signature
            attempt = dict(attempts.get(signature) or {})
            no_progress_count = int(attempt.get("no_progress_count", 0))
            before_report = self._audit_action(action)
            before_missing = _remaining_missing_hours(before_report, action)
            before_coverage = _action_coverage(before_report, action)

            if before_missing <= 0:
                result = BackfillExecutionResult(
                    action=action,
                    status="skipped_complete",
                    coverage_before=before_coverage,
                    coverage_after=before_coverage,
                    detail={"reason": "fresh SQLite audit found no missing slots"},
                )
                results.append(result)
                weighted.advance(
                    f"{index}/{len(actions)} {action.provider} already complete",
                    action.weight,
                    detail=result.to_dict(),
                    status="skipped_complete",
                )
                continue

            if no_progress_count >= self.max_no_progress_attempts:
                result = BackfillExecutionResult(
                    action=action,
                    status="skipped_no_progress",
                    coverage_before=before_coverage,
                    coverage_after=before_coverage,
                    detail={
                        "reason": "source was already retried without improving coverage",
                        "no_progress_count": no_progress_count,
                    },
                )
                results.append(result)
                weighted.advance(
                    f"{index}/{len(actions)} {action.provider} source-limited",
                    action.weight,
                    detail=result.to_dict(),
                    status="skipped_no_progress",
                )
                continue

            fraction_base = weighted.fraction
            action_fraction = action.weight / total_weight

            def provider_progress(
                fraction: float,
                task: str,
                detail: Mapping[str, Any],
            ) -> None:
                if self.progress is None:
                    return
                overall_execute_fraction = min(
                    1.0,
                    fraction_base + action_fraction * max(0.0, min(1.0, fraction)),
                )
                self.progress.update(
                    "execute",
                    overall_execute_fraction,
                    task=f"{index}/{len(actions)} {task}",
                    detail={"action": action.to_dict(), **dict(detail)},
                    completed_weight=(
                        weighted.completed_weight + action.weight * fraction
                    ),
                    total_weight=total_weight,
                    force=True,
                )

            provider = self.registry.create(
                action.provider,
                self.session,
                self.config,
                progress=provider_progress,
            )
            try:
                provider_result = provider.execute(action)
            except Exception as exc:
                logger.exception("Range-aware backfill action failed")
                provider_result = BackfillExecutionResult(
                    action=action,
                    status="failed",
                    errors=1,
                    detail={"error": str(exc), "error_type": type(exc).__name__},
                )

            after_report = self._audit_action(action)
            after_missing = _remaining_missing_hours(after_report, action)
            after_coverage = _action_coverage(after_report, action)
            provider_result.coverage_before = before_coverage
            provider_result.coverage_after = after_coverage
            provider_result.detail.update(
                {
                    "missing_hours_before": round(before_missing, 6),
                    "missing_hours_after": round(after_missing, 6),
                }
            )

            improved = after_missing < before_missing
            attempt["attempted_at"] = datetime.now(UTC).isoformat()
            attempt["provider"] = action.provider
            attempt["missing_hours_before"] = before_missing
            attempt["missing_hours_after"] = after_missing
            attempt["last_status"] = provider_result.status
            if improved:
                attempt["no_progress_count"] = 0
            else:
                attempt["no_progress_count"] = no_progress_count + 1
                if provider_result.status == "success":
                    provider_result.status = "partial"
            attempts[signature] = attempt
            self._save_attempts(attempts)

            results.append(provider_result)
            weighted.advance(
                f"{index}/{len(actions)} {action.provider} finished",
                action.weight,
                detail=provider_result.to_dict(),
                status=provider_result.status,
            )

        if self.progress is not None:
            self.progress.complete_stage(
                "execute",
                task=(
                    "all planned source actions processed"
                    if actions
                    else "no missing source actions required"
                ),
                detail={
                    "actions_processed": len(results),
                    "actions_planned": len(actions),
                },
            )

        original_datasets = (
            plan.coverage_before.datasets
            if plan.coverage_before is not None
            else ()
        )
        final_report = self.audit(
            plan.requested,
            air_parameters=tuple(
                item.parameter
                for item in original_datasets
                if item.dataset == "air"
            ),
            weather_parameters=tuple(
                item.parameter
                for item in original_datasets
                if item.dataset == "weather"
            ),
        )
        final_path = self.report_root / f"range-backfill-coverage-after-{stamp}.json"
        save_coverage_report(final_report, final_path)

        errors = sum(item.errors for item in results)
        unresolved = sum(
            len(item.missing_intervals) for item in final_report.datasets
        )
        status = "success" if errors == 0 and unresolved == 0 else "partial_success"
        payload = {
            "status": status,
            "plan_path": str(plan_path),
            "coverage_after_path": str(final_path),
            "actions_total": len(actions),
            "results": [item.to_dict() for item in results],
            "ignored": list(plan.ignored),
            "coverage_after": final_report.to_dict(),
            "errors": errors,
            "unresolved_interval_count": unresolved,
        }
        result_path = self.report_root / f"range-backfill-result-{stamp}.json"
        _write_json(result_path, payload)
        payload["result_path"] = str(result_path)
        return payload


def run_range_aware_backfill(
    session: Session,
    config: AppConfig,
    *,
    requested: TimeInterval,
    air_parameters: Sequence[str],
    weather_parameters: Sequence[str],
    cache_mode: str | None = None,
    dry_run: bool = False,
    include_isolated_gaps: bool = False,
    minimum_historical_gap_hours: int = 2,
    max_no_progress_attempts: int = 2,
    max_actions: int = 0,
    minimum_air_stations: int = 1,
    minimum_weather_stations: int = 1,
    progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    service = RangeAwareBackfillService(
        session,
        config,
        progress=progress,
        cache_mode=cache_mode,
        include_isolated_gaps=include_isolated_gaps,
        minimum_historical_gap_hours=minimum_historical_gap_hours,
        max_no_progress_attempts=max_no_progress_attempts,
        minimum_air_stations=minimum_air_stations,
        minimum_weather_stations=minimum_weather_stations,
    )
    if progress is not None:
        progress.update("audit", 0.0, task="auditing SQLite coverage", force=True)
    coverage = service.audit(
        requested,
        air_parameters=air_parameters,
        weather_parameters=weather_parameters,
    )
    if progress is not None:
        progress.complete_stage(
            "audit",
            task="SQLite coverage audited",
            detail=coverage.to_dict(),
        )
        progress.update("plan", 0.2, task="building source-aware plan", force=True)
    plan = service.build_plan(coverage)
    if progress is not None:
        progress.complete_stage(
            "plan",
            task="source-aware plan built",
            detail={
                "actions": len(plan.actions),
                "ignored": len(plan.ignored),
            },
        )
    result = service.execute(
        plan,
        dry_run=dry_run,
        max_actions=max_actions,
    )
    if progress is not None:
        progress.complete_stage(
            "verify",
            task="post-import coverage verified",
            detail={
                "status": result.get("status"),
                "unresolved_interval_count": result.get(
                    "unresolved_interval_count"
                ),
            },
        )
    return result
