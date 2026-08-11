from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

from smog_ai.config import MLflowConfig

if TYPE_CHECKING:  # pragma: no cover
    from smog_ai.config import AppConfig

logger = logging.getLogger(__name__)


def _scalar_metrics(values: dict[str, Any], *, prefix: str = "") -> dict[str, float]:
    """Flatten scalar metrics while ignoring nested/non-finite payloads."""

    output: dict[str, float] = {}
    for key, value in values.items():
        metric_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, bool):
            output[metric_key] = float(value)
        elif isinstance(value, (int, float)) and value is not None:
            numeric = float(value)
            if numeric == numeric and numeric not in {float("inf"), float("-inf")}:
                output[metric_key] = numeric
        elif isinstance(value, dict):
            output.update(_scalar_metrics(value, prefix=metric_key))
    return output


def _safe_param(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)[:5000]


def _resolve_config(config: MLflowConfig | AppConfig) -> tuple[MLflowConfig, bool]:
    """Accept either the whole AppConfig or only its MLflow section.

    HF19-era registration code passes AppConfig, while bounded candidate
    training passes config.mlflow. Keeping both forms makes the Bridge a stable
    plugin boundary and avoids hidden coupling in trainer code.
    """

    section = getattr(config, "mlflow", config)
    if not isinstance(section, MLflowConfig):
        raise TypeError(
            "create_mlflow_bridge expects AppConfig or MLflowConfig, got "
            f"{type(config).__name__}"
        )
    strict = bool(getattr(section, "strict", False))
    return section, strict


