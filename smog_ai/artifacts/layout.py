from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from smog_ai.storage.keys import join_key, sanitize_component


@dataclass(frozen=True, slots=True)
class ArtifactLayout:
    """Object-key policy, deliberately separate from storage implementation."""

    schema_version: str = "1"

    def raw_bundle(self, run_id: str, generated_at: datetime) -> str:
        return join_key(
            "datasets",
            "bronze",
            f"year={generated_at:%Y}",
            f"month={generated_at:%m}",
            f"day={generated_at:%d}",
            f"run={sanitize_component(run_id)}",
            "operational-data.json.gz",
        )

    @property
    def latest_raw_manifest(self) -> str:
        return join_key("datasets", "bronze", "latest.json")

    @property
    def last_raw_attempt_manifest(self) -> str:
        return join_key("datasets", "bronze", "last-attempt.json")

    def feature_dataset(self, parameter: str, horizon: int, dataset_id: str) -> str:
        return join_key(
            "datasets",
            "curated",
            f"parameter={sanitize_component(parameter)}",
            f"horizon={horizon}",
            f"dataset={sanitize_component(dataset_id)}.csv.gz",
        )

    def feature_manifest(self, parameter: str, horizon: int, dataset_id: str) -> str:
        return join_key(
            "datasets",
            "curated",
            f"parameter={sanitize_component(parameter)}",
            f"horizon={horizon}",
            f"dataset={sanitize_component(dataset_id)}.json",
        )

    def latest_feature_pointer(self, parameter: str, horizon: int) -> str:
        return join_key(
            "datasets",
            "curated",
            f"parameter={sanitize_component(parameter)}",
            f"horizon={horizon}",
            "latest.json",
        )


    def training_snapshot_manifest(self, dataset_id: str) -> str:
        return join_key(
            "datasets",
            "training-snapshots",
            f"dataset={sanitize_component(dataset_id)}",
            "manifest.json",
        )

    def latest_training_snapshot_pointer(self, profile: str) -> str:
        return join_key(
            "datasets",
            "training-snapshots",
            f"profile={sanitize_component(profile)}",
            "latest.json",
        )

    def hourly_feature_dataset(self, target: str, dataset_id: str) -> str:
        return join_key(
            "datasets",
            "curated-hourly",
            f"target={sanitize_component(target)}",
            f"dataset={sanitize_component(dataset_id)}.csv.gz",
        )

    def hourly_feature_manifest(self, target: str, dataset_id: str) -> str:
        return join_key(
            "datasets",
            "curated-hourly",
            f"target={sanitize_component(target)}",
            f"dataset={sanitize_component(dataset_id)}.json",
        )

    def latest_hourly_feature_pointer(self, target: str) -> str:
        return join_key(
            "datasets",
            "curated-hourly",
            f"target={sanitize_component(target)}",
            "latest.json",
        )

    def hourly_model_binary(self, target: str, version: str) -> str:
        return join_key(
            "models-hourly",
            f"target={sanitize_component(target)}",
            f"version={sanitize_component(version)}",
            "model.joblib",
        )

    def hourly_model_card(self, target: str, version: str) -> str:
        return join_key(
            "models-hourly",
            f"target={sanitize_component(target)}",
            f"version={sanitize_component(version)}",
            "model-card.json",
        )

    def active_hourly_model_pointer(self, target: str) -> str:
        return join_key(
            "models-hourly",
            f"target={sanitize_component(target)}",
            "active.json",
        )

    def hourly_model_metrics(self, target: str, version: str) -> str:
        return join_key(
            "metrics",
            "hourly-models",
            f"target={sanitize_component(target)}",
            f"version={sanitize_component(version)}.json",
        )

    @property
    def model_comparison_pointer(self) -> str:
        return join_key("metrics", "hourly-models", "comparison", "latest.json")

    @property
    def technical_processing_document(self) -> str:
        return join_key("documentation", "technical-processing.md")

    @property
    def technical_processing_latex(self) -> str:
        return join_key("documentation", "technical-processing.tex")

    @property
    def mathematical_model_document(self) -> str:
        return join_key("documentation", "mathematical-model.md")

    @property
    def mathematical_model_latex(self) -> str:
        return join_key("documentation", "mathematical-model.tex")

    @property
    def model_plugin_guide(self) -> str:
        return join_key("documentation", "model-plugin-guide.md")

    @property
    def hf20_document(self) -> str:
        return join_key("documentation", "hf20-time-contract-mlops.md")

    @property
    def hf20_latex(self) -> str:
        return join_key("documentation", "hf20-time-contract-mlops.tex")

    @property
    def documentation_manifest(self) -> str:
        return join_key("documentation", "manifest.json")

    def model_binary(self, parameter: str, horizon: int, version: str) -> str:
        return join_key(
            "models",
            f"parameter={sanitize_component(parameter)}",
            f"horizon={horizon}",
            f"version={sanitize_component(version)}",
            "model.joblib",
        )

    def model_card(self, parameter: str, horizon: int, version: str) -> str:
        return join_key(
            "models",
            f"parameter={sanitize_component(parameter)}",
            f"horizon={horizon}",
            f"version={sanitize_component(version)}",
            "model-card.json",
        )

    def active_model_pointer(self, parameter: str, horizon: int) -> str:
        return join_key(
            "models",
            f"parameter={sanitize_component(parameter)}",
            f"horizon={horizon}",
            "active.json",
        )


    def validation_report(self, kind: str, report_id: str) -> str:
        return join_key(
            "metrics",
            "data-validation",
            sanitize_component(kind),
            f"report={sanitize_component(report_id)}.json",
        )

    def latest_validation_pointer(self, kind: str) -> str:
        return join_key(
            "metrics",
            "data-validation",
            sanitize_component(kind),
            "latest.json",
        )

    def classical_model_metrics(self, parameter: str, horizon: int, version: str) -> str:
        return join_key(
            "metrics",
            "classical-models",
            f"parameter={sanitize_component(parameter)}",
            f"horizon={horizon}",
            f"version={sanitize_component(version)}.json",
        )


    def spatial_surface(
        self,
        surface_set_id: str,
        parameter: str,
        horizon: int,
    ) -> str:
        return join_key(
            "serving",
            "releases",
            f"release={sanitize_component(surface_set_id)}",
            "surfaces",
            sanitize_component(parameter),
            f"h{horizon:03d}.json.gz",
        )

    def spatial_surface_metadata(
        self,
        surface_set_id: str,
        parameter: str,
        horizon: int,
    ) -> str:
        return join_key(
            "serving",
            "releases",
            f"release={sanitize_component(surface_set_id)}",
            "metadata",
            sanitize_component(parameter),
            f"h{horizon:03d}.json",
        )

    def spatial_manifest(self, surface_set_id: str) -> str:
        return join_key(
            "serving",
            "releases",
            f"release={sanitize_component(surface_set_id)}",
            "manifest.json",
        )

    @property
    def latest_spatial_pointer(self) -> str:
        return join_key("serving", "latest.json")

    @property
    def spatial_boundary(self) -> str:
        return join_key("serving", "static", "poland-boundary.geojson.gz")

    @property
    def spatial_places(self) -> str:
        return join_key("serving", "static", "polish-places.json.gz")

    def forecast_snapshot(self, publication_id: str) -> str:
        return join_key(
            "forecasts",
            "runs",
            f"publication={sanitize_component(publication_id)}.json.gz",
        )

    @property
    def latest_forecast_pointer(self) -> str:
        return join_key("forecasts", "latest.json")

    def publication_audit(self, publication_id: str) -> str:
        return join_key(
            "forecasts",
            "audit",
            f"publication={sanitize_component(publication_id)}.json",
        )