class MlflowBridge(Protocol):
    backend_name: str

    def log_model_run(
        self,
        *,
        target: str,
        provider: str,
        version: str,
        artifact_path: str | Path,
        metrics: dict[str, Any],
        tags: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def log_candidate(
        self,
        *,
        target: str,
        provider: str,
        profile: str,
        metrics: dict[str, Any],
        parameters: dict[str, Any],
        dataset_provenance: dict[str, Any] | None,
    ) -> str | None:
        ...

    def mark_selected(
        self,
        run_id: str | None,
        *,
        model_version: str,
        artifact_path: str | None,
        target: str,
        provider: str,
    ) -> None:
        ...

    def compare_runs(
        self, *, target: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        ...


@dataclass(slots=True)
class NoopMlflowBridge:
    backend_name: str = "none"

    def log_model_run(self, **_: Any) -> dict[str, Any]:
        return {"backend": "none", "logged": False}

    def log_candidate(self, **_: Any) -> str | None:
        return None

    def mark_selected(self, run_id: str | None, **_: Any) -> None:
        del run_id
        return None

    def compare_runs(
        self, *, target: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        del target, limit
        return []


class ActiveMlflowBridge:
    backend_name = "mlflow"

    def __init__(self, config: MLflowConfig) -> None:
        try:
            import mlflow
            from mlflow.tracking import MlflowClient
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Install the 'mlops' extra to enable MLflow") from exc

        self.mlflow = mlflow
        tracking_uri = config.tracking_uri.strip()
        if not tracking_uri:
            config.local_artifact_dir.mkdir(parents=True, exist_ok=True)
            tracking_uri = config.local_artifact_dir.resolve().as_uri()
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(config.experiment_name)
        self.client = MlflowClient(tracking_uri=tracking_uri)
        self.config = config
        self.tracking_uri = tracking_uri

    def _base_tags(
        self,
        *,
        target: str,
        provider: str,
        profile: str | None = None,
        selected: bool = False,
    ) -> dict[str, str]:
        tags = {
            "smog_ai.target": target,
            "smog_ai.provider": provider,
            "smog_ai.selected": str(selected).lower(),
        }
        if profile:
            tags["smog_ai.profile"] = profile
        return tags

    def log_model_run(
        self,
        *,
        target: str,
        provider: str,
        version: str,
        artifact_path: str | Path,
        metrics: dict[str, Any],
        tags: dict[str, Any],
    ) -> dict[str, Any]:
        profile = str(metrics.get("training_profile") or tags.get("training_profile") or "")
        with self.mlflow.start_run(run_name=f"{target}-{provider}-{version}") as run:
            self.mlflow.set_tags(
                {
                    **self._base_tags(
                        target=target,
                        provider=provider,
                        profile=profile or None,
                        selected=True,
                    ),
                    "smog_ai.model_version": version,
                    **{str(k): _safe_param(v) for k, v in tags.items()},
                }
            )
            self.mlflow.log_metrics(_scalar_metrics(metrics))
            self.mlflow.log_dict(metrics, "metrics-full.json")
            path = Path(artifact_path)
            if self.config.log_model_artifacts and path.exists():
                self.mlflow.log_artifact(str(path), artifact_path="model")
            if self.config.registry_enabled:
                self.mlflow.set_tag(
                    "smog_ai.registered_model_name",
                    f"{self.config.registered_model_prefix}-{target}",
                )
            return {
                "backend": self.backend_name,
                "logged": True,
                "run_id": str(run.info.run_id),
                "tracking_uri": self.tracking_uri,
                "experiment_name": self.config.experiment_name,
            }

    def log_candidate(
        self,
        *,
        target: str,
        provider: str,
        profile: str,
        metrics: dict[str, Any],
        parameters: dict[str, Any],
        dataset_provenance: dict[str, Any] | None,
    ) -> str | None:
        with self.mlflow.start_run(run_name=f"{target}-{provider}-{profile}") as run:
            self.mlflow.set_tags(
                self._base_tags(
                    target=target,
                    provider=provider,
                    profile=profile,
                    selected=False,
                )
            )
            payload = {
                "target": target,
                "provider": provider,
                "profile": profile,
                **parameters,
            }
            if dataset_provenance:
                payload.update(
                    {
                        "dataset_id": dataset_provenance.get("dataset_id"),
                        "dataset_sha256": dataset_provenance.get("database_sha256")
                        or dataset_provenance.get("dataset_sha256"),
                    }
                )
            self.mlflow.log_params(
                {
                    key: _safe_param(value)
                    for key, value in payload.items()
                    if value is not None
                }
            )
            self.mlflow.log_metrics(_scalar_metrics(metrics))
            self.mlflow.log_dict(metrics, "metrics-full.json")
            if dataset_provenance:
                self.mlflow.log_dict(
                    dataset_provenance,
                    "dataset-provenance.json",
                )
            return str(run.info.run_id)

    def mark_selected(
        self,
        run_id: str | None,
        *,
        model_version: str,
        artifact_path: str | None,
        target: str,
        provider: str,
    ) -> None:
        if not run_id:
            return
        self.client.set_tag(run_id, "smog_ai.selected", "true")
        self.client.set_tag(run_id, "smog_ai.model_version", model_version)
        if artifact_path and self.config.log_model_artifacts:
            path = Path(artifact_path)
            if path.exists():
                self.client.log_artifact(run_id, str(path), artifact_path="model")
        if self.config.registry_enabled:
            # The shared Registry is an explicit deployment concern.  Keep the
            # intended registered-model name in the run until a DB-backed
            # tracking server is configured.
            self.client.set_tag(
                run_id,
                "smog_ai.registered_model_name",
                f"{self.config.registered_model_prefix}-{target}",
            )
            self.client.set_tag(run_id, "smog_ai.registry_pending", "true")
        self.client.set_tag(run_id, "smog_ai.provider", provider)

    def compare_runs(
        self, *, target: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        experiment = self.mlflow.get_experiment_by_name(
            self.config.experiment_name
        )
        if experiment is None:
            return []
        filters = []
        if target:
            escaped = str(target).replace("'", "\\'")
            filters.append(f"tags.`smog_ai.target` = '{escaped}'")
        runs = self.mlflow.search_runs(
            experiment_ids=[experiment.experiment_id],
            filter_string=" and ".join(filters),
            max_results=min(
                max(1, int(limit)),
                self.config.maximum_runs_per_target,
            ),
            order_by=["attributes.start_time DESC"],
            output_format="list",
        )
        output: list[dict[str, Any]] = []
        for run in runs:
            output.append(
                {
                    "run_id": run.info.run_id,
                    "status": run.info.status,
                    "start_time": run.info.start_time,
                    "end_time": run.info.end_time,
                    "target": run.data.tags.get("smog_ai.target"),
                    "provider": run.data.tags.get("smog_ai.provider"),
                    "profile": run.data.tags.get("smog_ai.profile"),
                    "selected": run.data.tags.get("smog_ai.selected") == "true",
                    "model_version": run.data.tags.get("smog_ai.model_version"),
                    "params": dict(run.data.params),
                    "metrics": dict(run.data.metrics),
                }
            )
        return output


def create_mlflow_bridge(
    config: MLflowConfig | AppConfig,
    *,
    strict: bool | None = None,
) -> MlflowBridge:
    section, configured_strict = _resolve_config(config)
    effective_strict = configured_strict if strict is None else bool(strict)
    if not section.enabled:
        return NoopMlflowBridge()
    try:
        return ActiveMlflowBridge(section)
    except Exception:
        if effective_strict:
            raise
        logger.warning(
            "MLflow initialization failed; tracking disabled",
            exc_info=True,
        )
        return NoopMlflowBridge()
